"""
Agentic retrieval loop: Claude with structured lexical tools.

Instead of one-shot retrieve-then-generate, the model drives retrieval through
tools built from the edition's own annotations (entity registry, BM25 over
text chunks, regex grep, context expansion). Hard budget of MAX_TOOL_ROUNDS
tool rounds, after which the model is forced to answer from what it has.

Every chunk that passes through a tool result gets a stable [Quelle N] number,
so citations in the final answer are directly resolvable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
SEARCH_TEXT_CHARS = 700
CONTEXT_TEXT_CHARS = 1400

SYSTEM_PROMPT = """Du bist der Rechercheassistent der **edition humboldt digital** \
(hg. v. Ottmar Ette, Berlin-Brandenburgische Akademie der Wissenschaften) — der digitalen \
Edition der Reisetagebücher, Briefe, Dokumente und Chronologie Alexander von Humboldts.

## Arbeitsweise
Du beantwortest Fragen, indem du gezielt in der Edition recherchierst. Dir stehen Werkzeuge \
zur Verfügung:
- `lookup_entity`: Personen, Orte, Institutionen, Pflanzen und Glossarbegriffe im Register \
  nachschlagen (normalisierte Namen — unempfindlich gegen historische Schreibvarianten).
- `chunks_for_entity`: alle Textstellen finden, an denen eine Registerentität von den \
  Herausgebern annotiert wurde (präziser als Volltextsuche!).
- `search`: BM25-Volltextsuche über die Textpassagen (Tagebücher, Briefe, Dokumente, \
  Chronologie, Forschungsbeiträge, editorische Kommentare) — mit optionalen Filtern für \
  Dokumenttyp und Datum.
- `grep`: Regex-Suche für exakte Wortformen, Messwerte, Zahlen, Einheiten.
- `get_context`: Nachbarpassagen und Umfeld einer Fundstelle laden.

Empfohlene Strategie:
1. Bei Fragen zu Personen/Orten: zuerst `lookup_entity`, dann `chunks_for_entity`.
2. Bei thematischen Fragen: `search`, ggf. mit Datums-/Typfiltern.
3. Bei Fragen zu Messungen oder exakten Formulierungen: `grep`.
4. Historische Schreibweisen beachten: Die Texte sind in Deutsch, Französisch und Spanisch \
   des 18./19. Jahrhunderts verfasst. Probiere bei `search`/`grep` auch zeitgenössische \
   Varianten (z. B. "Crocodile"/"Cocodrilo" statt "Krokodil").
Du hast maximal {max_rounds} Werkzeugrunden — recherchiere gezielt, nicht erschöpfend.

## Antwortregeln
- Jede faktische Aussage MUSS mit [Quelle N] belegt werden — N ist die quelle_nr aus den \
  Werkzeugergebnissen. Zitiere nur Quellen, die du tatsächlich gesehen hast.
- Unterscheide klar zwischen Humboldts eigenem Text und editorischen Kommentaren \
  ("Laut Anmerkung der Herausgeber ...", Quellen vom Typ editorial_note).
- Wenn die Recherche nichts Belastbares ergibt, sage das ehrlich.
- Antworte in der Sprache der Frage (Deutsch, Englisch oder Französisch).
- Antworte konkret, mit Namen, Daten, Orten und Messwerten aus den Quellen.
- Erfinde keine Quellenangaben und keine Inhalte."""


TOOLS = [
    {
        "name": "lookup_entity",
        "description": (
            "Findet Personen, Orte, Institutionen, Pflanzen oder Glossarbegriffe im "
            "Register der Edition. Fuzzy-Namenssuche, unempfindlich gegen Schreibvarianten. "
            "Liefert normalisierte Namen, Lebensdaten/Beschreibung, IDs (humboldt_id, GND) "
            "und die Zahl annotierter Erwähnungen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name oder Begriff, z. B. 'Bonpland' oder 'Chimborazo'"},
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "place", "institution", "glossary", "plants"],
                    "description": "Optional: auf einen Registertyp einschränken",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "chunks_for_entity",
        "description": (
            "Liefert Textpassagen, in denen eine Registerentität (per humboldt_id aus "
            "lookup_entity) von den Herausgebern annotiert wurde. Spellingunabhängig und "
            "präziser als Volltextsuche."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "humboldt_id": {"type": "string", "description": "ID aus lookup_entity, z. B. 'H0002222'"},
                "limit": {"type": "integer", "description": "Max. Passagen (Standard 6)"},
            },
            "required": ["humboldt_id"],
        },
    },
    {
        "name": "search",
        "description": (
            "BM25-Volltextsuche über die Textpassagen der Edition (keine Registereinträge). "
            "Optional nach Dokumenttyp und ISO-Datum gefiltert."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchbegriffe (Stichworte, keine ganzen Sätze)"},
                "doc_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["diary", "letter", "document", "chronology",
                                 "research_article", "editorial_note"],
                    },
                    "description": "Optional: nur diese Dokumenttypen",
                },
                "date_from": {"type": "string", "description": "Optional: ISO-Datum, z. B. '1801-03'"},
                "date_to": {"type": "string", "description": "Optional: ISO-Datum"},
                "top_k": {"type": "integer", "description": "Max. Treffer (Standard 8)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Regex-Suche (case-insensitive) über den regularisierten Text — für exakte "
            "Wortformen, Messwerte, Einheiten (z. B. 'toises', '3[0-9]{3}'). Kein Ranking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regulärer Ausdruck"},
                "doc_types": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional: nur diese Dokumenttypen",
                },
                "limit": {"type": "integer", "description": "Max. Treffer (Standard 12)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "get_context",
        "description": "Lädt Umfeld einer Fundstelle: vollständiger Text, Nachbarpassagen, editorische Anmerkungen.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "chunk_id aus einem früheren Ergebnis"},
            },
            "required": ["chunk_id"],
        },
    },
]


class AgenticAnswerer:
    """Claude tool loop over the lexical retrieval primitives."""

    def __init__(self, retriever, entity_index, api_key: str | None = None,
                 model: str | None = None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        # Haiku by default: the public widget optimizes for cost/latency;
        # stronger models are selectable per request (see main.ALLOWED_MODELS).
        self.model = model or os.environ.get("LLM_MODEL", "claude-haiku-4-5")
        self.retriever = retriever
        self.entity_index = entity_index

    # =========================================================================
    # Public API
    # =========================================================================

    def generate(self, query: str, max_tokens: int = 3000, model: str | None = None) -> dict:
        """Run the loop to completion and return the final answer dict."""
        final = {}
        for event in self.iter_events(query, max_tokens=max_tokens, model=model):
            if event["type"] == "answer":
                final = event["data"]
        return final

    def iter_events(self, query: str, max_tokens: int = 3000, model: str | None = None):
        """
        Run the agent loop, yielding progress events as they happen:
          {"type": "tool_call",   "round": n, "tool": ..., "input": {...}}
          {"type": "tool_result", "tool": ..., "n": ..., "preview": [labels]}
          {"type": "answer",      "data": {answer, sources, citations, model, trace, elapsed_s}}
        """
        t0 = time.time()
        active_model = model or self.model
        sources: dict[str, int] = {}   # chunk_id -> quelle_nr
        source_meta: dict[int, dict] = {}
        trace: list[dict] = []

        messages = [{"role": "user", "content": query}]
        system = SYSTEM_PROMPT.format(max_rounds=MAX_TOOL_ROUNDS)

        response = None
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            force_answer = round_no == MAX_TOOL_ROUNDS
            kwargs = dict(
                model=active_model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=TOOLS,
            )
            if force_answer:
                kwargs["tool_choice"] = {"type": "none"}
                messages.append({
                    "role": "user",
                    "content": "Werkzeugbudget erschöpft. Beantworte die Frage jetzt "
                               "auf Basis der bisherigen Ergebnisse (mit [Quelle N]).",
                })
                kwargs["messages"] = messages

            response = self.client.messages.create(**kwargs)

            if response.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield {
                    "type": "tool_call",
                    "round": round_no + 1,
                    "tool": block.name,
                    "input": block.input,
                }
                result = self._dispatch(block.name, block.input, sources, source_meta)
                trace.append({
                    "round": round_no + 1,
                    "tool": block.name,
                    "input": block.input,
                    "n_results": result.get("n", 0),
                })
                yield {
                    "type": "tool_result",
                    "tool": block.name,
                    "n": result.get("n", 0),
                    "preview": self._result_preview(result["payload"]),
                }
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result["payload"], ensure_ascii=False),
                })
            messages.append({"role": "user", "content": tool_results})

        answer_text = "".join(
            b.text for b in response.content if b.type == "text"
        ).strip() if response else ""

        cited = self._extract_citations(answer_text, source_meta)
        elapsed = round(time.time() - t0, 2)
        logger.info(
            f"Agentic answer in {elapsed}s, {len(trace)} tool calls, "
            f"{len(cited)} cited sources. Trace: "
            + "; ".join(f"{t['tool']}({json.dumps(t['input'], ensure_ascii=False)})→{t['n_results']}" for t in trace)
        )

        yield {
            "type": "answer",
            "data": {
                "answer": answer_text,
                "sources": cited or list(source_meta.values())[:8],
                "citations": [{"index": s["index"], "citation_label": s["citation_label"],
                               "url": s["url"]} for s in cited],
                "model": active_model,
                "trace": trace,
                "elapsed_s": elapsed,
            },
        }

    @staticmethod
    def _result_preview(payload, limit: int = 3) -> list[str]:
        """Short human-readable labels of what a tool returned."""
        if isinstance(payload, dict):
            if "error" in payload:
                return [payload["error"][:80]]
            return [payload.get("citation_label", "")][:1]
        labels = []
        for item in payload[:limit]:
            if not isinstance(item, dict):
                continue
            label = item.get("citation_label") or item.get("name") or ""
            if label:
                labels.append(label)
        return labels

    # =========================================================================
    # Tool dispatch
    # =========================================================================

    def _dispatch(self, name: str, tool_input: dict, sources: dict, source_meta: dict) -> dict:
        try:
            if name == "lookup_entity":
                payload = self._tool_lookup_entity(tool_input, sources, source_meta)
            elif name == "chunks_for_entity":
                payload = self._tool_chunks_for_entity(tool_input, sources, source_meta)
            elif name == "search":
                payload = self._tool_search(tool_input, sources, source_meta)
            elif name == "grep":
                payload = self._tool_grep(tool_input, sources, source_meta)
            elif name == "get_context":
                payload = self._tool_get_context(tool_input, sources, source_meta)
            else:
                payload = {"error": f"Unbekanntes Werkzeug: {name}"}
        except Exception as e:
            logger.exception(f"Tool {name} failed")
            payload = {"error": str(e)}
        n = len(payload) if isinstance(payload, list) else (0 if "error" in payload else 1)
        return {"payload": payload, "n": n}

    def _register_source(self, chunk_id: str, sources: dict, source_meta: dict) -> int:
        if chunk_id in sources:
            return sources[chunk_id]
        nr = len(sources) + 1
        sources[chunk_id] = nr
        chunk = self.retriever.chunk_store.get(chunk_id)
        source_meta[nr] = {
            "index": nr,
            "chunk_id": chunk_id,
            "citation_label": chunk.citation_label if chunk else chunk_id,
            "url": chunk.url if chunk else "",
            "doc_type": chunk.doc_type if chunk else "",
        }
        return nr

    def _chunk_payload(self, chunk_id: str, sources: dict, source_meta: dict,
                       text_chars: int = SEARCH_TEXT_CHARS, score: float | None = None) -> dict | None:
        chunk = self.retriever.chunk_store.get(chunk_id)
        if chunk is None:
            return None
        nr = self._register_source(chunk_id, sources, source_meta)
        out = {
            "quelle_nr": nr,
            "chunk_id": chunk_id,
            "citation_label": chunk.citation_label,
            "doc_type": chunk.doc_type,
            "date": chunk.date_iso,
            "url": chunk.url,
            "text": chunk.text_regularized[:text_chars],
        }
        if chunk.editorial_notes:
            out["editorische_anmerkungen"] = [n[:300] for n in chunk.editorial_notes[:3]]
        if score is not None:
            out["score"] = round(score, 3)
        return out

    def _tool_lookup_entity(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        hits = self.entity_index.lookup(
            tool_input.get("name", ""),
            entity_type=tool_input.get("entity_type"),
        )
        if not hits:
            return {"error": "Keine Registereinträge gefunden. Versuche eine andere Schreibweise oder search."}
        out = []
        for h in hits:
            item = {
                "humboldt_id": h["id"],
                "name": h["name"],
                "type": h["type"],
                "beschreibung": h["description"][:400],
                "gnd": h["gnd"],
                "mention_count": h["mention_count"],
                "url": h["url"],
            }
            # register entries are citable sources too — give them a quelle_nr
            if h.get("register_chunk_id"):
                item["quelle_nr"] = self._register_source(
                    h["register_chunk_id"], sources, source_meta
                )
            out.append(item)
        return out

    def _tool_chunks_for_entity(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        hid = tool_input.get("humboldt_id", "")
        limit = int(tool_input.get("limit") or 6)
        chunk_ids = self.entity_index.chunks_for(hid)
        if not chunk_ids:
            return {"error": f"Keine annotierten Textstellen für {hid}."}
        payloads = []
        for cid in chunk_ids[:limit]:
            p = self._chunk_payload(cid, sources, source_meta)
            if p:
                payloads.append(p)
        if len(chunk_ids) > limit:
            payloads.append({"hinweis": f"{len(chunk_ids) - limit} weitere Textstellen vorhanden."})
        return payloads

    def _tool_search(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        filters = {}
        if tool_input.get("doc_types"):
            filters["doc_types"] = tool_input["doc_types"]
        if tool_input.get("date_from"):
            filters["date_from"] = tool_input["date_from"]
        if tool_input.get("date_to"):
            filters["date_to"] = tool_input["date_to"]
        results = self.retriever.retrieve(
            query=tool_input.get("query", ""),
            top_k=int(tool_input.get("top_k") or 8),
            filters=filters or None,
        )
        if not results:
            return {"error": "Keine Treffer. Versuche andere Suchbegriffe oder Schreibvarianten."}
        return [
            p for r in results
            if (p := self._chunk_payload(r.chunk_id, sources, source_meta, score=r.fused_score))
        ]

    def _tool_grep(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        matches = self.retriever.grep(
            pattern=tool_input.get("pattern", ""),
            doc_types=tool_input.get("doc_types"),
            limit=int(tool_input.get("limit") or 12),
        )
        if matches and "error" in matches[0]:
            return matches[0]
        if not matches:
            return {"error": "Keine Regex-Treffer."}
        out = []
        for m in matches:
            nr = self._register_source(m["chunk_id"], sources, source_meta)
            out.append({
                "quelle_nr": nr,
                "chunk_id": m["chunk_id"],
                "citation_label": m["citation_label"],
                "doc_type": m["doc_type"],
                "date": m["date_iso"],
                "url": m["url"],
                "match": m["match"],
            })
        return out

    def _tool_get_context(self, tool_input: dict, sources: dict, source_meta: dict) -> dict:
        ctx = self.retriever.get_context(tool_input.get("chunk_id", ""))
        if "error" in ctx:
            return ctx
        nr = self._register_source(ctx["chunk_id"], sources, source_meta)
        ctx["quelle_nr"] = nr
        ctx["text"] = ctx["text"][:CONTEXT_TEXT_CHARS]
        return ctx

    # =========================================================================
    # Citations
    # =========================================================================

    @staticmethod
    def _extract_citations(answer: str, source_meta: dict[int, dict]) -> list[dict]:
        cited_nrs = []
        for m in re.finditer(r"\[Quelle[n]?\s+([\d,\s und]+)\]", answer):
            for num in re.findall(r"\d+", m.group(1)):
                nr = int(num)
                if nr in source_meta and nr not in cited_nrs:
                    cited_nrs.append(nr)
        return [source_meta[nr] for nr in cited_nrs]
