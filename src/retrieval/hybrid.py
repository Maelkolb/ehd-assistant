"""
Hybrid retrieval pipeline combining vector search, BM25, and graph queries.

Uses Reciprocal Rank Fusion (RRF) to merge results from different sources.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from src.models import HumboldtChunk
from src.retrieval.vector_search import VectorStore
from src.retrieval.keyword_search import BM25Index

logger = logging.getLogger(__name__)


class HybridRetriever:
    """
    Combines semantic search, keyword search, and graph queries
    into a unified retrieval pipeline.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        bm25_index: BM25Index,
        embedding_fn=None,
        chunk_store: dict[str, HumboldtChunk] | None = None,
        # RRF parameters
        vector_weight: float = 0.4,
        bm25_weight: float = 0.35,
        graph_weight: float = 0.25,
        rrf_k: int = 60,
    ):
        self.vector_store = vector_store
        self.bm25_index = bm25_index
        self.embedding_fn = embedding_fn
        self.chunk_store = chunk_store or {}
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight
        self.graph_weight = graph_weight
        self.rrf_k = rrf_k

    def retrieve(
        self,
        query: str,
        top_k: int = 8,
        filters: dict | None = None,
        use_vector: bool = True,
        use_bm25: bool = True,
        use_graph: bool = False,
    ) -> list[RetrievalResult]:
        """
        Execute hybrid retrieval.

        Args:
            query: User question
            top_k: Number of results to return
            filters: Metadata filters (date range, doc_type, etc.)
            use_vector: Enable vector search
            use_bm25: Enable BM25 search
            use_graph: Enable graph queries (requires Neo4j)

        Returns:
            Ranked list of RetrievalResult objects
        """
        all_results = {}

        # --- Vector search ---
        if use_vector and self.embedding_fn:
            query_embedding = self.embedding_fn(query)
            vector_hits = self.vector_store.search(
                query_embedding=query_embedding,
                top_k=top_k * 3,  # Retrieve more for fusion
                filters=filters,
            )
            for rank, hit in enumerate(vector_hits):
                cid = hit["chunk_id"]
                if cid not in all_results:
                    all_results[cid] = RetrievalResult(
                        chunk_id=cid,
                        metadata=hit["metadata"],
                        text=hit.get("text", ""),
                    )
                all_results[cid].vector_rank = rank
                all_results[cid].vector_score = hit["score"]

        # --- BM25 keyword search ---
        if use_bm25:
            bm25_hits = self.bm25_index.search(query, top_k=top_k * 3)
            for rank, hit in enumerate(bm25_hits):
                cid = hit["chunk_id"]
                if cid not in all_results:
                    all_results[cid] = RetrievalResult(
                        chunk_id=cid,
                        metadata=hit["metadata"],
                        text=hit.get("text", ""),
                    )
                all_results[cid].bm25_rank = rank
                all_results[cid].bm25_score = hit["score"]

        # --- Reciprocal Rank Fusion ---
        for result in all_results.values():
            result.fused_score = self._rrf_score(result)

        # Sort by fused score
        ranked = sorted(all_results.values(), key=lambda r: r.fused_score, reverse=True)

        # Enrich with full chunk data if available
        for r in ranked[:top_k]:
            if r.chunk_id in self.chunk_store:
                chunk = self.chunk_store[r.chunk_id]
                r.text = chunk.text_regularized
                r.metadata["citation_label"] = chunk.citation_label
                r.metadata["url"] = chunk.url
                r.metadata["editorial_notes"] = chunk.editorial_notes
                r.metadata["persons"] = [p.name for p in chunk.persons]
                r.metadata["places"] = [p.name for p in chunk.places]

        return ranked[:top_k]

    def _rrf_score(self, result: RetrievalResult) -> float:
        """Calculate Reciprocal Rank Fusion score."""
        score = 0.0
        k = self.rrf_k

        if result.vector_rank is not None:
            score += self.vector_weight * (1.0 / (k + result.vector_rank))

        if result.bm25_rank is not None:
            score += self.bm25_weight * (1.0 / (k + result.bm25_rank))

        if result.graph_rank is not None:
            score += self.graph_weight * (1.0 / (k + result.graph_rank))

        return score


class RetrievalResult:
    """Container for a retrieval result with scores from multiple sources."""

    def __init__(
        self,
        chunk_id: str,
        metadata: dict | None = None,
        text: str = "",
    ):
        self.chunk_id = chunk_id
        self.metadata = metadata or {}
        self.text = text

        # Per-source rankings
        self.vector_rank: int | None = None
        self.vector_score: float = 0.0
        self.bm25_rank: int | None = None
        self.bm25_score: float = 0.0
        self.graph_rank: int | None = None
        self.graph_score: float = 0.0

        # Fused score
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
            "vector_score": round(self.vector_score, 4),
            "bm25_score": round(self.bm25_score, 4),
            "citation_label": self.citation_label,
            "url": self.url,
            "text": self.text[:300] + "..." if len(self.text) > 300 else self.text,
            "sources_found_in": [
                s for s, r in [
                    ("vector", self.vector_rank),
                    ("bm25", self.bm25_rank),
                    ("graph", self.graph_rank),
                ] if r is not None
            ],
        }

    def __repr__(self):
        return f"RetrievalResult(id={self.chunk_id}, score={self.fused_score:.4f})"
