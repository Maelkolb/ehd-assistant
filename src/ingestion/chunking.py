"""
Intelligent chunking for Humboldt documents.

Handles:
- Splitting oversized chunks while preserving semantic boundaries
- Creating parent-child relationships for hierarchical retrieval
- Generating summary chunks for document-level retrieval
"""
from __future__ import annotations

import re
import logging
from src.models import HumboldtChunk

logger = logging.getLogger(__name__)

# Target chunk size in characters (roughly 200-400 tokens)
MAX_CHUNK_CHARS = 1500
MIN_CHUNK_CHARS = 100
OVERLAP_CHARS = 150  # Context overlap between splits


class ChunkingPipeline:
    """
    Takes raw parsed chunks and produces retrieval-ready chunks with:
    1. Size-appropriate splits (not too large, not too small)
    2. Parent-child relationships
    3. Metadata-enriched embedding text
    """

    def __init__(
        self,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        min_chunk_chars: int = MIN_CHUNK_CHARS,
        overlap_chars: int = OVERLAP_CHARS,
    ):
        self.max_chunk_chars = max_chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self.overlap_chars = overlap_chars

    def process(self, raw_chunks: list[HumboldtChunk]) -> list[HumboldtChunk]:
        """
        Process raw parser output into retrieval-optimized chunks.

        Steps:
        1. Filter out empty/trivial chunks
        2. Split oversized chunks at sentence boundaries
        3. Merge undersized chunks with neighbors
        4. Establish parent-child relationships
        """
        # Step 1: Filter
        chunks = [c for c in raw_chunks if len(c.text_regularized.strip()) >= self.min_chunk_chars]
        logger.info(f"After filtering: {len(chunks)} chunks (from {len(raw_chunks)})")

        # Step 2: Split oversized chunks
        split_chunks = []
        for chunk in chunks:
            if len(chunk.text_regularized) > self.max_chunk_chars:
                children = self._split_chunk(chunk)
                split_chunks.extend(children)
            else:
                split_chunks.append(chunk)

        logger.info(f"After splitting: {len(split_chunks)} chunks")

        # Step 3: Create document-level summary chunk
        if chunks:
            doc_summary = self._create_doc_summary(chunks)
            split_chunks.insert(0, doc_summary)

        return split_chunks

    def _split_chunk(self, chunk: HumboldtChunk) -> list[HumboldtChunk]:
        """
        Split an oversized chunk at sentence boundaries.
        Creates a parent chunk (with summary) and child chunks (with full text).
        """
        text = chunk.text_regularized
        sentences = self._sentence_split(text)

        if len(sentences) <= 1:
            return [chunk]  # Can't split further

        # Create sub-chunks by accumulating sentences
        sub_chunks = []
        current_text = ""
        current_start = 0

        for i, sentence in enumerate(sentences):
            if (len(current_text) + len(sentence) > self.max_chunk_chars
                    and len(current_text) >= self.min_chunk_chars):
                # Save current sub-chunk
                sub = self._create_sub_chunk(
                    parent=chunk,
                    text=current_text.strip(),
                    part_index=len(sub_chunks),
                )
                sub_chunks.append(sub)

                # Start new chunk with overlap
                overlap_text = self._get_overlap(current_text)
                current_text = overlap_text + " " + sentence
            else:
                current_text += " " + sentence

        # Don't forget the last sub-chunk
        if current_text.strip():
            sub = self._create_sub_chunk(
                parent=chunk,
                text=current_text.strip(),
                part_index=len(sub_chunks),
            )
            sub_chunks.append(sub)

        # Create parent chunk with summary text
        parent = HumboldtChunk(
            doc_id=chunk.doc_id,
            chunk_id=chunk.chunk_id,
            doc_title=chunk.doc_title,
            doc_type=chunk.doc_type,
            folio_start=chunk.folio_start,
            folio_end=chunk.folio_end,
            entry_number=chunk.entry_number,
            url=chunk.url,
            date_iso=chunk.date_iso,
            date_display=chunk.date_display,
            text_regularized=text[:500] + "..." if len(text) > 500 else text,
            text_plain=chunk.text_plain[:500],
            persons=chunk.persons,
            places=chunk.places,
            institutions=chunk.institutions,
            plants=chunk.plants,
            bibliography=chunk.bibliography,
            measurements=chunk.measurements,
            topics=chunk.topics,
            editorial_notes=chunk.editorial_notes,
            source_desc=chunk.source_desc,
            child_chunk_ids=[c.chunk_id for c in sub_chunks],
        )

        # Set parent reference on children
        for sub in sub_chunks:
            sub.parent_chunk_id = parent.chunk_id

        return [parent] + sub_chunks

    def _create_sub_chunk(
        self, parent: HumboldtChunk, text: str, part_index: int
    ) -> HumboldtChunk:
        """Create a child chunk inheriting parent metadata."""
        # Pre-compute a unique chunk_id including parent and part index
        import hashlib
        content = f"{parent.chunk_id}:part{part_index}:{text[:200]}"
        chunk_id = hashlib.md5(content.encode()).hexdigest()[:12]

        return HumboldtChunk(
            doc_id=parent.doc_id,
            chunk_id=chunk_id,
            doc_title=parent.doc_title,
            doc_type=parent.doc_type,
            folio_start=parent.folio_start,
            folio_end=parent.folio_end,
            entry_number=f"{parent.entry_number} (Teil {part_index + 1})" if parent.entry_number else "",
            url=parent.url,
            date_iso=parent.date_iso,
            date_display=parent.date_display,
            text_regularized=text,
            text_plain=text.lower(),
            # Inherit all entities from parent
            persons=parent.persons,
            places=parent.places,
            institutions=parent.institutions,
            plants=parent.plants,
            bibliography=parent.bibliography,
            measurements=[m for m in parent.measurements if m.context and m.context in text],
            topics=parent.topics,
            editorial_notes=[n for n in parent.editorial_notes if n[:30] in text],
            source_desc=parent.source_desc,
        )

    def _create_doc_summary(self, chunks: list[HumboldtChunk]) -> HumboldtChunk:
        """Create a document-level summary chunk for broad queries."""
        all_persons = {}
        all_places = {}
        all_dates = set()
        all_topics = set()

        for c in chunks:
            for p in c.persons:
                all_persons[p.name] = p
            for p in c.places:
                all_places[p.name] = p
            if c.date_iso:
                all_dates.add(c.date_iso)
            all_topics.update(c.topics)

        dates_sorted = sorted(all_dates)
        date_range = ""
        if dates_sorted:
            date_range = f"{dates_sorted[0]} bis {dates_sorted[-1]}"

        summary_text = (
            f"Dokument: {chunks[0].doc_title}. "
            f"Zeitraum: {date_range}. "
            f"Einträge: {len(chunks)}. "
            f"Erwähnte Personen: {', '.join(list(all_persons.keys())[:15])}. "
            f"Erwähnte Orte: {', '.join(list(all_places.keys())[:15])}. "
        )

        # Document-level URL: cut after the doc_id segment (drops any folio
        # suffix without ever cutting the doc_id itself).
        url = chunks[0].url
        if chunks[0].doc_id and chunks[0].doc_id in url:
            url = url[: url.index(chunks[0].doc_id) + len(chunks[0].doc_id)]

        return HumboldtChunk(
            doc_id=chunks[0].doc_id,
            doc_title=chunks[0].doc_title,
            doc_type=chunks[0].doc_type,
            folio_start=chunks[0].folio_start,
            folio_end=chunks[-1].folio_end,
            entry_number="Übersicht",
            url=url,
            date_iso=dates_sorted[0] if dates_sorted else "",
            date_display=date_range,
            text_regularized=summary_text,
            text_plain=summary_text.lower(),
            persons=list(all_persons.values()),
            places=list(all_places.values()),
            topics=list(all_topics),
            source_desc=chunks[0].source_desc,
            child_chunk_ids=[c.chunk_id for c in chunks],
        )

    # =========================================================================
    # Text utilities
    # =========================================================================

    @staticmethod
    def _sentence_split(text: str) -> list[str]:
        """Split text into sentences, handling German conventions."""
        # Split on sentence-ending punctuation followed by space + uppercase
        # Also handle period after abbreviations (careful with German titles)
        sentences = re.split(r'(?<=[.!?])\s+(?=[A-ZÄÖÜ])', text)
        # Also split on very long semicolon-separated clauses
        result = []
        for s in sentences:
            if len(s) > 800:
                sub = re.split(r';\s+', s)
                result.extend(sub)
            else:
                result.append(s)
        return [s.strip() for s in result if s.strip()]

    def _get_overlap(self, text: str) -> str:
        """Get the last N characters of text for overlap, at word boundary."""
        if len(text) <= self.overlap_chars:
            return text
        truncated = text[-self.overlap_chars:]
        # Find first word boundary
        space_idx = truncated.find(" ")
        if space_idx > 0:
            return truncated[space_idx:].strip()
        return truncated
