FROM python:3.12-slim
WORKDIR /app

# Lexical stack only — no torch, no sentence-transformers, no baked model.
# (Image went from ~3 GB to well under 300 MB; Cloud Run cold starts drop.)
RUN pip install --no-cache-dir \
    "lxml>=4.9" "pydantic>=2.0" "numpy>=1.24" \
    "fastapi>=0.104" "uvicorn[standard]>=0.24" \
    "rank-bm25>=0.2.2" \
    "anthropic>=0.116"

COPY src/ src/
COPY config/ config/
COPY static/ static/
# Indexes are build artifacts produced by:
#   ./scripts/fetch_corpus.sh && python -m src.ingestion.ingest data/edh-source/data
COPY data/processed/ data/processed/

# Model IDs are env-only. Set at deploy time:
#   ANTHROPIC_API_KEY, LLM_MODEL (default claude-sonnet-5), CORS_ORIGINS
ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
