#!/usr/bin/env bash
# ===========================================================================
# Humboldt RAG System — Quick Start Script
# ===========================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
    echo "Usage: ./run.sh <command>"
    echo ""
    echo "Commands:"
    echo "  setup       Install dependencies"
    echo "  fetch       Download the TEI corpus (pinned tag) into data/edh-source/"
    echo "  ingest      Ingest the corpus into lexical indexes"
    echo "  serve       Start the API server"
    echo "  docker      Start everything with Docker Compose"
    echo "  test        Run pipeline test"
    echo ""
    echo "Quick start:"
    echo "  1. Set ANTHROPIC_API_KEY in your environment"
    echo "  2. ./run.sh setup && ./run.sh fetch && ./run.sh ingest"
    echo "  3. ./run.sh serve"
}

cmd_setup() {
    echo -e "${GREEN}Installing dependencies...${NC}"
    pip install -r requirements.txt
    mkdir -p data/processed
    echo -e "${GREEN}✓ Setup complete${NC}"
    echo ""
    echo "Next: ./run.sh fetch && ./run.sh ingest"
}

cmd_fetch() {
    ./scripts/fetch_corpus.sh
}

cmd_ingest() {
    echo -e "${GREEN}Ingesting corpus from data/edh-source/data ...${NC}"
    PYTHONPATH=. python -m src.ingestion.ingest data/edh-source/data
    echo -e "${GREEN}✓ Ingestion complete${NC}"
}

cmd_serve() {
    echo -e "${GREEN}Starting API server on http://localhost:8000${NC}"
    echo "  Docs: http://localhost:8000/docs"
    echo ""
    PYTHONPATH=. uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
}

cmd_docker() {
    echo -e "${GREEN}Starting with Docker Compose...${NC}"
    docker compose up --build
}

cmd_test() {
    echo -e "${GREEN}Running pipeline test...${NC}"
    PYTHONPATH=. python -m pytest tests/ -v
}

# Main
case "${1:-}" in
    setup)       cmd_setup ;;
    fetch)       cmd_fetch ;;
    ingest)      cmd_ingest ;;
    serve)       cmd_serve ;;
    docker)      cmd_docker ;;
    test)        cmd_test ;;
    *)           usage ;;
esac
