"""
Entity index built from the edition's own annotations.

Two structures:
1. Entity registry: humboldt_id -> normalized entity (name, type, authority IDs,
   register description). Built from the register files.
2. Inverted chunk index: humboldt_id -> [chunk_ids]. Built from the persName/
   placeName annotations in the text documents.

This is what makes entity lookup immune to historical spelling variants:
the editors already resolved "Cocodrilo"/"Crocodile" etc. to stable IDs.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """Lowercase, strip diacritics and punctuation for name matching."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


class EntityIndex:
    def __init__(self):
        # humboldt_id -> entity record
        self.entities: dict[str, dict] = {}
        # normalized name -> set of humboldt_ids
        self.name_map: dict[str, set[str]] = defaultdict(set)
        # humboldt_id -> list of chunk_ids where the entity is annotated
        self.chunk_index: dict[str, list[str]] = defaultdict(list)

    # =========================================================================
    # Build
    # =========================================================================

    def add_register_entity(
        self,
        humboldt_id: str,
        name: str,
        entity_type: str,
        description: str = "",
        register_chunk_id: str = "",
        gnd_id: str = "",
        geonames_id: str = "",
        viaf_id: str = "",
        url: str = "",
    ) -> None:
        if not humboldt_id or not name:
            return
        rec = self.entities.setdefault(humboldt_id, {
            "id": humboldt_id,
            "name": name,
            "type": entity_type,
            "description": "",
            "register_chunk_id": "",
            "gnd": "", "geonames": "", "viaf": "",
            "url": "",
            "alt_names": [],
        })
        rec["name"] = name
        rec["type"] = entity_type
        if description:
            rec["description"] = description
        if register_chunk_id:
            rec["register_chunk_id"] = register_chunk_id
        if gnd_id:
            rec["gnd"] = gnd_id
        if geonames_id:
            rec["geonames"] = geonames_id
        if viaf_id:
            rec["viaf"] = viaf_id
        if url:
            rec["url"] = url
        self._register_name(name, humboldt_id)
        # "Surname, Forename" should also match "Forename Surname"
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            if len(parts) == 2 and parts[1]:
                self._register_name(f"{parts[1]} {parts[0]}", humboldt_id)

    def add_mention(self, humboldt_id: str, name_as_written: str, chunk_id: str) -> None:
        """Record that an entity is annotated in a text chunk."""
        if not humboldt_id:
            return
        chunks = self.chunk_index[humboldt_id]
        if chunk_id not in chunks:
            chunks.append(chunk_id)
        if name_as_written:
            norm = normalize_name(name_as_written)
            if norm:
                self.name_map[norm].add(humboldt_id)
                rec = self.entities.get(humboldt_id)
                if rec is not None and name_as_written != rec["name"] \
                        and name_as_written not in rec["alt_names"]:
                    rec["alt_names"].append(name_as_written)

    def _register_name(self, name: str, humboldt_id: str) -> None:
        norm = normalize_name(name)
        if norm:
            self.name_map[norm].add(humboldt_id)
        # index the surname token alone as well ("Bonpland" for "Bonpland, Aimé");
        # skip short leading tokens like "La" (place-name articles)
        tokens = norm.split()
        if len(tokens) > 1 and len(tokens[0]) >= 4:
            self.name_map[tokens[0]].add(humboldt_id)

    # =========================================================================
    # Query
    # =========================================================================

    def lookup(self, name: str, entity_type: str | None = None, limit: int = 5) -> list[dict]:
        """
        Fuzzy name -> entity resolution. Match order:
        exact normalized name, token/substring containment, then difflib.
        Results are ranked by number of annotated mentions (prominence).
        """
        norm = normalize_name(name)
        if not norm:
            return []

        ids: set[str] = set(self.name_map.get(norm, set()))

        if len(ids) < limit:
            query_tokens = set(norm.split())
            for key, key_ids in self.name_map.items():
                # query contained in a longer indexed name ("bonpland" in
                # "aime bonpland"), or an indexed name that is a whole token of
                # the query — never bare substring matches of short keys.
                if (len(norm) >= 3 and norm in key) or \
                        (len(key) >= 4 and key in query_tokens):
                    ids.update(key_ids)
                if len(ids) >= limit * 4:
                    break

        if not ids:
            close = difflib.get_close_matches(norm, self.name_map.keys(), n=8, cutoff=0.8)
            for key in close:
                ids.update(self.name_map[key])

        results = []
        for hid in ids:
            rec = self.entities.get(hid)
            if rec is None:
                # entity annotated in text but with no register entry
                rec = {"id": hid, "name": name, "type": "unknown", "description": "",
                       "register_chunk_id": "", "gnd": "", "geonames": "", "viaf": "",
                       "url": "", "alt_names": []}
            if entity_type and rec["type"] not in (entity_type, "unknown"):
                continue
            out = dict(rec)
            out["mention_count"] = len(self.chunk_index.get(hid, []))
            results.append(out)

        results.sort(key=lambda r: r["mention_count"], reverse=True)
        return results[:limit]

    def chunks_for(self, humboldt_id: str) -> list[str]:
        return list(self.chunk_index.get(humboldt_id, []))

    # =========================================================================
    # Persistence
    # =========================================================================

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "entities": self.entities,
            "name_map": {k: sorted(v) for k, v in self.name_map.items()},
            "chunk_index": dict(self.chunk_index),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(f"Saved entity index ({len(self.entities)} entities, "
                    f"{len(self.chunk_index)} with mentions) to {path}")

    def load(self, path: str | Path) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.entities = data["entities"]
        self.name_map = defaultdict(set, {k: set(v) for k, v in data["name_map"].items()})
        self.chunk_index = defaultdict(list, data["chunk_index"])
        logger.info(f"Loaded entity index: {len(self.entities)} entities, "
                    f"{len(self.chunk_index)} with mentions")
