"""Profile-based job filtering for early-career AI/backend roles."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from scraper import JobListing


@dataclass
class ProfileFilter:
    must_include_keywords: list[str] = field(default_factory=list)
    target_roles: list[str] = field(default_factory=list)
    location_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    posting_date_filter: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict) -> ProfileFilter:
        profile = config.get("profile_filter", {})
        return cls(
            must_include_keywords=profile.get("must_include_keywords", []),
            target_roles=profile.get("target_roles", []),
            location_keywords=profile.get("location_keywords", []),
            exclude_keywords=profile.get("exclude_keywords", []),
            posting_date_filter=profile.get("posting_date_filter", {}),
        )

    def evaluate(self, job: JobListing) -> tuple[bool, list[str]]:
        """
        Returns (passes, matched_terms).
        matched_terms lists which include/role keywords triggered the match.
        """
        title = job.title or ""
        description = job.description or ""
        location = job.location or ""
        full_text = " ".join([title, description, location])

        if not self._matches_posting_date(job):
            return False, []

        for term in self.exclude_keywords:
            if self._matches_term(term, title):
                return False, []

        role_hits = [r for r in self.target_roles if self._matches_term(r, title)]
        if not role_hits:
            role_hits = [r for r in self.target_roles if self._matches_term(r, full_text)]
        if self.target_roles and not role_hits:
            return False, []

        skill_hits = [k for k in self.must_include_keywords if self._matches_term(k, full_text)]
        if self.must_include_keywords and not skill_hits:
            return False, []

        location_hits = [k for k in self.location_keywords if self._matches_term(k, location)]
        if self.location_keywords and not location_hits:
            return False, []

        return True, role_hits + skill_hits + location_hits

    @staticmethod
    def _matches_term(term: str, text: str) -> bool:
        if not term.strip() or not text.strip():
            return False
        pattern = rf"\b{re.escape(term.strip())}\b"
        return bool(re.search(pattern, text, re.IGNORECASE))

    def _matches_posting_date(self, job: JobListing) -> bool:
        config = self.posting_date_filter or {}
        if not config.get("enabled", False):
            return True

        posted_at = self._parse_datetime(job.posted_at)
        if not posted_at:
            return not config.get("require_posted_at", True)

        now = datetime.now(timezone.utc)
        within_days = config.get("posted_within_days")
        if within_days:
            try:
                if posted_at < now - timedelta(days=int(within_days)):
                    return False
            except (TypeError, ValueError):
                return False

        posted_after = config.get("posted_after", "")
        if posted_after:
            after_dt = self._parse_date(posted_after)
            if after_dt and posted_at < after_dt:
                return False

        posted_before = config.get("posted_before", "")
        if posted_before:
            before_dt = self._parse_date(posted_before, end_of_day=True)
            if before_dt and posted_at > before_dt:
                return False

        return True

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        if not value:
            return None
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _parse_date(value: str, end_of_day: bool = False) -> datetime | None:
        try:
            parsed = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        if end_of_day:
            return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        return parsed
