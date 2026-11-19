"""Unit tests: run with `pytest -q`."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag import Retriever, tokenize
from scraper.scrape import Site, chunk_section, extract, url_ok

HTML = """<html><head><title>Test Co</title><meta name="description" content="A test company."></head>
<body><nav>menu menu</nav><main><h1>About</h1><p>We build luxury homes across many cities worldwide.</p>
<h2>Services</h2><p>Auctions and brokerage for buyers and sellers of property in the kingdom.</p></main>
<footer>footer junk</footer></body></html>"""


def test_extract_groups_by_heading_and_drops_noise():
    d = extract(HTML)
    assert d["title"] == "Test Co" and d["description"] == "A test company."
    heads = [h for h, _ in d["sections"]]
    assert heads == ["About", "Services"]
    assert "menu" not in " ".join(t for _, t in d["sections"])


def test_chunking_overlaps_and_keeps_heading():
    text = " ".join(f"w{i}" for i in range(400))
    chunks = list(chunk_section("Heading", text, size=100, overlap=20))
    assert len(chunks) >= 4 and all(c.startswith("Heading: ") for c in chunks)


def test_url_filter():
    s = Site("X", ["https://x.com/"], {"x.com"}, exclude=[r"[?&]page="])
    assert url_ok(s, "https://x.com/about")
    assert not url_ok(s, "https://x.com/list?page=2")
    assert not url_ok(s, "https://other.com/about")
    assert not url_ok(s, "https://x.com/logo.png")


def test_retriever_ranks_relevant_chunk_first_and_filters_source(tmp_path):
    rows = [
        {"source": "DarGlobal", "url": "u1", "title": "t", "heading": "CEO", "text": "Ziad El Chaar is the founder and chief executive"},
        {"source": "Wasalt", "url": "u2", "title": "t", "heading": "Auction", "text": "Wasalt Auction is licensed by Infath for online real estate auctions"},
    ] + [
        {"source": "DarGlobal", "url": f"f{i}", "title": "t", "heading": "Misc", "text": f"unrelated filler content number {i} about gardens and weather"}
        for i in range(6)
    ]  # BM25 gives ~zero IDF on tiny corpora, so use a realistic-size one
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    r = Retriever(p)
    assert r.search("who is the founder")[0]["url"] == "u1"
    assert r.search("auctions", source="Wasalt")[0]["url"] == "u2"
    assert r.search("auctions", source="DarGlobal") == []


def test_tokenize_handles_arabic():
    assert tokenize("وصلت Wasalt") == ["وصلت", "wasalt"]
