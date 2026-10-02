"""
Wikipedia/Wikimedia client for the experimental contextualizer tool.

Resolution is ID-first: register entities carry GND/GeoNames IDs, which map
via Wikidata (P227/P1566) to the exact article and its P18 image — no homonym
guessing. Terms without an authority ID fall back to title search.

Runs inside the synchronous SSE agent loop, so every call is bounded by a
short timeout, never retried, and cached in-process; any failure degrades to
None and the agent answers from the edition alone.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

USER_AGENT = "ehd-assistant/2.0 (edition humboldt digital research assistant; wikipedia context)"
TIMEOUT_S = 3.5
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 3600
_CACHE_MAX = 256

_PD_RE = re.compile(r"public domain|^PD\b|CC0|No restrictions", re.I)
_TAG_RE = re.compile(r"<[^>]+>")

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def _get_json(url: str):
    now = time.time()
    hit = _CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"wikipedia fetch failed: {url[:120]} ({e})")
        return None
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        del _CACHE[oldest]
    _CACHE[url] = (now + _CACHE_TTL, data)
    return data


def _strip_html(s: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", s or "")).split("\n")[0].strip()[:200]


def resolve_authority(gnd_id: str | None = None, geonames_id: str | None = None) -> dict | None:
    """Authority ID -> {qid, dewiki, enwiki, p18} via Wikidata."""
    statements = []
    if gnd_id:
        statements.append(f"P227={gnd_id}")
    if geonames_id:
        statements.append(f"P1566={geonames_id}")
    qid = None
    for st in statements:
        url = (
            WIKIDATA_API
            + "?"
            + urllib.parse.urlencode(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": f'haswbstatement:"{st}"',
                    "srnamespace": "0",
                    "srlimit": "1",
                    "format": "json",
                }
            )
        )
        data = _get_json(url)
        hits = (data or {}).get("query", {}).get("search", [])
        if hits:
            qid = hits[0]["title"]
            break
    if not qid:
        return None
    url = (
        WIKIDATA_API
        + "?"
        + urllib.parse.urlencode(
            {"action": "wbgetentities", "ids": qid, "props": "sitelinks|claims", "format": "json"}
        )
    )
    data = _get_json(url)
    ent = (data or {}).get("entities", {}).get(qid)
    if not ent:
        return None
    sitelinks = ent.get("sitelinks", {})
    p18 = None
    try:
        p18 = ent["claims"]["P18"][0]["mainsnak"]["datavalue"]["value"]
    except (KeyError, IndexError, TypeError):
        pass
    return {
        "qid": qid,
        "dewiki": sitelinks.get("dewiki", {}).get("title"),
        "enwiki": sitelinks.get("enwiki", {}).get("title"),
        "p18": p18,
    }


def fetch_summary(lang: str, title: str) -> dict | None:
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(
        title.replace(" ", "_"), safe=""
    )
    data = _get_json(url)
    if not data or not data.get("extract"):
        return None
    page_url = data.get("content_urls", {}).get("desktop", {}).get(
        "page"
    ) or f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    return {
        "lang": lang,
        "title": data.get("title") or title,
        "extract": data["extract"],
        "description": data.get("description", ""),
        "url": page_url,
        "image_source": (data.get("originalimage") or {}).get("source"),
    }


def search_title(lang: str, term: str) -> str | None:
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "opensearch", "search": term, "limit": "1", "namespace": "0", "format": "json"}
    )
    data = _get_json(url)
    try:
        return data[1][0]
    except (TypeError, IndexError, KeyError):
        return None


def _filename_from_image_url(image_url: str) -> str | None:
    """Commons File: name from an upload.wikimedia.org URL (thumbs included)."""
    if not image_url or "wikimedia.org" not in image_url:
        return None
    parts = image_url.split("/")
    if "thumb" in parts:
        # .../thumb/a/ab/Name.jpg/640px-Name.jpg -> Name.jpg
        name = (
            parts[parts.index("thumb") + 3] if len(parts) > parts.index("thumb") + 3 else parts[-1]
        )
    else:
        name = parts[-1]
    return urllib.parse.unquote(name) or None


def fetch_image_attribution(filename: str) -> dict | None:
    """Commons imageinfo -> display URL + license/artist attribution."""
    url = (
        COMMONS_API
        + "?"
        + urllib.parse.urlencode(
            {
                "action": "query",
                "titles": f"File:{filename}",
                "prop": "imageinfo",
                "iiprop": "extmetadata|url",
                "iiurlwidth": "640",
                "format": "json",
            }
        )
    )
    data = _get_json(url)
    pages = (data or {}).get("query", {}).get("pages", {})
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        ii = infos[0]
        em = ii.get("extmetadata") or {}
        license_name = (em.get("LicenseShortName") or {}).get("value", "")
        return {
            "url": ii.get("thumburl") or ii.get("url"),
            "file_page": ii.get("descriptionurl", ""),
            "artist": _strip_html((em.get("Artist") or {}).get("value", "")),
            "license": _strip_html(license_name),
            "license_url": (em.get("LicenseUrl") or {}).get("value", ""),
            "public_domain": bool(_PD_RE.search(license_name)),
        }
    return None


def get_context(
    term: str, gnd_id: str | None, geonames_id: str | None, lang: str, include_image: bool
) -> dict | None:
    """Full resolution: summary (+ optional attributed image) or None."""
    lang = lang if lang in ("de", "en") else "de"
    other = "en" if lang == "de" else "de"
    info = None
    p18 = None

    if gnd_id or geonames_id:
        auth = resolve_authority(gnd_id=gnd_id, geonames_id=geonames_id)
        if auth:
            p18 = auth.get("p18")
            for lg in (lang, other):
                title = auth.get(f"{lg}wiki")
                if title:
                    info = fetch_summary(lg, title)
                    if info:
                        break

    if info is None and term:
        info = fetch_summary(lang, term)
        if info is None:
            title = search_title(lang, term)
            if title:
                info = fetch_summary(lang, title)
        if info is None:
            info = fetch_summary(other, term)

    if info is None:
        return None

    if include_image:
        filename = p18 or _filename_from_image_url(info.get("image_source") or "")
        if filename:
            img = fetch_image_attribution(filename)
            if img and img.get("url"):
                info["image"] = img
    info.pop("image_source", None)
    return info
