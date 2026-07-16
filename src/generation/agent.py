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

import itertools
import json
import logging
import os
import re
import time
import urllib.parse
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8       # research budget
GRACE_ROUNDS = 2          # extra rounds allowed for visualizations only
SEARCH_TEXT_CHARS = 700
CONTEXT_TEXT_CHARS = 1400

# Editorial notes inside search_result content are prefixed with this marker so
# the model can attribute them; citations into blocks >= 1 are editor's voice.
EDITORIAL_PREFIX = "[Editorische Anmerkung der Herausgeber] "

# $/MTok (input, output) for cost telemetry; cache write = 1.25x/2x input,
# cache read = 0.1x input
_PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}


class _UsageTally:
    """Accumulates API usage across all rounds of one request."""

    def __init__(self, model: str):
        self.model = model
        self.input = self.output = self.cache_read = self.cache_write = 0
        self.rounds = 0

    def add(self, usage) -> None:
        if usage is None:
            return
        self.rounds += 1
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost_usd(self) -> float:
        inp, out = _PRICES.get(self.model, (3.0, 15.0))
        return round((self.input * inp + self.cache_read * inp * 0.1
                      + self.cache_write * inp * 1.25 + self.output * out) / 1e6, 4)

    def as_dict(self) -> dict:
        return {"rounds": self.rounds, "input": self.input, "output": self.output,
                "cache_read": self.cache_read, "cache_write": self.cache_write,
                "est_cost_usd": self.cost_usd()}

_SOURCE_CHUNK_RE = re.compile(r"#chunk=([\w.-]+)")


def build_fragment_url(url: str, cited_text: str) -> str:
    """
    Best-effort deep link: append a Text Fragment (#:~:text=) built from the
    cited span, so supporting browsers scroll to and highlight the passage on
    edition-humboldt.de. If the page text doesn't match exactly (diplomatic vs.
    regularized differences), browsers just land on the page — safe fallback.
    """
    if not url or not cited_text:
        return url
    words = re.sub(r"\s+", " ", cited_text).strip().split(" ")
    words = [w for w in words if w]
    if not words:
        return url
    q = lambda s: urllib.parse.quote(s, safe="")
    if len(words) <= 8:
        frag = q(" ".join(words))
    else:
        # start,end range syntax: only the boundaries must match exactly
        frag = q(" ".join(words[:5])) + "," + q(" ".join(words[-5:]))
    return f"{url}#:~:text={frag}"

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
- `create_network`: erzeugt ein interaktives Beziehungsnetz um eine Registerentität \
  (Person, Ort, Institution, Pflanze), das dem Nutzer angezeigt wird. Kanten = gemeinsame \
  Erwähnung in denselben Textpassagen. Nutze es bei Fragen nach Beziehungen, Umfeld, \
  Kontaktnetz oder "Wer/was taucht gemeinsam mit X auf?".
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
5. Für Einordnung, Kontext und Forschungsstand: IMMER auch `search` mit \
   doc_types=["research_article"] — die Forschungsbeiträge der Edition liefern die \
   wissenschaftliche Interpretation, die Tagebücher nur den Befund.
5. Historische Schreibweisen beachten: Die Texte sind in Deutsch, Französisch und Spanisch \
   des 18./19. Jahrhunderts verfasst. Probiere bei `search`/`grep` auch zeitgenössische \
   Varianten (z. B. "Crocodile"/"Cocodrilo" statt "Krokodil").
Du hast maximal {max_rounds} Werkzeugrunden — recherchiere gezielt, nicht erschöpfend.
Kündige Werkzeuge nie bloß an: Wenn du eine Karte, Zeitleiste oder ein Netzwerk \
erstellen willst, rufe das Werkzeug JETZT auf, bevor du deine Antwort beendest — \
beende einen Zug niemals mit "Ich erstelle …".

## Antwortregeln
- Belege jede faktische Aussage durch Zitieren der Suchergebnisse — die Zitierfunktion \
  verknüpft deine Sätze automatisch mit den Fundstellen. Zitiere DURCHGÄNGIG: Auch in \
  langen Antworten muss jeder Absatz mit Fakten mindestens ein Zitat tragen; eine \
  Antwort ganz ohne Zitate ist ein Fehler. Zitiere nur Ergebnisse, die du tatsächlich \
  gesehen hast, und schreibe KEINE eigenen Quellennummern wie "[Quelle 3]" in den \
  Antworttext.
- Unterscheide klar zwischen Humboldts eigenem Text und editorischen Kommentaren \
  ("Laut Anmerkung der Herausgeber ..." — Inhalte, die mit \
  "[Editorische Anmerkung der Herausgeber]" markiert sind, sowie Quellen vom Typ \
  editorial_note).
- Wenn die Recherche nichts Belastbares ergibt, sage das ehrlich.
- WICHTIG: Antworte IMMER in der Sprache der Nutzerfrage. Eine englische Frage bekommt \
  eine vollständig englische Antwort, eine französische eine französische — auch wenn \
  alle Quellen und Werkzeugergebnisse deutsch sind. Das gilt auch für die Titel von \
  Karten, Zeitleisten und Netzwerken (übersetze sie; Zitate aus den Quellen bleiben \
  natürlich im Original).
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
                    "enum": ["person", "place", "institution", "glossary", "plant"],
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
        "name": "create_network",
        "description": (
            "Erzeugt ein interaktives Beziehungsnetz (Graph) um eine Registerentität — "
            "Person, Ort, Institution oder Pflanze —, das dem Nutzer unter der Antwort "
            "angezeigt wird. Knoten sind mit-erwähnte Registerentitäten, Kanten gewichten "
            "die Zahl gemeinsamer Textpassagen (editorische Annotationen). Die wichtigsten "
            "Nachbarn werden auch dir zurückgemeldet, damit du sie besprechen kannst."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string", "description": "Name oder humboldt_id der zentralen Entität, z. B. 'Bonpland' oder 'H0002222'"},
                "entity_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["person", "place", "institution", "plant", "glossary"]},
                    "description": "Optional: nur diese Typen als Nachbarknoten",
                },
                "max_nodes": {"type": "integer", "description": "Max. Nachbarknoten (Standard 20, max. 40)"},
                "min_shared": {"type": "integer", "description": "Mindestzahl gemeinsamer Passagen für eine Kante (Standard 2)"},
                "title": {"type": "string", "description": "Optional: Titel des Netzes"},
            },
            "required": ["focus"],
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
        self._chunk_entities: dict[str, set[str]] | None = None  # lazy inverse of entity_index.chunk_index

    # =========================================================================
    # Public API
    # =========================================================================

    def generate(self, query: str, max_tokens: int = 8000, model: str | None = None,
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

    def iter_events(self, query: str, max_tokens: int = 8000, model: str | None = None,
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
        tally = _UsageTally(active_model)
        sources: dict[str, int] = {}   # chunk_id -> quelle_nr
        source_meta: dict[int, dict] = {}
        trace: list[dict] = []
        artifacts: list[dict] = []     # maps/timelines for the frontend

        tools = list(TOOLS)
        if _code_execution_enabled(active_model):
            tools.append(CODE_EXECUTION_TOOL)

        # optional cost knob (A/B via eval harness before adopting a default);
        # Haiku 4.5 rejects the effort parameter
        effort = os.environ.get("EFFORT", "").strip().lower()
        extra_kwargs = {}
        if effort in ("low", "medium", "high", "xhigh", "max") \
                and active_model.startswith(("claude-sonnet", "claude-opus")):
            extra_kwargs["output_config"] = {"effort": effort}

        messages = list(history or []) + [{"role": "user", "content": query}]
        # cache breakpoint on the system block also covers the (stable) tool
        # definitions, which render before it — saves the full static prefix
        # on every loop round and every follow-up request.
        # 1h TTL: the widget's traffic is sporadic; the static prefix is shared
        # across all users of the same model, so one 2x write per hour buys
        # 0.1x reads for everyone else
        system = [{
            "type": "text",
            "text": SYSTEM_PROMPT.format(max_rounds=MAX_TOOL_ROUNDS),
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]
        # explicit language hint AFTER the breakpoint (cache stays warm):
        # the German prompt + German sources otherwise pull answers into German
        lang = self._detect_lang(query)
        if lang == "en":
            system.append({"type": "text", "text":
                "The current user question is in ENGLISH. Answer entirely in "
                "English, including map/timeline/network titles and labels "
                "(verbatim quotes from the sources stay in their original language)."})
        elif lang == "fr":
            system.append({"type": "text", "text":
                "La question de l'utilisateur est en FRANÇAIS. Réponds entièrement "
                "en français, y compris les titres des cartes, chronologies et "
                "réseaux (les citations des sources restent dans leur langue d'origine)."})

        response = None
        rounds_used = 0
        iterations = 0
        soft_forced = False
        continuations = 0
        # text blocks from every round — the model often writes substantive
        # text before calling create_map etc.; only keeping the final round
        # silently dropped it from the rendered answer
        text_rounds: list[list] = []
        while True:
            iterations += 1
            # Budget in two stages: after MAX_TOOL_ROUNDS no more research,
            # but visualizations stay possible (a hard tool_choice=none here
            # used to leave "create a map" requests without a map); after the
            # grace rounds the model must answer. pause_turn continuations
            # don't consume the budget; the iteration cap stops pause loops.
            hard_force = (rounds_used >= MAX_TOOL_ROUNDS + GRACE_ROUNDS
                          or iterations > MAX_TOOL_ROUNDS * 3)
            kwargs = dict(
                model=active_model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                **extra_kwargs,
            )
            if hard_force:
                kwargs["tool_choice"] = {"type": "none"}
                messages.append({
                    "role": "user",
                    "content": self._hint(
                        lang,
                        "Werkzeugbudget erschöpft. Beantworte die Frage jetzt auf "
                        "Basis der bisherigen Ergebnisse und belege deine Aussagen "
                        "durch Zitieren der Suchergebnisse.",
                        "Tool budget exhausted. Answer the question now from the "
                        "results so far, citing the search results.",
                        "Budget d'outils épuisé. Réponds maintenant à partir des "
                        "résultats obtenus, en citant les résultats de recherche."),
                })
            elif rounds_used >= MAX_TOOL_ROUNDS and not soft_forced:
                soft_forced = True
                messages.append({
                    "role": "user",
                    "content": self._hint(
                        lang,
                        "Recherche-Budget erschöpft — keine weiteren Suchen. Falls "
                        "der Nutzer eine Karte, Zeitleiste oder ein Netzwerk wollte, "
                        "erstelle sie JETZT (create_map/create_timeline/"
                        "create_network) und beantworte dann die Frage mit Zitaten.",
                        "Research budget exhausted — no more searches. If the user "
                        "asked for a map, timeline or network, create them NOW "
                        "(create_map/create_timeline/create_network), then answer "
                        "with citations.",
                        "Budget de recherche épuisé — plus de recherches. Si "
                        "l'utilisateur a demandé une carte, chronologie ou réseau, "
                        "crée-les MAINTENANT, puis réponds avec citations."),
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
            tally.add(getattr(response, "usage", None))

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

            round_text = [b for b in response.content if getattr(b, "type", "") == "text"]
            if any((b.text or "").strip() for b in round_text):
                text_rounds.append(round_text)

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue

            if response.stop_reason != "tool_use":
                # announce-then-stop: the model sometimes ends its turn with
                # "Now let me visualize this on a map" without calling the
                # tool — nudge it to follow through instead of shipping a
                # broken promise
                if not hard_force and continuations < 2:
                    nudge = self._unfulfilled_visualization(text_rounds, artifacts, lang)
                    # echo only text blocks: a max_tokens-truncated turn can
                    # contain a dangling tool_use, which the API rejects
                    # without a matching tool_result
                    echo = [b for b in response.content
                            if getattr(b, "type", "") == "text" and (b.text or "").strip()]
                    if nudge and echo:
                        continuations += 1
                        logger.info("Answer announced a visualization without creating it — nudging")
                        messages.append({"role": "assistant", "content": echo})
                        messages.append({"role": "user", "content": nudge})
                        continue
                break

            rounds_used += 1
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            meta_notes = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield {
                    "type": "tool_call",
                    "round": rounds_used,
                    "tool": block.name,
                    "input": block.input,
                }
                result = self._dispatch(block.name, block.input, sources, source_meta,
                                        artifacts, lang=lang)
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
                    "preview": self._result_preview(result),
                }
                # citable chunks go out as search_result blocks (span-level
                # citations). The API forbids mixing block types inside one
                # tool_result, so the JSON metadata (chunk_ids, dates, scores,
                # hints) follows as a plain text block in the same user turn.
                blocks_out = result.get("blocks")
                if blocks_out:
                    content = blocks_out
                    if result["payload"] is not None:
                        meta_notes.append(
                            f"{block.name}: "
                            + json.dumps(result["payload"], ensure_ascii=False)
                        )
                else:
                    content = json.dumps(result["payload"], ensure_ascii=False)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })
            user_content = list(tool_results)
            if meta_notes:
                # language-neutral label; the answer_language tag is a recency
                # nudge — the system hint alone loses against pages of German
                # tool results
                lang_tag = {"en": " answer_language=English",
                            "fr": " answer_language=français"}.get(lang, "")
                user_content.append({
                    "type": "text",
                    "text": f"[tool_metadata]{lang_tag}\n" + "\n".join(meta_notes),
                })
            messages.append({"role": "user", "content": user_content})
            self._advance_cache_marker(messages)

        answer_text = "\n\n".join(
            "".join(b.text for b in blocks).strip() for blocks in text_rounds
        ).strip()

        # Span-level citations from the API (search_result blocks); the legacy
        # [Quelle N] regex stays as fallback for models that still write markers.
        answer_blocks, cited = self._collect_api_citations(text_rounds, sources, source_meta)
        if not cited:
            cited = self._extract_citations(answer_text, source_meta)
            answer_blocks = []

        # Backstop: models occasionally produce a long synthesis without a
        # single citation. One repair pass re-emits the answer with citations
        # (replaces the uncited text; costs one extra generation, rare).
        if (not cited and source_meta and len(answer_text) > 400
                and response is not None
                and os.environ.get("CITATION_REPAIR", "1") != "0"):
            logger.warning("Answer had no citations — running repair pass")
            yield {"type": "tool_call", "round": rounds_used + 1,
                   "tool": "repair_citations", "input": {}}
            yield {"type": "live_reset"}
            repair_echo = [b for b in response.content
                           if getattr(b, "type", "") == "text" and (b.text or "").strip()]
            messages.append({"role": "assistant", "content": repair_echo or response.content})
            repair_msg = self._hint(
                lang,
                "Deine Antwort enthält keine Zitate. Gib exakt dieselbe Antwort "
                "(gleiche Sprache, gleicher Inhalt) noch einmal vollständig aus, "
                "aber belege sie durchgängig über die Zitierfunktion mit den "
                "Suchergebnissen — mindestens ein Zitat pro Absatz.",
                "Your answer contains no citations. Reproduce exactly the same "
                "answer in full (same language, same content), but support it "
                "throughout via the citation mechanism with the search results — "
                "at least one citation per paragraph.",
                "Ta réponse ne contient aucune citation. Reproduis exactement la "
                "même réponse (même langue, même contenu), mais appuie-la via le "
                "mécanisme de citation — au moins une citation par paragraphe.")
            messages.append({"role": "user", "content": repair_msg})
            try:
                retry_kwargs = dict(model=active_model, max_tokens=max_tokens,
                                    system=system, messages=messages, tools=tools,
                                    tool_choice={"type": "none"}, **extra_kwargs)
                response = yield from self._stream_round(retry_kwargs)
                tally.add(getattr(response, "usage", None))
                retry_text = [b for b in response.content if getattr(b, "type", "") == "text"]
                if any((b.text or "").strip() for b in retry_text):
                    text_rounds = [retry_text]
                    answer_text = "".join(b.text for b in retry_text).strip()
                    answer_blocks, cited = self._collect_api_citations(
                        text_rounds, sources, source_meta)
                    if not cited:
                        cited = self._extract_citations(answer_text, source_meta)
                        answer_blocks = []
                yield {"type": "tool_result", "tool": "repair_citations",
                       "n": len(cited), "preview": []}
            except Exception as e:
                logger.warning(f"Citation repair failed: {e}")
        elapsed = round(time.time() - t0, 2)
        u = tally.as_dict()
        logger.info(
            f"Usage total ({active_model}): rounds={u['rounds']} in={u['input']} "
            f"out={u['output']} cache_read={u['cache_read']} "
            f"cache_write={u['cache_write']} est=${u['est_cost_usd']}"
        )
        logger.info(
            f"Agentic answer in {elapsed}s, {len(trace)} tool calls, "
            f"{len(cited)} cited sources ({'api' if answer_blocks else 'regex'} citations). Trace: "
            + "; ".join(f"{t['tool']}({json.dumps(t['input'], ensure_ascii=False)})→{t['n_results']}" for t in trace)
        )

        yield {
            "type": "answer",
            "data": {
                "answer": answer_text,
                "answer_blocks": answer_blocks,
                "sources": cited or list(source_meta.values())[:8],
                "citations": [{"index": s["index"], "citation_label": s["citation_label"],
                               "url": s["url"]} for s in cited],
                "model": active_model,
                "trace": trace,
                "artifacts": artifacts,
                "elapsed_s": elapsed,
                "usage": u,
            },
        }

        if answer_text and os.environ.get("SUGGESTIONS", "1") != "0":
            questions = self._suggest_followups(query, answer_text, lang)
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

    def _suggest_followups(self, query: str, answer: str,
                           lang: str | None = None) -> list[str]:
        """One cheap Haiku call for 3 follow-up questions (same language)."""
        lang_rule = {
            "en": "The user's question is in ENGLISH — the follow-up questions MUST "
                  "be in English.",
            "fr": "La question est en FRANÇAIS — les questions de suivi DOIVENT être "
                  "en français.",
        }.get(lang, "Verwende die Sprache der ursprünglichen Frage.")
        try:
            resp = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
                system="Du schlägst Anschlussfragen für einen Rechercheassistenten zur "
                       "edition humboldt digital (Tagebücher, Briefe, Chronologie Alexander "
                       "von Humboldts) vor. Antworte NUR mit 3 kurzen Fragen (je max. 12 "
                       "Wörter), eine pro Zeile, ohne Nummerierung. " + lang_rule,
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
    def _result_preview(result: dict, limit: int = 3) -> list[str]:
        """Short human-readable labels of what a tool returned (for the UI
        trace). Prefers the search_result block titles — the metadata payload
        no longer repeats the labels."""
        blocks = result.get("blocks") or []
        labels = [b.get("title", "") for b in blocks[:limit] if b.get("title")]
        if labels:
            return labels
        payload = result["payload"]
        if isinstance(payload, dict):
            if "error" in payload:
                return [payload["error"][:80]]
            return [payload.get("citation_label", "")][:1]
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

    # array-valued tool arguments that models occasionally emit as
    # JSON-encoded strings (observed on Sonnet with large create_map /
    # create_timeline payloads — the string iterates to zero valid items)
    _LIST_ARGS = frozenset({"stations", "events", "doc_types", "entity_types"})

    @classmethod
    def _coerce_tool_input(cls, tool_input: dict) -> dict:
        if not isinstance(tool_input, dict):
            return tool_input
        out = dict(tool_input)
        for key in cls._LIST_ARGS & out.keys():
            v = out[key]
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, list):
                    out[key] = parsed
        return out

    def _dispatch(self, name: str, tool_input: dict, sources: dict, source_meta: dict,
                  artifacts: list | None = None, lang: str | None = None) -> dict:
        if artifacts is None:
            artifacts = []
        tool_input = self._coerce_tool_input(tool_input)
        try:
            if name == "lookup_entity":
                out = self._tool_lookup_entity(tool_input, sources, source_meta)
            elif name == "chunks_for_entity":
                out = self._tool_chunks_for_entity(tool_input, sources, source_meta)
            elif name == "search":
                out = self._tool_search(tool_input, sources, source_meta)
            elif name == "grep":
                out = self._tool_grep(tool_input, sources, source_meta)
            elif name == "get_context":
                out = self._tool_get_context(tool_input, sources, source_meta)
            elif name == "where_was_humboldt":
                out = self._tool_whereabouts(tool_input, sources, source_meta)
            elif name == "create_map":
                out = self._tool_create_map(tool_input, artifacts, lang)
            elif name == "create_timeline":
                out = self._tool_create_timeline(tool_input, artifacts, lang)
            elif name == "create_network":
                out = self._tool_create_network(tool_input, artifacts, lang)
            elif name == "get_document":
                out = self._tool_get_document(tool_input, sources, source_meta)
            elif name == "query_measurements":
                out = self._tool_query_measurements(tool_input, sources, source_meta)
            else:
                out = {"error": f"Unbekanntes Werkzeug: {name}"}
        except Exception as e:
            logger.exception(f"Tool {name} failed")
            out = {"error": str(e)}
        # chunk-bearing tools return (payload, search_result_blocks)
        payload, blocks = out if isinstance(out, tuple) else (out, None)
        if blocks:
            n = len(blocks)
        else:
            n = len(payload) if isinstance(payload, list) else (0 if "error" in payload else 1)
        return {"payload": payload, "n": n, "blocks": blocks or None}

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

    def _sr_block(self, chunk, text: str, title: str | None = None,
                  notes: list | None = None) -> dict:
        """
        Build a citations-enabled search_result block for one chunk. The source
        URI carries the chunk_id in its fragment, so API citations can be
        resolved back to the exact chunk (URLs alone collide on shared folios).
        Editorial notes ride along as separately-attributable content blocks.
        """
        content = [{"type": "text", "text": text or "(kein Text)"}]
        if notes is None and chunk is not None:
            notes = chunk.editorial_notes
        for n in (notes or [])[:3]:
            content.append({"type": "text", "text": EDITORIAL_PREFIX + str(n)[:400]})
        base = (chunk.url if chunk is not None else "") or "https://edition-humboldt.de"
        return {
            "type": "search_result",
            "source": f"{base}#chunk={chunk.chunk_id}",
            "title": title or chunk.citation_label or chunk.chunk_id,
            "content": content,
            "citations": {"enabled": True},
        }

    def _chunk_payload(self, chunk_id: str, sources: dict, source_meta: dict,
                       text_chars: int = SEARCH_TEXT_CHARS,
                       score: float | None = None) -> tuple[dict | None, dict | None]:
        """(metadata for the model, search_result block) for one chunk.
        The text itself lives only in the block — the metadata stays compact."""
        chunk = self.retriever.chunk_store.get(chunk_id)
        if chunk is None:
            return None, None
        self._register_source(chunk_id, sources, source_meta)
        # label + url live in the block (title/source) — repeating them here
        # only burns tokens
        meta = {
            "chunk_id": chunk_id,
            "doc_type": chunk.doc_type,
            "date": chunk.date_iso,
        }
        if score is not None:
            meta["score"] = round(score, 3)
        block = self._sr_block(chunk, chunk.text_regularized[:text_chars])
        return meta, block

    def _collect_api_citations(self, text_rounds: list, sources: dict,
                               source_meta: dict) -> tuple[list, list]:
        """
        Extract span-level API citations from the text blocks of all rounds.
        Returns (answer_blocks, cited_sources): answer_blocks mirror the text
        blocks with their citations (display index, cited span, deep link),
        with paragraph separators between rounds; cited_sources are renumbered
        in reading order.
        """
        nr_by_chunk = dict(sources)
        display: dict[str, int] = {}
        cited: list[dict] = []
        answer_blocks: list[dict] = []
        any_citation = False
        flat: list = []
        for i, blocks in enumerate(text_rounds):
            if i:
                flat.append(None)  # round boundary -> paragraph break
            flat.extend(blocks)
        for block in flat:
            if block is None:
                answer_blocks.append({"text": "\n\n", "citations": []})
                continue
            entry = {"text": block.text, "citations": []}
            for cit in (getattr(block, "citations", None) or []):
                m = _SOURCE_CHUNK_RE.search(getattr(cit, "source", "") or "")
                if not m:
                    continue
                chunk_id = m.group(1)
                orig_nr = nr_by_chunk.get(chunk_id)
                if orig_nr is None or orig_nr not in source_meta:
                    continue
                if chunk_id not in display:
                    display[chunk_id] = len(display) + 1
                    s = dict(source_meta[orig_nr])
                    s["index"] = display[chunk_id]
                    cited.append(s)
                cited_text = (getattr(cit, "cited_text", "") or "")
                # block 0 is the passage text; blocks >= 1 are editorial notes
                editorial = (getattr(cit, "start_block_index", 0) or 0) >= 1
                if editorial and cited_text.startswith(EDITORIAL_PREFIX):
                    cited_text = cited_text[len(EDITORIAL_PREFIX):]
                url = source_meta[orig_nr].get("url", "")
                entry["citations"].append({
                    "index": display[chunk_id],
                    "chunk_id": chunk_id,
                    "cited_text": cited_text[:600],
                    "editorial": editorial,
                    "url": url,
                    "fragment_url": url if editorial else build_fragment_url(url, cited_text),
                })
                any_citation = True
            answer_blocks.append(entry)
        if not any_citation:
            return [], []
        return answer_blocks, cited

    _ANNOUNCE_RE = re.compile(
        r"\b(let me|i['’]ll|i will|now i|ich (erstelle|zeichne|visualisiere)|"
        r"erstelle (jetzt|nun|dir)|je vais)\b", re.I)
    _VIS_TYPES = {
        "map": re.compile(r"\b(map|karte|carte)\b", re.I),
        "timeline": re.compile(r"\b(timeline|zeitleiste|zeitstrahl|chronologie)\b", re.I),
        "network": re.compile(r"\b(network|netzwerk|réseau|beziehungsnetz)\b", re.I),
    }
    _VIS_TOOLS = {"map": "create_map", "timeline": "create_timeline",
                  "network": "create_network"}

    def _unfulfilled_visualization(self, text_rounds: list, artifacts: list,
                                   lang: str | None) -> str | None:
        """A nudge message if the final text promises a map/timeline/network
        that was never created; None otherwise."""
        if not text_rounds:
            return None
        tail = "".join(getattr(b, "text", "") or "" for b in text_rounds[-1]).strip()[-300:]
        if not self._ANNOUNCE_RE.search(tail):
            return None
        have = {a.get("type") for a in artifacts}
        missing = [t for t, rx in self._VIS_TYPES.items()
                   if rx.search(tail) and t not in have]
        if not missing:
            return None
        tool_names = ", ".join(self._VIS_TOOLS[m] for m in missing)
        return self._hint(
            lang,
            f"Du hast eine Visualisierung angekündigt, aber das Werkzeug nicht "
            f"aufgerufen. Rufe jetzt {tool_names} auf und schließe dann deine "
            "Antwort ab (nicht von vorn beginnen).",
            f"You announced a visualization but did not call the tool. Call "
            f"{tool_names} now, then finish your answer (do not start over).",
            f"Tu as annoncé une visualisation sans appeler l'outil. Appelle "
            f"{tool_names} maintenant, puis termine ta réponse.")

    _EN_WORDS = re.compile(
        r"\b(the|of|and|what|which|where|when|who|whom|create|show|make|draw|"
        r"map|timeline|network|journey|did|were|about|with|please|how|why|his)\b", re.I)
    _FR_WORDS = re.compile(
        r"\b(le|la|les|une?|est|était|où|quand|quelle?s?|carte|montre|"
        r"crée|voyage|avec|pourquoi|comment|chronologie|réseau)\b", re.I)

    @classmethod
    def _detect_lang(cls, query: str) -> str | None:
        """Rough EN/FR detection; None means German (the default)."""
        en = len(cls._EN_WORDS.findall(query))
        fr = len(cls._FR_WORDS.findall(query))
        if en >= 2 and en > fr:
            return "en"
        if fr >= 2 and fr > en:
            return "fr"
        return None

    @staticmethod
    def _advance_cache_marker(messages: list) -> None:
        """Move the single conversation cache breakpoint to the newest
        tool_result block (system prompt holds a second, static one)."""
        last = None
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
            if content and isinstance(content[-1], dict) \
                    and content[-1].get("type") in ("tool_result", "text"):
                last = content[-1]
        if last is not None:
            last["cache_control"] = {"type": "ephemeral"}

    def _tool_lookup_entity(self, tool_input: dict, sources: dict, source_meta: dict):
        hits = self.entity_index.lookup(
            tool_input.get("name", ""),
            entity_type=tool_input.get("entity_type"),
        )
        if not hits:
            return {"error": "Keine Registereinträge gefunden. Versuche eine andere Schreibweise oder search."}
        out, blocks = [], []
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
            # register entries are citable sources too
            if h.get("register_chunk_id"):
                self._register_source(h["register_chunk_id"], sources, source_meta)
                chunk = self.retriever.chunk_store.get(h["register_chunk_id"])
                if chunk is not None:
                    blocks.append(self._sr_block(
                        chunk,
                        h["description"][:400] or chunk.text_regularized[:400],
                        title=f"Registereintrag: {h['name']}",
                        notes=[],
                    ))
            out.append(item)
        return out, blocks

    def _tool_chunks_for_entity(self, tool_input: dict, sources: dict, source_meta: dict):
        hid = tool_input.get("humboldt_id", "")
        limit = int(tool_input.get("limit") or 6)
        chunk_ids = self.entity_index.chunks_for(hid)
        if not chunk_ids:
            return {"error": f"Keine annotierten Textstellen für {hid}."}
        metas, blocks = [], []
        for cid in chunk_ids[:limit]:
            meta, block = self._chunk_payload(cid, sources, source_meta)
            if meta:
                metas.append(meta)
                blocks.append(block)
        if len(chunk_ids) > limit:
            metas.append({"hinweis": f"{len(chunk_ids) - limit} weitere Textstellen vorhanden."})
        return metas, blocks

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
        metas, blocks = [], []
        for r in results:
            meta, block = self._chunk_payload(r.chunk_id, sources, source_meta, score=r.fused_score)
            if meta:
                metas.append(meta)
                blocks.append(block)
        return metas, blocks

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
        out, blocks = [], []
        for m in matches:
            self._register_source(m["chunk_id"], sources, source_meta)
            out.append({
                "chunk_id": m["chunk_id"],
                "doc_type": m["doc_type"],
                "date": m["date_iso"],
            })
            chunk = self.retriever.chunk_store.get(m["chunk_id"])
            if chunk is not None:
                blocks.append(self._sr_block(chunk, m["match"], notes=[]))
        return out, blocks

    def _tool_get_context(self, tool_input: dict, sources: dict, source_meta: dict):
        ctx = self.retriever.get_context(tool_input.get("chunk_id", ""))
        if "error" in ctx:
            return ctx
        nr = self._register_source(ctx["chunk_id"], sources, source_meta)
        ctx["quelle_nr"] = nr
        chunk = self.retriever.chunk_store.get(ctx["chunk_id"])
        block = self._sr_block(chunk, ctx.pop("text")[:CONTEXT_TEXT_CHARS],
                               notes=ctx.pop("editorial_notes", None))
        return ctx, [block]

    def _tool_whereabouts(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        entries = self.retriever.whereabouts(
            date_from=tool_input.get("date_from", ""),
            date_to=tool_input.get("date_to"),
            limit=min(int(tool_input.get("limit") or 20), 60),
        )
        if not entries:
            return {"error": "Keine Chronologie-Einträge in diesem Zeitraum. "
                             "Versuche einen breiteren Bereich (z. B. nur Jahr-Monat)."}
        # itinerary lists are the biggest single context item — compact
        # pipe-rows instead of JSON objects, short excerpts, one note max
        out, blocks = ["datum|chunk_id|orte|url"], []
        for e in entries:
            self._register_source(e["chunk_id"], sources, source_meta)
            out.append("|".join([
                e["date"], e["chunk_id"], ",".join(e["places"][:4]), e["url"],
            ]))
            chunk = self.retriever.chunk_store.get(e["chunk_id"])
            if chunk is not None:
                blocks.append(self._sr_block(
                    chunk, e["text"][:300],
                    title=f"{e['citation_label']} — {e['date']}",
                    notes=[n[:250] for n in chunk.editorial_notes[:1]],
                ))
        return out, blocks

    @staticmethod
    def _upsert_artifact(artifacts: list, art: dict) -> None:
        """Replace an earlier artifact of the same type+title (models retry
        create_map after fixing unresolved places — don't show duplicates)."""
        artifacts[:] = [a for a in artifacts
                        if not (a.get("type") == art["type"]
                                and a.get("title") == art["title"])]
        artifacts.append(art)

    @staticmethod
    def _hint(lang: str | None, de: str, en: str, fr: str | None = None) -> str:
        """Tool-result hints arrive right before the model answers — German
        hints there drag English answers back into German."""
        if lang == "en":
            return en
        if lang == "fr":
            return fr or en
        return de

    def _tool_create_map(self, tool_input: dict, artifacts: list,
                         lang: str | None = None) -> dict:
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
        self._upsert_artifact(artifacts, {
            "type": "map",
            "title": title,
            "connect_route": bool(tool_input.get("connect_route", True)),
            "stations": resolved,
        })
        result = {
            "ok": True,
            "hinweis": self._hint(
                lang,
                f"Karte „{title}“ mit {len(resolved)} Stationen wird dem Nutzer "
                "unter deiner Antwort angezeigt. Erwähne sie kurz, beschreibe sie "
                "aber nicht Station für Station.",
                f"The map “{title}” with {len(resolved)} stations is displayed below "
                "your answer. Mention it briefly; do not describe it station by "
                "station. Answer in English.",
                f"La carte « {title} » avec {len(resolved)} étapes s'affiche sous ta "
                "réponse. Mentionne-la brièvement. Réponds en français."),
        }
        if unresolved:
            result["nicht_aufgeloest"] = unresolved
        return result

    def _tool_create_timeline(self, tool_input: dict, artifacts: list,
                              lang: str | None = None) -> dict:
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
        self._upsert_artifact(artifacts, {"type": "timeline", "title": title, "events": events})
        return {
            "ok": True,
            "hinweis": self._hint(
                lang,
                f"Zeitleiste „{title}“ mit {len(events)} Ereignissen wird dem "
                "Nutzer unter deiner Antwort angezeigt.",
                f"The timeline “{title}” with {len(events)} events is displayed "
                "below your answer. Answer in English.",
                f"La chronologie « {title} » avec {len(events)} événements "
                "s'affiche sous ta réponse. Réponds en français."),
        }

    def _tool_get_document(self, tool_input: dict, sources: dict, source_meta: dict):
        doc = self.retriever.get_document(tool_input.get("doc_id", ""))
        if "error" in doc:
            return doc
        # one citable search_result per leaf chunk, so citations resolve to the
        # exact passage instead of just "somewhere in this document"; the notes
        # ride on the blocks, so drop them (and the text) from the payload
        doc.pop("text", None)
        doc.pop("editorial_notes", None)
        blocks = []
        for cid in doc["chunk_ids"]:
            chunk = self.retriever.chunk_store.get(cid)
            if chunk is None:
                continue
            self._register_source(cid, sources, source_meta)
            blocks.append(self._sr_block(
                chunk, chunk.text_regularized,
                title=f"{doc['doc_title']} — {chunk.citation_label}",
            ))
        return doc, blocks

    def _tool_query_measurements(self, tool_input: dict, sources: dict, source_meta: dict) -> list | dict:
        rows = self.retriever.query_measurements(
            unit=tool_input.get("unit"),
            context_contains=tool_input.get("context_contains"),
            limit=int(tool_input.get("limit") or 25),
        )
        if not rows:
            return {"error": "Keine Messwerte gefunden. Versuche es ohne Filter oder mit "
                             "grep nach Zahlen+Einheit im Text."}
        out, blocks, seen = [], [], set()
        for r in rows:
            self._register_source(r["chunk_id"], sources, source_meta)
            out.append({
                "chunk_id": r["chunk_id"],
                "wert": r["value"],
                "einheit": r["unit"],
                "metrisch": f"{r['value_metric']} {r['unit_metric']}" if r["value_metric"] is not None else "",
                "kontext": r["context"][:120],
            })
            if r["chunk_id"] not in seen:
                seen.add(r["chunk_id"])
                chunk = self.retriever.chunk_store.get(r["chunk_id"])
                if chunk is not None:
                    blocks.append(self._sr_block(
                        chunk, chunk.text_regularized[:300],
                        title=f"{chunk.citation_label} (Messwerte)", notes=[],
                    ))
        return out, blocks

    # =========================================================================
    # Entity co-occurrence network
    # =========================================================================

    def _chunk_entity_map(self) -> dict[str, set[str]]:
        """chunk_id -> set of annotated humboldt_ids (lazy inverse index)."""
        if self._chunk_entities is None:
            m: dict[str, set[str]] = defaultdict(set)
            for hid, cids in self.entity_index.chunk_index.items():
                for cid in cids:
                    m[cid].add(hid)
            self._chunk_entities = m
        return self._chunk_entities

    def _tool_create_network(self, tool_input: dict, artifacts: list,
                             lang: str | None = None) -> dict:
        focus_in = (tool_input.get("focus") or "").strip()
        types = set(tool_input.get("entity_types") or [])
        max_nodes = min(int(tool_input.get("max_nodes") or 20), 40)
        min_shared = max(1, int(tool_input.get("min_shared") or 2))

        ent = self.entity_index.entities.get(focus_in)
        if ent is None:
            hits = self.entity_index.lookup(focus_in, limit=1)
            ent = hits[0] if hits else None
        if ent is None:
            return {"error": f"Entität „{focus_in}“ nicht im Register gefunden. "
                             "Prüfe die Schreibweise mit lookup_entity."}
        fid = ent["id"]
        focus_chunks = set(self.entity_index.chunks_for(fid))
        if not focus_chunks:
            return {"error": f"Keine annotierten Erwähnungen für {ent['name']} ({fid})."}

        cmap = self._chunk_entity_map()
        co: Counter = Counter()
        for cid in focus_chunks:
            for hid in cmap.get(cid, ()):
                if hid != fid:
                    co[hid] += 1

        # rank by distinctiveness (shared / sqrt(overall frequency)), so
        # ubiquitous hubs like "Europa" don't drown out the actual circle
        def affinity(hid: str, shared: int) -> float:
            total = len(self.entity_index.chunk_index.get(hid, ())) or 1
            return shared / total ** 0.5

        ranked = sorted(co.items(), key=lambda kv: affinity(*kv), reverse=True)

        def pick(threshold: int) -> list:
            picked = []
            for hid, w in ranked:
                rec = self.entity_index.entities.get(hid)
                if rec is None or w < threshold:
                    continue
                if types and rec["type"] not in types:
                    continue
                picked.append((hid, rec, w))
                if len(picked) >= max_nodes:
                    break
            return picked

        neighbors = pick(min_shared) or pick(1)
        if not neighbors:
            return {"error": f"Keine mit-erwähnten Registerentitäten für {ent['name']} gefunden."}

        nodes = [{"id": fid, "label": ent["name"], "type": ent["type"],
                  "weight": len(focus_chunks), "url": ent.get("url", ""), "focus": True}]
        edges = []
        nb_chunks = {}
        for hid, rec, w in neighbors:
            nodes.append({"id": hid, "label": rec["name"], "type": rec["type"],
                          "weight": w, "url": rec.get("url", "")})
            edges.append({"source": fid, "target": hid, "weight": w})
            nb_chunks[hid] = set(self.entity_index.chunks_for(hid)) & focus_chunks
        # edges among neighbors: co-occurrence within the focus passages;
        # capped to the strongest links so the layout stays readable
        cross = []
        for (h1, _, _), (h2, _, _) in itertools.combinations(neighbors, 2):
            shared = len(nb_chunks[h1] & nb_chunks[h2])
            if shared >= min_shared:
                cross.append({"source": h1, "target": h2, "weight": shared})
        cross.sort(key=lambda e: e["weight"], reverse=True)
        edges.extend(cross[: 2 * max_nodes])

        title = tool_input.get("title") or f"Beziehungsnetz: {ent['name']}"
        self._upsert_artifact(artifacts, {"type": "network", "title": title,
                                          "nodes": nodes, "edges": edges})
        return {
            "ok": True,
            "hinweis": self._hint(
                lang,
                f"Netzwerk „{title}“ mit {len(nodes)} Knoten und {len(edges)} "
                "Kanten wird dem Nutzer unter deiner Antwort angezeigt. Gehe in "
                "der Antwort auf die wichtigsten Verbindungen ein.",
                f"The network “{title}” with {len(nodes)} nodes and {len(edges)} "
                "edges is displayed below your answer. Discuss the most important "
                "connections. Answer in English.",
                f"Le réseau « {title} » ({len(nodes)} nœuds, {len(edges)} liens) "
                "s'affiche sous ta réponse. Réponds en français."),
            "zentrale_entitaet": {"name": ent["name"], "typ": ent["type"],
                                  "humboldt_id": fid, "passagen": len(focus_chunks)},
            "wichtigste_nachbarn": [
                {"name": rec["name"], "typ": rec["type"], "humboldt_id": hid,
                 "gemeinsame_passagen": w}
                for hid, rec, w in neighbors[:10]
            ],
        }

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
