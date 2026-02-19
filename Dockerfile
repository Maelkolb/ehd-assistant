FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
    lxml>=4.9 pydantic>=2.0 numpy>=1.24 \
    fastapi>=0.104 "uvicorn[standard]>=0.24" \
    rank-bm25>=0.2.2 chromadb>=0.4 \
    sentence-transformers>=2.2 \
    google-cloud-aiplatform>=1.38 vertexai>=1.38

# Pre-download model AND tokenizer at build time
RUN python -c "from sentence_transformers import SentenceTransformer; \
    m = SentenceTransformer('intfloat/multilingual-e5-large-instruct'); \
    m.save('/app/models/e5-large-instruct')"

COPY src/ src/
COPY config/ config/
COPY static/ static/
COPY data/processed/ data/processed/
COPY data/vectordb/ data/vectordb/

# Force offline mode — never call HuggingFace at runtime
ENV TRANSFORMERS_OFFLINE=1
ENV HF_HUB_OFFLINE=1
ENV SENTENCE_TRANSFORMERS_HOME=/app/models
ENV EMBEDDING_MODEL=/app/models/e5-large-instruct
ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
