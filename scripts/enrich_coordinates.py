"""
Resolve GeoNames IDs of the place register to coordinates via Wikidata.

The v11 corpus dump carries no <geo> elements, but every place entry has a
GeoNames ID. Wikidata maps GeoNames (P1566) -> coordinates (P625) in bulk.

Writes data/processed/coordinates.json: {geonames_id: [lat, lon]}.
Idempotent: already-resolved IDs are kept and not re-queried.

Usage: python scripts/enrich_coordinates.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNK_STORE = ROOT / "data" / "processed" / "chunk_store.json"
OUT_PATH = ROOT / "data" / "processed" / "coordinates.json"

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "ehd-assistant-build/1.0 (coordinate enrichment for edition humboldt digital assistant)"
BATCH_SIZE = 300
POINT_RE = re.compile(r"Point\(([-\d.]+) ([-\d.]+)\)")


def collect_geonames_ids() -> set[str]:
    data = json.loads(CHUNK_STORE.read_text())
    ids = set()
    for chunk in data.values():
        if chunk.get("doc_type") != "register_place":
            continue
        for place in chunk.get("places", []):
            gid = str(place.get("geonames", "")).strip().strip("/")
            # some refs carry a trailing path, keep digits only
            gid = gid.split("/")[0]
            if gid.isdigit():
                ids.add(gid)
    return ids


def query_batch(ids: list[str], retries: int = 4) -> dict[str, list[float]]:
    values = " ".join(f'"{i}"' for i in ids)
    query = (
        "SELECT ?gn ?coord WHERE { "
        f"VALUES ?gn {{ {values} }} "
        "?item wdt:P1566 ?gn; wdt:P625 ?coord }"
    )
    url = SPARQL_URL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            out = {}
            for b in data["results"]["bindings"]:
                m = POINT_RE.match(b["coord"]["value"])
                if m:
                    lon, lat = float(m.group(1)), float(m.group(2))
                    out[b["gn"]["value"]] = [lat, lon]
            return out
        except Exception as e:
            wait = 5 * (attempt + 1)
            print(f"  batch failed ({e}), retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print("  batch permanently failed, skipping", file=sys.stderr)
    return {}


def main():
    ids = collect_geonames_ids()
    print(f"GeoNames IDs in place register: {len(ids)}")

    coords: dict[str, list[float]] = {}
    if OUT_PATH.exists():
        coords = json.loads(OUT_PATH.read_text())
        print(f"Already resolved: {len(coords)}")

    todo = sorted(i for i in ids if i not in coords)
    print(f"To resolve: {len(todo)}")

    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        result = query_batch(batch)
        coords.update(result)
        print(f"  batch {i // BATCH_SIZE + 1}/{-(-len(todo) // BATCH_SIZE)}: "
              f"+{len(result)} (total {len(coords)})")
        time.sleep(1)  # be polite to the endpoint

    OUT_PATH.write_text(json.dumps(coords, ensure_ascii=False))
    pct = 100 * len(coords) // max(len(ids), 1)
    print(f"Done: {len(coords)}/{len(ids)} ({pct}%) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
