"""
Job Monitor — scrapes company career pages via ATS JSON APIs,
filters by candidate profile, deduplicates via SQLite, and alerts via Telegram/Email.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from database import JobDatabase, SeenJob
from filters import ProfileFilter
from notifier import JobAlert, NotificationManager
from scraper import get_scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("job_monitor")

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DB_PATH = PROJECT_ROOT / "data" / "seen_jobs.db"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def process_company(
    company_config: dict,
    scrape_settings: dict,
    profile: ProfileFilter,
    db: JobDatabase,
    notifier: NotificationManager,
    include_seen_in_email: bool = False,
) -> tuple[int, int, int, list[JobAlert]]:
    """Returns (scraped_count, matched_count, new_alert_count, email_alerts)."""
    company_name = company_config["name"]
    scraper = get_scraper(company_config, scrape_settings)
    if not scraper:
        return 0, 0, 0, []

    try:
        jobs = scraper.scrape()
    except Exception as exc:
        logger.error("[%s] Scraper crashed: %s", company_name, exc, exc_info=True)
        return 0, 0, 0, []

    matched = 0
    new_alert_count = 0
    email_alerts: list[JobAlert] = []

    for job in jobs:
        passes, matched_terms = profile.evaluate(job)
        if not passes:
            continue

        matched += 1
        job.matched_keywords = matched_terms

        alert = JobAlert(
            company=job.company,
            title=job.title,
            location=job.location,
            matched_keywords=matched_terms,
            url=job.url,
        )

        if db.is_seen(job.company, job.job_id, job.url, job.title):
            if include_seen_in_email:
                email_alerts.append(alert)
            continue

        db.mark_seen(
            company=job.company,
            job_id=job.job_id,
            title=job.title,
            location=job.location,
            url=job.url,
            matched_keywords=matched_terms,
        )

        notifier.send_alert(alert)
        email_alerts.append(alert)
        new_alert_count += 1
        logger.info(
            "NEW: [%s] %s — matched: %s",
            job.company,
            job.title,
            ", ".join(matched_terms),
        )

    return len(jobs), matched, new_alert_count, email_alerts


def seen_job_to_alert(job: SeenJob) -> JobAlert:
    matched_keywords = [
        keyword.strip()
        for keyword in job.matched_keywords.split(",")
        if keyword.strip()
    ]
    return JobAlert(
        company=job.company,
        title=job.title,
        location=job.location,
        matched_keywords=matched_keywords,
        url=job.url,
    )


def yes_no(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"yes", "y", "true", "1", "on"}:
        return True
    if normalized in {"no", "n", "false", "0", "off"}:
        return False
    raise argparse.ArgumentTypeError("Use yes or no")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape jobs and send notifications.")
    parser.add_argument(
        "--email-all-seen",
        "--email-db-jobs",
        action="store_true",
        dest="email_all_seen",
        help="Send one email digest with every job already stored in the DB, then exit.",
    )
    parser.add_argument(
        "--email-seen",
        "--email-sent",
        nargs="?",
        const="yes",
        default=False,
        type=yes_no,
        dest="email_seen",
        help="Include already-seen matching jobs in the final email digest for this scrape run.",
    )
    return parser.parse_args()


def send_all_seen_jobs_email(db: JobDatabase, notifier: NotificationManager) -> None:
    jobs = db.get_all_seen()
    if not jobs:
        logger.warning("No jobs found in DB to email")
        return

    alerts = [seen_job_to_alert(job) for job in jobs]
    notifier.send_email_digest(alerts, title="All Saved Job Matches")


def run(email_all_seen: bool = False, email_seen: bool = False) -> None:
    load_dotenv()
    config = load_config()
    profile = ProfileFilter.from_config(config)
    scrape_settings = config.get("scrape_settings", {})
    companies = config.get("companies", [])

    if not companies:
        logger.error("No companies configured in config.json")
        sys.exit(1)

    db = JobDatabase(str(DB_PATH))
    notifier = NotificationManager()
    logger.info("Using seen-jobs database: %s", DB_PATH)

    if email_all_seen:
        logger.info("Sending one email digest for all jobs currently stored in DB")
        send_all_seen_jobs_email(db, notifier)
        return

    total_scraped = 0
    total_matched = 0
    total_alerted = 0
    errors = 0
    new_alerts: list[JobAlert] = []

    enabled = [c for c in companies if c.get("enabled", True)]
    logger.info(
        "Starting job monitor — %d companies enabled, profile: %d skills, %d roles, %d exclusions",
        len(enabled),
        len(profile.must_include_keywords),
        len(profile.target_roles),
        len(profile.exclude_keywords),
    )
    logger.info("Previously seen jobs in DB: %d", db.count_seen())

    for company_config in companies:
        if not company_config.get("enabled", True):
            logger.info("Skipping disabled company: %s", company_config.get("name"))
            continue

        company_name = company_config.get("name", "Unknown")
        logger.info("--- Scraping %s (%s) ---", company_name, company_config.get("scraper", "?"))

        try:
            scraped, matched, new_alert_count, company_alerts = process_company(
                company_config,
                scrape_settings,
                profile,
                db,
                notifier,
                include_seen_in_email=email_seen,
            )
            total_scraped += scraped
            total_matched += matched
            total_alerted += new_alert_count
            new_alerts.extend(company_alerts)
            logger.info(
                "[%s] Done — scraped: %d, profile matches: %d, new alerts: %d",
                company_name,
                scraped,
                matched,
                new_alert_count,
            )
        except Exception as exc:
            errors += 1
            logger.error("[%s] Unexpected error: %s", company_name, exc, exc_info=True)

    email_title = "All Matching Jobs From This Run" if email_seen else "New Job Matches"
    notifier.send_email_digest(new_alerts, title=email_title)

    logger.info(
        "Run complete — scraped: %d, matched: %d, new alerts: %d, errors: %d, total seen: %d",
        total_scraped,
        total_matched,
        total_alerted,
        errors,
        db.count_seen(),
    )


if __name__ == "__main__":
    args = parse_args()
    run(email_all_seen=args.email_all_seen, email_seen=args.email_seen)
