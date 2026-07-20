# Alex — ehd-assistant

**Alex** is a domain-specific research assistant for the **edition humboldt digital** — the digital edition of Alexander von Humboldt's diaries, letters, documents and chronology (Berlin-Brandenburgische Akademie der Wissenschaften).

Retrieval is **agentic and lexical**: Claude drives structured search tools built from the edition's own TEI annotations. There is no embedding/vector leg — the editors already solved spelling variance through entity annotation, which beats both embeddings and raw grep for this corpus. See `PLAN.md` for the full rationale.

## Architecture

```
User query
   │
   ▼
Agent loop (Claude + tools, max 4 tool rounds)
   ├─ lookup_entity(name)            → register index (fuzzy name → entity, bio, IDs)
   ├─ chunks_for_entity(humboldt_id) → inverted entity index (spelling-immune)
   ├─ search(query, doc_type?, date?)→ BM25 over TEXT chunks (registers excluded)
   ├─ grep(pattern, doc_type?)       → regex (exact phrases, measurements)
   ├─ get_context(chunk_id)          → neighboring passages / parent document
   ├─ where_was_humboldt(date range) → dated chronology/diary entries
   ├─ get_document(doc_id)           → full document text
   ├─ query_measurements(unit?, q?)  → structured <measure> annotations (+ metric)
   ├─ create_map / create_timeline   → interactive Leaflet map / visual timeline in the widget
   ├─ create_network(focus, types?)  → entity co-occurrence graph (persons/places/plants/institutions)
   ├─ code_execution (Sonnet/Opus)   → Anthropic server-side Python sandbox (calculations)
   └─ answer with span-level API citations
```

- **Citations**: retrieval results go to the model as citations-enabled `search_result`
  blocks, so every answer sentence carries exact quoted spans (Anthropic Citations API).
  The widget renders them as numbered chips; clicking opens a **source panel** with the
  cited passage highlighted, plus a deep link into edition-humboldt.de using
  [Text Fragments](https://developer.mozilla.org/en-US/docs/Web/URI/Fragment/Text_fragments)
  (`#:~:text=`). Fragments highlight reliably on research articles/chronology (modern
  German); on diary folios the diplomatic rendering differs from the regularized text,
  so links land on the correct page without the highlight. The legacy `[Quelle N]`
  regex path remains as fallback.
- **Registers** (17.5k persons/places/plants/glossary/institutions) never enter text ranking; they serve entity resolution and "Wer war X?" lookups. Text mentions are annotation-indexed for persons, places, institutions (`orgName`) and plants (`<name type="plant">`, resolved by Latin name against the plant register).
- **Editorial notes** are indexed as first-class chunks (`doc_type=editorial_note`) so the editors' voice is retrievable and attributable; inside search results they are marked `[Editorische Anmerkung der Herausgeber]` and citations into them are flagged editorial.
- **Prompt caching**: one breakpoint on system+tools, one advancing with the newest tool round.
- The one-shot retrieve-then-generate path is kept behind `RETRIEVAL_MODE=oneshot` for A/B comparison; `/search` is the deterministic debug endpoint.

## Quick Start

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh setup     # install dependencies
./run.sh fetch     # clone telota/edition-humboldt-digital @ v11.0.0 (pinned)
./run.sh ingest    # build chunk store, BM25 text index, entity index
./run.sh enrich    # resolve place coordinates via Wikidata (for maps)
./run.sh serve     # http://localhost:8000
```

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required for generation |
| `LLM_MODEL` | `claude-haiku-4-5` | Claude model ID (widget can select Sonnet/Opus) |
| `RETRIEVAL_MODE` | `agentic` | `oneshot` = legacy pipeline |
| `EFFORT` | unset (= `high`); deploys set `medium` | `low`/`medium`/`high`/`xhigh`/`max` — thinking/output budget on Sonnet/Opus (ignored on Haiku). `medium` measured at −22% cost with quality parity (`scripts/eval_cost.py` A/B, 2026-07). |
| `RATE_LIMIT_PER_HOUR` | `10` | Requests per IP per hour, any model. Per Cloud Run instance — deploy at `--max-instances=1` or this is effectively multiplied by the instance count. |
| `RATE_LIMIT_OPUS_PER_HOUR` | `2` | Opus-specific per-IP sub-limit; beyond it, Opus requests transparently fall back to Sonnet (not rejected). |
| `DAILY_REQUEST_BUDGET` | `500` | Global daily cap shared by all visitors (no per-IP daily cap by design). |
| `CITATION_REPAIR` | `1` | `0` disables the one-shot repair pass for uncited answers |
| `CORS_ORIGINS` | `*` (logs a warning) | comma-separated allowlist for production |
| `GEMINI_MODEL` | — | only if falling back to Vertex AI; env-only by design |

## Layout

- `scripts/fetch_corpus.sh` — pinned corpus download (reproducible `data/`)
- `src/ingestion/` — TEI parsing, chunking, register parsing, index build
- `src/retrieval/` — BM25 (`keyword_search.py`), entity index, lexical retriever
- `src/generation/agent.py` — the agentic tool loop (Claude)
- `src/generation/generate.py` — legacy one-shot generation
- `src/api/main.py` — FastAPI backend (`/chat`, `/search`, `/health`, `/stats`)
- `data/processed/` — build artifacts: `chunk_store.json`, `bm25_text.json`, `entity_index.json`

## Tech Stack
- **Search:** rank_bm25 + annotation-derived entity index (no vector DB)
- **LLM:** Anthropic Claude (native tool use); OpenAI/Gemini fallbacks
- **API:** FastAPI · **Deploy:** Docker/Cloud Run (slim image, no torch)
