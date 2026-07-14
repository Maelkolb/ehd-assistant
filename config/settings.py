"""
Configuration for the Humboldt retrieval system.

All settings can be overridden via environment variables.
"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    # === Paths ===
    data_dir: str = os.environ.get("DATA_DIR", "data")
    corpus_dir: str = os.environ.get("CORPUS_DIR", "data/edh-source/data")

    # === LLM ===
    llm_provider: str = os.environ.get("LLM_PROVIDER", "anthropic")  # "anthropic", "openai", or "gemini"
    llm_model: str = os.environ.get("LLM_MODEL", "claude-sonnet-5")
    # Gemini model is env-only on purpose: hardcoded defaults rotted twice
    # (gemini-2.0-flash retired 2026-06-01, gemini-2.5-flash retires 2026-10-16).
    gemini_model: str = os.environ.get("GEMINI_MODEL", "")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")

    # === Retrieval ===
    retrieval_mode: str = os.environ.get("RETRIEVAL_MODE", "agentic")  # "agentic" or "oneshot"
    retrieval_top_k: int = int(os.environ.get("RETRIEVAL_TOP_K", "8"))

    # === Chunking ===
    max_chunk_chars: int = int(os.environ.get("MAX_CHUNK_CHARS", "1500"))
    chunk_overlap: int = int(os.environ.get("CHUNK_OVERLAP", "150"))

    # === API ===
    api_host: str = os.environ.get("API_HOST", "0.0.0.0")
    api_port: int = int(os.environ.get("API_PORT", "8000"))
    cors_origins: list[str] = None  # parsed from CORS_ORIGINS

    def __post_init__(self):
        if self.cors_origins is None:
            origins = os.environ.get("CORS_ORIGINS", "*")
            self.cors_origins = [o.strip() for o in origins.split(",")]


config = Config()
