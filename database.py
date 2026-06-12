"""SQLite-backed state management to prevent duplicate job alerts."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SeenJob:
    job_hash: str
    company: str
    job_id: str
    title: str
    location: str
    url: str
    matched_keywords: str
    found_at: str


class JobDatabase:
    def __init__(self, db_path: str = "data/seen_jobs.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_jobs (
                    job_hash TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    location TEXT,
                    url TEXT NOT NULL,
                    matched_keywords TEXT,
                    found_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_jobs_company ON seen_jobs(company)"
            )

    @staticmethod
    def make_hash(company: str, job_id: str, url: str = "", title: str = "") -> str:
        identifier = job_id.strip() or url.strip() or title.strip()
        raw = f"{company.strip().lower()}::{identifier.lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_seen(self, company: str, job_id: str, url: str = "", title: str = "") -> bool:
        job_hash = self.make_hash(company, job_id, url, title)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_jobs WHERE job_hash = ?", (job_hash,)
            ).fetchone()
        return row is not None

    def mark_seen(
        self,
        company: str,
        job_id: str,
        title: str,
        location: str,
        url: str,
        matched_keywords: list[str],
    ) -> None:
        job_hash = self.make_hash(company, job_id, url, title)
        found_at = datetime.now(timezone.utc).isoformat()
        keywords_str = ", ".join(matched_keywords)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO seen_jobs
                (job_hash, company, job_id, title, location, url, matched_keywords, found_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_hash, company, job_id, title, location, url, keywords_str, found_at),
            )
        logger.debug("Marked seen: %s / %s", company, job_id)

    def count_seen(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM seen_jobs").fetchone()
        return int(row["total"]) if row else 0

    def get_recent(self, limit: int = 10) -> list[SeenJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_hash, company, job_id, title, location, url,
                       matched_keywords, found_at
                FROM seen_jobs
                ORDER BY found_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            SeenJob(
                job_hash=row["job_hash"],
                company=row["company"],
                job_id=row["job_id"],
                title=row["title"],
                location=row["location"] or "",
                url=row["url"],
                matched_keywords=row["matched_keywords"] or "",
                found_at=row["found_at"],
            )
            for row in rows
        ]

    def get_all_seen(self) -> list[SeenJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_hash, company, job_id, title, location, url,
                       matched_keywords, found_at
                FROM seen_jobs
                ORDER BY found_at DESC, company ASC, title ASC
                """
            ).fetchall()

        return [
            SeenJob(
                job_hash=row["job_hash"],
                company=row["company"],
                job_id=row["job_id"],
                title=row["title"],
                location=row["location"] or "",
                url=row["url"],
                matched_keywords=row["matched_keywords"] or "",
                found_at=row["found_at"],
            )
            for row in rows
        ]
