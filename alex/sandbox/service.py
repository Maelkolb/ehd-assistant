"""The sandbox as its own small service.

Runs in a container that has no network route to the outside, no secrets and
a read-only filesystem; the API reaches it over an internal network:

    uvicorn alex.sandbox.service:app --host 0.0.0.0 --port 8001
"""

from __future__ import annotations

import hmac
import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from alex.sandbox.runner import MAX_CODE_CHARS, Sandbox

TOKEN = os.environ.get("SANDBOX_TOKEN", "")
sandbox = Sandbox(
    mode="subprocess",
    store_path=Path(os.environ.get("STORE_PATH", "data/store/ehd.sqlite")),
    corpus_dir=Path(os.environ["CORPUS_DIR"]) if os.environ.get("CORPUS_DIR") else None,
    timeout_seconds=int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "40")),
    memory_mb=int(os.environ.get("SANDBOX_MEMORY_MB", "1024")),
    max_parallel=int(os.environ.get("SANDBOX_MAX_PARALLEL", "2")),
)
app = FastAPI(title="Alex sandbox", docs_url=None, redoc_url=None, openapi_url=None)


class Job(BaseModel):
    code: str = Field(..., min_length=1, max_length=MAX_CODE_CHARS)


@app.post("/run")
def run(job: Job, authorization: str = Header(default="")):
    if TOKEN and not hmac.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "Unauthorized")
    return asdict(sandbox.run(job.code))


@app.get("/health")
def health():
    return {"status": "ok"}
