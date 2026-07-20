"""
Lexical retrieval over the BM25 text index, plus the low-level access
primitives the agentic loop exposes as tools (grep, context expansion).

Replaces the hybrid vector+BM25 retriever: the dense leg was contributing
noise (mock embeddings), so retrieval is now purely annotation- and
keyword-driven.
"""
from __future__ import annotations

import itertools
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
        # (date_iso, chunk_id) over dated narrative leaf chunks (chronology,
        # diaries, letters …), sorted — whereabouts lookups
        self.date_index: list[tuple[str, str]] = []
        # flat measurement rows for structured queries
        self.measurement_index: list[dict] = []
        for cid, chunk in chunk_store.items():
            if chunk.doc_type == "editorial_note" or chunk.doc_type.startswith("register_"):
                continue
            self.doc_map[chunk.doc_id].append(cid)
            if chunk.date_iso and not chunk.child_chunk_ids:
                self.date_index.append((chunk.date_iso, cid))
            for m in chunk.measurements:
                self.measurement_index.append({
                    "value": m.value,
                    "unit": m.unit,
                    "value_metric": m.value_metric,
                    "unit_metric": m.unit_metric,
                    "context": m.context,
                    "chunk_id": cid,
                })
        self.date_index.sort()

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

        Counts every match per chunk (not just the first) and ranks hits by
        match density, so a common pattern surfaces the chunks where it
        actually clusters instead of whatever was ingested first.
        """
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return [{"error": f"Invalid regex: {e}"}]

        matches = []
        scan_cap = limit * 4  # over-collect, then rank by density
        for cid, chunk in self.chunk_store.items():
            if chunk.doc_type.startswith("register_"):
                continue
            if doc_types and chunk.doc_type not in doc_types:
                continue
            if chunk.child_chunk_ids:  # skip parents/summaries: children carry the text
                continue
            text = chunk.text_regularized
            hits = list(itertools.islice(rx.finditer(text), 50))
            if not hits:
                continue
            m = hits[0]
            start = max(0, m.start() - context_chars // 2)
            end = min(len(text), m.end() + context_chars // 2)
            matches.append({
                "chunk_id": cid,
                "citation_label": chunk.citation_label,
                "doc_type": chunk.doc_type,
                "date_iso": chunk.date_iso,
                "url": chunk.url,
                "match": text[start:end],
                "match_count": len(hits),
            })
            if len(matches) >= scan_cap:
                break
        matches.sort(key=lambda m: m["match_count"], reverse=True)
        return matches[:limit]

    def whereabouts(self, date_from: str, date_to: str | None = None, limit: int = 20) -> list[dict]:
        """
        Chronology entries in an ISO date range (prefix-tolerant, same
        semantics as the BM25 date filters). date_to defaults to date_from,
        so a single month/day works.
        """
        date_from = (date_from or "").strip()
        date_to = (date_to or date_from).strip()
        if not date_from:
            return []
        out = []
        seen: set[tuple[str, str]] = set()  # one entry per (doc, date)
        for date_iso, cid in self.date_index:
            if date_iso < date_from and not date_from.startswith(date_iso):
                continue
            if date_iso > date_to and not date_iso.startswith(date_to):
                break
            chunk = self.chunk_store[cid]
            key = (chunk.doc_id, date_iso)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "chunk_id": cid,
                "date": date_iso,
                "doc_type": chunk.doc_type,
                "citation_label": chunk.citation_label,
                "text": chunk.text_regularized,
                "places": [p.name for p in chunk.places],
                "url": chunk.url,
            })
            if len(out) >= limit:
                break
        return out

    def get_document(self, doc_id: str, max_chars: int = 7000) -> dict:
        """Full narrative text of one document, in order, with chunk markers."""
        chunk_ids = self.doc_map.get(doc_id, [])
        if not chunk_ids:
            return {"error": f"Unbekannte doc_id: {doc_id}"}

        leafs = [
            self.chunk_store[cid] for cid in chunk_ids
            if not self.chunk_store[cid].child_chunk_ids
        ]
        first = leafs[0] if leafs else self.chunk_store[chunk_ids[0]]

        parts, used_ids, total, truncated = [], [], 0, False
        notes = []
        for chunk in leafs:
            seg = chunk.text_regularized
            if total + len(seg) > max_chars:
                seg = seg[: max(0, max_chars - total)]
                truncated = True
            parts.append(f"[{chunk.chunk_id}" +
                         (f" | Bl. {chunk.folio_start}" if chunk.folio_start else "") +
                         (f" | {chunk.date_iso}" if chunk.date_iso else "") +
                         f"]\n{seg}")
            used_ids.append(chunk.chunk_id)
            notes.extend(chunk.editorial_notes)
            total += len(seg)
            if truncated:
                break

        return {
            "doc_id": doc_id,
            "doc_title": first.doc_title,
            "doc_type": first.doc_type,
            "date": first.date_iso,
            "url": first.url,
            "text": "\n\n".join(parts),
            "chunk_ids": used_ids,
            "editorial_notes": notes[:10],
            "truncated": truncated,
        }

    def query_measurements(
        self, unit: str | None = None, context_contains: str | None = None,
        min_value: float | None = None, max_value: float | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """Structured search over parsed <measure> annotations. Value bounds
        apply to the original (historical) unit's value."""
        needle = (context_contains or "").lower()
        unit_l = (unit or "").lower()
        out = []
        for row in self.measurement_index:
            if unit_l and unit_l not in row["unit"].lower() \
                    and unit_l != (row["unit_metric"] or "").lower():
                continue
            if min_value is not None and row["value"] < min_value:
                continue
            if max_value is not None and row["value"] > max_value:
                continue
            if needle:
                chunk = self.chunk_store[row["chunk_id"]]
                haystack = (row["context"] + " " + chunk.text_regularized).lower()
                if needle not in haystack:
                    continue
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def get_context(self, chunk_id: str, radius: int = 1) -> dict:
        """
        Neighboring chunks and parent document context for one chunk.

        radius (1-3) controls how many siblings each side are returned, so
        the model can widen the reading window around a hit — and `position`
        ("i/N") lets it walk a document chunk by chunk without pulling the
        whole thing via get_document.
        """
        chunk = self.chunk_store.get(chunk_id)
        if chunk is None:
            return {"error": f"Unknown chunk_id: {chunk_id}"}
        radius = max(1, min(int(radius or 1), 3))

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
            out["position"] = f"{idx + 1}/{len(siblings)}"

            def neighbors(step: int) -> list[dict]:
                found = []
                j = idx + step
                while 0 <= j < len(siblings) and len(found) < radius:
                    n = self.chunk_store[siblings[j]]
                    if not n.doc_type.startswith("register_"):
                        found.append({
                            "chunk_id": n.chunk_id,
                            "citation_label": n.citation_label,
                            "date": n.date_iso,
                            "text": n.text_regularized[:700],
                        })
                    j += step
                return found

            prev = neighbors(-1)
            nxt = neighbors(+1)
            if prev:
                out["previous_chunks"] = prev[::-1]  # document order
            if nxt:
                out["next_chunks"] = nxt
        return out
