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
    echo "  ingest      Ingest all XML files from data/raw/"
    echo "  ingest-real Ingest with real embeddings (requires GPU or patience)"
    echo "  serve       Start the API server"
    echo "  docker      Start everything with Docker Compose"
    echo "  test        Run pipeline test"
    echo ""
    echo "Quick start:"
    echo "  1. Put your XML files in data/raw/"
    echo "  2. Set ANTHROPIC_API_KEY in your environment"
    echo "  3. ./run.sh setup"
    echo "  4. ./run.sh ingest"
    echo "  5. ./run.sh serve"
}

cmd_setup() {
    echo -e "${GREEN}Installing dependencies...${NC}"
    pip install -r requirements.txt
    mkdir -p data/raw data/processed data/vectordb
    echo -e "${GREEN}✓ Setup complete${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Put Humboldt TEI-XML files in data/raw/"
    echo "  2. Run: ./run.sh ingest"
}

cmd_ingest() {
    echo -e "${GREEN}Ingesting XML files from data/raw/...${NC}"
    PYTHONPATH=. python -m src.ingestion.ingest data/raw/
    echo -e "${GREEN}✓ Ingestion complete${NC}"
}

cmd_ingest_real() {
    echo -e "${YELLOW}Ingesting with real embeddings (this may take a while)...${NC}"
    PYTHONPATH=. python -m src.ingestion.ingest data/raw/ --embeddings real
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
    ingest)      cmd_ingest ;;
    ingest-real) cmd_ingest_real ;;
    serve)       cmd_serve ;;
    docker)      cmd_docker ;;
    test)        cmd_test ;;
    *)           usage ;;
esac
