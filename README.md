# Humboldt RAG System

A domain-specific RAG chatbot for the **edition humboldt digital** — the digital edition of Alexander von Humboldt's diaries and letters.

## Architecture

```
User Query
    │
    ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ Query        │────▶│ Hybrid       │────▶│ LLM          │
│ Understanding│     │ Retrieval    │     │ Generation   │
└──────────────┘     └──────────────┘     └──────────────┘
                      │    │    │
                      ▼    ▼    ▼
                   Vector BM25 Graph
                   Store  Index (Neo4j)
                   (Qdrant)
```

## Implementation Steps

### Phase 1: Data Ingestion ✅
- `src/ingestion/tei_parser.py` — Parse TEI-XML into structured chunks
- `src/ingestion/chunking.py` — Intelligent chunking by Humboldt's entry numbers
- `src/ingestion/embeddings.py` — Generate embeddings for all chunks
- `src/ingestion/ingest.py` — Orchestrate the full pipeline

### Phase 2: Retrieval
- `src/retrieval/vector_search.py` — Semantic search via Qdrant
- `src/retrieval/keyword_search.py` — BM25 keyword search
- `src/retrieval/graph_search.py` — Structured graph queries (Neo4j)
- `src/retrieval/hybrid.py` — Reciprocal Rank Fusion + Re-ranking

### Phase 3: Generation
- `src/generation/prompt.py` — System prompt & context assembly
- `src/generation/generate.py` — LLM generation with citation grounding
- `src/generation/verify.py` — Post-generation citation verification

### Phase 4: API & Frontend
- `src/api/main.py` — FastAPI backend
- `frontend/` — Chat widget (embeddable in edition-humboldt.de)

## Quick Start

```bash
pip install -r requirements.txt
# Ingest a single diary:
python -m src.ingestion.ingest data/raw/H0019734g.xml
# Start the API:
uvicorn src.api.main:app --reload
```

## Tech Stack
- **Vector DB:** Qdrant (or ChromaDB for local dev)
- **Keyword Search:** rank_bm25
- **Knowledge Graph:** Neo4j (optional, enhances structured queries)
- **Embeddings:** sentence-transformers (multilingual-e5-large-instruct)
- **LLM:** Anthropic Claude API (or OpenAI)
- **API:** FastAPI
- **Frontend:** React (lightweight chat widget)
