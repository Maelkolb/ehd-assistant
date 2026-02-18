"""
Configuration for the Humboldt RAG system.

All settings can be overridden via environment variables.
"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    # === Paths ===
    data_dir: str = os.environ.get("DATA_DIR", "data")
    raw_dir: str = os.environ.get("RAW_DIR", "data/raw")

    # === Embedding ===
    embedding_backend: str = os.environ.get("EMBEDDING_BACKEND", "mock")  # "mock", "sentence-transformers", "openai"
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large-instruct")
    embedding_dim: int = int(os.environ.get("EMBEDDING_DIM", "384"))

    # === Vector Store ===
    vector_store: str = os.environ.get("VECTOR_STORE", "chroma")  # "chroma" or "qdrant"
    qdrant_url: str = os.environ.get("QDRANT_URL", "http://localhost:6333")

    # === LLM ===
    llm_provider: str = os.environ.get("LLM_PROVIDER", "anthropic")  # "anthropic" or "openai"
    llm_model: str = os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")

    # === Retrieval ===
    retrieval_top_k: int = int(os.environ.get("RETRIEVAL_TOP_K", "8"))
    vector_weight: float = float(os.environ.get("VECTOR_WEIGHT", "0.4"))
    bm25_weight: float = float(os.environ.get("BM25_WEIGHT", "0.35"))

    # === Chunking ===
    max_chunk_chars: int = int(os.environ.get("MAX_CHUNK_CHARS", "1500"))
    chunk_overlap: int = int(os.environ.get("CHUNK_OVERLAP", "150"))

    # === API ===
    api_host: str = os.environ.get("API_HOST", "0.0.0.0")
    api_port: int = int(os.environ.get("API_PORT", "8000"))
    cors_origins: list[str] = None  # Set in production

    def __post_init__(self):
        if self.cors_origins is None:
            origins = os.environ.get("CORS_ORIGINS", "*")
            self.cors_origins = [o.strip() for o in origins.split(",")]


config = Config()
