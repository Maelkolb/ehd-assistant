"""
Ingestion pipeline: TEI-XML -> lexical indexes.

Produces three artifacts in data/processed/:
1. chunk_store.json   — every chunk (text, editorial notes, registers) by ID
2. bm25_text.json     — BM25 index over TEXT chunks only (registers excluded)
3. entity_index.json  — register-backed entity registry + inverted mention index

There is deliberately no dense/vector leg: the mock-embedding defaults of the
old pipeline silently poisoned retrieval. If embeddings return, they must be
added explicitly (see PLAN.md, Phase 1.7).

Usage:
    # Ingest the full corpus (after ./scripts/fetch_corpus.sh):
    python -m src.ingestion.ingest data/edh-source/data

    # Ingest a single file (debugging):
    python -m src.ingestion.ingest data/edh-source/data/letter/H0000003.xml
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path

from src.ingestion.tei_parser import TEIParser
from src.ingestion.chunking import ChunkingPipeline
from src.ingestion.register_parser import RegisterParser
from src.retrieval.keyword_search import BM25Index
from src.retrieval.entity_index import EntityIndex
from src.models import HumboldtChunk

logger = logging.getLogger(__name__)

# Corpus subdirectories containing text documents (searched via BM25)
TEXT_DIRS = ["travel-journal", "letter", "document", "chronology", "research-article"]
# Register subdirectories under index/ (entity resolution + definition lookup)
REGISTER_TYPES = {
    "person": "person",
    "place": "place",
    "institution": "institution",
    "glossary": "glossary",
    "plants": "plants",
}

MIN_EDITORIAL_NOTE_CHARS = 25


class IngestionPipeline:
    """TEI-XML -> chunk store + BM25 text index + entity index."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        (self.data_dir / "processed").mkdir(parents=True, exist_ok=True)

        self.parser = TEIParser()
        self.chunker = ChunkingPipeline()
        self.register_parser = RegisterParser()

        self.chunk_store: dict[str, HumboldtChunk] = {}
        self.bm25_index = BM25Index()          # text chunks only
        self.entity_index = EntityIndex()

    # =========================================================================
    # Text documents
    # =========================================================================

    def ingest_text_file(self, filepath: str | Path) -> int:
        """Parse + chunk one text document. Returns number of chunks stored."""
        raw_chunks = self.parser.parse(Path(filepath))
        chunks = self.chunker.process(raw_chunks)

        count = 0
        seen_notes: set[str] = set()
        for chunk in chunks:
            self.chunk_store[chunk.chunk_id] = chunk
            count += 1

            is_leaf = not chunk.child_chunk_ids
            if is_leaf:
                # Inverted entity index from the edition's annotations
                for ent in chunk.persons + chunk.places:
                    if ent.humboldt_id:
                        self.entity_index.add_mention(
                            ent.humboldt_id, ent.name, chunk.chunk_id
                        )
                # Editorial notes as first-class retrievable units
                for note in chunk.editorial_notes:
                    note_chunk = self._make_editorial_note_chunk(chunk, note, seen_notes)
                    if note_chunk is not None:
                        self.chunk_store[note_chunk.chunk_id] = note_chunk
                        count += 1

        return count

    def _make_editorial_note_chunk(
        self, anchor: HumboldtChunk, note: str, seen_notes: set[str]
    ) -> HumboldtChunk | None:
        note = note.strip()
        if len(note) < MIN_EDITORIAL_NOTE_CHARS:
            return None
        note_hash = hashlib.md5(f"{anchor.doc_id}:{note}".encode()).hexdigest()[:12]
        if note_hash in seen_notes:
            return None
        seen_notes.add(note_hash)

        return HumboldtChunk(
            doc_id=anchor.doc_id,
            chunk_id=f"ed_{note_hash}",
            doc_title=anchor.doc_title,
            doc_type="editorial_note",
            folio_start=anchor.folio_start,
            entry_number=anchor.entry_number,
            url=anchor.url,
            date_iso=anchor.date_iso,
            date_display=anchor.date_display,
            text_regularized=note,
            text_plain=note.lower(),
            source_desc=anchor.source_desc,
            parent_chunk_id=anchor.chunk_id,
        )

    # =========================================================================
    # Registers
    # =========================================================================

    def ingest_registers(self, index_dir: str | Path) -> int:
        """Parse register files; store entries and feed the entity registry."""
        index_dir = Path(index_dir)
        count = 0
        for dirname, reg_type in REGISTER_TYPES.items():
            dirpath = index_dir / dirname
            if not dirpath.exists():
                logger.warning(f"Register directory missing: {dirpath}")
                continue
            chunks = self.register_parser.parse_directory(dirpath, reg_type)
            for chunk in chunks:
                self.chunk_store[chunk.chunk_id] = chunk
                self._register_entity_from_chunk(chunk)
                count += 1
        return count

    def _register_entity_from_chunk(self, chunk: HumboldtChunk) -> None:
        if chunk.doc_type == "register_person" and chunk.persons:
            ent = chunk.persons[0]
            self.entity_index.add_register_entity(
                humboldt_id=ent.humboldt_id, name=ent.name, entity_type="person",
                description=chunk.text_regularized,
                register_chunk_id=chunk.chunk_id,
                gnd_id=ent.gnd_id, viaf_id=ent.viaf_id, url=chunk.url,
            )
        elif chunk.doc_type == "register_place" and chunk.places:
            ent = chunk.places[0]
            self.entity_index.add_register_entity(
                humboldt_id=ent.humboldt_id, name=ent.name, entity_type="place",
                description=chunk.text_regularized,
                register_chunk_id=chunk.chunk_id,
                geonames_id=ent.geonames_id, url=chunk.url,
            )
        else:
            # institution / glossary / plant: doc_id is the stable ID
            name = chunk.doc_title.split(": ", 1)[-1] if chunk.doc_title else ""
            etype = chunk.doc_type.replace("register_", "")
            self.entity_index.add_register_entity(
                humboldt_id=chunk.doc_id, name=name, entity_type=etype,
                description=chunk.text_regularized,
                register_chunk_id=chunk.chunk_id, url=chunk.url,
            )

    # =========================================================================
    # Corpus-level orchestration
    # =========================================================================

    def ingest_corpus(self, corpus_dir: str | Path) -> int:
        """
        Ingest the full edition corpus (the data/ dir of
        telota/edition-humboldt-digital).
        """
        corpus_dir = Path(corpus_dir)
        total = 0

        for subdir in TEXT_DIRS:
            dirpath = corpus_dir / subdir
            if not dirpath.exists():
                logger.warning(f"Text directory missing: {dirpath}")
                continue
            xml_files = sorted(dirpath.glob("*.xml"))
            logger.info(f"Ingesting {len(xml_files)} files from {subdir}/")
            for xml_file in xml_files:
                try:
                    total += self.ingest_text_file(xml_file)
                except Exception as e:
                    logger.error(f"Failed to ingest {xml_file.name}: {e}")

        index_dir = corpus_dir / "index"
        if index_dir.exists():
            total += self.ingest_registers(index_dir)

        self.build_indexes()
        self.save()
        return total

    def build_indexes(self) -> None:
        """Build BM25 over text chunks only — registers never enter ranking."""
        text_chunks = [
            c for c in self.chunk_store.values()
            if not c.doc_type.startswith("register_")
        ]
        self.bm25_index.build(text_chunks)
        logger.info(
            f"Indexes built: {len(text_chunks)} text chunks in BM25, "
            f"{len(self.chunk_store) - len(text_chunks)} register entries outside, "
            f"{len(self.entity_index.entities)} entities"
        )

    # =========================================================================
    # Persistence
    # =========================================================================

    def save(self) -> None:
        processed = self.data_dir / "processed"
        store_path = processed / "chunk_store.json"
        with open(store_path, "w", encoding="utf-8") as f:
            json.dump(
                {cid: c.to_dict() for cid, c in self.chunk_store.items()},
                f, ensure_ascii=False,
            )
        logger.info(f"Saved chunk store ({len(self.chunk_store)} chunks) to {store_path}")

        self.bm25_index.save(str(processed / "bm25_text.json"))
        self.entity_index.save(processed / "entity_index.json")

    def load(self) -> None:
        processed = self.data_dir / "processed"

        store_path = processed / "chunk_store.json"
        if store_path.exists():
            with open(store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.chunk_store = {
                cid: HumboldtChunk.from_dict(d) for cid, d in data.items()
            }
            logger.info(f"Loaded {len(self.chunk_store)} chunks from store")

        bm25_path = processed / "bm25_text.json"
        if bm25_path.exists():
            self.bm25_index.load(str(bm25_path))

        entity_path = processed / "entity_index.json"
        if entity_path.exists():
            self.entity_index.load(entity_path)
        self.entity_index.load_coords(processed / "coordinates.json")


# =============================================================================
# CLI
# =============================================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python -m src.ingestion.ingest <corpus-data-dir | xml-file>")
        print("  e.g.: python -m src.ingestion.ingest data/edh-source/data")
        sys.exit(1)

    path = Path(sys.argv[1])
    pipeline = IngestionPipeline()

    if path.is_dir():
        total = pipeline.ingest_corpus(path)
        print(f"Ingested {total} chunks.")
    elif path.is_file():
        n = pipeline.ingest_text_file(path)
        pipeline.build_indexes()
        pipeline.save()
        print(f"Ingested {n} chunks from {path.name}.")
    else:
        print(f"Error: {path} not found")
        sys.exit(1)


if __name__ == "__main__":
    main()
