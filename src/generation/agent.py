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

MAX_TOOL_ROUNDS = 6
SEARCH_TEXT_CHARS = 700
CONTEXT_TEXT_CHARS = 1400

# Server-side code execution: only offered on Sonnet/Opus (availability on
# Haiku unverified); disable entirely with CODE_EXECUTION=0.
CODE_EXECUTION_TOOL = {"type": "code_execution_20260521", "name": "code_execution"}


def _code_execution_enabled(model: str) -> bool:
    if os.environ.get("CODE_EXECUTION", "1") == "0":
        return False
    return model.startswith(("claude-sonnet", "claude-opus"))

SYSTEM_PROMPT = """Du bist Alex, der Rechercheassistent der **edition humboldt digital** \
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

Außerdem:
- `where_was_humboldt`: Aufenthaltsorte in einem Datumsbereich (aus der Chronologie der \
  Edition) — das bevorzugte Werkzeug für "Wo war Humboldt am/im …?" und zum Sammeln von \
  Reisestationen.
- `create_map`: erzeugt eine interaktive Karte, die dem Nutzer angezeigt wird. Übergib \
  Stationen in Reihenfolge (Ortsname wie im Register); Koordinaten werden automatisch \
  aufgelöst. Nutze es IMMER, wenn nach einer Karte gefragt wird.
- `create_timeline`: erzeugt eine visuelle Zeitleiste (Datum + Ereignis), die dem Nutzer \
  angezeigt wird — für Reiseverläufe, Ereignisfolgen, Lebensstationen.
- `get_document`: den vollständigen Text eines Dokuments (Brief, Tagebuchabschnitt) laden — \
  für Zusammenfassungen ganzer Dokumente.
- `query_measurements`: strukturierte Suche in den von den Herausgebern ausgezeichneten \
  Messwerten (Wert, historische Einheit, metrische Umrechnung).
- `code_execution` (falls verfügbar): Python-Sandbox OHNE Internetzugang — für \
  Berechnungen, Umrechnungen, Statistik. Diagramme mit matplotlib sind möglich: \
  speichere sie als PNG (plt.savefig('diagramm.png')); erzeugte Bilder werden dem \
  Nutzer automatisch angezeigt. NIE für Karten (dafür gibt es create_map).

Empfohlene Strategie:
1. Bei Fragen zu Personen/Orten: zuerst `lookup_entity`, dann `chunks_for_entity`.
2. Bei thematischen Fragen: `search`, ggf. mit Datums-/Typfiltern.
3. Bei Itinerar-/Reisefragen: `where_was_humboldt`; für Kartenwünsche danach `create_map`.
4. Bei Fragen zu Messungen: `query_measurements`, ergänzend `grep`.
5. Historische Schreibweisen beachten: Die Texte sind in Deutsch, Französisch und Spanisch \
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
    {
        "name": "where_was_humboldt",
        "description": (
            "Aufenthaltsorte und Ereignisse in einem Datumsbereich, direkt aus der "
            "Chronologie der Edition. Bevorzugt für 'Wo war Humboldt am/im …?' und "
            "zum Sammeln von Reisestationen für Karten."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "ISO-Datum oder -Präfix, z. B. '1801-03' oder '1805-04-12'"},
                "date_to": {"type": "string", "description": "Optional: Ende des Bereichs (Standard = date_from)"},
                "limit": {"type": "integer", "description": "Max. Einträge (Standard 20)"},
            },
            "required": ["date_from"],
        },
    },
    {
        "name": "create_map",
        "description": (
            "Erzeugt eine interaktive Karte, die dem Nutzer unter der Antwort angezeigt "
            "wird. Übergib die Stationen in Reihenfolge; Koordinaten werden aus dem "
            "Ortsregister aufgelöst. Nicht auflösbare Orte werden zurückgemeldet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Kartentitel"},
                "stations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Ortsname (Registerschreibweise bevorzugt)"},
                            "humboldt_id": {"type": "string", "description": "Optional: Register-ID, wenn bekannt (präziser)"},
                            "label": {"type": "string", "description": "Optional: Beschriftung (Standard: Ortsname)"},
                            "date": {"type": "string", "description": "Optional: Datum der Station"},
                        },
                        "required": ["name"],
                    },
                },
                "connect_route": {"type": "boolean", "description": "Stationen als Route verbinden (Standard true)"},
            },
            "required": ["title", "stations"],
        },
    },
    {
        "name": "create_timeline",
        "description": (
            "Erzeugt eine visuelle Zeitleiste, die dem Nutzer unter der Antwort angezeigt "
            "wird — für Reiseverläufe, Ereignisfolgen, Lebensstationen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Titel der Zeitleiste"},
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "ISO-Datum oder lesbare Angabe"},
                            "label": {"type": "string", "description": "Ereignis (kurz)"},
                            "description": {"type": "string", "description": "Optional: Details"},
                            "url": {"type": "string", "description": "Optional: Quellen-Link"},
                        },
                        "required": ["date", "label"],
                    },
                },
            },
            "required": ["title", "events"],
        },
    },
    {
        "name": "get_document",
        "description": (
            "Lädt den vollständigen Text eines Dokuments (Brief, Tagebuchabschnitt, "
            "Dokument) anhand seiner doc_id — für Zusammenfassungen ganzer Dokumente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "Dokument-ID, z. B. 'H0000003'"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "query_measurements",
        "description": (
            "Strukturierte Suche in den von den Herausgebern ausgezeichneten Messwerten "
            "(Wert, historische Einheit wie toise/pied_de_roi, metrische Umrechnung, Kontext)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "unit": {"type": "string", "description": "Optional: Einheit filtern (z. B. 'toise', 'meter')"},
                "context_contains": {"type": "string", "description": "Optional: Stichwort im Kontext/Text (z. B. 'Chimborazo')"},
                "limit": {"type": "integer", "description": "Max. Zeilen (Standard 25)"},
            },
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

    def generate(self, query: str, max_tokens: int = 3000, model: str | None = None,
                 history: list[dict] | None = None) -> dict:
        """Run the loop to completion and return the final answer dict."""
        final = {}
        for event in self.iter_events(query, max_tokens=max_tokens, model=model,
                                      history=history):
            if event["type"] == "answer":
                final = event["data"]
            elif event["type"] == "suggestions" and final:
                final["suggestions"] = event["questions"]
        return final

    def iter_events(self, query: str, max_tokens: int = 3000, model: str | None = None,
                    history: list[dict] | None = None):
        """
        Run the agent loop, yielding progress events as they happen:
          {"type": "tool_call",    "round": n, "tool": ..., "input": {...}}
          {"type": "tool_result",  "tool": ..., "n": ..., "preview": [labels]}
          {"type": "answer_delta", "text": ...}                # streamed answer text
          {"type": "answer",       "data": {answer, sources, citations, model,
                                            trace, artifacts, elapsed_s}}
          {"type": "suggestions",  "questions": [...]}         # after the answer
        history: prior turns [{"role": "user"|"assistant", "content": str}, ...]
        """
        t0 = time.time()
        active_model = model or self.model
        sources: dict[str, int] = {}   # chunk_id -> quelle_nr
        source_meta: dict[int, dict] = {}
        trace: list[dict] = []
        artifacts: list[dict] = []     # maps/timelines for the frontend

        tools = list(TOOLS)
        if _code_execution_enabled(active_model):
            tools.append(CODE_EXECUTION_TOOL)

        messages = list(history or []) + [{"role": "user", "content": query}]
        system = SYSTEM_PROMPT.format(max_rounds=MAX_TOOL_ROUNDS)

        response = None
        rounds_used = 0
        iterations = 0
        while True:
            iterations += 1
            # pause_turn continuations don't consume the budget; the iteration
            # cap prevents a pathological pause loop.
            force_answer = rounds_used >= MAX_TOOL_ROUNDS or iterations > MAX_TOOL_ROUNDS * 3
            kwargs = dict(
                model=active_model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
            if force_answer:
                kwargs["tool_choice"] = {"type": "none"}
                messages.append({
                    "role": "user",
                    "content": "Werkzeugbudget erschöpft. Beantworte die Frage jetzt "
                               "auf Basis der bisherigen Ergebnisse (mit [Quelle N]).",
                })

            try:
                response = yield from self._stream_round(kwargs)
            except Exception as e:
                # graceful degradation if the model rejects the server tool
                if CODE_EXECUTION_TOOL in tools and "code_execution" in str(e):
                    logger.warning(f"code_execution rejected on {active_model}, retrying without: {e}")
                    tools = [t for t in tools if t is not CODE_EXECUTION_TOOL]
                    kwargs["tools"] = tools
                    response = yield from self._stream_round(kwargs)
                else:
                    raise

            # surface server-side tool activity (code execution) in the trace
            for block in response.content:
                btype = getattr(block, "type", "")
                if btype == "server_tool_use":
                    code = ""
                    if isinstance(getattr(block, "input", None), dict):
                        code = block.input.get("command") or block.input.get("code") or ""
                    yield {"type": "tool_call", "round": rounds_used + 1,
                           "tool": "code_execution", "input": {"code": code[:300]}}
                    trace.append({"round": rounds_used + 1, "tool": "code_execution",
                                  "input": {"code": code[:300]}, "n_results": 1})
                elif btype == "bash_code_execution_tool_result":
                    content = getattr(block, "content", None)
                    stdout = getattr(content, "stdout", "") or getattr(content, "stderr", "") or ""
                    yield {"type": "tool_result", "tool": "code_execution",
                           "n": 1, "preview": [stdout[:120]]}
            # image files created in the sandbox become inline artifacts
            self._collect_code_files(response, artifacts)

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue

            if response.stop_reason != "tool_use":
                break

            rounds_used += 1
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield {
                    "type": "tool_call",
                    "round": rounds_used,
                    "tool": block.name,
                    "input": block.input,
                }
                result = self._dispatch(block.name, block.input, sources, source_meta, artifacts)
                trace.append({
                    "round": rounds_used,
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
                "artifacts": artifacts,
                "elapsed_s": elapsed,
            },
        }

        if answer_text and os.environ.get("SUGGESTIONS", "1") != "0":
            questions = self._suggest_followups(query, answer_text)
            if questions:
                yield {"type": "suggestions", "questions": questions}

    def _stream_round(self, kwargs):
        """One streaming API round: yields answer_delta events, returns the message."""
        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    yield {"type": "answer_delta", "text": text}
            return stream.get_final_message()

    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")

    def _collect_code_files(self, response, artifacts: list) -> None:
        """Download image files produced by code_execution as inline artifacts."""
        import base64
        for block in response.content:
            if getattr(block, "type", "") != "bash_code_execution_tool_result":
                continue
            result = getattr(block, "content", None)
            for ref in (getattr(result, "content", None) or []):
                fid = getattr(ref, "file_id", None)
                if not fid:
                    continue
                try:
                    meta = self.client.beta.files.retrieve_metadata(fid)
                    name = getattr(meta, "filename", "") or "datei"
                    if not name.lower().endswith(self.IMAGE_EXTS):
                        continue
                    payload = self.client.beta.files.download(fid)
                    raw = payload.read() if hasattr(payload, "read") else payload.content
                    ext = name.rsplit(".", 1)[-1].lower()
                    media = "image/svg+xml" if ext == "svg" else f"image/{'jpeg' if ext == 'jpg' else ext}"
                    artifacts.append({
                        "type": "image", "title": name,
                        "media_type": media,
                        "data": base64.b64encode(raw).decode(),
                    })
                    logger.info(f"Attached generated image {name} ({len(raw)} bytes)")
                except Exception as e:
                    logger.warning(f"Could not fetch generated file {fid}: {e}")

    def _suggest_followups(self, query: str, answer: str) -> list[str]:
        """One cheap Haiku call for 3 follow-up questions (same language)."""
        try:
            resp = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
                system="Du schlägst Anschlussfragen für einen Rechercheassistenten zur "
                       "edition humboldt digital (Tagebücher, Briefe, Chronologie Alexander "
                       "von Humboldts) vor. Antworte NUR mit 3 kurzen Fragen (je max. 12 "
                       "Wörter), eine pro Zeile, ohne Nummerierung. Verwende die Sprache "
                       "der ursprünglichen Frage.",
                messages=[{
                    "role": "user",
                    "content": f"Frage: {query}\n\nAntwort (gekürzt): {answer[:1500]}\n\n"
                               "3 sinnvolle Anschlussfragen:",
                }],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            out = []
            for line in text.splitlines():
                q = line.strip().lstrip("-•0123456789. ").strip()
                if len(q) > 8:
                    out.append(q)
            return out[:3]
        except Exception as e:
            logger.warning(f"Follow-up suggestions failed: {e}")
            return []

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

    def _dispatch(self, name: str, tool_input: dict, sources: dict, source_meta: dict,
                  artifacts: list | None = None) -> dict:
        if artifacts is None:
            artifacts = []
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
            elif name == "where_was_humboldt":
                payload = self._tool_whereabouts(tool_input, sources, source_meta)
            elif name == "create_map":
                payload = self._tool_create_map(tool_input, artifacts)
            elif name == "create_timeline":
                payload = self._tool_create_timeline(tool_input, artifacts)
            elif name == "get_document":
                payload = self._tool_get_document(tool_input, sources, source_meta)
            elif name == "query_measurements":
                payload = self._tool_query_measurements(tool_input, sources, source_meta)
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
            "doc_title": chunk.doc_title if chunk else "",
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

    def _tool_whereabouts(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        entries = self.retriever.whereabouts(
            date_from=tool_input.get("date_from", ""),
            date_to=tool_input.get("date_to"),
            limit=int(tool_input.get("limit") or 20),
        )
        if not entries:
            return {"error": "Keine Chronologie-Einträge in diesem Zeitraum. "
                             "Versuche einen breiteren Bereich (z. B. nur Jahr-Monat)."}
        out = []
        for e in entries:
            nr = self._register_source(e["chunk_id"], sources, source_meta)
            out.append({
                "quelle_nr": nr,
                "datum": e["date"],
                "typ": e["doc_type"],
                "text": e["text"][:400],
                "orte": e["places"][:6],
                "url": e["url"],
            })
        return out

    def _tool_create_map(self, tool_input: dict, artifacts: list) -> dict:
        stations_in = tool_input.get("stations") or []
        title = tool_input.get("title") or "Karte"
        resolved, unresolved = [], []
        for st in stations_in:
            if not isinstance(st, dict):
                continue
            hid = (st.get("humboldt_id") or "").strip()
            name = (st.get("name") or "").strip()
            ent = None
            if hid and hid in self.entity_index.entities:
                ent = self.entity_index.entities[hid]
            elif name:
                hits = self.entity_index.lookup(name, entity_type="place", limit=1)
                ent = hits[0] if hits else None
            coords = self.entity_index.coords_for(ent["id"]) if ent else None
            if coords:
                resolved.append({
                    "label": st.get("label") or (ent["name"] if ent else name),
                    "lat": coords[0],
                    "lon": coords[1],
                    "date": st.get("date", ""),
                    "url": ent.get("url", "") if ent else "",
                })
            else:
                unresolved.append(name or hid)
        if not resolved:
            return {"error": "Keine Station konnte zu Koordinaten aufgelöst werden. "
                             "Prüfe die Ortsnamen (Registerschreibweise) mit lookup_entity."}
        artifacts.append({
            "type": "map",
            "title": title,
            "connect_route": bool(tool_input.get("connect_route", True)),
            "stations": resolved,
        })
        result = {
            "ok": True,
            "hinweis": f"Karte „{title}“ mit {len(resolved)} Stationen wird dem Nutzer "
                       "unter deiner Antwort angezeigt. Erwähne sie kurz, beschreibe sie "
                       "aber nicht Station für Station.",
        }
        if unresolved:
            result["nicht_aufgeloest"] = unresolved
        return result

    def _tool_create_timeline(self, tool_input: dict, artifacts: list) -> dict:
        events_in = tool_input.get("events") or []
        title = tool_input.get("title") or "Zeitleiste"
        events = [
            {
                "date": str(e.get("date", "")).strip(),
                "label": str(e.get("label", "")).strip(),
                "description": str(e.get("description", "")).strip(),
                "url": str(e.get("url", "")).strip(),
            }
            for e in events_in
            if isinstance(e, dict) and e.get("date") and e.get("label")
        ]
        if not events:
            return {"error": "Keine gültigen Ereignisse (date + label erforderlich)."}
        events.sort(key=lambda e: e["date"])
        artifacts.append({"type": "timeline", "title": title, "events": events})
        return {
            "ok": True,
            "hinweis": f"Zeitleiste „{title}“ mit {len(events)} Ereignissen wird dem "
                       "Nutzer unter deiner Antwort angezeigt.",
        }

    def _tool_get_document(self, tool_input: dict, sources: dict, source_meta: dict) -> dict:
        doc = self.retriever.get_document(tool_input.get("doc_id", ""))
        if "error" in doc:
            return doc
        anchor = doc["chunk_ids"][0] if doc["chunk_ids"] else None
        if anchor:
            doc["quelle_nr"] = self._register_source(anchor, sources, source_meta)
        return doc

    def _tool_query_measurements(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        rows = self.retriever.query_measurements(
            unit=tool_input.get("unit"),
            context_contains=tool_input.get("context_contains"),
            limit=int(tool_input.get("limit") or 25),
        )
        if not rows:
            return {"error": "Keine Messwerte gefunden. Versuche es ohne Filter oder mit "
                             "grep nach Zahlen+Einheit im Text."}
        out = []
        for r in rows:
            nr = self._register_source(r["chunk_id"], sources, source_meta)
            out.append({
                "quelle_nr": nr,
                "wert": r["value"],
                "einheit": r["unit"],
                "metrisch": f"{r['value_metric']} {r['unit_metric']}" if r["value_metric"] is not None else "",
                "kontext": r["context"][:150],
            })
        return out

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
