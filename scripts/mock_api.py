"""Mock of the HTTP API in docs/API.md, for working on the web client.

Serves alex/web at / and replays scripted chat streams chosen by keywords in
the question, so every part of the interface can be exercised without a model:

    karte / map / italien      map + timeline
    diagramm / chart / python  run_python with chart, table and image
    netzwerk / network         entity network
    lokal / marker             answer with inline [S#] markers, no citation events
    fehler / error             stream that ends in an error event
    limit                      HTTP 429 before the stream starts
    anything else              plain cited answer

Passages, notes and register entries come from the real document store when
data/store/ehd.sqlite exists, otherwise from a few canned records.

    python scripts/mock_api.py            # http://127.0.0.1:8791
    MOCK_SPEED=0 python scripts/mock_api.py   # no artificial delays
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WEB = ROOT / "alex" / "web"
STORE_PATH = ROOT / "data" / "store" / "ehd.sqlite"
SPEED = float(os.environ.get("MOCK_SPEED", "1"))

store = None
try:
    from alex.corpus.store import DocumentStore

    if STORE_PATH.exists():
        store = DocumentStore(STORE_PATH)
except Exception as error:  # the store API may move while the backend is being written
    print(f"mock: running without the document store ({error})")

app = FastAPI(title="Alex mock API")

MODELS = [
    {
        "id": "claude-sonnet-5-5",
        "label": "Claude Sonnet 5.5",
        "provider": "anthropic",
        "tier": "standard",
        "default": True,
    },
    {
        "id": "claude-opus-5-5",
        "label": "Claude Opus 5.5",
        "provider": "anthropic",
        "tier": "premium",
        "default": False,
    },
    {
        "id": "local",
        "label": "Qwen3 32B",
        "provider": "local",
        "tier": "standard",
        "default": False,
    },
]

CANNED_TEXT = (
    "10) In Wien Mechanikus Künn macht vielleicht beste Waagen in Europa. Vollständigen Apparat "
    "zu hydrostatischen Versuchen mit Fortinschem Fuß, angebend von 3 Loth bis 0,01 Gran.\n\n"
    "⟨Magnetabweichung zu Dresden ist 1797 = 18°30′ westlich.⟩ Franz Zambra zu Wien hat fürs "
    "Lorenz-Kloster 1 Barometer à 129 Pfund Queksilber gemacht.\n\n"
    "Valencia | 311 toises | 27.3 |\nMadrid | 290 toises | 26.1 |"
)
CANNED_DOCUMENT = {
    "doc_id": "H0019734",
    "doc_type": "diary",
    "title": "Dresden, Wien, Salzburg [= Tagebuch der Reise 1797/98]",
    "date_from": "1797-06-01",
    "date_to": "1798-04-30",
    "url": "https://edition-humboldt.de/v12/H0019734",
    "citation": "Humboldt, Alexander von: Dresden, Wien, Salzburg, hg. v. Ulrike Leitner. In: "
    "edition humboldt digital, hg. v. Ottmar Ette. Berlin 2026. URL: "
    "https://edition-humboldt.de/v12/H0019734",
    "repository": "Staatsbibliothek zu Berlin – Preußischer Kulturbesitz",
    "shelfmark": "Nachl. Alexander von Humboldt (Tagebücher) VI",
    "editors": "Ulrike Leitner",
    "authors": "",
    "n_chars": len(CANNED_TEXT),
}


async def pause(seconds: float) -> None:
    if SPEED > 0:
        await asyncio.sleep(seconds * SPEED)


def frame(event_type: str, **payload) -> str:
    return "data: " + json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n\n"


# --------------------------------------------------------------------- sources


def passage_label(passage) -> str:
    page = f"Bl. {passage.page}" if passage.page else ""
    if passage.kind == "editorial":
        return ", ".join(part for part in ("Editorische Anmerkung", page) if part)
    if passage.kind == "footnote":
        return "Fußnote"
    if passage.kind == "introduction":
        return "Editorische Einführung"
    return page


def source_from_passage(source_id: str, passage) -> dict:
    source = {
        "id": source_id,
        "kind": passage.kind,
        "title": passage.title,
        "label": passage_label(passage),
        "doc_id": passage.doc_id,
        "doc_type": passage.doc_type,
        "page": passage.page,
        "date": passage.date,
        "url": passage.url,
        "start": passage.start,
        "end": passage.end,
    }
    if passage.note_id is not None:
        source["note_id"] = passage.note_id
    return source


def first_sentences(text: str, count: int = 1, limit: int = 260) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    return " ".join(sentences[:count])[:limit]


def found(query: str, limit: int = 3, **filters) -> list:
    if store is None:
        return []
    try:
        return store.search(query, limit=limit, **filters)
    except Exception as error:
        print(f"mock: search failed ({error})")
        return []


def canned_source(source_id: str) -> dict:
    return {
        "id": source_id,
        "kind": "text",
        "title": CANNED_DOCUMENT["title"],
        "label": "Bl. 56v",
        "doc_id": "H0019734",
        "doc_type": "diary",
        "page": "56v",
        "date": "1797-06-01/1798-04-30",
        "url": CANNED_DOCUMENT["url"] + "/56v",
        "start": 0,
        "end": len(CANNED_TEXT),
    }


class Script:
    """Collects the pieces of one scripted answer."""

    def __init__(self):
        self.sources: dict[str, dict] = {}
        self.quotes: dict[str, str] = {}
        self.counter = 0

    def add_passages(self, passages) -> list[dict]:
        added = []
        for passage in passages:
            self.counter += 1
            source = source_from_passage(f"S{self.counter}", passage)
            self.sources[source["id"]] = source
            rendered = store.rendered(passage) if passage.kind == "text" else passage.text
            self.quotes[source["id"]] = first_sentences(rendered)
            added.append(source)
        return added

    def add(self, source: dict, quote: str = "") -> dict:
        self.counter += 1
        source = {**source, "id": f"S{self.counter}"}
        self.sources[source["id"]] = source
        self.quotes[source["id"]] = quote
        return source


# --------------------------------------------------------------------- scripted streams


async def text_stream(text: str):
    for match in re.finditer(r"\S+\s*", text):
        yield frame("text", text=match.group())
        await pause(0.03)


def finish(
    script: Script,
    blocks: list[dict],
    artifacts: list[dict],
    model: dict,
    elapsed: float,
    tool_calls: int,
) -> dict:
    numbers: dict[str, int] = {}
    for block in blocks:
        for citation in block["citations"]:
            citation["n"] = numbers.setdefault(citation["source_id"], len(numbers) + 1)
    sources = [
        {**script.sources[source_id], "n": number}
        for source_id, number in numbers.items()
        if source_id in script.sources
    ]
    return {
        "text": "".join(block["text"] for block in blocks),
        "blocks": blocks,
        "sources": sources,
        "artifacts": artifacts,
        "model": model,
        "usage": {
            "rounds": tool_calls + 1,
            "tool_calls": tool_calls,
            "input_tokens": 48211,
            "output_tokens": 1630,
            "cache_read_tokens": 39800,
            "cache_write_tokens": 5200,
            "cost_usd": 0.0412,
        },
        "elapsed_s": elapsed,
        "stop_reason": "end_turn",
    }


async def run_blocks(script: Script, parts: list[tuple[str, list[str]]]):
    """Stream answer blocks; each part is (text, [source ids cited])."""
    blocks = []
    for text, cited in parts:
        yield frame("block")
        async for chunk in text_stream(text):
            yield chunk
        citations = []
        for source_id in cited:
            if source_id not in script.sources:
                continue
            citation = {
                "source_id": source_id,
                "cited_text": script.quotes.get(source_id, ""),
                "in_note": False,
            }
            citations.append(citation)
            yield frame("citation", **citation)
        blocks.append({"text": text, "citations": citations})
    script.blocks = blocks


async def tool(
    name: str,
    call_id: str,
    arguments: dict,
    round_number: int,
    summary: str,
    sources: list[dict] | None = None,
    wait: float = 0.7,
    ok: bool = True,
):
    yield frame("tool_call", id=call_id, name=name, input=arguments, round=round_number)
    await pause(wait)
    yield frame(
        "tool_result",
        id=call_id,
        name=name,
        ok=ok,
        summary=summary,
        count=len(sources or []),
        duration_ms=int(wait * 1000),
        sources=sources or [],
    )


async def cited_answer(query: str, model: dict, wikipedia: bool, markers: bool = False):
    script = Script()
    request_id = uuid.uuid4().hex
    yield frame("start", request_id=request_id, model=model)
    await pause(0.3)
    for piece in (
        "Die Frage zielt auf Humboldts Aufenthalt in Wien 1797. ",
        "Ich schlage zuerst den Ort im Register nach und suche dann im Tagebuch der Reise ",
        "sowie in der Chronologie.",
    ):
        yield frame("thinking", text=piece)
        await pause(0.15)

    entity = {}
    if store:
        hits = store.lookup_entities("Wien", kind="place", limit=1)
        entity = hits[0] if hits else {}
    register = script.add(
        {
            "kind": "register",
            "title": entity.get("name", "Wien"),
            "label": "Register",
            "entity_id": entity.get("entity_id", "H0005222"),
            "url": entity.get("url", "https://edition-humboldt.de/v12/H0005222"),
        },
        entity.get("description", "")[:120],
    )
    async for event in tool(
        "lookup_entity",
        "t1",
        {"name": "Wien", "kind": "place"},
        1,
        "1 Registereintrag: Wien",
        [register],
        0.4,
    ):
        yield event

    diary = (
        script.add_passages(
            found(
                "Wien Mechanikus Barometer", doc_types=["diary"], date_from="1797", date_to="1798"
            )
        )
        if store
        else [
            script.add(
                canned_source(""),
                "In Wien Mechanikus Künn macht vielleicht beste Waagen in Europa.",
            )
        ]
    )
    async for event in tool(
        "search",
        "t2",
        {
            "query": "Wien Mechanikus Barometer",
            "doc_types": ["diary"],
            "date_from": "1797",
            "date_to": "1798",
        },
        2,
        f"{len(diary)} Fundstellen",
        diary,
        0.6,
    ):
        yield event

    chronology = script.add_passages(
        found("Wien", doc_types=["chronology"], date_from="1797", date_to="1798", limit=2)
    )
    notes = script.add_passages(
        [
            passage
            for passage in found("Wien Nüll Mineralien", limit=6)
            if passage.kind == "editorial"
        ][:1]
    )
    async for event in tool(
        "chronology",
        "t3",
        {"date_from": "1797-08", "date_to": "1798-04"},
        2,
        f"{len(chronology)} Chronologie-Einträge",
        chronology,
        0.5,
    ):
        yield event
    if notes:
        async for event in tool(
            "grep",
            "t4",
            {"pattern": r"van der N[üu]ll", "doc_id": "H0019734"},
            3,
            "2 Treffer in 1 Dokument",
            notes,
            0.4,
        ):
            yield event

    sql = (
        "SELECT substr(date_from, 1, 4) AS jahr, COUNT(*) AS eintraege\nFROM chronology\n"
        "WHERE date_from BETWEEN '1797' AND '1799'\nGROUP BY 1 ORDER BY 1"
    )
    rows = [["1797", 30], ["1798", 37]]
    if store:
        try:
            rows = store.query(sql)["rows"]
        except Exception:
            pass
    data = script.add(
        {
            "kind": "data",
            "title": "Chronologie-Einträge pro Jahr",
            "label": "Datenabfrage",
            "data": {
                "sql": sql,
                "columns": ["jahr", "eintraege"],
                "rows": rows,
                "truncated": False,
            },
        },
        "1797",
    )
    async for event in tool(
        "query_data", "t5", {"sql": sql}, 3, f"{len(rows)} Zeilen", [data], 0.4
    ):
        yield event

    wiki = None
    if wikipedia:
        wiki = script.add(
            {
                "kind": "wikipedia",
                "title": "Wien",
                "label": "Wikipedia (de)",
                "url": "https://de.wikipedia.org/wiki/Wien",
                "wiki": {
                    "lang": "de",
                    "title": "Wien",
                    "description": "Hauptstadt Österreichs",
                    "url": "https://de.wikipedia.org/wiki/Wien",
                    "extract": "Wien ist die Bundeshauptstadt der Republik Österreich und "
                    "zugleich eines der neun österreichischen Bundesländer. Um 1800 "
                    "war Wien Residenzstadt der Habsburger und ein Zentrum der "
                    "Naturforschung.",
                },
            },
            "Um 1800 war Wien Residenzstadt der Habsburger",
        )
        async for event in tool(
            "wikipedia_context",
            "t6",
            {"term": "Wien", "lang": "de"},
            3,
            "Wikipedia: Wien",
            [wiki],
            0.5,
        ):
            yield event

    ids = lambda sources: [source["id"] for source in sources]  # noqa: E731
    parts = [
        (
            "Humboldt hielt sich im Herbst und Winter 1797/98 in **Wien** auf und nutzte die Zeit vor allem, "
            "um Instrumentenmacher, Sammler und Naturforscher aufzusuchen.",
            ids(chronology[:1]) + [register["id"]],
        ),
        ("\n\n### Instrumentenmacher und Werkstätten\n\n", []),
        (
            "Im Tagebuch der Reise hält er fest, wen er wegen Waagen und Barometern aufsuchte – "
            "etwa den Mechanikus Künn, der „vielleicht beste Waagen in Europa“ mache.",
            ids(diary[:1]),
        ),
        (
            " Auch die Werkstatt Zambras und andere Wiener Mechaniker notiert er mit Preisen und "
            "technischen Details.",
            ids(diary[1:3]),
        ),
        ("\n\n### Sammler\n\n", []),
        ("- den Bankier und Mineraliensammler **Friedrich Jacob van der Nüll**", ids(notes)),
        (
            "\n- weitere Gelehrte, die in der Chronologie für diese Monate genannt werden",
            ids(chronology[1:2]),
        ),
        (
            "\n\nFür die Jahre 1797 und 1798 verzeichnet die Chronologie zusammen "
            f"{sum(row[1] for row in rows[:2])} Einträge.",
            [data["id"]],
        ),
    ]
    if wiki:
        parts.append(
            (
                " Wien war um 1800 Residenzstadt der Habsburger und ein Zentrum der Naturforschung.",
                [wiki["id"]],
            )
        )

    if markers:
        blocks = []
        yield frame("block")
        for text, cited in parts:
            suffix = "".join(
                f" [{source_id}]" for source_id in cited if source_id in script.sources
            )
            tail = re.search(r"\s*$", text).group()
            body = (
                text[: len(text) - len(tail)]
                + (suffix if text.strip() and not text.strip().startswith("#") else "")
                + tail
            )
            async for chunk in text_stream(body):
                yield chunk
            blocks.append(
                {
                    "text": text,
                    "citations": [
                        {"source_id": source_id, "cited_text": "", "in_note": False}
                        for source_id in cited
                        if source_id in script.sources
                    ],
                }
            )
        script.blocks = blocks
    else:
        async for event in run_blocks(script, parts):
            yield event

    yield frame(
        "done", answer=finish(script, script.blocks, [], model, 18.4, 6 if wikipedia else 5)
    )
    await pause(0.6)
    yield frame(
        "suggestions",
        questions=[
            "Welche Instrumente kaufte Humboldt in Wien?",
            "Wer war Friedrich Jacob van der Nüll?",
            "Wohin reiste Humboldt nach dem Wiener Aufenthalt?",
        ],
    )


ITALY = [
    ("Paris", 48.8566, 2.3522, "1805-03-11", "Abreise mit Gay-Lussac"),
    ("Lyon", 45.764, 4.8357, "1805-03-18", ""),
    ("Mont Cenis", 45.2333, 6.9, "1805-03-26", "Alpenübergang, barometrische Messungen"),
    ("Turin", 45.0703, 7.6869, "1805-03-29", ""),
    ("Genua", 44.4056, 8.9463, "1805-04-08", ""),
    ("Mailand", 45.4642, 9.19, "1805-04-14", "Begegnung mit Volta"),
    ("Bologna", 44.4949, 11.3426, "1805-04-22", ""),
    ("Rom", 41.9028, 12.4964, "1805-05-01", "Aufenthalt bei Wilhelm von Humboldt bis 16. Juli"),
    ("Neapel", 40.8518, 14.2681, "1805-07-20", "Vesuvbesteigungen mit Gay-Lussac und Buch"),
    ("Florenz", 43.7696, 11.2558, "1805-09-20", ""),
    ("Berlin", 52.52, 13.405, "1805-11-16", "Ankunft"),
]


async def map_answer(query: str, model: dict):
    script = Script()
    yield frame("start", request_id=uuid.uuid4().hex, model=model)
    await pause(0.3)
    yield frame(
        "thinking",
        text="Für die Karte brauche ich die Stationen der Italienreise in Reihenfolge; "
        "die Chronologie liefert Daten und Orte.",
    )
    chronology = script.add_passages(
        found(
            "Gay-Lussac Rom Neapel",
            doc_types=["chronology"],
            date_from="1805",
            date_to="1805",
            limit=3,
        )
    )
    async for event in tool(
        "chronology",
        "t1",
        {"date_from": "1805-03", "date_to": "1805-11"},
        1,
        "21 Chronologie-Einträge",
        chronology,
        0.8,
    ):
        yield event
    diary = script.add_passages(found("Vesuv Gay Lussac", doc_types=["diary"], limit=2))
    async for event in tool(
        "read_document",
        "t2",
        {"doc_id": "H0019155", "page_from": "11v", "page_to": "12r"},
        2,
        "2 Seiten gelesen",
        diary,
        0.6,
    ):
        yield event

    stations = [
        {
            "label": label,
            "lat": lat,
            "lon": lon,
            "date": date,
            "description": description,
            "url": "",
            "group": "",
        }
        for label, lat, lon, date, description in ITALY
    ]
    map_artifact = {
        "id": "a1",
        "type": "map",
        "title": "Italienreise 1805",
        "connect_route": True,
        "stations": stations,
    }
    async for event in tool(
        "create_map",
        "t3",
        {
            "title": "Italienreise 1805",
            "stations": [{"name": station["label"]} for station in stations],
        },
        3,
        f"Karte mit {len(stations)} Stationen",
        None,
        0.4,
    ):
        yield event
    yield frame("artifact", artifact=map_artifact)
    timeline_artifact = {
        "id": "a2",
        "type": "timeline",
        "title": "Stationen der Italienreise 1805",
        "events": [
            {"date": date, "label": label, "description": description, "url": ""}
            for label, _, _, date, description in ITALY
        ],
    }
    async for event in tool(
        "create_timeline",
        "t4",
        {"title": timeline_artifact["title"], "events": timeline_artifact["events"]},
        3,
        f"Zeitleiste mit {len(ITALY)} Ereignissen",
        None,
        0.3,
    ):
        yield event
    yield frame("artifact", artifact=timeline_artifact)

    ids = lambda sources: [source["id"] for source in sources]  # noqa: E731
    parts = [
        (
            "Humboldt brach am 11. März 1805 mit Joseph Louis Gay-Lussac von Paris nach Italien auf.",
            ids(chronology[:1]),
        ),
        (
            " Über den Mont Cenis, Turin, Genua und Mailand erreichten sie am 1. Mai Rom, wo Humboldt bis "
            "Mitte Juli bei seinem Bruder Wilhelm blieb.",
            ids(chronology[1:2]),
        ),
        (" Von Neapel aus bestieg er im Juli und August mehrfach den Vesuv.", ids(diary[:1])),
        ("\n\nDie Karte zeigt die Stationen in Reihenfolge, die Zeitleiste die Daten dazu.", []),
    ]
    async for event in run_blocks(script, parts):
        yield event
    yield frame(
        "done",
        answer=finish(script, script.blocks, [map_artifact, timeline_artifact], model, 24.9, 4),
    )
    yield frame(
        "suggestions", questions=["Was beobachtete Humboldt am Vesuv?", "Wen traf Humboldt in Rom?"]
    )


def letters_per_year() -> list[list]:
    if store:
        try:
            result = store.query(
                "SELECT CAST(substr(date_from, 1, 4) AS INTEGER) AS jahr, COUNT(*) AS briefe "
                "FROM letters WHERE date_from != '' GROUP BY 1 ORDER BY 1"
            )
            return result["rows"]
        except Exception as error:
            print(f"mock: query failed ({error})")
    return [
        [1791, 2],
        [1795, 4],
        [1799, 6],
        [1805, 9],
        [1810, 12],
        [1820, 18],
        [1829, 41],
        [1835, 26],
        [1840, 33],
        [1845, 38],
        [1850, 29],
        [1855, 35],
        [1859, 11],
    ]


def sample_png(rows: list[list]) -> str:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figure, axis = plt.subplots(figsize=(7.2, 3.4), dpi=110)
        decades: dict[int, int] = {}
        for year, count in rows:
            decades[int(year) // 10 * 10] = decades.get(int(year) // 10 * 10, 0) + count
        axis.bar(
            [str(decade) for decade in decades], list(decades.values()), color="#2a78d6", width=0.6
        )
        axis.set_ylabel("Briefe")
        axis.set_title("Briefe pro Jahrzehnt", loc="left", fontsize=11)
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
        axis.grid(axis="y", color="#e6e2da")
        axis.set_axisbelow(True)
        figure.tight_layout()
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", facecolor="#fdfcfa")
        plt.close(figure)
        return base64.b64encode(buffer.getvalue()).decode()
    except Exception as error:
        print(f"mock: no matplotlib image ({error})")
        return (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5E"
            "rkJggg=="
        )


async def python_answer(query: str, model: dict):
    script = Script()
    rows = letters_per_year()
    yield frame("start", request_id=uuid.uuid4().hex, model=model)
    await pause(0.3)
    yield frame(
        "thinking",
        text="Das ist eine Zählfrage: Die Briefe pro Jahr lassen sich direkt aus den "
        "Metadaten berechnen und als Diagramm zeigen.",
    )
    code = (
        "import plotly.express as px\n\n"
        'letters = ehd.sql("""\n'
        "    SELECT CAST(substr(date_from, 1, 4) AS INTEGER) AS jahr, COUNT(*) AS briefe\n"
        "    FROM letters WHERE date_from != '' GROUP BY 1 ORDER BY 1\n"
        '""")\n'
        "print(letters.briefe.sum(), 'datierte Briefe;', 'Maximum:', letters.loc[letters.briefe.idxmax()].to_dict())\n\n"
        "figure = px.bar(letters, x='jahr', y='briefe', title='Briefe pro Jahr')\n"
        "ehd.show(figure)\n"
        "ehd.show(letters, title='Briefe pro Jahr (Tabelle)')\n"
    )
    total = sum(row[1] for row in rows)
    peak = max(rows, key=lambda row: row[1])
    output = f"{total} datierte Briefe; Maximum: {{'jahr': {peak[0]}, 'briefe': {peak[1]}}}"
    computation = script.add(
        {
            "kind": "computation",
            "title": "Briefe pro Jahr",
            "label": "Berechnung",
            "computation": {"code": code, "output": output},
        },
        str(total),
    )
    async for event in tool(
        "run_python",
        "t1",
        {"code": code},
        1,
        "Ausgabe, 1 Diagramm, 1 Tabelle, 1 Abbildung",
        [computation],
        1.4,
    ):
        yield event
    chart = {
        "id": "a1",
        "type": "chart",
        "title": "Briefe pro Jahr",
        "figure": {
            "data": [
                {
                    "type": "bar",
                    "x": [row[0] for row in rows],
                    "y": [row[1] for row in rows],
                    "name": "Briefe",
                    "hovertemplate": "%{x}: %{y} Briefe<extra></extra>",
                }
            ],
            "layout": {
                "title": {"text": "Briefe pro Jahr"},
                "xaxis": {"title": {"text": "Jahr"}},
                "yaxis": {"title": {"text": "Briefe"}},
                "template": {"layout": {"paper_bgcolor": "white"}},
            },
        },
    }
    yield frame("artifact", artifact=chart)
    await pause(0.2)
    table = {
        "id": "a2",
        "type": "table",
        "title": "Briefe pro Jahr (Tabelle)",
        "columns": ["jahr", "briefe"],
        "rows": rows,
        "truncated": False,
    }
    yield frame("artifact", artifact=table)
    await pause(0.2)
    image = {
        "id": "a3",
        "type": "image",
        "title": "Briefe pro Jahrzehnt",
        "media_type": "image/png",
        "data": sample_png(rows),
        "alt": "Säulendiagramm der Briefe pro Jahrzehnt",
        "attribution": None,
    }
    yield frame("artifact", artifact=image)

    parts = [
        (
            f"Die Edition enthält **{total} datierte Briefe**. Die meisten stammen aus dem Jahr "
            f"{peak[0]} ({peak[1]} Briefe).",
            [computation["id"]],
        ),
        (
            "\n\n| Kennzahl | Wert |\n|---|---:|\n"
            f"| Datierte Briefe | {total} |\n| Jahre mit Briefen | {len(rows)} |\n"
            f"| Stärkstes Jahr | {peak[0]} |\n",
            [],
        ),
        (
            "\nDas Diagramm zeigt die Verteilung über die Jahre; die Tabelle darunter lässt sich "
            "sortieren und als CSV herunterladen.",
            [],
        ),
    ]
    async for event in run_blocks(script, parts):
        yield event
    yield frame("done", answer=finish(script, script.blocks, [chart, table, image], model, 12.7, 1))
    yield frame("suggestions", questions=["Mit wem korrespondierte Humboldt 1829 am häufigsten?"])


async def network_answer(query: str, model: dict):
    script = Script()
    yield frame("start", request_id=uuid.uuid4().hex, model=model)
    await pause(0.3)
    focus_id = "H0003476"
    nodes = [
        {
            "id": focus_id,
            "label": "Bonpland, Aimé",
            "kind": "person",
            "weight": 340,
            "url": "https://edition-humboldt.de/v12/H0003476",
            "description": "",
            "focus": True,
        }
    ]
    edges = []
    neighbours, links = [], []
    if store:
        try:
            graph = store.cooccurring(focus_id, limit=16)
            neighbours, links = graph["neighbours"], graph["links"]
        except Exception as error:
            print(f"mock: cooccurring failed ({error})")
    if not neighbours:
        neighbours = [
            {
                "entity_id": "H0006403",
                "name": "Willdenow, Karl Ludwig",
                "kind": "person",
                "shared": 23,
                "url": "",
            },
            {"entity_id": "H0007693", "name": "Cumaná", "kind": "place", "shared": 18, "url": ""},
            {
                "entity_id": "H0010593",
                "name": "Río Orinoco",
                "kind": "place",
                "shared": 12,
                "url": "",
            },
            {
                "entity_id": "H0000583",
                "name": "Institut de France",
                "kind": "institution",
                "shared": 6,
                "url": "",
            },
            {
                "entity_id": "plant-cinchona",
                "name": "Cinchona",
                "kind": "plant",
                "shared": 5,
                "url": "",
            },
        ]
        links = [{"source": "H0007693", "target": "H0010593", "shared": 4}]
    for neighbour in neighbours:
        nodes.append(
            {
                "id": neighbour["entity_id"],
                "label": neighbour["name"],
                "kind": neighbour["kind"],
                "weight": neighbour["shared"],
                "url": neighbour.get("url") or "",
                "description": "",
            }
        )
        edges.append(
            {
                "source": focus_id,
                "target": neighbour["entity_id"],
                "weight": neighbour["shared"],
                "label": "",
            }
        )
    edges += [
        {"source": link["source"], "target": link["target"], "weight": link["shared"], "label": ""}
        for link in links
    ]
    register = script.add(
        {
            "kind": "register",
            "title": "Bonpland, Aimé",
            "label": "Register",
            "entity_id": focus_id,
            "url": "https://edition-humboldt.de/v12/H0003476",
        }
    )
    async for event in tool(
        "lookup_entity", "t1", {"name": "Bonpland"}, 1, "3 Registereinträge", [register], 0.4
    ):
        yield event
    mentions = script.add_passages(found("Bonpland Herbarium Pflanzen", limit=3))
    async for event in tool(
        "entity_mentions",
        "t2",
        {"entity_id": focus_id, "doc_types": ["diary"]},
        2,
        "340 Erwähnungen in 68 Dokumenten",
        mentions,
        0.7,
    ):
        yield event
    network = {
        "id": "a1",
        "type": "network",
        "title": "Umfeld von Aimé Bonpland",
        "nodes": nodes,
        "edges": edges,
    }
    async for event in tool(
        "create_network",
        "t3",
        {"focus": focus_id, "title": network["title"]},
        3,
        f"Netzwerk mit {len(nodes)} Knoten",
        None,
        0.4,
    ):
        yield event
    yield frame("artifact", artifact=network)
    ids = lambda sources: [source["id"] for source in sources]  # noqa: E731
    parts = [
        (
            "**Aimé Bonpland** begleitete Humboldt auf der amerikanischen Reise und ist in den Texten "
            "der Edition 340-mal annotiert.",
            [register["id"]],
        ),
        (
            " Am engsten ist er mit dem Botaniker Willdenow und mit den Orten der Reise verbunden.",
            ids(mentions[:2]),
        ),
        (
            "\n\nDas Netzwerk zeigt, welche Personen, Orte und Pflanzen in denselben Absätzen wie "
            "Bonpland genannt werden.",
            [],
        ),
    ]
    async for event in run_blocks(script, parts):
        yield event
    yield frame("done", answer=finish(script, script.blocks, [network], model, 15.2, 3))


async def error_answer(query: str, model: dict):
    yield frame("start", request_id=uuid.uuid4().hex, model=model)
    await pause(0.3)
    async for event in tool(
        "search", "t1", {"query": "Chimborazo Besteigung"}, 1, "8 Fundstellen", None, 0.6
    ):
        yield event
    async for event in tool(
        "grep",
        "t2",
        {"pattern": "(unbalanced"},
        2,
        "Invalid regular expression: missing )",
        None,
        0.3,
        ok=False,
    ):
        yield event
    yield frame("block")
    async for chunk in text_stream(
        "Humboldt versuchte am 23. Juni 1802, den Chimborazo zu besteigen, und "
    ):
        yield chunk
    await pause(0.4)
    yield frame("error", message="upstream connection reset", code="overloaded")


SCENARIOS = [
    (re.compile(r"karte|\bmap\b|italien|italy", re.I), map_answer),
    (re.compile(r"diagramm|chart|python|pro jahr|per year", re.I), python_answer),
    (re.compile(r"netzwerk|network|umfeld", re.I), network_answer),
    (re.compile(r"fehler|error", re.I), error_answer),
]


# --------------------------------------------------------------------- endpoints


@app.get("/api/config")
def config() -> dict:
    documents = {
        "diary": 13,
        "letter": 558,
        "document": 62,
        "chronology": 1684,
        "research_article": 23,
        "about": 24,
    }
    entities = {
        "person": 9399,
        "place": 4749,
        "institution": 235,
        "plant": 3292,
        "glossary": 245,
        "siglum": 41,
    }
    version, commit, commit_date = (
        "12.0.0",
        "f128f89b9dc101254cd311b096f7569fbc1e001e",
        "2026-09-10",
    )
    if store:
        statistics = store.statistics()
        documents, entities = statistics["documents"], statistics["entities"]
        version = statistics["corpus_version"] or version
        commit = statistics["corpus_commit"] or commit
        commit_date = statistics["corpus_commit_date"] or commit_date
    return {
        "assistant": "Alex",
        "corpus": {
            "version": version,
            "commit": commit,
            "commit_date": commit_date,
            "documents": documents,
            "entities": entities,
        },
        "models": MODELS if os.environ.get("MOCK_SINGLE_MODEL") != "1" else MODELS[:1],
        "features": {"python": True, "wikipedia": True, "feedback": True},
        "limits": {"max_query_chars": 2000, "max_history_turns": 12},
        "links": {
            "edition": "https://edition-humboldt.de",
            "imprint": "https://edition-humboldt.de/impressum.xql",
            "privacy": "https://edition-humboldt.de/impressum.xql",
        },
        "map": {
            "tile_url": "https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png",
            "attribution": "© OpenStreetMap-Mitwirkende",
            "max_zoom": 18,
        },
    }


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    query = str(body.get("query", "")).strip()
    if not query:
        raise HTTPException(422, "query must not be empty")
    if re.search(r"\blimit\b", query, re.I):
        raise HTTPException(
            429,
            "Zu viele Anfragen — bitte in einer Stunde erneut versuchen. / "
            "Too many requests — please try again in an hour.",
        )
    model = next((entry for entry in MODELS if entry["id"] == body.get("model")), MODELS[0])
    model = {"id": model["id"], "label": model["label"]}
    markers = bool(re.search(r"lokal|local|marker", query, re.I)) or model["id"] == "local"
    for pattern, scenario in SCENARIOS:
        if pattern.search(query):
            stream = scenario(query, model)
            break
    else:
        stream = cited_answer(query, model, bool(body.get("wikipedia")), markers)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def document_dict(doc_id: str) -> dict:
    if store is None:
        return CANNED_DOCUMENT
    row = dict(store.document(doc_id))
    return {
        key: row.get(key)
        for key in (
            "doc_id",
            "doc_type",
            "title",
            "date_from",
            "date_to",
            "url",
            "citation",
            "repository",
            "shelfmark",
            "editors",
            "authors",
            "n_chars",
        )
    }


def passage_dict(doc_id: str, start: int, end: int) -> dict:
    if store is None:
        text = CANNED_TEXT
        return {
            "document": CANNED_DOCUMENT,
            "start": 0,
            "end": len(text),
            "page": "56v",
            "url": CANNED_DOCUMENT["url"] + "/56v",
            "text": text,
            "pages": [{"label": "57r", "start": 190, "facsimile": ""}],
            "notes": [
                {
                    "note_id": 1,
                    "kind": "editorial",
                    "label": "",
                    "text": "Gemeint ist der Wiener Mechaniker Johann Künn.",
                    "anchor_start": 22,
                    "anchor_end": 26,
                }
            ],
            "before": None,
            "after": None,
        }
    full = store.text(doc_id)
    end = min(end, start + 20000, len(full))
    passage = store.passage(doc_id, start, end)
    pages = [dict(page) for page in store.pages(doc_id) if start < page["start"] < end]
    current = [page for page in store.pages(doc_id) if page["start"] <= start]
    step = 1500

    def boundary(offset: int, forward: bool) -> int:
        if forward:
            cut = full.find("\n\n", offset)
            return len(full) if cut < 0 else cut
        cut = full.rfind("\n\n", 0, offset)
        return 0 if cut < 0 else cut + 2

    return {
        "document": document_dict(doc_id),
        "start": passage.start,
        "end": passage.end,
        "page": passage.page,
        "url": passage.url,
        "facsimile": current[-1]["facsimile"] if current else "",
        "text": passage.text,
        "pages": pages,
        "notes": [
            {
                "note_id": note.note_id,
                "kind": note.kind,
                "label": note.label,
                "text": note.text,
                "anchor_start": note.anchor_start,
                "anchor_end": note.anchor_end,
            }
            for note in passage.notes
        ],
        "before": boundary(max(0, start - step), False) if start > 0 else None,
        "after": boundary(min(len(full), end + step), True) if end < len(full) else None,
    }


@app.get("/api/passage")
def passage(doc_id: str, start: int = 0, end: int = 2000) -> dict:
    try:
        return passage_dict(doc_id, max(0, start), max(start, end))
    except Exception as error:
        raise HTTPException(404, str(error)) from error


@app.get("/api/notes/{note_id}")
def note(note_id: int) -> dict:
    if store is None:
        return {
            "note": {
                "note_id": note_id,
                "kind": "editorial",
                "label": "",
                "text": "Gemeint ist der Wiener Mechaniker Johann Künn.",
                "anchor_start": 22,
                "anchor_end": 26,
            },
            "passage": passage_dict("H0019734", 0, 400),
        }
    try:
        found_note = store.note_passage(note_id)
    except Exception as error:
        raise HTTPException(404, str(error)) from error
    start, end = store.snapped(
        found_note.doc_id,
        max(0, found_note.start - 500),
        min(len(store.text(found_note.doc_id)), found_note.end + 500),
        0,
        len(store.text(found_note.doc_id)),
    )
    return {
        "note": {
            "note_id": note_id,
            "kind": found_note.kind,
            "label": "",
            "text": found_note.text,
            "anchor_start": found_note.start,
            "anchor_end": found_note.end,
        },
        "passage": passage_dict(found_note.doc_id, start, end),
    }


@app.get("/api/entities/{entity_id}")
def entity(entity_id: str) -> dict:
    record = store.entity(entity_id) if store else None
    if record is None:
        if store is not None:
            raise HTTPException(404, "Unknown entity")
        record = {
            "entity_id": entity_id,
            "kind": "place",
            "name": "Wien",
            "alternative_names": ["Vienna", "Vienne"],
            "description": "Haupt- und Residenzstadt der Habsburgermonarchie.",
            "born": "",
            "died": "",
            "gnd": "4066009-6",
            "geonames": "2761369",
            "viaf": "",
            "wikidata": "Q1741",
            "url": "https://edition-humboldt.de/v12/H0005222",
            "lat": 48.208,
            "lon": 16.372,
            "links": {},
            "in_register": 1,
            "mention_count": 172,
            "document_count": 48,
        }
    record.pop("entity_no", None)
    return record


@app.post("/api/feedback")
async def feedback(request: Request) -> Response:
    print("feedback:", await request.json())
    return Response(status_code=204)


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse(
        {"status": "ok", "corpus_version": "12.0.0", "models": [model["id"] for model in MODELS]}
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """The same policy the real deployment should send, so the client is
    developed under it."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.de https://upload.wikimedia.org; "
        "font-src 'self'; connect-src 'self'; frame-ancestors *; base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/assets/") or request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")


if __name__ == "__main__":
    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ.get("PORT", "8791")), log_level="warning"
    )
