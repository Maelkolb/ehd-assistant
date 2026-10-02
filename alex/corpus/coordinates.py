"""Coordinates for the edition's places.

The dump carries GeoNames ids but no coordinates. They are resolved once and
kept in a small JSON file next to the corpus, so building the store needs no
network: in bulk through Wikidata (GeoNames id P1566 -> coordinate P625), and
for the ids Wikidata does not know, one by one from GeoNames' own RDF records.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from alex.corpus.tei import PARSER, TEI, authority_ids

logger = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
GEONAMES_RECORD = "https://sws.geonames.org/{}/about.rdf"
USER_AGENT = "ehd-assistant/2.0 (edition humboldt digital research assistant; build step)"
BATCH_SIZE = 250
POINT = re.compile(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)")
GEONAMES_REFERENCE = re.compile(r"geonames\.org/(\d+)")
RDF_LATITUDE = re.compile(r"<wgs84_pos:lat>([-\d.]+)</wgs84_pos:lat>")
RDF_LONGITUDE = re.compile(r"<wgs84_pos:long>([-\d.]+)</wgs84_pos:long>")


def geonames_ids_in_dump(corpus_root: Path) -> set[str]:
    """GeoNames ids of the place register and of every place reference in the texts."""
    from lxml import etree

    ids: set[str] = set()
    for path in (corpus_root / "data" / "index" / "place").glob("*.xml"):
        root = etree.parse(str(path), PARSER).getroot()
        for idno in root.iter(TEI + "idno"):
            geonames = authority_ids(idno.text or "").get("geonames")
            if geonames:
                ids.add(geonames)
    for path in (corpus_root / "data").glob("*/*.xml"):
        ids.update(GEONAMES_REFERENCE.findall(path.read_text(encoding="utf-8")))
    return {geonames for geonames in ids if geonames.isdigit()}


def fetch(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def from_wikidata(geonames_ids: list[str], attempts: int = 4) -> dict[str, list[float]]:
    values = " ".join(f'"{geonames}"' for geonames in geonames_ids)
    query = (
        f"SELECT ?id ?point WHERE {{ VALUES ?id {{ {values} }} "
        "?item wdt:P1566 ?id; wdt:P625 ?point }"
    )
    url = SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    for attempt in range(attempts):
        try:
            bindings = json.loads(fetch(url))["results"]["bindings"]
        except Exception as error:
            wait = 5 * (attempt + 1)
            logger.warning("Wikidata batch failed (%s); retrying in %d s", error, wait)
            time.sleep(wait)
            continue
        resolved = {}
        for binding in bindings:
            match = POINT.match(binding["point"]["value"])
            if match:
                resolved[binding["id"]["value"]] = [float(match.group(2)), float(match.group(1))]
        return resolved
    return {}


def from_geonames(geonames_id: str) -> list[float] | None:
    try:
        record = fetch(GEONAMES_RECORD.format(geonames_id), timeout=30).decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as error:
        logger.warning("GeoNames record %s unavailable (%s)", geonames_id, error)
        return None
    latitude, longitude = RDF_LATITUDE.search(record), RDF_LONGITUDE.search(record)
    if latitude and longitude:
        return [float(latitude.group(1)), float(longitude.group(1))]
    return None


def update_coordinates(corpus_root: Path, coordinates_path: Path) -> dict[str, int]:
    known: dict[str, list[float]] = {}
    if coordinates_path.exists():
        known = json.loads(coordinates_path.read_text(encoding="utf-8"))
    wanted = geonames_ids_in_dump(corpus_root)

    def save() -> None:
        coordinates_path.parent.mkdir(parents=True, exist_ok=True)
        coordinates_path.write_text(json.dumps(known, sort_keys=True), encoding="utf-8")

    missing = sorted(wanted - known.keys())
    for position in range(0, len(missing), BATCH_SIZE):
        known.update(from_wikidata(missing[position : position + BATCH_SIZE]))
        time.sleep(1)
    save()

    remaining = sorted(wanted - known.keys())
    for position, geonames_id in enumerate(remaining, start=1):
        coordinates = from_geonames(geonames_id)
        if coordinates:
            known[geonames_id] = coordinates
        if position % 100 == 0:
            save()
            logger.info("GeoNames: %d of %d looked up", position, len(remaining))
        time.sleep(0.4)
    save()

    resolved = len(wanted & known.keys())
    logger.info("Coordinates: %d of %d GeoNames ids resolved", resolved, len(wanted))
    return {"wanted": len(wanted), "resolved": resolved, "looked_up": len(missing)}
