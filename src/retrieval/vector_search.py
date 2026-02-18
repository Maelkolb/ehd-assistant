"""
Vector store abstraction for semantic search.

Supports:
- ChromaDB (local development, zero-config)
- Qdrant (production, metadata filtering, hybrid search)
"""
from __future__ import annotations

import logging
from typing import Protocol, Any
from pathlib import Path

from src.models import HumboldtChunk

logger = logging.getLogger(__name__)


class VectorStore(Protocol):
    """Protocol for vector store backends."""
    def add(self, chunk_id: str, embedding: list[float], metadata: dict, text: str) -> None: ...
    def search(self, query_embedding: list[float], top_k: int = 10, filters: dict | None = None) -> list[dict]: ...
    def count(self) -> int: ...


class ChromaStore:
    """ChromaDB backend for local development."""

    def __init__(self, collection_name: str = "humboldt", persist_dir: str = "data/vectordb"):
        import chromadb

        persist_path = Path(persist_dir)
        persist_path.mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(path=str(persist_path))
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"ChromaDB collection '{collection_name}': {self.collection.count()} documents")

    def add(self, chunk_id: str, embedding: list[float], metadata: dict, text: str) -> None:
        # ChromaDB metadata must be flat (str, int, float, bool)
        flat_meta = {}
        for k, v in metadata.items():
            if isinstance(v, list):
                flat_meta[k] = ", ".join(str(x) for x in v) if v else ""
            elif isinstance(v, (str, int, float, bool)):
                flat_meta[k] = v
            else:
                flat_meta[k] = str(v)

        self.collection.upsert(
            ids=[chunk_id],
            embeddings=[embedding],
            metadatas=[flat_meta],
            documents=[text],
        )

    def add_batch(self, items: list[dict]) -> None:
        """Batch add for efficiency."""
        ids = [item["chunk_id"] for item in items]
        embeddings = [item["embedding"] for item in items]
        metadatas = []
        documents = []

        for item in items:
            flat_meta = {}
            for k, v in item["metadata"].items():
                if isinstance(v, list):
                    flat_meta[k] = ", ".join(str(x) for x in v) if v else ""
                elif isinstance(v, (str, int, float, bool)):
                    flat_meta[k] = v
                else:
                    flat_meta[k] = str(v)
            metadatas.append(flat_meta)
            documents.append(item["metadata"].get("text_preview", ""))

        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )
        logger.info(f"Added {len(items)} items to ChromaDB")

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[dict]:
        where = None
        if filters:
            where = {}
            for k, v in filters.items():
                if isinstance(v, str):
                    where[k] = {"$eq": v}
                elif isinstance(v, dict):
                    where[k] = v

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where if where else None,
            include=["metadatas", "documents", "distances"],
        )

        hits = []
        if results["ids"] and results["ids"][0]:
            for i, chunk_id in enumerate(results["ids"][0]):
                hits.append({
                    "chunk_id": chunk_id,
                    "score": 1 - (results["distances"][0][i] if results["distances"] else 0),
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "text": results["documents"][0][i] if results["documents"] else "",
                    "source": "vector",
                })
        return hits

    def count(self) -> int:
        return self.collection.count()


class QdrantStore:
    """
    Qdrant backend for production.
    Supports metadata filtering, hybrid search, and payload indexing.
    """

    def __init__(
        self,
        collection_name: str = "humboldt",
        url: str = "http://localhost:6333",
        embedding_dim: int = 1024,
    ):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self.client = QdrantClient(url=url)
        self.collection_name = collection_name

        # Create collection if it doesn't exist
        collections = [c.name for c in self.client.get_collections().collections]
        if collection_name not in collections:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=embedding_dim,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(f"Created Qdrant collection '{collection_name}'")

    def add(self, chunk_id: str, embedding: list[float], metadata: dict, text: str) -> None:
        from qdrant_client.models import PointStruct

        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                PointStruct(
                    id=chunk_id,
                    vector=embedding,
                    payload={**metadata, "text": text},
                )
            ],
        )

    def add_batch(self, items: list[dict]) -> None:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=item["chunk_id"],
                vector=item["embedding"],
                payload=item["metadata"],
            )
            for item in items
        ]

        self.client.upsert(
            collection_name=self.collection_name,
            points=points,
        )
        logger.info(f"Added {len(items)} items to Qdrant")

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[dict]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        qdrant_filter = None
        if filters:
            conditions = []
            for k, v in filters.items():
                conditions.append(FieldCondition(key=k, match=MatchValue(value=v)))
            qdrant_filter = Filter(must=conditions)

        results = self.client.search(
            collection_name=self.collection_name,
            query_vector=query_embedding,
            limit=top_k,
            query_filter=qdrant_filter,
        )

        return [
            {
                "chunk_id": str(hit.id),
                "score": hit.score,
                "metadata": hit.payload,
                "text": hit.payload.get("text_preview", ""),
                "source": "vector",
            }
            for hit in results
        ]

    def count(self) -> int:
        return self.client.count(collection_name=self.collection_name).count
