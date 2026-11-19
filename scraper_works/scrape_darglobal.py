"""Scrape a sample of DarGlobal (darglobal.co.uk) project pages.

Strategy: darglobal.co.uk sits behind Imperva/Incapsula bot management, so plain
HTTP requests get a JS-challenge stub instead of real content. Its sitemap.xml is
NOT protected, so we:
  1. Fetch the unprotected sitemap.xml. Individual project microsites live at
     root-level slugs (e.g. /marea, /neptune, /one-of-one/trump-penthouse) rather
     than under /projects/ -- that path only holds thin per-region/type category
     shells, not per-project content -- so we take every URL EXCEPT known non-project
     sections (blog, press, insights, legal/careers/marketing pages, etc).
  2. Render each one with headless Playwright (a real browser executing JS, which
     is what Incapsula's challenge requires) and parse the rendered HTML.
  3. Any page still blocked after rendering is logged and skipped rather than
     retried aggressively.

Output: ../data/darglobal.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from common import DEFAULT_HEADERS, RobotsGate, USER_AGENT, fetch_sitemap_xml, now_iso, polite_sleep, render_page_html

BASE_URL = "https://darglobal.co.uk"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "darglobal.json"
MAX_PROJECTS = 60  # cap: this is a sample crawl, not a full-site scrape

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Top-level sections that are never individual project pages.
EXCLUDE_TOP_SEGMENTS = {
    "blog", "press", "insights", "partners", "projects", "landing-page", "landingpage",
    "pay-online", "about", "campaigns", "careers", "internship-application", "internships",
    "become-a-broker", "become-an-agent", "bookyourunit", "aida-360", "aston-martin-media",
}
# Single-segment slugs that are legal/marketing/utility pages, not projects.
EXCLUDE_SLUGS = {
    "faq", "get-in-touch", "success", "thank-you", "win-a-trip-to-dubai",
    "broker-contest-terms-conditions", "tokenization", "investor", "why-invest",
    "exclusive-membership", "dg-circle", "dg1", "commercial", "development-management",
    "hospitality", "new-home-page", "privacy-policy", "terms-conditions", "terms-of-uses",
    "darglobal-insignia-card", "darglobal-exclusive-off-plan-investment-catalogue",
    "invest-crypto", "insignia", "live-all-in", "luxury-golf-communities",
}


def get_project_urls() -> list[str]:
    xml_text = fetch_sitemap_xml(SITEMAP_URL)
    root = ET.fromstring(xml_text)
    urls = [el.text.strip() for el in root.findall(".//sm:loc", SITEMAP_NS) if el.text]

    # The homepage isn't itself in the sitemap's /projects/ or root-slug shape, but it
    # carries company-level facts (track record, GDV, market count) useful for general
    # questions, so it's included as its own doc, tagged distinctly below.
    project_urls = [f"{BASE_URL}/"]
    seen = {f"{BASE_URL}/".rstrip("/")}
    for u in urls:
        path = u.replace(BASE_URL, "").strip("/")
        if not path:
            continue
        segments = path.split("/")
        if segments[0].lower() in EXCLUDE_TOP_SEGMENTS:
            continue
        if len(segments) == 1 and segments[0].lower() in EXCLUDE_SLUGS:
            continue
        key = u.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        project_urls.append(u)
    return project_urls[:MAX_PROJECTS]


def _looks_like_error_page(soup: BeautifulSoup) -> bool:
    """A handful of sitemap URLs (e.g. rayana/trump-international-simaisma)
    render a normal 200-status page whose content is just a client-side "404:
    This page could not be found" -- a real render, so it doesn't fall into
    the blocked/failed path, but it's not a project either and shouldn't be
    indexed as one."""
    title = soup.title.get_text(strip=True) if soup.title else ""
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True) if h1 else ""
    combined = f"{title} {h1_text}".lower()
    return "404" in combined or "page could not be found" in combined or "page not found" in combined


def parse_project_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    is_homepage = url.rstrip("/") == BASE_URL

    if not is_homepage and _looks_like_error_page(soup):
        return None

    def text_or_none(selector):
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else None

    title = text_or_none("h1") or text_or_none("title")
    if title:
        title = re.sub(r"\s*\|\s*DarGlobal.*$", "", title).strip()
    if is_homepage:
        title = "DarGlobal — Company Overview"

    meta_desc = soup.find("meta", attrs={"name": "description"})
    description = meta_desc["content"].strip() if meta_desc and meta_desc.get("content") else None

    # Body text as a fallback content blob (trimmed) so the RAG index has something
    # to search over even when specific fields can't be located structurally.
    body_text = soup.get_text(separator=" ", strip=True)
    body_text = re.sub(r"\s+", " ", body_text)[:4000]

    og_image = soup.find("meta", property="og:image")
    image_url = og_image["content"] if og_image and og_image.get("content") else None

    price_match = re.search(r"(AED|USD|SAR|GBP|\$|£)\s?[\d,.]+(?:\s?(?:Million|million|M|K))?", body_text)
    price = price_match.group(0) if price_match else None

    bedroom_match = re.findall(r"\b(\d+(?:\s?-\s?\d+)?)\s*(?:Bed|Bedroom)s?\b", body_text, flags=re.IGNORECASE)
    bedrooms = sorted(set(bedroom_match)) if bedroom_match else []

    return {
        "site": "darglobal",
        "page_type": "homepage" if is_homepage else "project",
        "source_url": url,
        "title": title,
        "description": description,
        "starting_price": None if is_homepage else price,
        "bedroom_types": [] if is_homepage else bedrooms,
        "image_url": image_url,
        "text": body_text,
        "source": "rendered",
        "scraped_at": now_iso(),
    }


def main() -> None:
    gate = RobotsGate.load(BASE_URL)
    urls = get_project_urls()
    urls = [u for u in urls if gate.can_fetch(u)]
    print(f"[darglobal] {len(urls)} project URLs to scrape (capped at {MAX_PROJECTS})", file=sys.stderr)

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT.split(" (")[0] + " Mozilla/5.0 (compatible)")
        page = context.new_page()
        for i, url in enumerate(urls, 1):
            print(f"[darglobal] ({i}/{len(urls)}) {url}", file=sys.stderr)
            html = render_page_html(page, url, wait_selector="h1")
            if html is None:
                # Incapsula's challenge is session/probabilistic, not a hard block --
                # one retry after a beat clears most transient failures.
                print(f"[darglobal]   blocked/failed, retrying once", file=sys.stderr)
                polite_sleep()
                html = render_page_html(page, url, wait_selector="h1")
            if html is None:
                print(f"[darglobal]   still blocked, skipping", file=sys.stderr)
                results.append({
                    "site": "darglobal",
                    "source_url": url,
                    "title": url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title(),
                    "source": "sitemap-only",
                    "scraped_at": now_iso(),
                })
            else:
                parsed = parse_project_page(html, url)
                if parsed is None:
                    print(f"[darglobal]   404/error page, skipping", file=sys.stderr)
                else:
                    results.append(parsed)
            polite_sleep()
        browser.close()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    rendered = sum(1 for r in results if r.get("source") == "rendered")
    print(f"[darglobal] wrote {len(results)} records ({rendered} rendered, {len(results) - rendered} sitemap-only) -> {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
