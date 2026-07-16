#!/usr/bin/env python3
"""
Cost/quality eval harness: runs a fixed prompt set against the local server
and records per prompt: token usage + estimated cost (from the agent's usage
telemetry), elapsed time, and quality signals (citations, sources, artifacts,
answer length, language). Compare runs with --compare.

Usage:
    python scripts/eval_cost.py --out baseline.json [--model claude-sonnet-5]
    python scripts/eval_cost.py --out after.json
    python scripts/eval_cost.py --compare baseline.json after.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

API = "http://localhost:8000"

# fixed subset: retrieval, entity, measurements, itinerary+artifacts, network,
# English/German, and an abstention probe
PROMPTS = [
    {"q": "Wer war Bonpland?", "expect": {}},
    {"q": "Was schreibt Humboldt über Erdbeben in Cumaná?", "expect": {}},
    {"q": "Welche Höhenmessungen in Toisen finden sich zum Chimborazo?", "expect": {}},
    {"q": "Wo war Humboldt im März 1801?", "expect": {}},
    {"q": "Erstelle eine Karte der Stationen auf Humboldts Reise nach Italien 1805 (Hinreise Paris bis Rom).", "expect": {"artifacts": ["map"]}},
    {"q": "Create a map and timeline of Humboldts journey to the Americas with the most important stations.", "expect": {"artifacts": ["map", "timeline"], "lang": "en"}},
    {"q": "Zeig mir ein Beziehungsnetz um den Chimborazo.", "expect": {"artifacts": ["network"]}},
    {"q": "Was merken die Herausgeber zum Vulkan Soufrière an?", "expect": {}},
    {"q": "Who was Bonpland and what did he do on the American expedition?", "expect": {"lang": "en"}},
    {"q": "Fasse den Brief H0000003 zusammen.", "expect": {}},
]

ANNOUNCE_TAIL = re.compile(
    r"(let me|i['’]ll|i will|ich (erstelle|zeichne)|je vais)[^.!?]*"
    r"(map|karte|timeline|zeitleiste|network|netzwerk)\W*$", re.I)


def detect_lang(text: str) -> str:
    de = len(re.findall(r"\b(und|der|die|das|wurde|über|nach|mit|ist)\b", text))
    en = len(re.findall(r"\b(the|and|of|was|with|from|his|is)\b", text))
    return "en" if en > de else "de"


def run_prompt(q: str, model: str, timeout: int = 420) -> dict:
    req = urllib.request.Request(
        API + "/chat/stream",
        data=json.dumps({"query": q, "model": model}).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = ""
        for raw in resp:
            line = raw.decode("utf-8", "replace")
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    answers = [e for e in events if e["type"] == "answer"]
    errors = [e for e in events if e["type"] == "error"]
    if not answers:
        return {"error": errors[0]["message"][:200] if errors else "no answer event"}
    d = answers[0]["data"]
    blocks = d.get("answer_blocks") or []
    return {
        "elapsed_s": round(time.time() - t0, 1),
        "usage": d.get("usage", {}),
        "n_tool_calls": sum(1 for e in events if e["type"] == "tool_call"),
        "n_citations": sum(len(b.get("citations", [])) for b in blocks),
        "n_sources": len(d.get("sources", [])),
        "artifacts": sorted({a.get("type") for a in d.get("artifacts", [])}),
        "answer_chars": len(d.get("answer", "")),
        "lang": detect_lang(d.get("answer", "")),
        "announce_tail": bool(ANNOUNCE_TAIL.search((d.get("answer", "") or "").strip()[-200:])),
        "repair_used": any(e.get("tool") == "repair_citations" for e in events),
    }


def run_prompt_retry(q: str, model: str, attempts: int = 4) -> dict:
    """Retry on transient API congestion (overloaded/rate limit)."""
    for i in range(attempts):
        r = run_prompt(q, model)
        msg = r.get("error", "")
        if "overloaded" not in msg.lower() and "rate limit" not in msg.lower():
            return r
        wait = 45 * (i + 1)
        print(f"    API overloaded — retrying in {wait}s", flush=True)
        time.sleep(wait)
    return r


def run_all(model: str, out: str) -> None:
    results = []
    for i, p in enumerate(PROMPTS, 1):
        print(f"[{i}/{len(PROMPTS)}] {p['q'][:60]}…", flush=True)
        r = run_prompt_retry(p["q"], model)
        r["q"] = p["q"]
        r["expect"] = p["expect"]
        results.append(r)
        cost = r.get("usage", {}).get("est_cost_usd", "?")
        print(f"    ${cost} | {r.get('elapsed_s','?')}s | cites={r.get('n_citations','?')} "
              f"| artifacts={r.get('artifacts',[])} | lang={r.get('lang','?')}"
              + (" | ERROR: " + r["error"] if "error" in r else ""))
    total = sum(r.get("usage", {}).get("est_cost_usd", 0) for r in results)
    summary = {"model": model, "total_cost_usd": round(total, 4), "results": results}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"\nTotal estimated cost: ${round(total,4)}  ->  {out}")


def gates(r: dict) -> list[str]:
    """Quality gate violations for one result."""
    v = []
    if "error" in r:
        return ["error: " + r["error"]]
    exp = r.get("expect", {})
    for a in exp.get("artifacts", []):
        if a not in r["artifacts"]:
            v.append(f"missing artifact {a}")
    if exp.get("lang") and r["lang"] != exp["lang"]:
        v.append(f"language {r['lang']} != {exp['lang']}")
    if r["n_citations"] == 0:
        v.append("no citations")
    if r["announce_tail"]:
        v.append("announce-then-stop tail")
    if r["answer_chars"] < 200:
        v.append("suspiciously short answer")
    return v


def compare(base_path: str, new_path: str) -> None:
    base = json.load(open(base_path))
    new = json.load(open(new_path))
    print(f"{'prompt':<52} {'$ base':>8} {'$ new':>8} {'Δ%':>6}  gates(new)")
    tb = tn = 0
    fail = False
    for rb, rn in zip(base["results"], new["results"]):
        cb = rb.get("usage", {}).get("est_cost_usd", 0)
        cn = rn.get("usage", {}).get("est_cost_usd", 0)
        tb += cb; tn += cn
        delta = f"{(cn-cb)/cb*100:+.0f}%" if cb else "—"
        g = gates(rn)
        if g:
            fail = True
        print(f"{rb['q'][:50]:<52} {cb:>8.4f} {cn:>8.4f} {delta:>6}  {'; '.join(g) or 'ok'}")
    print(f"\nTOTAL  base=${round(tb,4)}  new=${round(tn,4)}  "
          f"Δ={((tn-tb)/tb*100 if tb else 0):+.1f}%")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="run the eval and write results JSON")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "NEW"))
    args = ap.parse_args()
    if args.compare:
        compare(*args.compare)
    elif args.out:
        run_all(args.model, args.out)
    else:
        ap.print_help()
