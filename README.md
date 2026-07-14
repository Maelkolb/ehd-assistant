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
   └─ answer with [Quelle N] citations
```

- **Registers** (17.5k persons/places/plants/glossary/institutions) never enter text ranking; they serve entity resolution and "Wer war X?" lookups.
- **Editorial notes** are indexed as first-class chunks (`doc_type=editorial_note`) so the editors' voice is retrievable and attributable.
- The one-shot retrieve-then-generate path is kept behind `RETRIEVAL_MODE=oneshot` for A/B comparison; `/search` is the deterministic debug endpoint.

## Quick Start

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh setup     # install dependencies
./run.sh fetch     # clone telota/edition-humboldt-digital @ v11.0.0 (pinned)
./run.sh ingest    # build chunk store, BM25 text index, entity index
./run.sh serve     # http://localhost:8000
```

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required for generation |
| `LLM_MODEL` | `claude-haiku-4-5` | Claude model ID (widget can select Sonnet/Opus) |
| `RETRIEVAL_MODE` | `agentic` | `oneshot` = legacy pipeline |
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
