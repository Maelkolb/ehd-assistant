"""
Main ingestion pipeline — orchestrates the full flow from TEI-XML to searchable index.

Usage:
    # Ingest a single file:
    python -m src.ingestion.ingest data/raw/H0019734g.xml

    # Ingest all XML files in a directory:
    python -m src.ingestion.ingest data/raw/

    # Use real embeddings (requires sentence-transformers):
    python -m src.ingestion.ingest data/raw/ --embeddings real
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from src.ingestion.tei_parser import TEIParser
from src.ingestion.chunking import ChunkingPipeline
from src.ingestion.embeddings import EmbeddingPipeline, MockBackend, SentenceTransformerBackend
from src.retrieval.vector_search import ChromaStore
from src.retrieval.keyword_search import BM25Index
from src.models import HumboldtChunk

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """
    End-to-end ingestion from TEI-XML to indexed chunks.

    Steps:
    1. Parse TEI-XML → raw HumboldtChunks
    2. Chunk splitting & hierarchy → retrieval-ready chunks
    3. Generate embeddings
    4. Index in vector store + BM25
    5. Save chunk store for retrieval enrichment
    """

    def __init__(
        self,
        data_dir: str = "data",
        use_real_embeddings: bool = False,
        embedding_model: str = "intfloat/multilingual-e5-large-instruct",
        vector_store_type: str = "chroma",  # "chroma" or "qdrant"
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "processed").mkdir(exist_ok=True)
        (self.data_dir / "vectordb").mkdir(exist_ok=True)

        # Initialize components
        self.parser = TEIParser()
        self.chunker = ChunkingPipeline()

        # Embedding backend
        if use_real_embeddings:
            backend = SentenceTransformerBackend(embedding_model)
        else:
            backend = MockBackend(dimension=384)
        self.embedder = EmbeddingPipeline(backend=backend)

        # Vector store
        if vector_store_type == "chroma":
            self.vector_store = ChromaStore(
                persist_dir=str(self.data_dir / "vectordb")
            )
        else:
            from src.retrieval.vector_search import QdrantStore
            self.vector_store = QdrantStore()

        # BM25 index
        self.bm25_index = BM25Index()

        # Chunk store (all chunks by ID, for retrieval enrichment)
        self.chunk_store: dict[str, HumboldtChunk] = {}

    def ingest_file(self, filepath: str | Path) -> int:
        """Ingest a single TEI-XML file. Returns number of chunks created."""
        filepath = Path(filepath)
        logger.info(f"{'='*60}")
        logger.info(f"Ingesting: {filepath.name}")
        logger.info(f"{'='*60}")

        # Step 1: Parse
        raw_chunks = self.parser.parse(filepath)
        logger.info(f"  Parsed: {len(raw_chunks)} raw chunks")

        # Step 2: Chunk
        chunks = self.chunker.process(raw_chunks)
        logger.info(f"  Chunked: {len(chunks)} retrieval chunks")

        # Step 3: Embed
        embedded = self.embedder.embed_chunks(chunks)
        logger.info(f"  Embedded: {len(embedded)} vectors")

        # Step 4: Index
        self.vector_store.add_batch(embedded)
        logger.info(f"  Vector store: {self.vector_store.count()} total documents")

        # Step 5: Store chunks
        for chunk in chunks:
            self.chunk_store[chunk.chunk_id] = chunk

        return len(chunks)

    def ingest_directory(self, dirpath: str | Path) -> int:
        """Ingest all XML files in a directory."""
        dirpath = Path(dirpath)
        xml_files = sorted(dirpath.glob("*.xml"))
        logger.info(f"Found {len(xml_files)} XML files in {dirpath}")

        total = 0
        for xml_file in xml_files:
            total += self.ingest_file(xml_file)

        # Build BM25 index from all chunks
        all_chunks = list(self.chunk_store.values())
        self.bm25_index.build(all_chunks)
        self.bm25_index.save(str(self.data_dir / "processed" / "bm25_index.json"))

        # Save chunk store
        self._save_chunk_store()

        logger.info(f"\n{'='*60}")
        logger.info(f"INGESTION COMPLETE")
        logger.info(f"  Total chunks: {total}")
        logger.info(f"  Vector store: {self.vector_store.count()} documents")
        logger.info(f"  BM25 index: {len(all_chunks)} documents")
        logger.info(f"{'='*60}")

        return total

    def _save_chunk_store(self) -> None:
        """Persist chunk store as JSON for fast loading."""
        store_path = self.data_dir / "processed" / "chunk_store.json"
        data = {cid: chunk.to_dict() for cid, chunk in self.chunk_store.items()}
        with open(store_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved chunk store ({len(data)} chunks) to {store_path}")

    def load(self) -> None:
        """Load persisted chunk store and BM25 index."""
        # Load chunk store
        store_path = self.data_dir / "processed" / "chunk_store.json"
        if store_path.exists():
            with open(store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.chunk_store = {
                cid: HumboldtChunk.from_dict(d) for cid, d in data.items()
            }
            logger.info(f"Loaded {len(self.chunk_store)} chunks from store")

        # Load BM25
        bm25_path = self.data_dir / "processed" / "bm25_index.json"
        if bm25_path.exists():
            self.bm25_index.load(str(bm25_path))


# =============================================================================
# CLI
# =============================================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python -m src.ingestion.ingest <path> [--embeddings real]")
        print("  <path>: XML file or directory containing XML files")
        sys.exit(1)

    path = Path(sys.argv[1])
    use_real = "--embeddings" in sys.argv and "real" in sys.argv

    pipeline = IngestionPipeline(use_real_embeddings=use_real)

    if path.is_file():
        pipeline.ingest_file(path)
        # Also build BM25
        all_chunks = list(pipeline.chunk_store.values())
        pipeline.bm25_index.build(all_chunks)
        pipeline.bm25_index.save("data/processed/bm25_index.json")
        pipeline._save_chunk_store()
    elif path.is_dir():
        pipeline.ingest_directory(path)
    else:
        print(f"Error: {path} not found")
        sys.exit(1)


if __name__ == "__main__":
    main()
