"""
BM25 keyword search for Humboldt corpus.

Complements vector search by handling:
- Exact name matching (persons, places)
- Historical spelling variants
- Domain-specific terminology (scientific instruments, units)
"""
from __future__ import annotations

import re
import logging
import json
from pathlib import Path
from collections import defaultdict

from src.models import HumboldtChunk

logger = logging.getLogger(__name__)


class BM25Index:
    """
    In-memory BM25 index for keyword search.

    For production, replace with Elasticsearch or OpenSearch.
    This implementation uses rank_bm25 for simplicity.
    """

    def __init__(self):
        self.documents: list[dict] = []  # {chunk_id, text, metadata}
        self.bm25 = None
        self._tokenized_corpus: list[list[str]] = []

    def build(self, chunks: list[HumboldtChunk]) -> None:
        """Build the BM25 index from chunks."""
        from rank_bm25 import BM25Okapi

        self.documents = []
        self._tokenized_corpus = []

        for chunk in chunks:
            # Index multiple text fields for better recall
            searchable_text = self._build_searchable_text(chunk)
            tokens = self._tokenize(searchable_text)

            self.documents.append({
                "chunk_id": chunk.chunk_id,
                "text": chunk.text_regularized,
                "metadata": {
                    "doc_id": chunk.doc_id,
                    "folio_start": chunk.folio_start,
                    "entry_number": chunk.entry_number,
                    "date_iso": chunk.date_iso,
                    "url": chunk.url,
                    "citation_label": chunk.citation_label,
                    "text_preview": chunk.text_regularized[:200],
                },
            })
            self._tokenized_corpus.append(tokens)

        self.bm25 = BM25Okapi(self._tokenized_corpus)
        logger.info(f"Built BM25 index with {len(self.documents)} documents")

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Search the BM25 index."""
        if self.bm25 is None:
            return []

        tokens = self._tokenize(query)
        scores = self.bm25.get_scores(tokens)

        # Get top-k results
        top_indices = scores.argsort()[-top_k:][::-1]

        results = []
        for idx in top_indices:
            if scores[idx] > 0:
                doc = self.documents[idx]
                results.append({
                    "chunk_id": doc["chunk_id"],
                    "score": float(scores[idx]),
                    "metadata": doc["metadata"],
                    "text": doc["text"][:200],
                    "source": "bm25",
                })

        return results

    def _build_searchable_text(self, chunk: HumboldtChunk) -> str:
        """
        Build a rich searchable text by combining multiple fields.
        Boosting entity names by repeating them.
        """
        parts = [chunk.text_regularized]

        # Boost person names (repeat for higher weight)
        for person in chunk.persons:
            parts.append(person.name)
            parts.append(person.name)  # Double-weight

        # Boost place names
        for place in chunk.places:
            parts.append(place.name)
            parts.append(place.name)

        # Add topics
        parts.extend(chunk.topics)

        # Add entry number
        if chunk.entry_number:
            parts.append(chunk.entry_number)

        # Add date as text
        if chunk.date_display:
            parts.append(chunk.date_display)

        return " ".join(parts)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Simple tokenization with German-aware processing."""
        text = text.lower()
        # Keep umlauts and accented characters
        tokens = re.findall(r'[\wäöüàáâéèêïîôùûç]+', text, re.UNICODE)
        # Remove very short tokens
        tokens = [t for t in tokens if len(t) > 1]
        return tokens

    def save(self, path: str) -> None:
        """Save index data to disk."""
        filepath = Path(path)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "documents": self.documents,
            "tokenized_corpus": self._tokenized_corpus,
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(f"Saved BM25 index to {path}")

    def load(self, path: str) -> None:
        """Load index data from disk."""
        from rank_bm25 import BM25Okapi

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.documents = data["documents"]
        self._tokenized_corpus = data["tokenized_corpus"]
        self.bm25 = BM25Okapi(self._tokenized_corpus)
        logger.info(f"Loaded BM25 index from {path} ({len(self.documents)} documents)")
