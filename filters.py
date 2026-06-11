"""Profile-based job filtering for early-career AI/backend roles."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from scraper import JobListing


@dataclass
class ProfileFilter:
    must_include_keywords: list[str] = field(default_factory=list)
    target_roles: list[str] = field(default_factory=list)
    location_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict) -> ProfileFilter:
        profile = config.get("profile_filter", {})
        return cls(
            must_include_keywords=profile.get("must_include_keywords", []),
            target_roles=profile.get("target_roles", []),
            location_keywords=profile.get("location_keywords", []),
            exclude_keywords=profile.get("exclude_keywords", []),
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
