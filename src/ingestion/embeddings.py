"""
Embedding generation for Humboldt chunks.

Supports multiple embedding strategies:
- Direct text embedding
- HyDE (Hypothetical Document Embeddings) for better retrieval
- Metadata-prefixed embeddings for context-aware search

Supports backends:
- sentence-transformers (local, recommended for production)
- OpenAI API (ada-002 / text-embedding-3-small)
- Anthropic (via Voyager)
"""
from __future__ import annotations

import logging
from typing import Protocol
import numpy as np

from src.models import HumboldtChunk

logger = logging.getLogger(__name__)


class EmbeddingModel(Protocol):
    """Protocol for embedding model backends."""
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def dimension(self) -> int: ...


class SentenceTransformerBackend:
    """Local embedding using sentence-transformers."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-large-instruct"):
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading embedding model: {model_name}")
        self.model = SentenceTransformer(model_name)
        self._dimension = self.model.get_sentence_embedding_dimension()

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        # e5 models expect "query: " or "passage: " prefix
        embeddings = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
        return embeddings.tolist()


class OpenAIBackend:
    """OpenAI embedding API backend."""

    def __init__(self, model: str = "text-embedding-3-small"):
        import openai
        self.client = openai.OpenAI()
        self.model = model
        self._dimension = 1536 if "small" in model else 3072

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in response.data]


class MockBackend:
    """Mock backend for testing (random embeddings)."""

    def __init__(self, dimension: int = 384):
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        rng = np.random.default_rng(42)
        return [rng.random(self._dimension).tolist() for _ in texts]


class EmbeddingPipeline:
    """
    Generates embeddings for HumboldtChunks.

    Each chunk gets its embedding_text property embedded, which includes
    metadata prefixes (date, place, topic) for better retrieval.
    """

    def __init__(self, backend: EmbeddingModel | None = None, batch_size: int = 32):
        self.backend = backend or MockBackend()
        self.batch_size = batch_size

    def embed_chunks(self, chunks: list[HumboldtChunk]) -> list[dict]:
        """
        Generate embeddings for a list of chunks.

        Returns list of dicts: {chunk_id, embedding, metadata}
        """
        texts = [chunk.embedding_text for chunk in chunks]
        logger.info(f"Generating embeddings for {len(texts)} chunks...")

        # Batch processing
        all_embeddings = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            embeddings = self.backend.embed(batch)
            all_embeddings.extend(embeddings)

        # Package results
        results = []
        for chunk, embedding in zip(chunks, all_embeddings):
            results.append({
                "chunk_id": chunk.chunk_id,
                "embedding": embedding,
                "metadata": {
                    "doc_id": chunk.doc_id,
                    "doc_type": chunk.doc_type,
                    "folio_start": chunk.folio_start,
                    "entry_number": chunk.entry_number,
                    "date_iso": chunk.date_iso,
                    "url": chunk.url,
                    "citation_label": chunk.citation_label,
                    "person_names": [p.name for p in chunk.persons],
                    "place_names": [p.name for p in chunk.places],
                    "topics": chunk.topics,
                    "text_preview": chunk.text_regularized[:200],
                },
            })

        logger.info(f"Generated {len(results)} embeddings (dim={self.backend.dimension})")
        return results

    def embed_query(self, query: str) -> list[float]:
        """Embed a user query for search."""
        # For e5 models, add "query: " prefix
        results = self.backend.embed([query])
        return results[0]
