"""Smoke-evaluate the running chatbot against known facts.

Usage:  python eval/run_eval.py [BASE_URL]      (default http://localhost:8000)
Each case passes if the answer contains at least one expected keyword.
"""
import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
CASES = [
    ("Who is the founder and CEO of DarGlobal?", ["ziad", "chaar"]),
    ("Which stock exchange is DarGlobal listed on?", ["london"]),
    ("Name some luxury brands DarGlobal partners with.", ["aston", "lamborghini", "fendi", "missoni"]),
    ("What is Wasalt Auction?", ["auction", "infath"]),
    ("Which companies founded Wasalt as a joint venture?", ["dar al arkan", "quara"]),
    ("What does Wasalt Business do?", ["crm", "broker"]),
    ("What is the capital of France?", ["don't have", "not in", "no information", "cannot", "can't"]),  # should refuse
]

passed = 0
for q, kws in CASES:
    t = time.time()
    r = requests.post(f"{BASE}/chat", json={"message": q}, timeout=90)
    ans = r.json().get("answer", "") if r.ok else f"HTTP {r.status_code}"
    ok = any(k in ans.lower() for k in kws)
    passed += ok
    print(f"[{'PASS' if ok else 'FAIL'}] {q}  ({time.time() - t:.1f}s)\n      {ans[:160]!r}")
print(f"\n{passed}/{len(CASES)} passed")
sys.exit(0 if passed == len(CASES) else 1)
