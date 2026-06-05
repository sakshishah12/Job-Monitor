"""
Company-specific job scrapers.

Strategy per company:
  - Amazon:   Direct JSON API (/en/search.json) — no browser needed.
  - Microsoft: Hidden PCSX REST API (/api/pcsx/search) used by apply.careers.microsoft.com.
  - Google:   Embedded AF_initDataCallback JSON in server-rendered HTML.
  - Apple:    Server-rendered HTML parsed with BeautifulSoup (API endpoint deprecated).

JobSpy is not used here because it targets job boards (LinkedIn, Indeed, etc.),
not direct company career pages.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
]


@dataclass
class JobListing:
    company: str
    job_id: str
    title: str
    location: str
    url: str
    description: str = ""
    matched_keywords: list[str] = field(default_factory=list)


class BaseScraper(ABC):
    def __init__(self, company_config: dict, scrape_settings: dict) -> None:
        self.company_name = company_config["name"]
        self.base_url = company_config.get("base_url", "")
        self.careers_url = company_config.get("careers_url", "")
        self.max_pages = scrape_settings.get("max_pages_per_company", 5)
        self.page_size = scrape_settings.get("results_per_page", 20)
        self.delay_min = scrape_settings.get("delay_min_seconds", 2)
        self.delay_max = scrape_settings.get("delay_max_seconds", 6)
        self.timeout = scrape_settings.get("request_timeout_seconds", 30)
        self.session = requests.Session()

    def _random_delay(self) -> None:
        delay = random.uniform(self.delay_min, self.delay_max)
        logger.debug("[%s] Sleeping %.1fs", self.company_name, delay)
        time.sleep(delay)

    def _get_headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    @abstractmethod
    def scrape(self) -> list[JobListing]:
        pass


class AmazonScraper(BaseScraper):
    """Scrapes amazon.jobs via the internal search.json XHR endpoint."""

    API_URL = "https://www.amazon.jobs/en/search.json"

    def scrape(self) -> list[JobListing]:
        jobs: list[JobListing] = []
        offset = 0

        for page in range(self.max_pages):
            try:
                self._random_delay()
                response = self.session.get(
                    self.API_URL,
                    params={
                        "base_query": "",
                        "result_limit": self.page_size,
                        "offset": offset,
                        "sort": "recent",
                    },
                    headers={
                        **self._get_headers(),
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": self.careers_url,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, json.JSONDecodeError) as exc:
                logger.error("[%s] Page %d failed: %s", self.company_name, page + 1, exc)
                break

            batch = data.get("jobs", [])
            if not batch:
                break

            for item in batch:
                job_path = item.get("job_path", "")
                url = f"https://www.amazon.jobs{job_path}" if job_path.startswith("/") else job_path
                location_parts = [
                    p for p in [item.get("city"), item.get("state"), item.get("country_code")] if p
                ]
                jobs.append(
                    JobListing(
                        company=self.company_name,
                        job_id=str(item.get("id_icims") or item.get("id", "")),
                        title=item.get("title", "").strip(),
                        location=", ".join(location_parts),
                        url=url,
                        description=item.get("description", "") or item.get("description_short", ""),
                    )
                )

            offset += self.page_size
            if offset >= data.get("hits", 0):
                break

        logger.info("[%s] Scraped %d jobs", self.company_name, len(jobs))
        return jobs


class MicrosoftScraper(BaseScraper):
    """Scrapes apply.careers.microsoft.com via the PCSX search API."""

    API_URL = "https://apply.careers.microsoft.com/api/pcsx/search"

    def scrape(self) -> list[JobListing]:
        jobs: list[JobListing] = []
        start = 0

        for page in range(self.max_pages):
            try:
                self._random_delay()
                response = self.session.get(
                    self.API_URL,
                    params={
                        "domain": "microsoft.com",
                        "start": start,
                        "count": self.page_size,
                    },
                    headers={
                        **self._get_headers(),
                        "Accept": "application/json",
                        "Referer": self.careers_url,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, json.JSONDecodeError, KeyError) as exc:
                logger.error("[%s] Page %d failed: %s", self.company_name, page + 1, exc)
                break

            positions = data.get("data", {}).get("positions", [])
            if not positions:
                break

            for pos in positions:
                position_url = pos.get("positionUrl", "")
                url = (
                    f"https://apply.careers.microsoft.com{position_url}"
                    if position_url.startswith("/")
                    else position_url
                )
                locations = pos.get("locations") or pos.get("standardizedLocations") or []
                jobs.append(
                    JobListing(
                        company=self.company_name,
                        job_id=str(pos.get("displayJobId") or pos.get("id", "")),
                        title=pos.get("name", "").strip(),
                        location="; ".join(locations) if isinstance(locations, list) else str(locations),
                        url=url,
                        description=pos.get("department", ""),
                    )
                )

            start += self.page_size
            total = data.get("data", {}).get("count", 0)
            if start >= total:
                break

        logger.info("[%s] Scraped %d jobs", self.company_name, len(jobs))
        return jobs


class GoogleScraper(BaseScraper):
    """Parses embedded AF_initDataCallback JSON from Google Careers HTML."""

    def scrape(self) -> list[JobListing]:
        jobs: list[JobListing] = []

        for page in range(1, self.max_pages + 1):
            try:
                self._random_delay()
                response = self.session.get(
                    self.careers_url,
                    params={"q": "", "page": page},
                    headers=self._get_headers(),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                batch = self._parse_jobs_from_html(response.text)
            except requests.RequestException as exc:
                logger.error("[%s] Page %d failed: %s", self.company_name, page, exc)
                break

            if not batch:
                break
            jobs.extend(batch)

        logger.info("[%s] Scraped %d jobs", self.company_name, len(jobs))
        return jobs

    def _parse_jobs_from_html(self, html: str) -> list[JobListing]:
        match = re.search(
            r"AF_initDataCallback\(\{key:\s*'ds:1'.*?data:(.*?)\s*,\s*sideChannel",
            html,
            re.DOTALL,
        )
        if not match:
            logger.warning("[%s] Could not find job data in page HTML", self.company_name)
            return []

        try:
            raw_jobs = json.loads(match.group(1))[0]
        except (json.JSONDecodeError, IndexError) as exc:
            logger.error("[%s] Failed to parse job JSON: %s", self.company_name, exc)
            return []

        listings: list[JobListing] = []
        for entry in raw_jobs:
            if not isinstance(entry, list) or len(entry) < 3:
                continue

            job_id = str(entry[0])
            title = str(entry[1]) if entry[1] else ""
            url = str(entry[2]) if entry[2] else ""

            if url and "jobId=" in url:
                job_id_param = re.search(r"jobId=([^&]+)", url)
                if job_id_param:
                    url = (
                        f"https://www.google.com/about/careers/applications/"
                        f"jobs/results/{job_id_param.group(1)}"
                    )

            location = self._extract_location(entry)
            description = self._extract_description(entry)

            listings.append(
                JobListing(
                    company=self.company_name,
                    job_id=job_id,
                    title=title.strip(),
                    location=location,
                    url=url,
                    description=description,
                )
            )

        return listings

    @staticmethod
    def _extract_location(entry: list[Any]) -> str:
        if len(entry) > 9 and isinstance(entry[9], list) and entry[9]:
            first_loc = entry[9][0]
            if isinstance(first_loc, list) and first_loc:
                return str(first_loc[0])
        return ""

    @staticmethod
    def _extract_description(entry: list[Any]) -> str:
        parts: list[str] = []
        for idx in (3, 4, 10, 19):
            if len(entry) > idx and entry[idx]:
                val = entry[idx]
                if isinstance(val, list) and len(val) > 1 and val[1]:
                    parts.append(str(val[1]))
                elif isinstance(val, str):
                    parts.append(val)
        return " ".join(parts)


class AppleScraper(BaseScraper):
    """Parses server-rendered HTML from jobs.apple.com (20 jobs per page)."""

    def scrape(self) -> list[JobListing]:
        jobs: list[JobListing] = []

        for page in range(1, self.max_pages + 1):
            try:
                self._random_delay()
                response = self.session.get(
                    self.careers_url,
                    params={"page": page},
                    headers=self._get_headers(),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                batch = self._parse_jobs_from_html(response.text)
            except requests.RequestException as exc:
                logger.error("[%s] Page %d failed: %s", self.company_name, page, exc)
                break

            if not batch:
                break
            jobs.extend(batch)

        logger.info("[%s] Scraped %d jobs", self.company_name, len(jobs))
        return jobs

    def _parse_jobs_from_html(self, html: str) -> list[JobListing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[JobListing] = []

        for item in soup.select("li.rc-accordion-item"):
            title_link = item.select_one(".job-title h3 a")
            if not title_link:
                continue

            title = title_link.get_text(strip=True)
            href = title_link.get("href", "")
            url = f"{self.base_url}{href}" if href.startswith("/") else href

            team_el = item.select_one(".team-name")
            team = team_el.get_text(strip=True) if team_el else ""

            role_el = item.select_one("[class*='role-number'], [id*='Role-Number']")
            role_text = item.get_text(" ", strip=True)
            job_id_match = re.search(r"Role Number:\s*(\d+)", role_text)
            job_id = job_id_match.group(1) if job_id_match else href.split("/details/")[-1].split("/")[0]

            location = self._extract_location(item)
            date_posted = self._extract_date(item)

            listings.append(
                JobListing(
                    company=self.company_name,
                    job_id=str(job_id),
                    title=title,
                    location=location,
                    url=url,
                    description=f"{team}. Posted: {date_posted}".strip(". "),
                )
            )

        return listings

    @staticmethod
    def _extract_location(item: BeautifulSoup) -> str:
        for el in item.select("span, div"):
            text = el.get_text(strip=True)
            if text.startswith("Location") and len(text) > 10:
                return text.replace("Location", "", 1).strip()
        loc_match = re.search(r"Location\s+(.+?)(?:Actions|$)", item.get_text(" ", strip=True))
        return loc_match.group(1).strip() if loc_match else ""

    @staticmethod
    def _extract_date(item: BeautifulSoup) -> str:
        date_match = re.search(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}",
            item.get_text(" ", strip=True),
        )
        return date_match.group(0) if date_match else ""


SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "amazon": AmazonScraper,
    "microsoft": MicrosoftScraper,
    "google": GoogleScraper,
    "apple": AppleScraper,
}


def match_keywords(job: JobListing, keywords: list[str]) -> list[str]:
    """Return keywords found in job title, location, or description (case-insensitive)."""
    searchable = " ".join([job.title, job.location, job.description])
    matched = []
    for keyword in keywords:
        if not keyword.strip():
            continue
        # Short keywords (e.g. "AI") use word boundaries to avoid matching inside "Retail".
        if len(keyword) <= 3 and " " not in keyword:
            pattern = rf"\b{re.escape(keyword)}\b"
            if re.search(pattern, searchable, re.IGNORECASE):
                matched.append(keyword)
        elif keyword.lower() in searchable.lower():
            matched.append(keyword)
    return matched


def get_scraper(company_config: dict, scrape_settings: dict) -> Optional[BaseScraper]:
    scraper_type = company_config.get("scraper", "").lower()
    scraper_cls = SCRAPER_REGISTRY.get(scraper_type)
    if not scraper_cls:
        logger.error("Unknown scraper type: %s", scraper_type)
        return None
    return scraper_cls(company_config, scrape_settings)
