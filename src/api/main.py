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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.ingestion.ingest import IngestionPipeline
from src.retrieval.lexical import LexicalRetriever
from src.generation.generate import AnswerGenerator, MockGenerator

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load .env from the repo root; real environment variables win."""
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_dotenv()

# =============================================================================
# Global state (initialized on startup)
# =============================================================================

pipeline: IngestionPipeline | None = None
retriever: LexicalRetriever | None = None
generator: AnswerGenerator | MockGenerator | None = None
agent = None  # AgenticAnswerer when Claude is available
generation_healthy: bool = False

# Models selectable from the public widget. Requests with anything else
# fall back to the default (first entry / LLM_MODEL).
ALLOWED_MODELS = {
    "claude-haiku-4-5": "Haiku 4.5 (schnell)",
    "claude-sonnet-5": "Sonnet 5 (ausgewogen)",
    "claude-opus-4-8": "Opus 4.8 (gründlich)",
}


def _validated_model(requested: str | None) -> str | None:
    """Return the requested model if allowlisted, else None (= agent default)."""
    return requested if requested in ALLOWED_MODELS else None


MAX_HISTORY_TURNS = 12
MAX_TURN_CHARS = 4000


def _validated_history(history: list | None) -> list[dict]:
    """Sanitize client-provided conversation history (text turns only)."""
    out = []
    for m in (history or [])[-MAX_HISTORY_TURNS:]:
        if (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str) and m["content"].strip()):
            out.append({"role": m["role"], "content": m["content"][:MAX_TURN_CHARS]})
    while out and out[0]["role"] == "assistant":  # first message must be user
        out.pop(0)
    return out


class RateLimiter:
    """Per-IP sliding hour window + global daily request budget (in-memory,
    per instance — pair with --max-instances on Cloud Run)."""

    def __init__(self):
        import collections
        self.per_hour = int(os.environ.get("RATE_LIMIT_PER_HOUR", "30"))
        self.daily_budget = int(os.environ.get("DAILY_REQUEST_BUDGET", "500"))
        self.ip_hits: dict[str, "collections.deque"] = {}
        self._deque = collections.deque
        self.day = ""
        self.day_count = 0

    def check(self, ip: str) -> str | None:
        """Record a request; returns a refusal message or None if allowed."""
        import time as _t
        now = _t.time()
        today = _t.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_count = today, 0
        if self.day_count >= self.daily_budget:
            return ("Das Tageskontingent des Assistenten ist erschöpft — bitte morgen "
                    "wieder vorbeischauen. / Daily budget exhausted — please come back tomorrow.")
        dq = self.ip_hits.setdefault(ip, self._deque())
        while dq and dq[0] < now - 3600:
            dq.popleft()
        if len(dq) >= self.per_hour:
            return ("Zu viele Anfragen — bitte in einer Stunde erneut versuchen. / "
                    "Too many requests — please try again in an hour.")
        dq.append(now)
        self.day_count += 1
        if len(self.ip_hits) > 10000:  # bounded memory
            self.ip_hits = {k: v for k, v in self.ip_hits.items() if v and v[-1] > now - 3600}
        return None


rate_limiter = RateLimiter()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _generation_health_check(gen) -> None:
    """Fire one tiny generation call and log loudly on failure."""
    global generation_healthy
    if isinstance(gen, MockGenerator):
        logger.warning("Generation health check skipped: mock generator active.")
        generation_healthy = False
        return
    try:
        if isinstance(gen, AnswerGenerator) and gen.provider == "anthropic":
            gen.client.messages.create(
                model=gen.model, max_tokens=16,
                messages=[{"role": "user", "content": "ping"}],
            )
        elif isinstance(gen, AnswerGenerator) and gen.provider == "openai":
            gen.client.chat.completions.create(
                model=gen.model, max_tokens=16,
                messages=[{"role": "user", "content": "ping"}],
            )
        else:  # GeminiGenerator
            gen._utility.generate_content(
                "ping", generation_config=gen._gc(max_output_tokens=16)
            )
        generation_healthy = True
        logger.info(f"✓ Generation health check passed: {getattr(gen, 'model', '?')}")
    except Exception as e:
        generation_healthy = False
        logger.error("=" * 70)
        logger.error(f"GENERATION HEALTH CHECK FAILED: {e}")
        logger.error(f"Model: {getattr(gen, 'model', '?')} — the bot cannot answer questions.")
        logger.error("Check LLM_MODEL / GEMINI_MODEL and API credentials.")
        logger.error("=" * 70)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the RAG system on startup."""
    global pipeline, retriever, generator, agent

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Humboldt RAG system...")

    # Load ingested data
    data_dir = os.environ.get("DATA_DIR", "data")
    pipeline = IngestionPipeline(data_dir=data_dir)
    pipeline.load()

    if not pipeline.chunk_store:
        logger.error(
            "No data available! Run ./scripts/fetch_corpus.sh and then "
            "`python -m src.ingestion.ingest data/edh-source/data`."
        )

    # Initialize retriever (lexical only — no dense leg, see PLAN.md)
    retriever = LexicalRetriever(
        bm25_index=pipeline.bm25_index,
        chunk_store=pipeline.chunk_store,
    )

    # Initialize generator — Claude is the default backend.
    # Model IDs come from the environment only (LLM_MODEL / GEMINI_MODEL);
    # hardcoded model defaults have caused two silent outages.
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT")

    if anthropic_key:
        generator = AnswerGenerator(provider="anthropic", api_key=anthropic_key)
        logger.info(f"Using Anthropic Claude for generation: {generator.model}")
        # Agentic retrieval loop is the default answer path; the one-shot
        # retrieve-then-generate pipeline stays available via RETRIEVAL_MODE=oneshot.
        if os.environ.get("RETRIEVAL_MODE", "agentic") == "agentic":
            from src.generation.agent import AgenticAnswerer
            agent = AgenticAnswerer(
                retriever=retriever,
                entity_index=pipeline.entity_index,
                api_key=anthropic_key,
            )
            logger.info(f"Agentic retrieval loop enabled ({agent.model})")
    elif openai_key:
        generator = AnswerGenerator(provider="openai", model="gpt-4o", api_key=openai_key)
        logger.info("Using OpenAI GPT-4o for generation")
    elif gcp_project:
        try:
            from src.generation.gemini_backend import GeminiGenerator
            generator = GeminiGenerator(project_id=gcp_project, use_vertex=True)
            logger.info(f"Using Gemini via Vertex AI for generation: {generator.model}")
        except Exception as e:
            logger.error(f"Gemini init failed: {e}")
            generator = MockGenerator()
    else:
        generator = MockGenerator()
        logger.warning("No LLM API key found. Using mock generator.")

    # Startup health check: one tiny generation call, so a broken/retired
    # model ID fails loudly at deploy time instead of silently in production.
    _generation_health_check(generator)

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

_cors_origins = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
]
if _cors_origins == ["*"]:
    logger.warning("CORS is open to all origins — set CORS_ORIGINS in production.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Request / Response Models
# =============================================================================

class ChatRequest(BaseModel):
    query: str = Field(..., description="User question about Alexander von Humboldt")
    model: str | None = Field(default=None, description="Claude model (must be allowlisted)")
    history: list[dict] = Field(default=[], description="Prior turns [{role, content}]")
    top_k: int = Field(default=6, ge=1, le=20, description="Number of sources to retrieve")
    use_vector: bool = Field(default=True, description="Use semantic search")
    use_bm25: bool = Field(default=True, description="Use keyword search")

class Source(BaseModel):
    index: int
    citation_label: str
    url: str
    score: float
    doc_type: str = ""
    doc_title: str = ""
    text_preview: str = ""

class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]
    model: str
    chunks_retrieved: int
    trace: list[dict] = []
    artifacts: list[dict] = []  # maps/timelines/images rendered by the widget
    suggestions: list[str] = []

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
async def chat(request: ChatRequest, http_request: Request):
    """Main chat endpoint. Agentic retrieval loop by default;
    one-shot retrieve-then-generate as fallback (RETRIEVAL_MODE=oneshot)."""
    if retriever is None or generator is None:
        raise HTTPException(500, "System not initialized")

    refusal = rate_limiter.check(_client_ip(http_request))
    if refusal:
        raise HTTPException(429, refusal)

    if agent is not None:
        result = agent.generate(
            request.query,
            model=_validated_model(request.model),
            history=_validated_history(request.history),
        )
        return ChatResponse(
            answer=result["answer"],
            sources=[
                Source(
                    index=s["index"],
                    citation_label=s["citation_label"],
                    url=s.get("url", ""),
                    score=0.0,
                    doc_type=s.get("doc_type", ""),
                    doc_title=s.get("doc_title", ""),
                )
                for s in result["sources"]
            ],
            model=result["model"],
            chunks_retrieved=len(result["sources"]),
            trace=result.get("trace", []),
            artifacts=result.get("artifacts", []),
            suggestions=result.get("suggestions", []),
        )

    # --- legacy one-shot path ---
    # Stage 1: Query expansion (if generator supports it)
    if hasattr(generator, 'expand_query'):
        queries = generator.expand_query(request.query)
    else:
        queries = [request.query]

    # Stage 2: Multi-query retrieval + deduplication
    all_results = {}
    per_query_k = max(5, request.top_k // len(queries) + 2)
    for q in queries:
        results = retriever.retrieve(
            query=q,
            top_k=per_query_k,
            use_vector=request.use_vector,
            use_bm25=request.use_bm25,
        )
        for r in results:
            if r.chunk_id not in all_results or r.fused_score > all_results[r.chunk_id].fused_score:
                all_results[r.chunk_id] = r

    # Sort by fused score, take top results
    results = sorted(all_results.values(), key=lambda r: r.fused_score, reverse=True)
    results = results[:request.top_k + 4]  # Extra for reranking to filter

    # Stage 3+4: Generate (includes reranking inside generator)
    answer = generator.generate(
        query=request.query,
        retrieval_results=results,
    )

    # Format response
    if isinstance(answer, dict):
        answer_text = answer.get("answer", "")
        answer_model = getattr(generator, "model", "unknown")
        # Use sources from generator (post-reranking)
        gen_sources = answer.get("sources", [])
    else:
        answer_text = answer.text
        answer_model = getattr(answer, "model", "unknown")
        gen_sources = []

    sources = []
    if gen_sources:
        # Use the generator's source list (already reranked)
        for s in gen_sources:
            sources.append(Source(
                index=s.get("index", 0),
                citation_label=s.get("citation_label", ""),
                url=s.get("url", ""),
                score=0.0,
                doc_type=s.get("doc_type", ""),
                text_preview="",
            ))
    else:
        # Fallback: build from retrieval results
        for i, r in enumerate(results):
            sources.append(Source(
                index=i + 1,
                citation_label=r.citation_label or r.chunk_id,
                url=r.url or "",
                score=round(r.fused_score, 4),
                doc_type=r.metadata.get("doc_type", "") if hasattr(r, "metadata") else "",
                text_preview=r.text[:200] if r.text else "",
            ))

    return ChatResponse(
        answer=answer_text,
        sources=sources,
        model=answer_model,
        chunks_retrieved=len(results),
    )

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest, http_request: Request):
    """
    Streaming chat: Server-Sent Events with live agent-loop progress.
    Events: tool_call, tool_result, answer_delta, answer, suggestions, error.
    """
    if agent is None:
        raise HTTPException(400, "Agentic mode not enabled (RETRIEVAL_MODE!=agentic or no Claude key)")

    refusal = rate_limiter.check(_client_ip(http_request))
    if refusal:
        raise HTTPException(429, refusal)

    import json as _json
    from fastapi.responses import StreamingResponse

    def event_source():
        try:
            for event in agent.iter_events(
                request.query,
                model=_validated_model(request.model),
                history=_validated_history(request.history),
            ):
                yield f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("chat_stream failed")
            yield f"data: {_json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
        "status": "ok" if generation_healthy else "degraded",
        "generation_healthy": generation_healthy,
        "model": getattr(generator, "model", None),
        "chunks_loaded": len(pipeline.chunk_store) if pipeline else 0,
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
        "entities": len(pipeline.entity_index.entities),
        "date_range": {
            "earliest": min(all_dates) if all_dates else None,
            "latest": max(all_dates) if all_dates else None,
        },
    }

# --- Static file serving (appended fix) ---
from fastapi.responses import FileResponse
from pathlib import Path as _P
_static = _P(__file__).parent.parent.parent / "static"
if _static.exists():
    @app.get("/")
    async def _serve_index():
        return FileResponse(_static / "index.html")
