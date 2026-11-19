"""Shared scraping helpers: robots.txt compliance, rate limiting, Playwright rendering.

Used by scrape_darglobal.py and scrape_wasalt.py. Kept deliberately conservative:
a fixed User-Agent that identifies the bot and its purpose, a rate limit between
requests, and a hard cap on how many pages we render per site so this behaves as
a small, polite, one-off sample crawl rather than an aggressive full-site scrape.
"""
from __future__ import annotations

import random
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

USER_AGENT = "AECC-TechAssignment-Bot/1.0 (+contact: prafull.shukla@aeccglobal.com; one-off educational data sample)"

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MIN_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 2.0


def polite_sleep() -> None:
    time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RobotsGate:
    """Loads a site's robots.txt once and answers can_fetch() for a given user-agent."""

    base_url: str
    parser: urllib.robotparser.RobotFileParser

    @classmethod
    def load(cls, base_url: str) -> "RobotsGate":
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = requests.get(robots_url, headers=DEFAULT_HEADERS, timeout=15)
            resp.raise_for_status()
            rp.parse(resp.text.splitlines())
        except Exception:
            # If robots.txt can't be fetched, default to permissive (most sites allow "/")
            rp.parse(["User-agent: *", "Allow: /"])
        return cls(base_url=base_url, parser=rp)

    def can_fetch(self, url: str) -> bool:
        try:
            return self.parser.can_fetch(USER_AGENT, url)
        except Exception:
            return True


def fetch_sitemap_xml(url: str) -> str:
    """Fetch a sitemap (plain XML, possibly served pre-decompressed despite a .gz name)."""
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def render_page_html(playwright_page, url: str, wait_selector: str | None = None, timeout_ms: int = 20000) -> str | None:
    """Navigate a Playwright page to url and return rendered HTML, or None if blocked/failed."""
    try:
        playwright_page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        if wait_selector:
            try:
                playwright_page.wait_for_selector(wait_selector, timeout=timeout_ms)
            except Exception:
                pass
        else:
            playwright_page.wait_for_timeout(1500)
        html = playwright_page.content()
        # crude bot-block detection (Incapsula / Cloudflare challenge stubs)
        lowered = html.lower()
        if "incapsula incident" in lowered or "just a moment" in lowered or "_incapsula_resource" in lowered:
            return None
        return html
    except Exception:
        return None
