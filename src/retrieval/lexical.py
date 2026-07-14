"""
Lexical retrieval over the BM25 text index, plus the low-level access
primitives the agentic loop exposes as tools (grep, context expansion).

Replaces the hybrid vector+BM25 retriever: the dense leg was contributing
noise (mock embeddings), so retrieval is now purely annotation- and
keyword-driven.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from src.models import HumboldtChunk
from src.retrieval.keyword_search import BM25Index
from src.retrieval.results import RetrievalResult

logger = logging.getLogger(__name__)


class LexicalRetriever:
    def __init__(self, bm25_index: BM25Index, chunk_store: dict[str, HumboldtChunk]):
        self.bm25_index = bm25_index
        self.chunk_store = chunk_store
        # doc_id -> narrative chunk_ids in insertion (= document) order, for
        # neighbor lookup. Editorial notes link via parent_chunk_id instead.
        self.doc_map: dict[str, list[str]] = defaultdict(list)
        for cid, chunk in chunk_store.items():
            if chunk.doc_type == "editorial_note" or chunk.doc_type.startswith("register_"):
                continue
            self.doc_map[chunk.doc_id].append(cid)

    def retrieve(
        self,
        query: str,
        top_k: int = 8,
        filters: dict | None = None,
        **_ignored,  # tolerate legacy use_vector/use_bm25 kwargs
    ) -> list[RetrievalResult]:
        hits = self.bm25_index.search(query, top_k=top_k, filters=filters)
        results = []
        for rank, hit in enumerate(hits):
            r = RetrievalResult(
                chunk_id=hit["chunk_id"],
                metadata=dict(hit["metadata"]),
                text=hit.get("text", ""),
            )
            r.bm25_rank = rank
            r.bm25_score = hit["score"]
            r.fused_score = hit["score"]
            self._enrich(r)
            results.append(r)
        return results

    def _enrich(self, r: RetrievalResult) -> None:
        chunk = self.chunk_store.get(r.chunk_id)
        if chunk is None:
            return
        r.text = chunk.text_regularized
        r.metadata["doc_type"] = chunk.doc_type
        r.metadata["citation_label"] = chunk.citation_label
        r.metadata["url"] = chunk.url
        r.metadata["date_iso"] = chunk.date_iso
        r.metadata["editorial_notes"] = chunk.editorial_notes
        r.metadata["persons"] = [p.name for p in chunk.persons]
        r.metadata["places"] = [p.name for p in chunk.places]

    def result_for_chunk(self, chunk_id: str, score: float = 0.0) -> RetrievalResult | None:
        """Wrap a chunk from the store as a RetrievalResult (for entity-index hits)."""
        chunk = self.chunk_store.get(chunk_id)
        if chunk is None:
            return None
        r = RetrievalResult(chunk_id=chunk_id, metadata={}, text=chunk.text_regularized)
        r.fused_score = score
        self._enrich(r)
        return r

    # =========================================================================
    # Agent tool primitives
    # =========================================================================

    def grep(
        self,
        pattern: str,
        doc_types: list[str] | None = None,
        limit: int = 20,
        context_chars: int = 240,
    ) -> list[dict]:
        """
        Regex search over the regularized text of non-register chunks.
        For exact phrases, measurements, units — not for concept search.
        """
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return [{"error": f"Invalid regex: {e}"}]

        matches = []
        for cid, chunk in self.chunk_store.items():
            if chunk.doc_type.startswith("register_"):
                continue
            if doc_types and chunk.doc_type not in doc_types:
                continue
            if chunk.child_chunk_ids:  # skip parents/summaries: children carry the text
                continue
            m = rx.search(chunk.text_regularized)
            if m:
                start = max(0, m.start() - context_chars // 2)
                end = min(len(chunk.text_regularized), m.end() + context_chars // 2)
                matches.append({
                    "chunk_id": cid,
                    "citation_label": chunk.citation_label,
                    "doc_type": chunk.doc_type,
                    "date_iso": chunk.date_iso,
                    "url": chunk.url,
                    "match": chunk.text_regularized[start:end],
                })
                if len(matches) >= limit:
                    break
        return matches

    def get_context(self, chunk_id: str) -> dict:
        """Neighboring chunks and parent document context for one chunk."""
        chunk = self.chunk_store.get(chunk_id)
        if chunk is None:
            return {"error": f"Unknown chunk_id: {chunk_id}"}

        out = {
            "chunk_id": chunk_id,
            "doc_id": chunk.doc_id,
            "doc_title": chunk.doc_title,
            "citation_label": chunk.citation_label,
            "url": chunk.url,
            "text": chunk.text_regularized,
            "editorial_notes": chunk.editorial_notes,
        }

        if chunk.parent_chunk_id:
            parent = self.chunk_store.get(chunk.parent_chunk_id)
            if parent is not None:
                out["parent_text"] = parent.text_regularized[:600]

        siblings = self.doc_map.get(chunk.doc_id, [])
        if chunk_id in siblings:
            idx = siblings.index(chunk_id)
            for offset, key in ((-1, "previous_chunk"), (1, "next_chunk")):
                j = idx + offset
                if 0 <= j < len(siblings):
                    neighbor = self.chunk_store[siblings[j]]
                    if not neighbor.doc_type.startswith("register_"):
                        out[key] = {
                            "chunk_id": neighbor.chunk_id,
                            "citation_label": neighbor.citation_label,
                            "text": neighbor.text_regularized[:600],
                        }
        return out
