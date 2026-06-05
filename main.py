"""
Job Monitor — scrapes FAANG career pages, filters by keywords,
deduplicates via SQLite, and sends Telegram/Email alerts.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from database import JobDatabase
from notifier import JobAlert, NotificationManager
from scraper import JobListing, get_scraper, match_keywords

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("job_monitor")

CONFIG_PATH = Path("config.json")
DB_PATH = Path("data/seen_jobs.db")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def process_company(
    company_config: dict,
    scrape_settings: dict,
    keywords: list[str],
    db: JobDatabase,
    notifier: NotificationManager,
) -> tuple[int, int, int]:
    """Returns (scraped_count, matched_count, new_alert_count)."""
    company_name = company_config["name"]
    scraper = get_scraper(company_config, scrape_settings)
    if not scraper:
        return 0, 0, 0

    try:
        jobs = scraper.scrape()
    except Exception as exc:
        logger.error("[%s] Scraper crashed: %s", company_name, exc, exc_info=True)
        return 0, 0, 0

    matched = 0
    alerted = 0

    for job in jobs:
        keywords_found = match_keywords(job, keywords)
        if not keywords_found:
            continue

        matched += 1
        job.matched_keywords = keywords_found

        if db.is_seen(job.company, job.job_id):
            continue

        db.mark_seen(
            company=job.company,
            job_id=job.job_id,
            title=job.title,
            location=job.location,
            url=job.url,
            matched_keywords=keywords_found,
        )

        notifier.send_alert(
            JobAlert(
                company=job.company,
                title=job.title,
                location=job.location,
                matched_keywords=keywords_found,
                url=job.url,
            )
        )
        alerted += 1
        logger.info(
            "NEW: [%s] %s — keywords: %s",
            job.company,
            job.title,
            ", ".join(keywords_found),
        )

    return len(jobs), matched, alerted


def run() -> None:
    load_dotenv()
    config = load_config()
    keywords = config.get("keywords", [])
    scrape_settings = config.get("scrape_settings", {})
    companies = config.get("companies", [])

    if not keywords:
        logger.warning("No keywords configured in config.json")
    if not companies:
        logger.error("No companies configured in config.json")
        sys.exit(1)

    db = JobDatabase(str(DB_PATH))
    notifier = NotificationManager()

    total_scraped = 0
    total_matched = 0
    total_alerted = 0
    errors = 0

    logger.info("Starting job monitor — %d companies, %d keywords", len(companies), len(keywords))
    logger.info("Previously seen jobs in DB: %d", db.count_seen())

    for company_config in companies:
        if not company_config.get("enabled", True):
            logger.info("Skipping disabled company: %s", company_config.get("name"))
            continue

        company_name = company_config.get("name", "Unknown")
        logger.info("--- Scraping %s ---", company_name)

        try:
            scraped, matched, alerted = process_company(
                company_config, scrape_settings, keywords, db, notifier
            )
            total_scraped += scraped
            total_matched += matched
            total_alerted += alerted
            logger.info(
                "[%s] Done — scraped: %d, keyword matches: %d, new alerts: %d",
                company_name,
                scraped,
                matched,
                alerted,
            )
        except Exception as exc:
            errors += 1
            logger.error("[%s] Unexpected error: %s", company_name, exc, exc_info=True)

    logger.info(
        "Run complete — scraped: %d, matched: %d, new alerts: %d, errors: %d, total seen: %d",
        total_scraped,
        total_matched,
        total_alerted,
        errors,
        db.count_seen(),
    )


if __name__ == "__main__":
    run()
