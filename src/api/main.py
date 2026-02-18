"""
FastAPI backend for the Humboldt RAG chatbot.

Endpoints:
- POST /chat     — Main chat endpoint
- POST /search   — Search without generation (for debugging)
- GET  /health   — Health check
- GET  /stats    — Index statistics
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.ingestion.ingest import IngestionPipeline
from src.ingestion.embeddings import EmbeddingPipeline, MockBackend
from src.retrieval.hybrid import HybridRetriever
from src.generation.generate import AnswerGenerator, MockGenerator

logger = logging.getLogger(__name__)

# =============================================================================
# Global state (initialized on startup)
# =============================================================================

pipeline: IngestionPipeline | None = None
retriever: HybridRetriever | None = None
generator: AnswerGenerator | MockGenerator | None = None


def _load_query_embedding_fn():
    """
    Load an embedding function for query-time use.
    Tries sentence-transformers (CPU is fine for single queries),
    then falls back to mock.
    """
    model_name = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large-instruct")
    try:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading embedding model for queries: {model_name} (CPU)...")
        model = SentenceTransformer(model_name, device="cpu")

        def embed_query(text: str) -> list[float]:
            # e5-instruct models expect "query: " prefix
            result = model.encode(
                [f"query: {text}"],
                normalize_embeddings=True,
            )
            return result[0].tolist()

        logger.info(f"✓ Query embeddings: {model_name} on CPU")
        return embed_query

    except ImportError:
        logger.warning("sentence-transformers not installed. Using mock embeddings.")
        logger.warning("Install with: pip install sentence-transformers")
        embed_pipeline = EmbeddingPipeline(backend=MockBackend(384))
        return embed_pipeline.embed_query


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the RAG system on startup."""
    global pipeline, retriever, generator

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Humboldt RAG system...")

    # Load ingested data
    data_dir = os.environ.get("DATA_DIR", "data")
    pipeline = IngestionPipeline(data_dir=data_dir)
    pipeline.load()

    # If no data loaded, check for raw files to ingest
    if not pipeline.chunk_store:
        raw_dir = Path(data_dir) / "raw"
        if raw_dir.exists() and list(raw_dir.glob("*.xml")):
            logger.info("No processed data found. Running initial ingestion...")
            pipeline.ingest_directory(raw_dir)

    if not pipeline.chunk_store:
        logger.error("No data available! Run ingestion first or check DATA_DIR.")

    # Set up embedding function for queries
    embed_fn = _load_query_embedding_fn()

    # Initialize retriever
    retriever = HybridRetriever(
        vector_store=pipeline.vector_store,
        bm25_index=pipeline.bm25_index,
        embedding_fn=embed_fn,
        chunk_store=pipeline.chunk_store,
    )

    # Initialize generator
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if anthropic_key:
        generator = AnswerGenerator(provider="anthropic", api_key=anthropic_key)
        logger.info("Using Anthropic Claude for generation")
    elif openai_key:
        generator = AnswerGenerator(provider="openai", model="gpt-4o", api_key=openai_key)
        logger.info("Using OpenAI GPT-4o for generation")
    else:
        generator = MockGenerator()
        logger.warning("No LLM API key found. Using mock generator.")
        logger.warning("Set ANTHROPIC_API_KEY or OPENAI_API_KEY to enable real generation.")

    logger.info(f"System ready. {len(pipeline.chunk_store)} chunks loaded.")

    yield

    logger.info("Shutting down Humboldt RAG system.")


# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Humboldt RAG API",
    description="Domain-specific chatbot for the edition humboldt digital",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Request / Response Models
# =============================================================================

class ChatRequest(BaseModel):
    query: str = Field(..., description="User question about Alexander von Humboldt")
    top_k: int = Field(default=6, ge=1, le=20, description="Number of sources to retrieve")
    use_vector: bool = Field(default=True, description="Use semantic search")
    use_bm25: bool = Field(default=True, description="Use keyword search")

class Source(BaseModel):
    citation_label: str
    url: str
    score: float
    text_preview: str = ""

class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]
    model: str
    chunks_retrieved: int

class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=50)

class SearchResult(BaseModel):
    chunk_id: str
    score: float
    citation_label: str
    url: str
    text_preview: str
    found_in: list[str]

class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int


# =============================================================================
# Endpoints
# =============================================================================

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Main chat endpoint — retrieve context and generate answer."""
    if retriever is None or generator is None:
        raise HTTPException(500, "System not initialized")

    # Retrieve
    results = retriever.retrieve(
        query=request.query,
        top_k=request.top_k,
        use_vector=request.use_vector,
        use_bm25=request.use_bm25,
    )

    # Generate
    answer = generator.generate(
        query=request.query,
        retrieval_results=results,
    )

    # Format response
    sources = []
    for r in results:
        sources.append(Source(
            citation_label=r.citation_label,
            url=r.url,
            score=round(r.fused_score, 4),
            text_preview=r.text[:200] if r.text else "",
        ))

    return ChatResponse(
        answer=answer.text,
        sources=sources,
        model=answer.model,
        chunks_retrieved=len(results),
    )


@app.post("/search", response_model=SearchResponse)
async def search(request: SearchRequest):
    """Search endpoint — retrieval only, no generation."""
    if retriever is None:
        raise HTTPException(500, "System not initialized")

    results = retriever.retrieve(query=request.query, top_k=request.top_k)

    search_results = []
    for r in results:
        search_results.append(SearchResult(
            chunk_id=r.chunk_id,
            score=round(r.fused_score, 4),
            citation_label=r.citation_label,
            url=r.url,
            text_preview=r.text[:300] if r.text else "",
            found_in=[
                s for s, rank in [
                    ("vector", r.vector_rank),
                    ("bm25", r.bm25_rank),
                ] if rank is not None
            ],
        ))

    return SearchResponse(results=search_results, total=len(search_results))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "chunks_loaded": len(pipeline.chunk_store) if pipeline else 0,
        "vector_count": pipeline.vector_store.count() if pipeline else 0,
    }


@app.get("/stats")
async def stats():
    """Get detailed index statistics."""
    if pipeline is None:
        raise HTTPException(500, "System not initialized")

    chunks = list(pipeline.chunk_store.values())
    all_persons = set()
    all_places = set()
    all_dates = set()
    doc_ids = set()

    for c in chunks:
        doc_ids.add(c.doc_id)
        for p in c.persons:
            all_persons.add(p.name)
        for p in c.places:
            all_places.add(p.name)
        if c.date_iso:
            all_dates.add(c.date_iso)

    return {
        "total_chunks": len(chunks),
        "documents": len(doc_ids),
        "unique_persons": len(all_persons),
        "unique_places": len(all_places),
        "date_range": {
            "earliest": min(all_dates) if all_dates else None,
            "latest": max(all_dates) if all_dates else None,
        },
        "vector_count": pipeline.vector_store.count(),
    }
