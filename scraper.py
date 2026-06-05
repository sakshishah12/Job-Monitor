"""
ATS JSON API scrapers — no HTML parsing.

Supported ATS types (set ``scraper`` in config.json):
  - greenhouse  → boards-api.greenhouse.io
  - ashby       → api.ashbyhq.com/posting-api
  - workday     → *.myworkdayjobs.com/wday/cxs (CXS API)
  - amazon      → amazon.jobs/en/search.json
  - microsoft   → apply.careers.microsoft.com/api/pcsx/search
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
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
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
        self.config = company_config
        self.company_name = company_config["name"]
        self.careers_url = company_config.get("careers_url", "")
        self.max_pages = scrape_settings.get("max_pages_per_company", 5)
        self.page_size = scrape_settings.get("results_per_page", 20)
        self.delay_min = scrape_settings.get("delay_min_seconds", 2)
        self.delay_max = scrape_settings.get("delay_max_seconds", 6)
        self.timeout = scrape_settings.get("request_timeout_seconds", 30)
        self.fetch_descriptions = scrape_settings.get("fetch_descriptions", True)
        self.session = requests.Session()

    def _random_delay(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if self.careers_url:
            headers["Referer"] = self.careers_url
        return headers

    @abstractmethod
    def scrape(self) -> list[JobListing]:
        pass


class GreenhouseScraper(BaseScraper):
    """GET https://boards-api.greenhouse.io/v1/boards/{board_slug}/jobs?content=true"""

    def scrape(self) -> list[JobListing]:
        board_slug = self.config["board_slug"]
        url = f"https://boards-api.greenhouse.io/v1/boards/{board_slug}/jobs"
        jobs: list[JobListing] = []

        try:
            self._random_delay()
            response = self.session.get(
                url,
                params={"content": "true"},
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw_jobs = response.json().get("jobs", [])
        except (requests.RequestException, json.JSONDecodeError) as exc:
            logger.error("[%s] Greenhouse API failed: %s", self.company_name, exc)
            return []

        for item in raw_jobs:
            location = item.get("location", {}) or {}
            loc_name = location.get("name", "") if isinstance(location, dict) else str(location)
            content = item.get("content", "") or ""
            jobs.append(
                JobListing(
                    company=self.company_name,
                    job_id=str(item.get("id", "")),
                    title=(item.get("title") or "").strip(),
                    location=loc_name,
                    url=item.get("absolute_url", ""),
                    description=_strip_html(content),
                )
            )

        logger.info("[%s] Greenhouse: scraped %d jobs", self.company_name, len(jobs))
        return jobs


class AshbyScraper(BaseScraper):
    """GET https://api.ashbyhq.com/posting-api/job-board/{board_slug}"""

    def scrape(self) -> list[JobListing]:
        board_slug = self.config["board_slug"]
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board_slug}"
        jobs: list[JobListing] = []

        try:
            self._random_delay()
            response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
            raw_jobs = response.json().get("jobs", [])
        except (requests.RequestException, json.JSONDecodeError) as exc:
            logger.error("[%s] Ashby API failed: %s", self.company_name, exc)
            return []

        for item in raw_jobs:
            if item.get("isListed") is False:
                continue
            jobs.append(
                JobListing(
                    company=self.company_name,
                    job_id=str(item.get("id", "")),
                    title=(item.get("title") or "").strip(),
                    location=(item.get("location") or "").strip(),
                    url=item.get("jobUrl") or item.get("applyUrl", ""),
                    description=item.get("descriptionPlain") or _strip_html(item.get("descriptionHtml", "")),
                )
            )

        logger.info("[%s] Ashby: scraped %d jobs", self.company_name, len(jobs))
        return jobs


class WorkdayScraper(BaseScraper):
    """
    POST https://{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    Fetches job detail for description when fetch_descriptions is enabled.
    """

    WORKDAY_PAGE_SIZE = 20

    def scrape(self) -> list[JobListing]:
        tenant = self.config["tenant"]
        wd_server = self.config.get("wd_server", "wd3")
        site = self.config["site"]
        base = f"https://{tenant}.{wd_server}.myworkdayjobs.com"
        list_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        referer = f"{base}/en-US/{site}"

        jobs: list[JobListing] = []
        offset = 0

        for page in range(self.max_pages):
            try:
                self._random_delay()
                response = self.session.post(
                    list_url,
                    json={
                        "appliedFacets": {},
                        "limit": self.WORKDAY_PAGE_SIZE,
                        "offset": offset,
                        "searchText": "",
                    },
                    headers={**self._headers(), "Content-Type": "application/json", "Referer": referer},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, json.JSONDecodeError) as exc:
                logger.error("[%s] Workday page %d failed: %s", self.company_name, page + 1, exc)
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break

            for post in postings:
                external_path = post.get("externalPath", "")
                url = urljoin(f"{base}/en-US/{site}/", external_path.lstrip("/"))
                description = ""

                if self.fetch_descriptions and external_path:
                    description = self._fetch_description(base, tenant, site, external_path, referer)

                jobs.append(
                    JobListing(
                        company=self.company_name,
                        job_id=external_path.split("/")[-1] if external_path else post.get("title", ""),
                        title=(post.get("title") or "").strip(),
                        location=(post.get("locationsText") or "").strip(),
                        url=url,
                        description=description,
                    )
                )

            offset += self.WORKDAY_PAGE_SIZE
            total = data.get("total", 0)
            if offset >= total:
                break

        logger.info("[%s] Workday: scraped %d jobs", self.company_name, len(jobs))
        return jobs

    def _fetch_description(
        self, base: str, tenant: str, site: str, external_path: str, referer: str
    ) -> str:
        detail_url = f"{base}/wday/cxs/{tenant}/{site}/job/{external_path.lstrip('/')}"
        try:
            self._random_delay()
            response = self.session.get(
                detail_url,
                headers={**self._headers(), "Referer": referer},
                timeout=self.timeout,
            )
            response.raise_for_status()
            info = response.json().get("jobPostingInfo", {})
            return _strip_html(info.get("jobDescription", ""))
        except (requests.RequestException, json.JSONDecodeError, AttributeError) as exc:
            logger.debug("[%s] Workday detail fetch failed for %s: %s", self.company_name, external_path, exc)
            return ""


class AmazonScraper(BaseScraper):
    API_URL = "https://www.amazon.jobs/en/search.json"

    def scrape(self) -> list[JobListing]:
        jobs: list[JobListing] = []
        offset = 0

        for page in range(self.max_pages):
            try:
                self._random_delay()
                response = self.session.get(
                    self.API_URL,
                    params={"base_query": "", "result_limit": self.page_size, "offset": offset, "sort": "recent"},
                    headers={**self._headers(), "X-Requested-With": "XMLHttpRequest"},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, json.JSONDecodeError) as exc:
                logger.error("[%s] Amazon page %d failed: %s", self.company_name, page + 1, exc)
                break

            batch = data.get("jobs", [])
            if not batch:
                break

            for item in batch:
                job_path = item.get("job_path", "")
                url = f"https://www.amazon.jobs{job_path}" if job_path.startswith("/") else job_path
                location_parts = [p for p in [item.get("city"), item.get("state"), item.get("country_code")] if p]
                jobs.append(
                    JobListing(
                        company=self.company_name,
                        job_id=str(item.get("id_icims") or item.get("id", "")),
                        title=(item.get("title") or "").strip(),
                        location=", ".join(location_parts),
                        url=url,
                        description=item.get("description", "") or item.get("description_short", ""),
                    )
                )

            offset += self.page_size
            if offset >= data.get("hits", 0):
                break

        logger.info("[%s] Amazon: scraped %d jobs", self.company_name, len(jobs))
        return jobs


class MicrosoftScraper(BaseScraper):
    API_URL = "https://apply.careers.microsoft.com/api/pcsx/search"

    def scrape(self) -> list[JobListing]:
        jobs: list[JobListing] = []
        start = 0

        for page in range(self.max_pages):
            try:
                self._random_delay()
                response = self.session.get(
                    self.API_URL,
                    params={"domain": "microsoft.com", "start": start, "count": self.page_size},
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, json.JSONDecodeError, KeyError) as exc:
                logger.error("[%s] Microsoft page %d failed: %s", self.company_name, page + 1, exc)
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
                        title=(pos.get("name") or "").strip(),
                        location="; ".join(locations) if isinstance(locations, list) else str(locations),
                        url=url,
                        description=pos.get("department", ""),
                    )
                )

            start += self.page_size
            if start >= data.get("data", {}).get("count", 0):
                break

        logger.info("[%s] Microsoft: scraped %d jobs", self.company_name, len(jobs))
        return jobs


SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "greenhouse": GreenhouseScraper,
    "ashby": AshbyScraper,
    "workday": WorkdayScraper,
    "amazon": AmazonScraper,
    "microsoft": MicrosoftScraper,
}


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def get_scraper(company_config: dict, scrape_settings: dict) -> Optional[BaseScraper]:
    scraper_type = company_config.get("scraper", "").lower()
    scraper_cls = SCRAPER_REGISTRY.get(scraper_type)
    if not scraper_cls:
        logger.error("Unknown scraper type '%s' for %s", scraper_type, company_config.get("name"))
        return None
    return scraper_cls(company_config, scrape_settings)
