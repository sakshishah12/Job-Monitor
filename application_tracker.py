"""Track submitted job applications from Gmail confirmation emails."""

from __future__ import annotations

import argparse
import csv
import email
import hashlib
import imaplib
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("application_tracker")

DEFAULT_SHEET_PATH = Path("data/applications.csv")
DEFAULT_DAYS_BACK = 30
DEFAULT_MAX_EMAILS = 200
MAX_FETCH_ATTEMPTS = 3
DATE_ARG_FORMAT = "%Y-%m-%d"

APPLICATION_SUBJECT_TERMS = [
    "thank you for applying",
    "thanks for applying",
    "application received",
    "we received your application",
    "your application",
    "application submitted",
    "application status",
    "status update",
    "update on your application",
    "received your application",
    "not moving forward",
    "not move forward",
    "unfortunately",
    "interview",
    "assessment",
]

STATUS_PATTERNS = [
    (re.compile(r"\b(unfortunately|not move forward|not moving forward|unable to proceed|will not be proceeding)\b", re.IGNORECASE), "Rejected"),
    (re.compile(r"\b(no longer under consideration|not selected|pursue other candidates|other applicants)\b", re.IGNORECASE), "Rejected"),
    (re.compile(r"\b(interview|schedule a call|speak with|meet with)\b", re.IGNORECASE), "Interview"),
    (re.compile(r"\b(assessment|coding challenge|technical challenge|take-home|take home)\b", re.IGNORECASE), "Assessment"),
    (re.compile(r"\b(offer letter|extend an offer|pleased to offer|job offer)\b", re.IGNORECASE), "Offer"),
    (re.compile(r"\bthank(s| you)\b.*\b(applying|application)\b", re.IGNORECASE), "Applied"),
    (re.compile(r"\b(application|profile) (received|submitted|complete)\b", re.IGNORECASE), "Applied"),
    (re.compile(r"\breceived your application\b", re.IGNORECASE), "Applied"),
    (re.compile(r"\b(status update|update on your application)\b", re.IGNORECASE), "Updated"),
]

ROLE_COMPANY_PATTERNS = [
    re.compile(
        r"application\s*[-:]\s*(?P<role>[A-Za-z0-9/,+&(). -]{3,90}?)\s*-\s*at\s+(?P<company>[A-Za-z0-9&() -]{2,60}?)(?:[.,\n]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"applying to\s+(?:the\s+)?(?P<role>[A-Za-z0-9/,+&(). -]{3,90}?)\s+(?:role|position|job|opening)?\s+at\s+(?P<company>[A-Za-z0-9&() -]{2,60}?)(?:[.,\n]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"application for\s+(?P<role>[A-Za-z0-9/,+&(). -]{3,90}?)\s+at\s+(?P<company>[A-Za-z0-9&() -]{2,60}?)(?:[.,\n]|$)",
        re.IGNORECASE,
    ),
]

ROLE_PATTERNS = [
    re.compile(r"interest in this\s+(?P<role>[A-Za-z0-9/,+&(). -]{3,90}?)\s+(?:role|position|job|opening)", re.IGNORECASE),
    re.compile(r"(?:for|to the|to our)\s+(?:the\s+)?(?P<role>[A-Z][A-Za-z0-9/,+&(). -]{3,90}?)\s+at\s+[A-Z][A-Za-z0-9&()., -]{2,60}", re.IGNORECASE),
    re.compile(r"(?:for|to the|to our)\s+(?:the\s+)?(?P<role>[A-Z][A-Za-z0-9/,+&(). -]{3,90}?)(?:\s+(?:role|position|job|opening))", re.IGNORECASE),
    re.compile(r"(?:position|role|job title)\s*[:\-]\s*(?P<role>[A-Za-z0-9/,+&(). -]{3,90})", re.IGNORECASE),
    re.compile(r"application for\s+(?P<role>[A-Za-z0-9/,+&(). -]{3,90})", re.IGNORECASE),
]

COMPANY_PATTERNS = [
    re.compile(r"thank(?:s| you)?(?:\s+\w+)?\s+for\s+applying\s+to\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,80}?)(?:\s*[-–—:]|!|\.|,|\n|$)", re.IGNORECASE),
    re.compile(r"application\s+received\s+(?:for\s+.+?\s+)?at\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,80}?)(?:\.|,|\n|$)", re.IGNORECASE),
    re.compile(r"received\s+your\s+application\s+for\s+.+?\s+at\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,80}?)(?:\.|,|\n|$)", re.IGNORECASE),
    re.compile(r"application\s+for\s+.+?\s+at\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,80}?)(?:\.|,|\n|$)", re.IGNORECASE),
    re.compile(r"applying\s+to\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,80}?)(?:\s*[-–—:]|!|\.|,|\n|$)", re.IGNORECASE),
    re.compile(r"interest\s+in\s+(?:joining\s+our\s+team\s+at|joining|working\s+at)\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,80}?)(?:\.|,|\n|$)", re.IGNORECASE),
    re.compile(r"position\s+with\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,80}?)(?:\.|,|\n|$)", re.IGNORECASE),
    re.compile(r"(?:at|with|to)\s+(?P<company>[A-Z][A-Za-z0-9&()., -]{2,60})(?:\.|,|\n|$)"),
    re.compile(r"company\s*[:\-]\s*(?P<company>[A-Za-z0-9&()., -]{2,60})", re.IGNORECASE),
]

SIGNATURE_NAME_PATTERN = re.compile(
    r"(?:best|regards|sincerely|thanks),?\s*\n+(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})",
    re.IGNORECASE,
)

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


@dataclass
class ApplicationEntry:
    application_hash: str
    application_key: str
    company: str
    role: str
    status: str
    source_email: str
    sender: str
    subject: str
    received_at: str
    last_email_at: str
    recruiter_name: str
    recruiter_email: str
    hiring_manager_name: str
    hiring_manager_email: str
    raw_excerpt: str
    updated_at: str


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded: list[str] = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def message_text(message: Message) -> str:
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            chunks.append(strip_html(text) if content_type == "text/html" else text)
    else:
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            chunks.append(strip_html(text) if message.get_content_type() == "text/html" else text)

    return normalize_whitespace("\n".join(chunks))


def strip_html(text: str) -> str:
    text = re.sub(r"<(br|/p|/div|/li)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(text)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()


def infer_status(text: str) -> str:
    for pattern, status in STATUS_PATTERNS:
        if pattern.search(text):
            return status
    return "Applied"


def infer_role(subject: str, body: str) -> str:
    combined = f"{subject}\n{body}"
    for pattern in ROLE_COMPANY_PATTERNS:
        for text in (subject, combined):
            match = pattern.search(text)
            if match:
                return clean_value(match.group("role"))
    for pattern in ROLE_PATTERNS:
        match = pattern.search(combined)
        if match:
            return clean_value(match.group("role"))
    return ""


def infer_company(subject: str, body: str, sender: str) -> str:
    combined = f"{subject}\n{body}"
    for pattern in COMPANY_PATTERNS:
        for text in (subject, combined):
            match = pattern.search(text)
            if match:
                return clean_company(match.group("company"))

    domain_match = re.search(r"@(?:mail\.|jobs\.|careers\.)?(?P<domain>[A-Za-z0-9-]+)\.", sender)
    if domain_match:
        return clean_company(domain_match.group("domain").replace("-", " ").title())
    return ""


def infer_recruiter(sender: str, body: str) -> tuple[str, str]:
    sender_email = EMAIL_PATTERN.search(sender)
    recruiter_email = sender_email.group(0) if sender_email else ""

    signature = SIGNATURE_NAME_PATTERN.search(body)
    recruiter_name = clean_value(signature.group("name")) if signature else ""
    return recruiter_name, recruiter_email


def clean_value(value: str) -> str:
    value = re.split(r"\s{2,}|\||<|>|https?://", value.strip())[0]
    value = re.sub(r"\b(apply|application|submitted|received)\b.*$", "", value, flags=re.IGNORECASE)
    return value.strip(" .,-:")


def clean_company(value: str) -> str:
    value = clean_value(value)
    value = re.split(
        r"\s+-\s+|\s+–\s+|\s+—\s+|\s+for\s+|\s+role\b|\s+position\b|\s+job\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = re.sub(r"\b(careers|jobs|talent|recruiting|workday|greenhouse|ashbyhq)\b$", "", value, flags=re.IGNORECASE)
    return value.strip(" .,-:")


def make_hash(message_id: str, sender: str, subject: str, received_at: str) -> str:
    raw = message_id or f"{sender}::{subject}::{received_at}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_application_key(company: str, role: str) -> str:
    company_key = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    if not company_key:
        return ""
    return hashlib.sha256(company_key.encode("utf-8")).hexdigest()


def parse_received_at(message: Message) -> str:
    date_header = message.get("Date", "")
    try:
        parsed = parsedate_to_datetime(date_header)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def parse_date_arg(value: str, end_of_day: bool = False) -> datetime:
    parsed = datetime.strptime(value, DATE_ARG_FORMAT).replace(tzinfo=timezone.utc)
    if end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def parse_received_at_value(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def within_date_window(value: str, since_date: str = "", until_date: str = "") -> bool:
    received_at = parse_received_at_value(value)
    if since_date and received_at < parse_date_arg(since_date):
        return False
    if until_date and received_at > parse_date_arg(until_date, end_of_day=True):
        return False
    return True


def looks_like_application_confirmation(subject: str, body: str) -> bool:
    combined = f"{subject}\n{body}".lower()
    return any(term in combined for term in APPLICATION_SUBJECT_TERMS)


def parse_application(
    message: Message,
    source_email: str,
) -> ApplicationEntry | None:
    subject = decode_mime_header(message.get("Subject"))
    sender = decode_mime_header(message.get("From"))
    body = message_text(message)

    if not looks_like_application_confirmation(subject, body):
        return None

    received_at = parse_received_at(message)
    recruiter_name, recruiter_email = infer_recruiter(sender, body)
    entry = ApplicationEntry(
        application_hash=make_hash(message.get("Message-ID", ""), sender, subject, received_at),
        application_key="",
        company=infer_company(subject, body, sender),
        role=infer_role(subject, body),
        status=infer_status(f"{subject}\n{body}"),
        source_email=source_email,
        sender=sender,
        subject=subject,
        received_at=received_at,
        last_email_at=received_at,
        recruiter_name=recruiter_name,
        recruiter_email=recruiter_email,
        hiring_manager_name="",
        hiring_manager_email="",
        raw_excerpt=body[:500],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    entry.application_key = make_application_key(entry.company, entry.role)
    return entry


class ApplicationSheet:
    fieldnames = list(ApplicationEntry.__dataclass_fields__.keys())

    def __init__(self, path: Path = DEFAULT_SHEET_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        with self.path.open(newline="", encoding="utf-8") as file:
            rows: dict[str, dict[str, str]] = {}
            for row in csv.DictReader(file):
                if not row.get("application_hash"):
                    continue
                if not row.get("application_key"):
                    row["application_key"] = make_application_key(row.get("company", ""), row.get("role", ""))
                rows[row["application_hash"]] = row
            return rows

    def upsert_many(self, entries: list[ApplicationEntry]) -> int:
        rows = self.load()
        rows_by_application_key = {
            row.get("application_key", ""): row
            for row in rows.values()
            if row.get("application_key")
        }
        inserted = 0
        for entry in entries:
            row = asdict(entry)
            existing = rows_by_application_key.get(entry.application_key) if entry.application_key else None
            if existing:
                existing.update(merge_application_rows(existing, row))
                rows[existing["application_hash"]] = existing
                continue

            if entry.application_hash not in rows:
                inserted += 1
            rows[entry.application_hash] = row
            if entry.application_key:
                rows_by_application_key[entry.application_key] = rows[entry.application_hash]

        self.write_rows(list(rows.values()))
        return inserted

    def save_rows(self, rows: list[dict[str, str]]) -> None:
        normalized_rows = []
        for row in rows:
            normalized_rows.append({field: row.get(field, "") for field in self.fieldnames})

        self.write_rows(normalized_rows)

    def write_rows(self, rows: list[dict[str, str]]) -> Path:
        normalized_rows = [{field: row.get(field, "") for field in self.fieldnames} for row in rows]
        sorted_rows = sorted(normalized_rows, key=lambda row: row["received_at"], reverse=True)
        target_path = self.path
        fallback_path = self.path.with_name(
            f"{self.path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{self.path.suffix}"
        )

        try:
            return self._write_rows_to_path(target_path, sorted_rows)
        except PermissionError:
            logger.warning(
                "Could not write %s. It may be open in Excel or another app; writing fallback %s",
                target_path,
                fallback_path,
            )
            return self._write_rows_to_path(fallback_path, sorted_rows)

    def _write_rows_to_path(self, path: Path, rows: list[dict[str, str]]) -> Path:
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path


def merge_application_rows(existing: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    merged = dict(existing)
    for field, value in incoming.items():
        if field in {"application_hash", "application_key", "source_email", "received_at"}:
            continue
        if value and field != "role":
            merged[field] = value
        if field == "role" and value and not merged.get("role"):
            merged[field] = value
    if incoming.get("received_at"):
        merged["last_email_at"] = incoming["received_at"]
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    return merged


def connect_imap(host: str, username: str, password: str, mailbox: str) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(host)
    client.login(username, password)
    client.select(mailbox)
    return client


def close_imap(client: imaplib.IMAP4_SSL | None) -> None:
    if not client:
        return
    try:
        client.close()
    except imaplib.IMAP4.error:
        pass
    try:
        client.logout()
    except imaplib.IMAP4.error:
        pass


def fetch_raw_message(
    client: imaplib.IMAP4_SSL,
    message_id: bytes,
) -> bytes | None:
    status, payload = client.fetch(message_id, "(RFC822)")
    if status != "OK" or not payload:
        return None
    raw = payload[0][1]
    return raw if isinstance(raw, bytes) else None


def fetch_application_confirmations(
    days_back: int,
    max_emails: int,
    since_date: str = "",
    until_date: str = "",
) -> list[ApplicationEntry]:
    username = os.getenv("APPLICATION_EMAIL_USERNAME") or os.getenv("EMAIL_SENDER")
    password = os.getenv("APPLICATION_EMAIL_APP_PASSWORD") or os.getenv("EMAIL_PASSWORD")
    host = os.getenv("APPLICATION_IMAP_HOST", "imap.gmail.com")
    mailbox = os.getenv("APPLICATION_IMAP_MAILBOX", "INBOX")

    if not username or not password:
        raise RuntimeError(
            "Set APPLICATION_EMAIL_USERNAME and APPLICATION_EMAIL_APP_PASSWORD in .env. "
            "For Gmail, use an App Password, not your normal password."
        )

    since = datetime.now().strftime("%d-%b-%Y")
    if since_date:
        since = parse_date_arg(since_date).strftime("%d-%b-%Y")
    elif days_back > 0:
        since_timestamp = datetime.now().timestamp() - (days_back * 24 * 60 * 60)
        since = datetime.fromtimestamp(since_timestamp).strftime("%d-%b-%Y")

    entries: list[ApplicationEntry] = []
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = connect_imap(host, username, password, mailbox)
        status, data = client.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError(f"IMAP search failed with status: {status}")

        message_ids = list(reversed(data[0].split()))
        if max_emails > 0:
            message_ids = message_ids[:max_emails]

        logger.info("Scanning %d newest emails since %s", len(message_ids), since)
        for message_id in message_ids:
            raw = None
            for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
                try:
                    raw = fetch_raw_message(client, message_id)
                    break
                except (imaplib.IMAP4.abort, OSError) as exc:
                    logger.warning(
                        "IMAP fetch dropped for message %s on attempt %d/%d: %s",
                        message_id.decode(errors="replace"),
                        attempt,
                        MAX_FETCH_ATTEMPTS,
                        exc,
                    )
                    close_imap(client)
                    client = connect_imap(host, username, password, mailbox)

            if not raw:
                continue

            parsed = email.message_from_bytes(raw)
            entry = parse_application(parsed, username)
            if entry and within_date_window(entry.received_at, since_date, until_date):
                entries.append(entry)
    finally:
        close_imap(client)
    return entries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill application sheet from Gmail confirmation emails.")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    parser.add_argument("--since-date", default="", help="Only process emails received on or after YYYY-MM-DD.")
    parser.add_argument("--until-date", default="", help="Only process emails received on or before YYYY-MM-DD.")
    parser.add_argument("--max-emails", type=int, default=DEFAULT_MAX_EMAILS)
    parser.add_argument("--sheet-path", type=Path, default=DEFAULT_SHEET_PATH)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    load_dotenv()
    args = parse_args()
    entries = fetch_application_confirmations(
        args.days_back,
        args.max_emails,
        args.since_date,
        args.until_date,
    )
    inserted = ApplicationSheet(args.sheet_path).upsert_many(entries)
    logger.info("Found %d application confirmations; inserted %d new rows into %s", len(entries), inserted, args.sheet_path)


if __name__ == "__main__":
    main()
