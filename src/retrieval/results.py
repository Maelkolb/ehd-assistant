"""Shared retrieval result container."""
from __future__ import annotations


class RetrievalResult:
    """Container for a retrieval result with per-source scores."""

    def __init__(
        self,
        chunk_id: str,
        metadata: dict | None = None,
        text: str = "",
    ):
        self.chunk_id = chunk_id
        self.metadata = metadata or {}
        self.text = text

        self.bm25_rank: int | None = None
        self.bm25_score: float = 0.0
        # kept for API compatibility with the retired dense leg
        self.vector_rank: int | None = None
        self.vector_score: float = 0.0

        self.fused_score: float = 0.0

    @property
    def citation_label(self) -> str:
        return self.metadata.get("citation_label", self.chunk_id)

    @property
    def url(self) -> str:
        return self.metadata.get("url", "")

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "fused_score": round(self.fused_score, 4),
            "bm25_score": round(self.bm25_score, 4),
            "citation_label": self.citation_label,
            "url": self.url,
            "text": self.text[:300] + "..." if len(self.text) > 300 else self.text,
        }

    def __repr__(self):
        return f"RetrievalResult(id={self.chunk_id}, score={self.fused_score:.4f})"
