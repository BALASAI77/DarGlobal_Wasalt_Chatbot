import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .rag import Retriever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chatbot")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Comma-separated, tried in order. Free model availability changes: check https://openrouter.ai/models?q=free
MODELS = [m.strip() for m in os.getenv(
    "OPENROUTER_MODELS",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
).split(",") if m.strip()]

SYSTEM = (
    "You are a helpful assistant for DarGlobal and Wasalt. Answer ONLY using the context "
    "provided below, which was scraped from their public websites. If the answer is not in the "
    "context, say you don't have that information and suggest visiting the company's website. "
    "Be concise and accurate. Mention which company (DarGlobal or Wasalt) your answer is about."
)

app = FastAPI(title="DarGlobal & Wasalt Chatbot")
retriever = Retriever()
STATIC = Path(__file__).resolve().parent / "static"


class Msg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[Msg] = Field(default_factory=list, max_length=20)


def detect_source(q: str):
    ql = q.lower()
    d, w = "dar" in ql, "wasalt" in ql
    if d and not w:
        return "DarGlobal"
    if w and not d:
        return "Wasalt"
    return None


@app.get("/health")
def health():
    return {"status": "ok", "chunks": len(retriever.docs), "models": MODELS, "key_set": bool(API_KEY)}


@app.post("/chat")
async def chat(req: ChatReq):
    if not API_KEY:
        raise HTTPException(500, "OPENROUTER_API_KEY is not set on the server")
    hits = retriever.search(req.message, k=6, source=detect_source(req.message))
    if not hits and req.history:
        hits = retriever.search(req.history[-1].content + " " + req.message, k=6)
    context = "\n\n".join(f"[{h['source']} | {h['url']}]\n{h['text']}" for h in hits) or "(no relevant context found)"
    messages = [{"role": "system", "content": f"{SYSTEM}\n\nCONTEXT:\n{context}"}]
    messages += [m.model_dump() for m in req.history[-6:]]
    messages.append({"role": "user", "content": req.message})

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    last_err = "no models configured"
    async with httpx.AsyncClient(timeout=60) as client:
        for model in MODELS:
            try:
                r = await client.post(OPENROUTER_URL, headers=headers,
                                      json={"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 600})
                if r.status_code == 200:
                    answer = (r.json()["choices"][0]["message"]["content"] or "").strip()
                    if not answer:
                        last_err = f"{model}: empty response"
                        continue
                    sources = sorted({h["url"] for h in hits})
                    log.info("answered with %s using %d chunks", model, len(hits))
                    return {"answer": answer, "sources": sources, "model": model}
                last_err = f"{model}: {r.status_code} {r.text[:200]}"
            except Exception as e:  # noqa: BLE001
                last_err = f"{model}: {e}"
            log.warning("model failed: %s", last_err)
    raise HTTPException(502, f"All models failed. Last error: {last_err}")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
