"""Scrape a sample of Wasalt (wasalt.sa) property listings.

Strategy: wasalt.sa itself sits behind a Cloudflare managed challenge, so plain
HTTP requests to a property page get a "Just a moment..." JS-challenge (403).
Its CDN-hosted sitemaps at cdn.wasalt.sa are NOT protected and its robots.txt
explicitly welcomes AI bots, so we:
  1. Fetch the unprotected sale + rental sitemaps from cdn.wasalt.sa.
  2. Parse each listing's URL slug for free structured data (property type,
     bedroom count, sale/rent, listing id) that requires no page load at all.
  3. Take a reproducible random sample and try to render each with headless
     Playwright for richer fields (price, area, district, amenities).
  4. Any URL Cloudflare still blocks after rendering falls back to slug-only
     fields, tagged "source": "sitemap-only".

Output: ../data/wasalt.json
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from common import USER_AGENT, fetch_sitemap_xml, now_iso, polite_sleep, render_page_html

SALE_SITEMAP = "https://cdn.wasalt.sa/sitemap/product_sitemap_en_sa.xml.gz"
RENTAL_SITEMAP = "https://cdn.wasalt.sa/sitemap/rental_pdp_sitemap_en_sa.xml.gz"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "wasalt.json"

SAMPLE_SIZE = 200  # ~150-300 listings/site per the agreed scrape scope
RANDOM_SEED = 42

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Listing slugs vary a lot, e.g.:
#   apartment-with-3-bedrooms-5823529
#   apartment-73099-sqm-with-1-bedroom-5882504
#   villa-472-sqm-facing-north-on-20m-width-street-5852637
#   land-900-sqm-facing-south-on-52m-width-street-5817144
#   apartment-for-booking-2705                          (dailyrental)
# so pull each field independently rather than matching one rigid shape. Area is
# NOT parsed from the slug: the site strips the decimal point (730.99 -> "73099"),
# so a slug number can't be mapped back to a real sqm value -- area only ever
# comes from the rendered page text (see parse_listing_page's AREA_RE).
DEAL_PATH_RE = re.compile(r"/property/(?P<deal>sale|rent)/", re.IGNORECASE)
DAILY_PATH_RE = re.compile(r"/dailyrental/", re.IGNORECASE)
ID_RE = re.compile(r"-(?P<id>\d+)/?$")
BEDROOMS_RE = re.compile(r"(?P<bedrooms>\d+)-bedrooms?\b", re.IGNORECASE)
TYPE_RE = re.compile(r"^(?P<type>[a-z]+(?:-[a-z]+)*?)(?:-\d|-with-|-facing-|-for-booking|$)", re.IGNORECASE)


def get_listing_urls(sitemap_url: str) -> list[str]:
    xml_text = fetch_sitemap_xml(sitemap_url)
    root = ET.fromstring(xml_text)
    return [el.text.strip() for el in root.findall(".//sm:loc", SITEMAP_NS) if el.text]


def parse_slug(url: str) -> dict:
    path = urlparse(url).path
    slug = path.rstrip("/").rsplit("/", 1)[-1]

    deal_m = DEAL_PATH_RE.search(path)
    if deal_m:
        deal_type = "sale" if deal_m.group("deal").lower() == "sale" else "rent"
    elif DAILY_PATH_RE.search(path):
        deal_type = "daily-rental"
    else:
        deal_type = None

    id_m = ID_RE.search(slug)
    bedrooms_m = BEDROOMS_RE.search(slug)
    type_m = TYPE_RE.match(slug)

    return {
        "listing_id": id_m.group("id") if id_m else None,
        "property_type": type_m.group("type").replace("-", " ").strip() if type_m else None,
        "bedrooms": bedrooms_m.group("bedrooms") if bedrooms_m else None,
        "deal_type": deal_type,
    }


def parse_listing_page(html: str, url: str, slug_fields: dict) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = None
    h1 = soup.select_one("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        title_tag = soup.select_one("title")
        title = title_tag.get_text(strip=True) if title_tag else None

    body_text = soup.get_text(separator=" ", strip=True)
    body_text = re.sub(r"\s+", " ", body_text)[:4000]

    price_match = re.search(r"(SAR|ريال)\s?[\d,.]+", body_text)
    price = price_match.group(0) if price_match else None

    area_match = re.search(r"[\d,.]+\s?(?:sqm|sq\.?\s?m|m²)", body_text, flags=re.IGNORECASE)
    area = area_match.group(0) if area_match else None

    return {
        "site": "wasalt",
        "source_url": url,
        "title": title,
        "price": price,
        "area": area,
        "text": body_text,
        **slug_fields,
        "source": "rendered",
        "scraped_at": now_iso(),
    }


def main() -> None:
    random.seed(RANDOM_SEED)

    all_urls: list[str] = []
    for sitemap_url in (SALE_SITEMAP, RENTAL_SITEMAP):
        try:
            urls = get_listing_urls(sitemap_url)
            print(f"[wasalt] {sitemap_url}: {len(urls)} URLs", file=sys.stderr)
            all_urls.extend(urls)
        except Exception as e:
            print(f"[wasalt] failed to fetch {sitemap_url}: {e}", file=sys.stderr)

    if not all_urls:
        print("[wasalt] no URLs found, aborting", file=sys.stderr)
        return

    sample = random.sample(all_urls, min(SAMPLE_SIZE, len(all_urls)))
    print(f"[wasalt] sampled {len(sample)} of {len(all_urls)} total listing URLs", file=sys.stderr)

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT.split(" (")[0] + " Mozilla/5.0 (compatible)")
        page = context.new_page()
        for i, url in enumerate(sample, 1):
            slug_fields = parse_slug(url)
            print(f"[wasalt] ({i}/{len(sample)}) {url} -> {slug_fields}", file=sys.stderr)
            html = render_page_html(page, url, wait_selector="h1")
            if html is None:
                print(f"[wasalt]   blocked/failed, using slug-only fallback", file=sys.stderr)
                results.append({
                    "site": "wasalt",
                    "source_url": url,
                    "title": (slug_fields.get("property_type") or "Property").title()
                    + (f" ({slug_fields['bedrooms']} bed)" if slug_fields.get("bedrooms") else ""),
                    **slug_fields,
                    "source": "sitemap-only",
                    "scraped_at": now_iso(),
                })
            else:
                results.append(parse_listing_page(html, url, slug_fields))
            polite_sleep()
        browser.close()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    rendered = sum(1 for r in results if r.get("source") == "rendered")
    print(f"[wasalt] wrote {len(results)} records ({rendered} rendered, {len(results) - rendered} sitemap-only) -> {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
