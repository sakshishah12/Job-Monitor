"""Track submitted job applications from Gmail confirmation emails."""

from __future__ import annotations

import argparse
import csv
import email
import hashlib
import imaplib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger("application_tracker")

DEFAULT_SHEET_PATH = Path("data/applications.csv")
DEFAULT_DAYS_BACK = 30
DEFAULT_MAX_EMAILS = 200
MAX_FETCH_ATTEMPTS = 3
DEFAULT_LLM_MODEL = "gemini-flash-latest"
DATE_ARG_FORMAT = "%Y-%m-%d"

APPLICATION_SUBJECT_TERMS = [
    "thank you for applying",
    "thanks for applying",
    "application received",
    "we received your application",
    "your application",
    "application submitted",
]

STATUS_PATTERNS = [
    (re.compile(r"\b(unfortunately|not move forward|not moving forward|unable to proceed|will not be proceeding)\b", re.IGNORECASE), "Rejected"),
    (re.compile(r"\b(interview|schedule a call|speak with|meet with)\b", re.IGNORECASE), "Interview"),
    (re.compile(r"\b(assessment|coding challenge|technical challenge|take-home|take home)\b", re.IGNORECASE), "Assessment"),
    (re.compile(r"\b(offer|congratulations)\b", re.IGNORECASE), "Offer"),
    (re.compile(r"\bthank(s| you)\b.*\b(applying|application)\b", re.IGNORECASE), "Applied"),
    (re.compile(r"\bapplication (received|submitted|complete)\b", re.IGNORECASE), "Applied"),
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
    recruiter_name: str
    recruiter_email: str
    hiring_manager_name: str
    hiring_manager_email: str
    llm_confidence: str
    llm_notes: str
    raw_excerpt: str
    updated_at: str


APPLICATION_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_application_confirmation": {"type": "boolean"},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "status": {"type": "string"},
        "recruiter_name": {"type": "string"},
        "recruiter_email": {"type": "string"},
        "hiring_manager_name": {"type": "string"},
        "hiring_manager_email": {"type": "string"},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": [
        "is_application_confirmation",
        "company",
        "role",
        "status",
        "recruiter_name",
        "recruiter_email",
        "hiring_manager_name",
        "hiring_manager_email",
        "confidence",
        "notes",
    ],
}


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
    for pattern in ROLE_COMPANY_PATTERNS:
        for text in (subject, combined):
            match = pattern.search(text)
            if match:
                return clean_value(match.group("company"))
    for pattern in COMPANY_PATTERNS:
        match = pattern.search(combined)
        if match:
            return clean_value(match.group("company"))

    domain_match = re.search(r"@(?:mail\.|jobs\.|careers\.)?(?P<domain>[A-Za-z0-9-]+)\.", sender)
    if domain_match:
        return domain_match.group("domain").replace("-", " ").title()
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


def llm_enabled() -> bool:
    enabled = os.getenv("APPLICATION_LLM_ENABLED", "false").strip().lower()
    return enabled in {"1", "true", "yes", "on"} and bool(os.getenv("GEMINI_API_KEY"))


def extract_with_llm(sender: str, subject: str, body: str) -> dict[str, Any] | None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("APPLICATION_LLM_MODEL", DEFAULT_LLM_MODEL)
    prompt = (
        "Extract job application tracking data from this email.\n\n"
        "Rules:\n"
        "- Only set is_application_confirmation to true when the email confirms a submitted job application.\n"
        "- Ignore newsletters, job alerts, generic career advice, marketing email, and LinkedIn content.\n"
        "- If a field is not explicitly present, return an empty string.\n"
        "- Use status values like Applied, Rejected, Interview, Assessment, Offer, Withdrawn, or Unknown.\n\n"
        "Return only a valid JSON object with exactly these keys:\n"
        "{\n"
        '  "is_application_confirmation": true,\n'
        '  "company": "",\n'
        '  "role": "",\n'
        '  "status": "",\n'
        '  "recruiter_name": "",\n'
        '  "recruiter_email": "",\n'
        '  "hiring_manager_name": "",\n'
        '  "hiring_manager_email": "",\n'
        '  "confidence": 0.0,\n'
        '  "notes": ""\n'
        "}\n\n"
        + json.dumps(
            {
                "sender": sender,
                "subject": subject,
                "body_excerpt": body[:3000],
            },
            ensure_ascii=True,
        )
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=45,
            )
            if response.status_code == 429 and attempt < MAX_FETCH_ATTEMPTS:
                retry_after = extract_retry_after_seconds(response.text) or 60
                logger.warning(
                    "Gemini quota hit for subject %r; waiting %.1f seconds before retry %d/%d",
                    subject,
                    retry_after,
                    attempt + 1,
                    MAX_FETCH_ATTEMPTS,
                )
                time.sleep(retry_after)
                continue
            if not response.ok:
                logger.warning("Gemini API error for subject %r: %s", subject, response.text[:1000])
                return None
            content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(content)
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as exc:
            logger.warning("Gemini extraction failed for subject %r: %s", subject, exc)
            return None
    return None


def extract_retry_after_seconds(error_text: str) -> float | None:
    match = re.search(r"retry in ([0-9.]+)s", error_text, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1)) + 2
    except ValueError:
        return None


def apply_llm_fields(entry: ApplicationEntry, extraction: dict[str, Any] | None) -> ApplicationEntry | None:
    if not extraction:
        return entry
    if extraction.get("is_application_confirmation") is False:
        return None

    entry.company = clean_value(str(extraction.get("company") or entry.company))
    entry.role = clean_value(str(extraction.get("role") or entry.role))
    entry.status = clean_value(str(extraction.get("status") or entry.status or "Applied"))
    entry.recruiter_name = clean_value(str(extraction.get("recruiter_name") or entry.recruiter_name))
    entry.recruiter_email = clean_value(str(extraction.get("recruiter_email") or entry.recruiter_email))
    entry.hiring_manager_name = clean_value(str(extraction.get("hiring_manager_name") or entry.hiring_manager_name))
    entry.hiring_manager_email = clean_value(str(extraction.get("hiring_manager_email") or entry.hiring_manager_email))
    entry.llm_confidence = str(extraction.get("confidence", ""))
    entry.llm_notes = clean_value(str(extraction.get("notes") or ""))
    entry.updated_at = datetime.now(timezone.utc).isoformat()
    return entry


def make_hash(message_id: str, sender: str, subject: str, received_at: str) -> str:
    raw = message_id or f"{sender}::{subject}::{received_at}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_application_key(company: str, role: str) -> str:
    company_key = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    role_key = re.sub(r"[^a-z0-9]+", " ", role.lower()).strip()
    if not company_key or not role_key:
        return ""
    return hashlib.sha256(f"{company_key}::{role_key}".encode("utf-8")).hexdigest()


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


def parse_application(message: Message, source_email: str, use_llm: bool = False) -> ApplicationEntry | None:
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
        recruiter_name=recruiter_name,
        recruiter_email=recruiter_email,
        hiring_manager_name="",
        hiring_manager_email="",
        llm_confidence="",
        llm_notes="",
        raw_excerpt=body[:500],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    if use_llm:
        entry = apply_llm_fields(entry, extract_with_llm(sender, subject, body))
        if not entry:
            return None
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

        with self.path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(sorted(rows.values(), key=lambda row: row["received_at"], reverse=True))
        return inserted

    def save_rows(self, rows: list[dict[str, str]]) -> None:
        normalized_rows = []
        for row in rows:
            normalized_rows.append({field: row.get(field, "") for field in self.fieldnames})

        with self.path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(sorted(normalized_rows, key=lambda row: row["received_at"], reverse=True))


def merge_application_rows(existing: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    merged = dict(existing)
    for field, value in incoming.items():
        if field in {"application_hash", "application_key", "source_email", "received_at"}:
            continue
        if value:
            merged[field] = value
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
    use_llm: bool,
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
            entry = parse_application(parsed, username, use_llm=use_llm)
            if entry and within_date_window(entry.received_at, since_date, until_date):
                entries.append(entry)
    finally:
        close_imap(client)
    return entries


def enrich_existing_sheet(
    sheet_path: Path,
    limit: int,
    drop_irrelevant: bool,
    since_date: str = "",
    until_date: str = "",
) -> tuple[int, int]:
    if not llm_enabled():
        raise RuntimeError("Set GEMINI_API_KEY and APPLICATION_LLM_ENABLED=true before using --enrich-existing.")

    sheet = ApplicationSheet(sheet_path)
    rows = list(sheet.load().values())
    updated = 0
    removed = 0
    enriched_rows: list[dict[str, str]] = []

    for index, row in enumerate(rows):
        if not within_date_window(row.get("received_at", ""), since_date, until_date):
            enriched_rows.append(row)
            continue

        if limit > 0 and index >= limit:
            enriched_rows.append(row)
            continue

        extraction = extract_with_llm(
            sender=row.get("sender", ""),
            subject=row.get("subject", ""),
            body=row.get("raw_excerpt", ""),
        )
        if extraction and extraction.get("is_application_confirmation") is False:
            removed += 1
            if drop_irrelevant:
                continue
            row["status"] = "Irrelevant"
            row["llm_confidence"] = str(extraction.get("confidence", ""))
            row["llm_notes"] = clean_value(str(extraction.get("notes") or ""))
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            enriched_rows.append(row)
            updated += 1
            continue

        if extraction:
            for field in (
                "company",
                "role",
                "status",
                "recruiter_name",
                "recruiter_email",
                "hiring_manager_name",
                "hiring_manager_email",
            ):
                value = clean_value(str(extraction.get(field) or ""))
                if value:
                    row[field] = value
            row["llm_confidence"] = str(extraction.get("confidence", ""))
            row["llm_notes"] = clean_value(str(extraction.get("notes") or ""))
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            updated += 1
        enriched_rows.append(row)

    sheet.save_rows(enriched_rows)
    return updated, removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill application sheet from Gmail confirmation emails.")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    parser.add_argument("--since-date", default="", help="Only process emails received on or after YYYY-MM-DD.")
    parser.add_argument("--until-date", default="", help="Only process emails received on or before YYYY-MM-DD.")
    parser.add_argument("--max-emails", type=int, default=DEFAULT_MAX_EMAILS)
    parser.add_argument("--sheet-path", type=Path, default=DEFAULT_SHEET_PATH)
    parser.add_argument("--use-llm", action="store_true", help="Use Gemini to extract cleaner fields while scanning Gmail.")
    parser.add_argument("--enrich-existing", action="store_true", help="Use Gemini to clean rows already saved in the CSV.")
    parser.add_argument("--enrich-limit", type=int, default=0, help="Maximum existing rows to enrich; 0 means all rows.")
    parser.add_argument("--drop-irrelevant", action="store_true", help="Remove rows the LLM classifies as non-application emails.")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    load_dotenv()
    args = parse_args()

    if args.enrich_existing:
        updated, removed = enrich_existing_sheet(
            args.sheet_path,
            args.enrich_limit,
            args.drop_irrelevant,
            args.since_date,
            args.until_date,
        )
        logger.info("LLM-enriched %d existing rows; classified %d as irrelevant", updated, removed)
        return

    use_llm = args.use_llm or llm_enabled()
    entries = fetch_application_confirmations(
        args.days_back,
        args.max_emails,
        use_llm,
        args.since_date,
        args.until_date,
    )
    inserted = ApplicationSheet(args.sheet_path).upsert_many(entries)
    logger.info("Found %d application confirmations; inserted %d new rows into %s", len(entries), inserted, args.sheet_path)


if __name__ == "__main__":
    main()
