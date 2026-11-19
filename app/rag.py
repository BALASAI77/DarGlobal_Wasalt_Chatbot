"""Lightweight retrieval over scraped chunks (BM25, no heavy ML deps)."""
import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

DATA = Path(__file__).resolve().parent.parent / "data" / "chunks.jsonl"
TOKEN = re.compile(r"[a-z0-9؀-ۿ]+")


def tokenize(s: str):
    return TOKEN.findall(s.lower())


class Retriever:
    def __init__(self, path: Path = DATA):
        self.docs = []
        if path.exists():
            with path.open(encoding="utf-8") as f:
                self.docs = [json.loads(l) for l in f if l.strip()]
        self.bm25 = BM25Okapi(
            [tokenize(f'{d.get("title", "")} {d.get("heading", "")} {d["text"]}') for d in self.docs]
        ) if self.docs else None

    def search(self, query: str, k: int = 6, source: str | None = None):
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(self.docs)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked:
            if scores[i] <= 0:
                break
            if source and self.docs[i]["source"].lower() != source.lower():
                continue
            out.append(self.docs[i])
            if len(out) >= k:
                break
        return out
