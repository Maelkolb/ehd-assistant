"""The web application: the chat stream, the source panel's data, the static client."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from alex import __version__
from alex.api.ratelimit import RateLimiter, Refusal
from alex.corpus.store import DocumentStore, Passage, StoreError
from alex.runtime import Runtime, create_runtime
from alex.sandbox.runner import shield_this_process

logger = logging.getLogger("alex.api")

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_QUERY_CHARS = 2000
MAX_HISTORY_TURNS = 12
MAX_TURN_CHARS = 6000
MAX_PASSAGE_CHARS = 20_000
CONTEXT_STEP = 1500


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    model: str | None = None
    history: list[dict] = Field(default_factory=list)
    language: str = "de"


class Feedback(BaseModel):
    request_id: str = Field(..., max_length=40)
    rating: str = Field(..., pattern="^(up|down)$")
    comment: str = Field(default="", max_length=2000)


def clean_history(history: list[dict]) -> list[dict]:
    """Client-held history: text turns only, bounded, starting with the user."""
    turns = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        if (
            isinstance(turn, dict)
            and turn.get("role") in ("user", "assistant")
            and isinstance(turn.get("content"), str)
            and turn["content"].strip()
        ):
            turns.append({"role": turn["role"], "content": turn["content"][:MAX_TURN_CHARS]})
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)
    return turns


def passage_payload(store: DocumentStore, passage: Passage) -> dict:
    document = store.document(passage.doc_id)
    length = document["n_chars"]
    pages = [
        {"label": page["label"], "start": page["start"], "facsimile": page["facsimile"]}
        for page in store.pages(passage.doc_id)
        if passage.start < page["start"] < passage.end
    ]
    current = next(
        (page for page in reversed(store.pages(passage.doc_id)) if page["start"] <= passage.start),
        None,
    )
    before = (
        store.snapped(passage.doc_id, max(0, passage.start - CONTEXT_STEP), passage.end, 0, length)[
            0
        ]
        if passage.start > 0
        else None
    )
    after = (
        store.snapped(
            passage.doc_id, passage.start, min(length, passage.end + CONTEXT_STEP), 0, length
        )[1]
        if passage.end < length
        else None
    )
    return {
        "document": {
            key: document[key]
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
        },
        "start": passage.start,
        "end": passage.end,
        "page": passage.page,
        "url": passage.url,
        "facsimile": current["facsimile"] if current else "",
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
        "before": before,
        "after": after,
    }


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or create_runtime()
    settings, store, agent = runtime.settings, runtime.store, runtime.agent
    limiter = RateLimiter(settings)
    app = FastAPI(
        title="Alex", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.runtime = runtime

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    tile_host = re.match(r"https?://[^/]+", settings.map_tile_url)
    tile_host = tile_host.group().replace("{s}", "*") if tile_host else ""
    content_security_policy = "; ".join(
        [
            "default-src 'self'",
            f"img-src 'self' data: blob: {tile_host}",
            "style-src 'self' 'unsafe-inline'",
            "script-src 'self'",
            "connect-src 'self'",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            f"frame-ancestors {' '.join(settings.frame_ancestors)}",
        ]
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", content_security_policy)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if request.url.path.startswith("/assets/vendor/") or request.url.path.startswith(
            "/assets/fonts/"
        ):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        elif request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    if runtime.sandbox.mode == "subprocess":
        shield_this_process()
        # the first run builds matplotlib's font cache; do it before a user waits for it
        threading.Thread(target=runtime.sandbox.run, args=("print('ready')",), daemon=True).start()

    @app.get("/api/config")
    def config():
        statistics = agent.statistics
        return {
            "assistant": "Alex",
            "version": __version__,
            "corpus": {
                "version": statistics["corpus_version"],
                "commit": statistics["corpus_commit"],
                "commit_date": statistics["corpus_commit_date"],
                "documents": statistics["documents"],
                "entities": statistics["entities"],
            },
            "models": [
                {
                    "id": spec.id,
                    "label": spec.label,
                    "provider": spec.provider,
                    "tier": spec.tier,
                    "default": spec.id == settings.default_model,
                }
                for spec in settings.models
            ],
            "features": {
                "python": runtime.sandbox.enabled,
                "feedback": bool(settings.feedback_log),
            },
            "limits": {"max_query_chars": MAX_QUERY_CHARS, "max_history_turns": MAX_HISTORY_TURNS},
            "links": settings.links,
            "map": {
                "tile_url": settings.map_tile_url,
                "attribution": settings.map_attribution,
                "max_zoom": 18,
            },
        }

    @app.post("/api/chat")
    def chat(body: ChatRequest, request: Request):
        language = body.language if body.language in ("de", "en") else "de"
        spec = settings.model(body.model)
        if spec is None:
            raise HTTPException(503, "No language model is configured.")
        try:
            spec = limiter.admit(limiter.client_key(request), spec)
        except Refusal as refusal:
            raise HTTPException(429, refusal.messages[language]) from refusal
        history = clean_history(body.history)

        def events():
            started = time.time()
            try:
                for event in agent.run(body.query, spec, history, language):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception:
                logger.exception("chat failed")
                failure = {
                    "type": "error",
                    "code": "internal",
                    "message": "Internal error." if language == "en" else "Interner Fehler.",
                }
                yield f"data: {json.dumps(failure)}\n\n"
            finally:
                limiter.release()
                logger.debug("stream closed after %.1f s", time.time() - started)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/passage")
    def passage(
        doc_id: str = Query(..., max_length=40),
        start: int = Query(0, ge=0),
        end: int = Query(..., ge=0),
    ):
        if end - start > MAX_PASSAGE_CHARS or end < start:
            raise HTTPException(422, f"A passage may span at most {MAX_PASSAGE_CHARS} characters.")
        try:
            return passage_payload(store, store.passage(doc_id, start, end))
        except StoreError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/notes/{note_id}")
    def note(note_id: int):
        try:
            found = store.note_passage(note_id)
        except StoreError as error:
            raise HTTPException(404, str(error)) from error
        length = len(store.text(found.doc_id))
        start, end = store.snapped(
            found.doc_id, max(0, found.start - 400), min(length, found.end + 600), 0, length
        )
        return {
            "note": {
                "note_id": found.note_id,
                "kind": found.kind,
                "label": "",
                "text": found.text,
                "anchor_start": found.start,
                "anchor_end": found.end,
            },
            "passage": passage_payload(store, store.passage(found.doc_id, start, end)),
        }

    @app.get("/api/entities/{entity_id}")
    def entity(entity_id: str):
        found = store.entity(entity_id)
        if found is None:
            raise HTTPException(404, "Unknown entity.")
        found.pop("entity_no", None)
        return found

    @app.post("/api/feedback", status_code=204)
    def feedback(body: Feedback):
        if settings.feedback_log:
            record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **body.model_dump()}
            with open(settings.feedback_log, "a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        return Response(status_code=204)

    @app.get("/api/health")
    def health():
        ready = bool(settings.models)
        payload = {
            "status": "ok" if ready else "unavailable",
            "corpus_version": agent.statistics["corpus_version"],
            "models": [spec.id for spec in settings.models],
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    if WEB_ROOT.exists():
        if (WEB_ROOT / "assets").exists():
            app.mount("/assets", StaticFiles(directory=WEB_ROOT / "assets"), name="assets")

        @app.get("/widget.js")
        def widget():
            return FileResponse(
                WEB_ROOT / "widget.js",
                media_type="text/javascript",
                headers={"Cache-Control": "no-cache"},
            )

        @app.get("/")
        def index():
            return FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-cache"})

    return app
