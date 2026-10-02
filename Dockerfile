# syntax=docker/dockerfile:1
# One image for both services: the API (default command) and the sandbox.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git fonts-paratype \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY alex/__init__.py alex/__init__.py
RUN pip install .
COPY alex/ alex/
RUN pip install --no-deps .

# The document store is built from the edition's original TEI dump at image
# build time. CORPUS_REF is a branch, tag or commit of the dump repository;
# "main" is its newest state. Pin a commit for reproducible images.
FROM base AS corpus
ARG CORPUS_REPOSITORY=https://github.com/telota/edition-humboldt-digital.git
ARG CORPUS_REF=main
ENV CORPUS_REPOSITORY=${CORPUS_REPOSITORY} CORPUS_REF=${CORPUS_REF} DATA_DIR=/data
COPY data/geo/ /data/geo/
RUN alex fetch && alex build && rm -rf /data/ehd-source/.git

FROM base AS runtime
ENV DATA_DIR=/data MPLBACKEND=Agg
COPY --from=corpus /data /data
RUN useradd --system --uid 10001 --home-dir /nonexistent alex
USER alex
# PORT is honoured so the same image runs on platforms that assign one (Cloud Run).
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3     CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/api/health', timeout=4)"
CMD ["sh", "-c", "exec uvicorn alex.api.app:create_app --factory --host 0.0.0.0 --port ${PORT} --workers 1"]
