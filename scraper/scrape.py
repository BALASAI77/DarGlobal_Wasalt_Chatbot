"""Production-style scraper for public pages of DarGlobal and Wasalt.

Pipeline:  discover URLs (seeds + sitemap + link crawl) -> fetch (robots-aware, retries,
optional headless-browser fallback) -> extract main content by heading -> chunk -> dedupe.

Outputs
  data/raw/pages.jsonl   one record per fetched page (url, title, description, text, fetched_at)
  data/chunks.jsonl      retrieval chunks used by the chatbot

Usage
  python -m scraper.scrape                   # default run
  python -m scraper.scrape --max-pages 120   # crawl more
  python -m scraper.scrape --browser         # use Playwright for JS-rendered pages (optional)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
RAW_OUT = ROOT / "data" / "raw" / "pages.jsonl"
CHUNKS_OUT = ROOT / "data" / "chunks.jsonl"
UA = "Mozilla/5.0 (compatible; AssignmentResearchBot/1.0; +educational-use)"
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".mp4", ".css", ".js", ".xml", ".ico")

log = logging.getLogger("scraper")


@dataclass
class Site:
    name: str
    seeds: list[str]
    allowed_hosts: set[str]
    include: list[str] = field(default_factory=list)   # regexes a URL must match (if any)
    exclude: list[str] = field(default_factory=list)   # regexes to skip (listing pagination etc.)


SITES = [
    Site(
        name="DarGlobal",
        seeds=[
            "https://darglobal.co.uk/",
            "https://darglobal.co.uk/about",
            "https://darglobal.co.uk/faq",
            "https://darglobal.co.uk/investor",
            "https://darglobal.co.uk/become-an-agent",
        ],
        allowed_hosts={"darglobal.co.uk"},
        exclude=[r"/(ru|ar|es|fr|de|zh)(/|$)", r"\?"],
    ),
    Site(
        name="Wasalt",
        seeds=[
            "https://wasalt.sa/en",
            "https://wasalt.sa/en/corporate",
            "https://wasalt.com/en/about",
            "https://wasalt.sa/en/s/wasalt-business",
        ],
        allowed_hosts={"wasalt.sa", "wasalt.com", "blog.wasalt.sa"},
        # Listing pages are thousands of near-duplicate property cards; keep company/product/blog content.
        exclude=[r"[?&]page=", r"-for-(sale|rent)-in-", r"/ar(/|$)", r"/property/"],
    ),
]


# ----------------------------------------------------------------------------- HTTP
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def norm_host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def norm_url(url: str) -> str:
    return urldefrag(url)[0].rstrip("/")


# ----------------------------------------------------------------------------- discovery
def sitemap_urls(session: requests.Session, base: str, limit: int = 400) -> list[str]:
    """Collect <loc> entries from robots.txt-declared sitemaps or /sitemap.xml (follows sitemap indexes)."""
    roots = []
    try:
        r = session.get(urljoin(base, "/robots.txt"), timeout=15)
        roots = re.findall(r"(?im)^sitemap:\s*(\S+)", r.text) if r.ok else []
    except requests.RequestException:
        pass
    roots = roots or [urljoin(base, "/sitemap.xml")]
    out, queue, seen = [], deque(roots), set()
    while queue and len(out) < limit:
        sm = queue.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = session.get(sm, timeout=20)
            if not r.ok:
                continue
            tree = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError):
            continue
        for loc in tree.iter():
            if loc.tag.endswith("loc") and loc.text:
                u = loc.text.strip()
                (queue if u.endswith(".xml") else out).append(u)
    log.info("sitemap %s -> %d urls", base, len(out))
    return out


def url_ok(site: Site, url: str) -> bool:
    if url.lower().endswith(SKIP_EXT) or norm_host(url) not in site.allowed_hosts:
        return False
    if any(re.search(p, url) for p in site.exclude):
        return False
    return not site.include or any(re.search(p, url) for p in site.include)


# ----------------------------------------------------------------------------- extraction
NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header", "aside", "button"]


def extract(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""

    # JSON-LD often carries clean organisation / FAQ data
    jsonld = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            jsonld.append(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            pass

    links = [a["href"] for a in soup.find_all("a", href=True)]
    for t in soup(NOISE_TAGS):
        t.decompose()
    root = soup.find("main") or soup.body or soup

    # Walk the tree and group text under the nearest preceding heading
    sections, heading, buf = [], title or "Overview", []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "dd", "dt", "blockquote"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(txt) < 3:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            if buf:
                sections.append((heading, " ".join(buf)))
                buf = []
            heading = txt
        else:
            buf.append(txt)
    if buf:
        sections.append((heading, " ".join(buf)))
    return {"title": title, "description": description, "sections": sections, "links": links, "jsonld": jsonld}


def chunk_section(heading: str, text: str, size: int = 170, overlap: int = 30):
    words = text.split()
    step = max(size - overlap, 1)
    for i in range(0, len(words), step):
        piece = words[i:i + size]
        if len(piece) >= 20 or (i == 0 and len(piece) >= 8):
            yield f"{heading}: " + " ".join(piece)
        if i + size >= len(words):
            break


def fetch_html(session, url, use_browser, browser_ctx):
    r = session.get(url, timeout=25)
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None
    html = r.text
    # Thin server-rendered shell => likely a JS app; retry with headless browser if allowed
    if use_browser and browser_ctx is not None and len(BeautifulSoup(html, "html.parser").get_text(" ").split()) < 120:
        try:
            page = browser_ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            html = page.content()
            page.close()
        except Exception as e:  # noqa: BLE001
            log.warning("browser fallback failed for %s: %s", url, e)
    return html


# ----------------------------------------------------------------------------- crawl
def crawl(site: Site, session, max_pages: int, delay: float, use_browser: bool, browser_ctx):
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(urljoin(site.seeds[0], "/robots.txt"))
    try:
        rp.read()
    except Exception as e:  # noqa: BLE001
        # robots.txt unreachable: urllib would then refuse everything, so make the decision explicit
        log.warning("could not read robots.txt for %s (%s); proceeding with rate limiting only", site.name, e)
        rp.allow_all = True

    queue = deque(site.seeds)
    for u in sitemap_urls(session, site.seeds[0]):
        if url_ok(site, u):
            queue.append(u)
    seen, pages, chunks, hashes = set(), [], [], set()

    while queue and len(pages) < max_pages:
        url = norm_url(queue.popleft())
        if url in seen:
            continue
        seen.add(url)
        if not url_ok(site, url) and url not in {norm_url(s) for s in site.seeds}:
            continue
        if not rp.can_fetch(UA, url):
            log.info("robots.txt disallows %s", url)
            continue
        try:
            html = fetch_html(session, url, use_browser, browser_ctx)
        except requests.RequestException as e:
            log.warning("fetch failed %s: %s", url, e)
            continue
        if not html:
            continue
        doc = extract(html)
        for href in doc["links"]:
            nxt = norm_url(urljoin(url, href))
            if nxt not in seen and url_ok(site, nxt):
                queue.append(nxt)

        n_new = 0
        for heading, text in doc["sections"]:
            for c in chunk_section(heading, text):
                h = hashlib.md5(c.lower().encode()).hexdigest()
                if h in hashes:
                    continue
                hashes.add(h)
                chunks.append({"source": site.name, "url": url, "title": doc["title"], "heading": heading, "text": c})
                n_new += 1
        if doc["description"]:
            chunks.append({"source": site.name, "url": url, "title": doc["title"], "heading": "Summary", "text": doc["description"]})
        pages.append({
            "source": site.name, "url": url, "title": doc["title"], "description": doc["description"],
            "text": "\n".join(f"{h}\n{t}" for h, t in doc["sections"]),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
        log.info("[%s] %s -> %d chunks", site.name, url, n_new)
        time.sleep(delay)
    return pages, chunks


def write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-pages", type=int, default=100, help="max pages per site")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--browser", action="store_true", help="use Playwright fallback for JS-rendered pages")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    session = make_session()
    pw = browser = ctx = None
    if args.browser:
        from playwright.sync_api import sync_playwright  # pip install playwright && playwright install chromium
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        ctx = browser.new_context(user_agent=UA)

    all_pages, all_chunks = [], []
    try:
        for site in SITES:
            p, c = crawl(site, session, args.max_pages, args.delay, args.browser, ctx)
            all_pages += p
            all_chunks += c
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()

    if not all_chunks:
        log.error("Nothing scraped (blocked/offline). Existing data files left untouched.")
        return
    write_jsonl(RAW_OUT, all_pages)
    write_jsonl(CHUNKS_OUT, all_chunks)
    per = {s.name: sum(1 for c in all_chunks if c["source"] == s.name) for s in SITES}
    log.info("Done: %d pages, %d chunks %s", len(all_pages), len(all_chunks), per)


if __name__ == "__main__":
    main()
