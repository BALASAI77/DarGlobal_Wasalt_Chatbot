"""Sample Wasalt listing URLs from its CDN sitemaps.

Matches the working implementation in scraper_works/scrape_wasalt.py, while
writing records into the app's existing JSONL files. The CDN's missing robots
file (HTTP 404) does not prevent reading its public sitemap. Listing pages are
rendered only when Wasalt's own robots.txt permits the identified user agent;
otherwise, sitemap slug metadata is retained without requesting the page.

Run from the project directory:
    python -m scraper.scrape_wasalt --sample-size 200
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
RAW_OUT = ROOT / "data" / "raw" / "pages.jsonl"
CHUNKS_OUT = ROOT / "data" / "chunks.jsonl"
USER_AGENT = "AECC-TechAssignment-Bot/1.0 (+contact: prafull.shukla@aeccglobal.com; one-off educational data sample)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SITEMAPS = (
    "https://cdn.wasalt.sa/sitemap/product_sitemap_en_sa.xml.gz",
    "https://cdn.wasalt.sa/sitemap/rental_pdp_sitemap_en_sa.xml.gz",
)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

DEAL_RE = re.compile(r"/property/(?P<deal>sale|rent)/", re.IGNORECASE)
DAILY_RE = re.compile(r"/dailyrental/", re.IGNORECASE)
ID_RE = re.compile(r"-(?P<id>\d+)/?$")
BEDROOMS_RE = re.compile(r"(?P<bedrooms>\d+)-bedrooms?\b", re.IGNORECASE)
TYPE_RE = re.compile(r"^(?P<type>[a-z]+(?:-[a-z]+)*?)(?:-\d|-with-|-facing-|-for-booking|$)", re.IGNORECASE)


def fetch_listing_urls(sitemap_url: str) -> list[str]:
    response = requests.get(sitemap_url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    body = response.content
    # Some CDNs serve the XML already decompressed despite the .gz suffix.
    if body.startswith(b"\x1f\x8b"):
        body = gzip.decompress(body)
    root = ET.fromstring(body)
    return [node.text.strip() for node in root.findall(".//sm:loc", SITEMAP_NS) if node.text and node.text.strip()]


class RobotsGate:
    """Load robots once per host. A 404 means no robots file was published."""

    def __init__(self) -> None:
        self._rules: dict[str, urllib.robotparser.RobotFileParser] = {}

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._rules:
            robots_url = f"{base}/robots.txt"
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            try:
                response = requests.get(robots_url, headers=HEADERS, timeout=15)
                if response.status_code == 404:
                    parser.parse(["User-agent: *", "Allow: /"])
                else:
                    response.raise_for_status()
                    parser.parse(response.text.splitlines())
            except requests.RequestException as exc:
                # Do not request pages when robots policy cannot be determined.
                print(f"[wasalt] could not read {robots_url}: {exc}; skipping page fetches")
                parser.parse(["User-agent: *", "Disallow: /"])
            self._rules[base] = parser
        return self._rules[base].can_fetch(USER_AGENT, url)


def parse_slug(url: str) -> dict[str, str | None]:
    path = urlparse(url).path
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    deal = DEAL_RE.search(path)
    if deal:
        deal_type = deal.group("deal").lower()
    elif DAILY_RE.search(path):
        deal_type = "daily-rental"
    else:
        deal_type = None
    id_match = ID_RE.search(slug)
    bedrooms = BEDROOMS_RE.search(slug)
    prop_type = TYPE_RE.match(slug)
    return {
        "listing_id": id_match.group("id") if id_match else None,
        "property_type": prop_type.group("type").replace("-", " ").strip() if prop_type else None,
        "bedrooms": bedrooms.group("bedrooms") if bedrooms else None,
        "deal_type": deal_type,
    }


def render_listing(page, url: str) -> str | None:
    try:
        page.goto(url, timeout=20_000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("h1", timeout=5_000)
        except Exception:
            pass
        html = page.content()
        lowered = html.lower()
        if "just a moment" in lowered or "cloudflare" in lowered and "challenge" in lowered:
            return None
        return html
    except Exception:
        return None


def parse_listing_page(html: str, url: str, slug_fields: dict) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1") or soup.select_one("title")
    title = heading.get_text(" ", strip=True) if heading else None
    body_text = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))[:4000]
    price_match = re.search(r"(SAR|ريال)\s?[\d,.]+", body_text)
    area_match = re.search(r"[\d,.]+\s?(?:sqm|sq\.?\s?m|m²)", body_text, re.IGNORECASE)
    return {
        "source": "Wasalt",
        "title": title,
        "text": body_text,
        "price": price_match.group(0) if price_match else None,
        "area": area_match.group(0) if area_match else None,
        "data_source": "rendered listing page",
        **slug_fields,
    }


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=200, help="maximum number of listing URLs to sample")
    parser.add_argument("--seed", type=int, default=42, help="random seed for a repeatable sample")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between rendered page requests")
    args = parser.parse_args()
    if args.sample_size < 1 or args.delay < 0:
        parser.error("--sample-size must be positive and --delay must not be negative")

    urls: list[str] = []
    for sitemap in SITEMAPS:
        try:
            found = fetch_listing_urls(sitemap)
            print(f"[wasalt] {sitemap}: {len(found)} URLs")
            urls.extend(found)
        except (requests.RequestException, ET.ParseError, OSError) as exc:
            print(f"[wasalt] failed to fetch {sitemap}: {exc}")
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise SystemExit("No Wasalt listing URLs found; no files were changed.")

    sample = random.Random(args.seed).sample(urls, min(args.sample_size, len(urls)))
    print(f"[wasalt] sampled {len(sample)} of {len(urls)} total listing URLs")

    from playwright.sync_api import sync_playwright

    gate = RobotsGate()
    records = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        for index, url in enumerate(sample, 1):
            slug_fields = parse_slug(url)
            print(f"[wasalt] ({index}/{len(sample)}) {url}")
            record = {
                "source": "Wasalt",
                "url": url,
                "title": (slug_fields.get("property_type") or "Property").title()
                + (f" ({slug_fields['bedrooms']} bed)" if slug_fields.get("bedrooms") else ""),
                "description": "",
                "text": "",
                "price": None,
                "area": None,
                "data_source": "sitemap URL slug; listing page not fetched",
                **slug_fields,
            }
            if gate.can_fetch(url):
                html = render_listing(page, url)
                if html:
                    record.update(parse_listing_page(html, url, slug_fields))
                else:
                    print("[wasalt]   page render blocked or failed; keeping sitemap metadata")
            else:
                print("[wasalt]   robots.txt disallows this page; keeping sitemap metadata")
            record["fetched_at"] = datetime.now(timezone.utc).isoformat()
            record["text"] = record["text"] or "Wasalt property listing. " + "; ".join(
                f"{key.replace('_', ' ')}: {value}" for key, value in slug_fields.items() if value
            )
            records.append(record)
            time.sleep(args.delay)
        browser.close()

    old_pages = read_jsonl(RAW_OUT)
    old_chunks = read_jsonl(CHUNKS_OUT)
    sample_urls = {row["url"] for row in records}
    # Replace overlapping Wasalt records only; preserve DarGlobal and other data.
    old_pages = [r for r in old_pages if not (r.get("source") == "Wasalt" and r.get("url") in sample_urls)]
    old_chunks = [r for r in old_chunks if not (r.get("source") == "Wasalt" and r.get("url") in sample_urls)]
    old_pages.extend(records)
    for record in records:
        old_chunks.append({
            "source": "Wasalt",
            "url": record["url"],
            "title": record["title"],
            "heading": "Property listing",
            "text": record["text"],
        })
    write_jsonl(RAW_OUT, old_pages)
    write_jsonl(CHUNKS_OUT, old_chunks)
    rendered = sum(row["data_source"] == "rendered listing page" for row in records)
    print(f"[wasalt] added {len(records)} records ({rendered} rendered, {len(records) - rendered} sitemap-only)")
    print(f"[wasalt] updated {RAW_OUT} and {CHUNKS_OUT}")


if __name__ == "__main__":
    main()
