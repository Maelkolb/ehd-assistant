"""The tools Alex works with: search and read the edition, query its data,
run Python, and build maps, timelines and networks."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from alex.agent import wikipedia
from alex.agent.sources import Source, SourceRegistry
from alex.config import ModelSpec
from alex.corpus.store import DOC_TYPES, DocumentStore, Passage, StoreError
from alex.llm.base import CitableText, ToolCall, ToolSpec
from alex.sandbox.runner import Sandbox

ENTITY_KINDS = ["person", "place", "institution", "plant", "glossary", "siglum"]
MAX_WIKIPEDIA_CALLS = 4
STRING, INTEGER, NUMBER, BOOLEAN = (
    {"type": name} for name in ("string", "integer", "number", "boolean")
)


def described(schema: dict, description: str) -> dict:
    return {**schema, "description": description}


def string_list(description: str, choices: list[str] | None = None) -> dict:
    items = {"type": "string", "enum": choices} if choices else {"type": "string"}
    return {"type": "array", "items": items, "description": description}


@dataclass
class ToolResult:
    text: str = ""
    sources: list[CitableText] = field(default_factory=list)
    new_sources: list[Source] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    summary: str = ""
    count: int = 0
    is_error: bool = False


def failure(message: str) -> ToolResult:
    return ToolResult(text=message, summary=message[:140], is_error=True)


@dataclass
class ToolContext:
    store: DocumentStore
    sandbox: Sandbox
    registry: SourceRegistry
    spec: ModelSpec
    language: str = "de"
    artifacts: list[dict] = field(default_factory=list)
    wikipedia_calls: int = 0

    @property
    def compact(self) -> bool:
        return self.spec.compact

    def cite(self, result: ToolResult, passage: Passage, max_notes: int = 8) -> CitableText:
        source, citable, new = self.registry.passage(passage, max_notes)
        result.sources.append(citable)
        if new:
            result.new_sources.append(source)
        return citable

    def tabulate(
        self, result: ToolResult, title: str, query: str, columns: list[str], rows: list[list]
    ) -> None:
        """Counts and listings a tool computed, as a citable table."""
        text = table_text(columns, rows, 2_500 if self.compact else 6_000)
        source, citable = self.registry.data(title, query, columns, rows, False, text)
        result.sources.append(citable)
        if source not in result.new_sources:
            result.new_sources.append(source)

    def show(self, result: ToolResult, artifact: dict) -> dict:
        """An artifact with the type and title of an earlier one replaces it."""
        for position, existing in enumerate(self.artifacts):
            if existing["type"] == artifact["type"] and existing.get("title") == artifact.get(
                "title"
            ):
                artifact["id"] = existing["id"]
                self.artifacts[position] = artifact
                break
        else:
            artifact["id"] = f"a{len(self.artifacts) + 1}"
            self.artifacts.append(artifact)
        result.artifacts.append(artifact)
        return artifact


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    handler: Callable[[dict, ToolContext], ToolResult]
    required: tuple[str, ...] = ()
    long_input: bool = False

    @property
    def spec(self) -> ToolSpec:
        schema = {"type": "object", "properties": self.properties, "required": list(self.required)}
        return ToolSpec(self.name, self.description, schema, self.long_input)


# ----------------------------------------------------------------------
# Search and reading
# ----------------------------------------------------------------------


def tool_search(arguments: dict, context: ToolContext) -> ToolResult:
    limit = min(
        arguments.get("limit") or (5 if context.compact else 8), 8 if context.compact else 20
    )
    passages = context.store.search(
        arguments["query"],
        doc_types=arguments.get("doc_types"),
        date_from=arguments.get("date_from"),
        date_to=arguments.get("date_to"),
        doc_id=arguments.get("doc_id"),
        limit=limit,
        max_chars=700 if context.compact else 1200,
    )
    if not passages:
        return ToolResult(
            text="No matches. Try other spellings or languages (the texts are "
            "German, French, Spanish, Latin and English of around 1800), "
            "fewer terms, a prefix like kroko*, or grep.",
            summary="0",
        )
    result = ToolResult(count=len(passages), summary=str(len(passages)))
    for passage in passages:
        context.cite(result, passage, max_notes=3 if context.compact else 6)
    return result


def tool_grep(arguments: dict, context: ToolContext) -> ToolResult:
    limit = min(arguments.get("limit") or (8 if context.compact else 15), 40)
    found = context.store.grep(
        arguments["pattern"],
        doc_types=arguments.get("doc_types"),
        doc_id=arguments.get("doc_id"),
        limit=limit,
        ignore_case=not arguments.get("case_sensitive", False),
    )
    if not found["total_matches"]:
        note = " (stopped at the time limit)" if found["timed_out"] else ""
        return ToolResult(text=f"No matches{note}.", summary="0")
    result = ToolResult(count=found["total_matches"])
    german = context.language == "de"
    context.tabulate(
        result,
        ("Treffer je Dokument: " if german else "Matches per document: ") + arguments["pattern"],
        f"grep {arguments['pattern']}",
        ["doc_id", "doc_type", "title", "matches"],
        [
            [entry["doc_id"], entry["doc_type"], entry["title"], entry["matches"]]
            for entry in found["documents"]
        ],
    )
    for passage in found["passages"]:
        context.cite(result, passage, max_notes=0)
    if found["timed_out"]:
        result.text = "The search stopped at its time limit; the counts are incomplete."
    return result


def document_line(document) -> str:
    parts = [document["doc_id"], document["doc_type"], document["title"]]
    if document["date_from"]:
        parts.append(
            document["date_from"]
            if document["date_from"] == document["date_to"]
            else f"{document['date_from']}/{document['date_to']}"
        )
    if document["sender_name"] or document["recipient_name"]:
        parts.append(f"{document['sender_name']} → {document['recipient_name']}")
    if document["origin_place"]:
        parts.append(f"from {document['origin_place']}")
    parts.append(f"{document['n_chars']} chars")
    return " | ".join(parts)


def tool_read_document(arguments: dict, context: ToolContext) -> ToolResult:
    store = context.store
    document = store.document(arguments["doc_id"])
    doc_id = document["doc_id"]
    ceiling = 12_000 if context.compact else 150_000
    max_chars = min(arguments.get("max_chars") or (6_000 if context.compact else 30_000), ceiling)
    whole = not arguments.get("page_from") and arguments.get("start") is None
    if arguments.get("page_from"):
        start, end = store.page_span(doc_id, arguments["page_from"], arguments.get("page_to"))
        passage = store.read(doc_id, start, max_chars, end=end)
    else:
        passage = store.read(doc_id, max(0, arguments.get("start") or 0), max_chars)

    result = ToolResult(count=passage.end - passage.start)
    record = document_line(document)
    if document["editors"]:
        record += f" | edited by {document['editors']}"
    hints = []
    if whole:
        introduction = store.introduction(doc_id)
        if introduction:
            context.cite(result, introduction)
        if passage.truncated_after:
            hints.append(outline(store, doc_id))
    citable = context.cite(result, passage, max_notes=10 if context.compact else 30)
    citable.notes.insert(0, f"[Document record] {record}")
    if whole and document["abstract"]:
        citable.notes.insert(1, f"[Abstract] {document['abstract'][:1200]}")
    if passage.truncated_after:
        hints.append(
            f"The text continues: read on with start={passage.end} "
            f"(document length {document['n_chars']})."
        )
    result.text = "\n".join(hints)
    return result


def outline(store: DocumentStore, doc_id: str) -> str:
    pages = [page["label"] for page in store.pages(doc_id)]
    sections = [section for section in store.sections(doc_id) if section["heading"]]
    lines = []
    if pages:
        shown = ", ".join(pages[:80]) + (f", … ({len(pages)} pages)" if len(pages) > 80 else "")
        lines.append(f"Pages: {shown}")
    if sections:
        headings = "; ".join(
            f"{section['heading'][:60]} @{section['start']}" for section in sections[:50]
        )
        lines.append(
            f"Sections (heading @offset): {headings}" + (" …" if len(sections) > 50 else "")
        )
    return "\n".join(lines)


def tool_list_documents(arguments: dict, context: ToolContext) -> ToolResult:
    limit = min(arguments.get("limit") or 30, 100)
    rows, total = context.store.list_documents(
        doc_type=arguments.get("doc_type"),
        date_from=arguments.get("date_from"),
        date_to=arguments.get("date_to"),
        person=arguments.get("person"),
        title_contains=arguments.get("title_contains"),
        limit=limit,
        offset=arguments.get("offset") or 0,
    )
    if not rows:
        return ToolResult(text="No documents match.", summary="0")
    result = ToolResult(count=total)
    filters = ", ".join(
        f"{name}={arguments[name]}"
        for name in ("doc_type", "date_from", "date_to", "person", "title_contains")
        if arguments.get(name)
    )
    context.tabulate(
        result,
        ("Dokumente" if context.language == "de" else "Documents")
        + (f": {filters}" if filters else ""),
        f"list_documents {filters} offset={arguments.get('offset') or 0}",
        ["doc_id", "doc_type", "date", "title", "sender", "recipient", "from", "chars"],
        [
            [
                row["doc_id"],
                row["doc_type"],
                row["date_from"]
                if row["date_from"] == row["date_to"]
                else f"{row['date_from']}/{row['date_to']}",
                row["title"],
                row["sender_name"],
                row["recipient_name"],
                row["origin_place"],
                row["n_chars"],
            ]
            for row in rows
        ],
    )
    more = total - (arguments.get("offset") or 0) - len(rows)
    if more > 0:
        result.text = (
            f"{total} documents match; {more} more with "
            f"offset={(arguments.get('offset') or 0) + len(rows)}."
        )
    return result


# ----------------------------------------------------------------------
# Registers and annotations
# ----------------------------------------------------------------------


def resolve_entity(store: DocumentStore, reference: str, kind: str | None = None) -> dict | None:
    reference = (reference or "").strip()
    if not reference:
        return None
    return store.entity(reference) or next(iter(store.lookup_entities(reference, kind, 1)), None)


def tool_lookup_entity(arguments: dict, context: ToolContext) -> ToolResult:
    entities = context.store.lookup_entities(
        arguments["name"], arguments.get("kind"), limit=4 if context.compact else 6
    )
    if not entities:
        return ToolResult(
            text="No register entry found. Try another spelling, or search the texts directly.",
            summary="0",
        )
    result = ToolResult(count=len(entities), summary=entities[0]["name"])
    for entity in entities:
        source, citable, new = context.registry.entity(entity)
        result.sources.append(citable)
        if new:
            result.new_sources.append(source)
    return result


def tool_entity_mentions(arguments: dict, context: ToolContext) -> ToolResult:
    entity = resolve_entity(context.store, arguments["entity_id"])
    if entity is None:
        return failure(f"No entity {arguments['entity_id']!r}; use lookup_entity first.")
    limit = min(arguments.get("limit") or (5 if context.compact else 8), 25)
    found = context.store.mentions(
        entity["entity_id"],
        doc_types=arguments.get("doc_types"),
        doc_id=arguments.get("doc_id"),
        date_from=arguments.get("date_from"),
        date_to=arguments.get("date_to"),
        limit=limit,
        offset=arguments.get("offset") or 0,
        context=220 if context.compact else 320,
    )
    if not found["total_mentions"]:
        return ToolResult(
            text=f"{entity['name']} is not annotated in the selected texts.", summary="0"
        )
    result = ToolResult(count=found["total_mentions"])
    context.tabulate(
        result,
        (
            "Annotierte Erwähnungen je Dokument: "
            if context.language == "de"
            else "Annotated mentions per document: "
        )
        + entity["name"],
        f"entity_mentions {entity['entity_id']} "
        + " ".join(
            f"{name}={arguments[name]}"
            for name in ("doc_types", "doc_id", "date_from", "date_to")
            if arguments.get(name)
        ),
        ["doc_id", "doc_type", "date", "title", "mentions"],
        [
            [
                document["doc_id"],
                document["doc_type"],
                document["date_from"],
                document["title"],
                document["mentions"],
            ]
            for document in found["documents"]
        ],
    )
    for passage in found["passages"]:
        context.cite(result, passage, max_notes=0)
    shown = len(found["passages"]) + (arguments.get("offset") or 0)
    if found["total_mentions"] > shown:
        result.text = (
            f"{len(found['passages'])} passages shown, taken in turn from the documents; "
            "narrow with doc_id or page on with offset for more."
        )
    return result


def tool_chronology(arguments: dict, context: ToolContext) -> ToolResult:
    store = context.store
    limit = min(arguments.get("limit") or (12 if context.compact else 30), 80)
    entries = store.chronology(arguments["date_from"], arguments.get("date_to"), limit + 1)
    result = ToolResult()
    note = ""
    if len(entries) > limit:
        entries = entries[:limit]
        last = entries[-1]["date_from"]
        note = (
            f"Only the first {limit} entries of this range are shown (up to {last}). "
            "Narrow the dates or page on with date_from after the last entry shown. "
        )
    if not entries:
        entries = store.chronology_neighbours(arguments["date_from"], arguments.get("date_to"))
        if not entries:
            return ToolResult(text="No chronology entries.", summary="0")
        note = (
            "No chronology entry falls in this range; these are the nearest entries "
            "before and after it. "
        )
    for entry in entries:
        citable = context.cite(
            result, store.passage(entry["doc_id"], 0, len(entry["text"]), with_notes=False)
        )
        places = "; ".join(
            f"{place['name']} [{place['entity_id']}]"
            + (f" ({place['lat']:.2f}, {place['lon']:.2f})" if place["lat"] is not None else "")
            for place in entry["places"][:8]
        )
        if places:
            citable.notes.append(f"[Places annotated in this entry] {places}")
    result.count = len(entries)
    result.text = note.strip()
    return result


# ----------------------------------------------------------------------
# Data and code
# ----------------------------------------------------------------------


def table_text(columns: list[str], rows: list[list], max_chars: int) -> str:
    def cell(value) -> str:
        text = "" if value is None else str(value)
        text = re.sub(r"\s+", " ", text)
        return text if len(text) <= 160 else text[:157] + "…"

    lines = [" | ".join(columns)]
    used = len(lines[0])
    for position, row in enumerate(rows):
        line = " | ".join(cell(value) for value in row)
        if used + len(line) > max_chars:
            lines.append(
                f"… {len(rows) - position} more rows not shown; narrow the query or aggregate."
            )
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def tool_query_data(arguments: dict, context: ToolContext) -> ToolResult:
    sql = arguments["sql"].strip().rstrip(";")
    outcome = context.store.query(sql, max_rows=120 if context.compact else 300)
    rows = outcome["rows"]
    title = arguments.get("title") or ("Datenabfrage" if context.language == "de" else "Data query")
    text = table_text(outcome["columns"], rows, 3_000 if context.compact else 9_000)
    if outcome["truncated"]:
        text += "\n(result truncated at the row limit)"
    if not rows:
        text = "The query returned no rows."
    source, citable = context.registry.data(
        title, sql, outcome["columns"], rows, outcome["truncated"], text
    )
    return ToolResult(
        sources=[citable], new_sources=[source], count=len(rows), summary=str(len(rows))
    )


ARTIFACT_NAMES = {
    "image": "figure",
    "chart": "interactive chart",
    "table": "table",
    "map": "map",
    "timeline": "timeline",
    "network": "network",
}


def tool_run_python(arguments: dict, context: ToolContext) -> ToolResult:
    if not context.sandbox.enabled:
        return failure("Code execution is not available in this deployment.")
    code = arguments["code"]
    outcome = context.sandbox.run(code)
    result = ToolResult(count=len(outcome.artifacts))
    shown = []
    for artifact in outcome.artifacts:
        cleaned = clean_artifact(artifact)
        if cleaned and cleaned["type"] == "table":
            cleaned["links"] = context.store.edition_links(
                cell for row in cleaned["rows"] for cell in row
            )
        if cleaned:
            context.show(result, cleaned)
            shown.append(
                f"{ARTIFACT_NAMES[cleaned['type']]} “{cleaned.get('title') or 'untitled'}”"
            )
    output = outcome.output.strip()
    if not outcome.ok:
        text = "\n".join(part for part in (output, outcome.error) if part)
        result.text = text + "\nFix the code and run it again."
        result.is_error = True
        result.summary = outcome.error.splitlines()[-1][:140] if outcome.error else "error"
        return result
    title = arguments.get("title") or ("Berechnung" if context.language == "de" else "Computation")
    limit = 3_000 if context.compact else 10_000
    printed = output if len(output) <= limit else output[:limit] + "\n… (truncated)"
    source, citable = context.registry.computation(title, code, printed)
    result.sources.append(citable)
    result.new_sources.append(source)
    result.text = (
        "Displayed to the user: " + ", ".join(shown) + "."
        if shown
        else "Nothing was displayed to the user."
    )
    result.summary = f"{outcome.duration_s:.1f} s"
    return result


def clean_artifact(artifact: dict) -> dict | None:
    """Only well-formed artifacts of known types reach the client."""
    kind = artifact.get("type")
    title = str(artifact.get("title") or "")[:160]
    if (
        kind == "image"
        and isinstance(artifact.get("data"), str)
        and artifact.get("media_type") in ("image/png", "image/jpeg", "image/svg+xml")
    ):
        return {
            "type": "image",
            "title": title,
            "media_type": artifact["media_type"],
            "data": artifact["data"],
        }
    if kind == "chart" and isinstance(artifact.get("figure"), dict):
        figure = artifact["figure"]
        return {
            "type": "chart",
            "title": title,
            "figure": {"data": figure.get("data", []), "layout": figure.get("layout", {})},
        }
    if kind == "table" and isinstance(artifact.get("rows"), list):
        return {
            "type": "table",
            "title": title,
            "columns": artifact.get("columns", []),
            "rows": artifact["rows"],
            "truncated": bool(artifact.get("truncated")),
        }
    if kind == "map":
        return map_artifact(title, artifact.get("stations", []), artifact.get("connect_route"))
    if kind == "timeline":
        return timeline_artifact(title, artifact.get("events", []))
    if kind == "network":
        return network_artifact(title, artifact.get("nodes", []), artifact.get("edges", []))
    return None


# ----------------------------------------------------------------------
# Visualisations
# ----------------------------------------------------------------------


def short(value, length: int) -> str:
    return str(value or "").strip()[:length]


def link(value) -> str:
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def map_artifact(title: str, stations: list, connect_route) -> dict | None:
    cleaned = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        lat, lon = station.get("lat"), station.get("lon")
        if not (
            isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            continue
        cleaned.append(
            {
                "label": short(station.get("label") or station.get("name"), 120),
                "lat": float(lat),
                "lon": float(lon),
                "date": short(station.get("date"), 40),
                "description": short(station.get("description"), 400),
                "url": link(station.get("url")),
                "group": short(station.get("group"), 60),
            }
        )
    if not cleaned:
        return None
    return {
        "type": "map",
        "title": title,
        "connect_route": bool(connect_route),
        "stations": cleaned[:400],
    }


def date_order(date: str) -> tuple[int, int, int]:
    match = re.search(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", date)
    if not match:
        return (9999, 12, 31)
    return (int(match.group(1)), int(match.group(2) or 6), int(match.group(3) or 15))


def timeline_artifact(title: str, events: list) -> dict | None:
    cleaned = [
        {
            "date": short(event.get("date"), 40),
            "label": short(event.get("label"), 200),
            "description": short(event.get("description"), 500),
            "url": link(event.get("url")),
        }
        for event in events
        if isinstance(event, dict) and event.get("date") and event.get("label")
    ]
    if not cleaned:
        return None
    cleaned.sort(key=lambda event: date_order(event["date"]))
    return {"type": "timeline", "title": title, "events": cleaned[:300]}


def network_artifact(title: str, nodes: list, edges: list) -> dict | None:
    cleaned_nodes, seen = [], set()
    for node in nodes[:150]:
        if not isinstance(node, dict):
            continue
        node_id = short(node.get("id"), 80)
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        try:
            weight = max(0.0, float(node.get("weight", 1)))
        except (TypeError, ValueError):
            weight = 1.0
        cleaned_nodes.append(
            {
                "id": node_id,
                "label": short(node.get("label") or node_id, 80),
                "kind": short(node.get("kind") or node.get("type") or "other", 40),
                "weight": weight,
                "url": link(node.get("url")),
                "description": short(node.get("description"), 200),
                "focus": bool(node.get("focus")),
            }
        )
    cleaned_edges = []
    for edge in edges[:600]:
        if not isinstance(edge, dict):
            continue
        source, target = short(edge.get("source"), 80), short(edge.get("target"), 80)
        if source in seen and target in seen and source != target:
            try:
                weight = max(0.0, float(edge.get("weight", 1)))
            except (TypeError, ValueError):
                weight = 1.0
            cleaned_edges.append(
                {
                    "source": source,
                    "target": target,
                    "weight": weight,
                    "label": short(edge.get("label"), 40),
                }
            )
    if len(cleaned_nodes) < 2:
        return None
    return {"type": "network", "title": title, "nodes": cleaned_nodes, "edges": cleaned_edges}


def tool_create_map(arguments: dict, context: ToolContext) -> ToolResult:
    store = context.store
    stations, unresolved = [], []
    for station in arguments.get("stations") or []:
        if not isinstance(station, dict):
            continue
        name = short(station.get("name"), 120)
        entity = None
        if not isinstance(station.get("lat"), (int, float)):
            candidates = []
            if station.get("entity_id"):
                found = store.entity(str(station["entity_id"]))
                candidates = [found] if found else []
            elif name:
                candidates = store.lookup_entities(name, "place", limit=5)
            entity = next(
                (candidate for candidate in candidates if candidate.get("lat") is not None), None
            )
            if entity is None:
                unresolved.append(name or str(station.get("entity_id", "")))
                continue
        stations.append(
            {
                **station,
                "label": station.get("label") or name or (entity or {}).get("name"),
                "lat": station.get("lat") if entity is None else entity["lat"],
                "lon": station.get("lon") if entity is None else entity["lon"],
                "url": station.get("url") or (entity or {}).get("url", ""),
            }
        )
    artifact = map_artifact(
        short(arguments.get("title"), 160), stations, arguments.get("connect_route", False)
    )
    if artifact is None:
        return failure(
            "No station could be placed. Give lat/lon, or place names or entity ids "
            "from lookup_entity that have coordinates."
            + (f" Unresolved: {', '.join(unresolved[:15])}." if unresolved else "")
        )
    result = ToolResult(count=len(artifact["stations"]), summary=str(len(artifact["stations"])))
    context.show(result, artifact)
    result.text = f"The map with {len(artifact['stations'])} stations is shown to the user." + (
        f" Without coordinates, left out: {', '.join(unresolved[:15])}." if unresolved else ""
    )
    return result


def tool_create_timeline(arguments: dict, context: ToolContext) -> ToolResult:
    artifact = timeline_artifact(short(arguments.get("title"), 160), arguments.get("events") or [])
    if artifact is None:
        return failure("No valid events; each needs a date and a label.")
    result = ToolResult(count=len(artifact["events"]), summary=str(len(artifact["events"])))
    context.show(result, artifact)
    result.text = f"The timeline with {len(artifact['events'])} events is shown to the user."
    return result


def tool_create_network(arguments: dict, context: ToolContext) -> ToolResult:
    title = short(arguments.get("title"), 160)
    text = ""
    if arguments.get("nodes"):
        artifact = network_artifact(title, arguments["nodes"], arguments.get("edges") or [])
    else:
        entity = resolve_entity(context.store, arguments.get("focus", ""))
        if entity is None:
            return failure(
                "Give either focus (an entity id or name) for a co-occurrence "
                "network, or your own nodes and edges."
            )
        related = context.store.cooccurring(
            entity["entity_id"],
            kinds=arguments.get("kinds"),
            limit=min(arguments.get("max_nodes") or 20, 40),
            min_shared=max(1, arguments.get("min_shared") or 2),
        )
        if not related["neighbours"]:
            return failure(f"No entities are annotated together with {entity['name']}.")
        nodes = [
            {
                "id": entity["entity_id"],
                "label": entity["name"],
                "kind": entity["kind"],
                "weight": entity["mention_count"],
                "url": entity["url"],
                "focus": True,
            }
        ]
        nodes += [
            {
                "id": row["entity_id"],
                "label": row["name"],
                "kind": row["kind"],
                "weight": row["shared"],
                "url": row["url"],
            }
            for row in related["neighbours"]
        ]
        edges = [
            {"source": entity["entity_id"], "target": row["entity_id"], "weight": row["shared"]}
            for row in related["neighbours"]
        ]
        edges += [
            {"source": row["source"], "target": row["target"], "weight": row["shared"]}
            for row in related["links"]
        ]
        artifact = network_artifact(title or entity["name"], nodes, edges)
        strongest = "; ".join(
            f"{row['name']} ({row['shared']})" for row in related["neighbours"][:12]
        )
        text = (
            f" Edges count paragraphs in which both are annotated. Closest to "
            f"{entity['name']}: {strongest}."
        )
    if artifact is None:
        return failure("A network needs at least two nodes with id and label.")
    result = ToolResult(count=len(artifact["nodes"]), summary=str(len(artifact["nodes"])))
    context.show(result, artifact)
    result.text = (
        f"The network with {len(artifact['nodes'])} nodes and "
        f"{len(artifact['edges'])} edges is shown to the user." + text
    )
    return result


def tool_wikipedia_context(arguments: dict, context: ToolContext) -> ToolResult:
    if context.wikipedia_calls >= MAX_WIKIPEDIA_CALLS:
        return failure("Wikipedia budget for this answer is used up.")
    context.wikipedia_calls += 1
    info = wikipedia.get_context(
        term=arguments.get("term", ""),
        gnd_id=arguments.get("gnd_id") or None,
        geonames_id=arguments.get("geonames_id") or None,
        lang=arguments.get("lang") or context.language,
        include_image=bool(arguments.get("include_image")),
    )
    if info is None:
        return ToolResult(
            text="No matching Wikipedia article (or Wikipedia is unreachable).", summary="0"
        )
    source, citable = context.registry.wikipedia(info)
    result = ToolResult(sources=[citable], new_sources=[source], count=1, summary=info["title"])
    image = info.get("image")
    if image and image.get("url"):
        context.show(
            result,
            {
                "type": "image",
                "title": info["title"],
                "url": image["url"],
                "attribution": {
                    key: image.get(key, "")
                    for key in ("artist", "license", "license_url", "file_page", "public_domain")
                },
            },
        )
        result.text = "The image is shown to the user with its licence."
    return result


# ----------------------------------------------------------------------
# Tool definitions
# ----------------------------------------------------------------------

DOC_TYPE_LIST = string_list("Restrict to these document types.", list(DOC_TYPES))
DATE_FROM = described(STRING, "ISO date or prefix, e.g. 1801 or 1801-03 or 1801-03-14.")
DATE_TO = described(STRING, "End of the range (defaults to date_from).")

TOOLS = [
    Tool(
        "search",
        "Ranked full-text search over the whole edition: Humboldt's journals, letters and "
        "documents, the chronology, the research articles, and the editors' notes. "
        'Diacritic- and case-insensitive. Use keywords, not sentences; "quoted phrases" and '
        "prefix* are supported. Passages containing more of the terms rank first. The sources "
        "are in German, French, Spanish, Latin and English as written around 1800, so search "
        "in the language and spelling of the sources (Crocodile, cocodrilo, Caiman) and run "
        "several searches rather than one long one. Returns passages with document id, page "
        "and character offsets.",
        {
            "query": described(STRING, 'Keywords, "phrases", prefix*.'),
            "doc_types": DOC_TYPE_LIST,
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "doc_id": described(STRING, "Search inside this one document."),
            "limit": described(INTEGER, "Passages to return (default 8, max 20)."),
        },
        tool_search,
        required=("query",),
    ),
    Tool(
        "grep",
        "Regular-expression search over the complete texts (Python regex syntax, "
        "case-insensitive unless case_sensitive). For exact word forms, numbers with units, "
        "patterns. Returns every document's match count and the first matches in context.",
        {
            "pattern": described(STRING, "Regular expression, e.g. \\b\\d{3,4}\\s*toises."),
            "doc_types": DOC_TYPE_LIST,
            "doc_id": described(STRING, "Search inside this one document."),
            "limit": described(INTEGER, "Matches to show in context (default 15, max 40)."),
            "case_sensitive": BOOLEAN,
        },
        tool_grep,
        required=("pattern",),
    ),
    Tool(
        "read_document",
        "Read a document itself: all of it, a page range, or onward from a character offset. "
        "Long documents come in parts; the result says how to continue and, on the first "
        "read, lists the document's pages and section headings. Editorial notes anchored in "
        "the part you read are attached.",
        {
            "doc_id": described(STRING, "Document id, e.g. H0019734."),
            "page_from": described(STRING, "First page or leaf, e.g. 56v."),
            "page_to": described(STRING, "Last page (defaults to page_from)."),
            "start": described(INTEGER, "Character offset to read from."),
            "max_chars": described(INTEGER, "Characters to read (default 30000, max 150000)."),
        },
        tool_read_document,
        required=("doc_id",),
    ),
    Tool(
        "list_documents",
        "List documents by metadata: type, date range, correspondent, title. For browsing "
        "what exists (all letters to one person, the documents of a year).",
        {
            "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "person": described(STRING, "Sender or recipient: entity id or part of the name."),
            "title_contains": STRING,
            "limit": described(INTEGER, "Default 30, max 100."),
            "offset": INTEGER,
        },
        tool_list_documents,
    ),
    Tool(
        "lookup_entity",
        "Look up a person, place, institution, plant, glossary term or siglum in the "
        "edition's registers. Tolerant of spelling; returns the register entries with ids, "
        "dates, description, coordinates and how often each is annotated in the texts.",
        {
            "name": described(STRING, "Name or term, or an entity id."),
            "kind": {"type": "string", "enum": ENTITY_KINDS},
        },
        tool_lookup_entity,
        required=("name",),
    ),
    Tool(
        "entity_mentions",
        "Where the editors annotated an entity in the texts, whatever the spelling there: "
        "counts per document and the passages around the mentions. More precise than "
        "full-text search for people, places, institutions and plants.",
        {
            "entity_id": described(STRING, "Entity id from lookup_entity, e.g. H0003476."),
            "doc_types": DOC_TYPE_LIST,
            "doc_id": described(STRING, "Only mentions in this document."),
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "limit": described(INTEGER, "Passages to return (default 8, max 25)."),
            "offset": described(INTEGER, "Skip this many passages (to page through)."),
        },
        tool_entity_mentions,
        required=("entity_id",),
    ),
    Tool(
        "chronology",
        "Entries of the edition's Humboldt chronology in a date range, with the places "
        "annotated in each. The first stop for where Humboldt was and what he did when, and "
        "for collecting the stations of a journey.",
        {
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "limit": described(INTEGER, "Default 30, max 80."),
        },
        tool_chronology,
        required=("date_from",),
    ),
    Tool(
        "query_data",
        "Run one read-only SQL SELECT (SQLite) on the corpus database described in the "
        "system prompt. For counting, ranking and joining: letters per correspondent or "
        "year, measurements by unit, mentions per document. The result is shown to the user "
        "as a citable table in which document ids, entity ids, exact document titles and "
        "register names link to the edition, so select doc_id or entity_id (or the title or "
        "name) for every row that stands for a document or an entity.",
        {
            "sql": described(STRING, "A single SELECT statement."),
            "title": described(STRING, "Short title for the result, in the user's language."),
        },
        tool_query_data,
        required=("sql",),
    ),
    Tool(
        "run_python",
        "Run Python in a sandbox for analysis and for charts. Available: the ehd module "
        "(see system prompt), pandas as pd, numpy as np, matplotlib.pyplot as plt, plotly, "
        "networkx. No network. Each call starts fresh, so put everything in one script. "
        "Show results with ehd.show(...); print() what you need to read yourself.",
        {
            "code": described(STRING, "The complete script."),
            "title": described(STRING, "Short title for the computation, in the user's language."),
        },
        tool_run_python,
        required=("code",),
        long_input=True,
    ),
    Tool(
        "create_map",
        "Show the user an interactive map. Give stations in order, each with a place name or "
        "entity id (coordinates come from the register) or with lat and lon.",
        {
            "title": STRING,
            "stations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": described(STRING, "Place name as in the register."),
                        "entity_id": STRING,
                        "lat": NUMBER,
                        "lon": NUMBER,
                        "label": described(STRING, "Marker label (defaults to the name)."),
                        "date": STRING,
                        "description": STRING,
                        "url": STRING,
                        "group": described(STRING, "Stations with the same group share a colour."),
                    },
                },
            },
            "connect_route": described(BOOLEAN, "Draw a line through the stations in order."),
        },
        tool_create_map,
        required=("title", "stations"),
    ),
    Tool(
        "create_timeline",
        "Show the user a timeline of dated events.",
        {
            "title": STRING,
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": described(STRING, "ISO date, may be partial (1805, 1805-03)."),
                        "label": STRING,
                        "description": STRING,
                        "url": STRING,
                    },
                    "required": ["date", "label"],
                },
            },
        },
        tool_create_timeline,
        required=("title", "events"),
    ),
    Tool(
        "create_network",
        "Show the user a network graph. Either give focus (an entity) to get the entities "
        "annotated in the same paragraphs as it, or give your own nodes and edges, e.g. a "
        "correspondence network built from query_data results.",
        {
            "title": STRING,
            "focus": described(STRING, "Entity id or name for a co-occurrence network."),
            "kinds": string_list("Co-occurrence: only neighbours of these kinds.", ENTITY_KINDS),
            "max_nodes": described(
                INTEGER, "Co-occurrence: neighbours to show (default 20, max 40)."
            ),
            "min_shared": described(
                INTEGER, "Co-occurrence: minimum shared paragraphs (default 2)."
            ),
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": STRING,
                        "label": STRING,
                        "kind": described(STRING, "Groups nodes by colour, e.g. person, place."),
                        "weight": NUMBER,
                        "url": STRING,
                        "description": STRING,
                        "focus": BOOLEAN,
                    },
                    "required": ["id", "label"],
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": STRING,
                        "target": STRING,
                        "weight": NUMBER,
                        "label": STRING,
                    },
                    "required": ["source", "target"],
                },
            },
        },
        tool_create_network,
    ),
]

WIKIPEDIA_TOOL = Tool(
    "wikipedia_context",
    "A short Wikipedia summary (German or English) as outside context, optionally with an "
    "image from Wikimedia Commons. Pass the gnd or geonames id from lookup_entity when you "
    "have one; it identifies the article exactly. Wikipedia is not part of the edition.",
    {
        "term": described(STRING, "Name or term."),
        "gnd_id": STRING,
        "geonames_id": STRING,
        "lang": {"type": "string", "enum": ["de", "en"]},
        "include_image": described(BOOLEAN, "Show the article's main image to the user."),
    },
    tool_wikipedia_context,
    required=("term",),
)

COUNTED = {
    "search": (("Passage", "Passagen"), ("passage", "passages")),
    "grep": (("Treffer", "Treffer"), ("match", "matches")),
    "read_document": (
        ("Zeichen gelesen", "Zeichen gelesen"),
        ("character read", "characters read"),
    ),
    "list_documents": (("Dokument", "Dokumente"), ("document", "documents")),
    "entity_mentions": (
        ("annotierte Erwähnung", "annotierte Erwähnungen"),
        ("annotated mention", "annotated mentions"),
    ),
    "chronology": (("Eintrag", "Einträge"), ("entry", "entries")),
    "query_data": (("Zeile", "Zeilen"), ("row", "rows")),
    "create_map": (("Station", "Stationen"), ("station", "stations")),
    "create_timeline": (("Ereignis", "Ereignisse"), ("event", "events")),
    "create_network": (("Knoten", "Knoten"), ("node", "nodes")),
}
NOTHING = ("nichts gefunden", "nothing found")


def counted(tool_name: str, count: int, language: str) -> str:
    english = language == "en"
    if not count:
        return NOTHING[english]
    return f"{count} {COUNTED[tool_name][english][count != 1]}"


TYPE_CHECKS = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}

# Names models use for a parameter instead of the declared one, in the order
# of the declared names they may stand for. An alias only applies to a tool
# that declares the target and does not declare the alias itself.
ALIASES: dict[str, tuple[str, ...]] = {
    "q": ("query", "pattern", "sql", "name", "term"),
    "query": ("pattern", "sql", "name", "term"),
    "text": ("query", "pattern"),
    "keywords": ("query",),
    "search": ("query",),
    "regex": ("pattern",),
    "regexp": ("pattern",),
    "expression": ("pattern",),
    "statement": ("sql",),
    "source": ("code",),
    "script": ("code",),
    "python": ("code",),
    "top_k": ("limit",),
    "k": ("limit",),
    "n": ("limit",),
    "max_results": ("limit",),
    "max_passages": ("limit",),
    "max": ("limit",),
    "count": ("limit",),
    "doc_type": ("doc_types",),
    "doc_types": ("doc_type",),
    "document_type": ("doc_types", "doc_type"),
    "document_types": ("doc_types", "doc_type"),
    "type": ("doc_types", "doc_type", "kind"),
    "types": ("doc_types", "kinds"),
    "kinds": ("kind",),
    "entity_type": ("kind", "kinds"),
    "entity_types": ("kinds", "kind"),
    "entity_kind": ("kind",),
    "start_date": ("date_from",),
    "end_date": ("date_to",),
    "from_date": ("date_from",),
    "to_date": ("date_to",),
    "from": ("date_from",),
    "to": ("date_to",),
    "date": ("date_from",),
    "year": ("date_from",),
    "since": ("date_from",),
    "until": ("date_to",),
    "document_id": ("doc_id",),
    "document": ("doc_id",),
    "id": ("doc_id", "entity_id", "name"),
    "humboldt_id": ("entity_id", "focus", "name"),
    "entity": ("entity_id", "focus", "name"),
    "entity_name": ("name", "focus"),
    "person": ("name", "focus"),
    "place": ("name", "focus"),
    "page": ("page_from",),
    "from_page": ("page_from",),
    "to_page": ("page_to",),
    "folio": ("page_from",),
    "offset": ("start",),
    "position": ("start",),
    "length": ("max_chars",),
    "chars": ("max_chars",),
    "max_length": ("max_chars",),
    "correspondent": ("person",),
    "title": ("title_contains",),
    "language": ("lang",),
    "name": ("term", "focus"),
    "places": ("stations",),
    "points": ("stations",),
    "locations": ("stations",),
    "items": ("events", "stations"),
    "connect": ("connect_route",),
    "route": ("connect_route",),
    "include_picture": ("include_image",),
    "image": ("include_image",),
}

# Spellings models use for the fixed choices.
CHOICE_SYNONYMS = {
    "letters": "letter",
    "brief": "letter",
    "briefe": "letter",
    "correspondence": "letter",
    "diaries": "diary",
    "journal": "diary",
    "journals": "diary",
    "travel_journal": "diary",
    "travel-journal": "diary",
    "travel journal": "diary",
    "tagebuch": "diary",
    "tagebücher": "diary",
    "documents": "document",
    "dokument": "document",
    "dokumente": "document",
    "chronik": "chronology",
    "chronologie": "chronology",
    "chronology_entry": "chronology",
    "research-article": "research_article",
    "research_articles": "research_article",
    "research article": "research_article",
    "article": "research_article",
    "articles": "research_article",
    "forschungsbeitrag": "research_article",
    "forschungsbeiträge": "research_article",
    "persons": "person",
    "people": "person",
    "personen": "person",
    "places": "place",
    "ort": "place",
    "orte": "place",
    "institutions": "institution",
    "institutionen": "institution",
    "plants": "plant",
    "pflanze": "plant",
    "pflanzen": "plant",
    "glossar": "glossary",
    "term": "glossary",
    "sigla": "siglum",
    "sigle": "siglum",
    "german": "de",
    "deutsch": "de",
    "english": "en",
    "englisch": "en",
}


def normalised_choice(value, choices: list[str]) -> str | None:
    text = str(value).strip().lower().replace("-", "_")
    for candidate in (text, CHOICE_SYNONYMS.get(text), CHOICE_SYNONYMS.get(text.replace("_", "-"))):
        if candidate in choices:
            return candidate
    return None


def aliased(tool: Tool, call_input: dict) -> tuple[dict, list[str]]:
    """Rename parameters the model gave under another name; report the rest."""
    arguments: dict = {}
    unknown: list[str] = []
    for name, value in call_input.items():
        if name in tool.properties:
            arguments.setdefault(name, value)
            continue
        target = next(
            (
                candidate
                for candidate in ALIASES.get(name.lower(), ())
                if candidate in tool.properties and candidate not in call_input
            ),
            None,
        )
        if target and target not in arguments:
            arguments[target] = value
        else:
            unknown.append(name)
    return arguments, unknown


def coerced(value, schema: dict):
    """Models sometimes send numbers as strings or arrays as JSON text."""
    kind = schema.get("type")
    if isinstance(value, str):
        text = value.strip()
        if kind in ("array", "object"):
            try:
                value = json.loads(text)
            except ValueError:
                return value
        elif kind == "integer" and re.fullmatch(r"-?\d+", text):
            return int(text)
        elif kind == "number":
            try:
                return float(text)
            except ValueError:
                return value
        elif kind == "boolean" and text.lower() in ("true", "false"):
            return text.lower() == "true"
    if kind == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if kind == "array" and isinstance(value, str | int | float):
        return [value]
    return value


class ToolBox:
    def __init__(self, python: bool, wikipedia: bool):
        self.tools = {tool.name: tool for tool in TOOLS if python or tool.name != "run_python"}
        if wikipedia:
            self.tools[WIKIPEDIA_TOOL.name] = WIKIPEDIA_TOOL

    @property
    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools.values()]

    def find(self, name: str) -> Tool | None:
        if name in self.tools:
            return self.tools[name]
        lowered = {tool_name.lower(): tool for tool_name, tool in self.tools.items()}
        return lowered.get((name or "").lower())

    def run(self, call: ToolCall, context: ToolContext) -> ToolResult:
        tool = self.find(call.name)
        if tool is None:
            return failure(f"Unknown tool {call.name!r}. Available: {', '.join(self.tools)}.")
        if call.invalid_input:
            return failure(
                f"The input for {tool.name} was not usable ({call.invalid_input}); "
                "send it again as one valid JSON object."
            )
        given, unknown = aliased(tool, call.input)
        arguments = {}
        hints = []
        for name, value in given.items():
            schema = tool.properties[name]
            if value is None or value == "":
                continue
            value = coerced(value, schema)
            expected = TYPE_CHECKS.get(schema.get("type"))
            if expected and (
                not isinstance(value, expected)
                or (schema.get("type") in ("integer", "number") and isinstance(value, bool))
            ):
                return failure(f"{tool.name}: {name} must be of type {schema['type']}.")
            choices = schema.get("enum") or schema.get("items", {}).get("enum")
            if choices:
                if isinstance(value, list):
                    kept = [normalised_choice(item, choices) for item in value]
                    dropped = [
                        str(item) for item, ok in zip(value, kept, strict=True) if ok is None
                    ]
                    value = list(dict.fromkeys(item for item in kept if item))
                    if dropped:
                        hints.append(
                            f"Ignored unknown {name} value(s) {', '.join(dropped)}; "
                            f"allowed: {', '.join(choices)}."
                        )
                    if not value:
                        continue
                else:
                    value = normalised_choice(value, choices)
                    if value is None:
                        return failure(f"{tool.name}: {name} must be one of {', '.join(choices)}.")
            arguments[name] = value
        missing = [name for name in tool.required if name not in arguments]
        if missing:
            received = f" (received: {', '.join(unknown)})" if unknown else ""
            return failure(
                f"{tool.name}: missing required {', '.join(missing)}{received}. "
                f"Parameters: {', '.join(tool.properties)}."
            )
        if unknown:
            hints.append(
                f"Ignored unknown parameter(s) {', '.join(unknown)}; this tool takes "
                f"{', '.join(tool.properties)}."
            )
        try:
            result = tool.handler(arguments, context)
        except StoreError as error:
            return failure(str(error))
        if not result.is_error and tool.name in COUNTED:
            result.summary = counted(tool.name, result.count, context.language)
        if hints and not result.is_error:
            result.text = "\n".join(part for part in (result.text, *hints) if part)
        return result
