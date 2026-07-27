"""
Core data models for the Humboldt RAG system.
These models are used across ingestion, retrieval, and generation.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import json
import hashlib


@dataclass
class Entity:
    """A named entity (person, place, work) with authority file links."""
    name: str
    entity_type: str  # "person", "place", "bibliography"
    humboldt_id: str = ""  # edition-humboldt.de ID (e.g. "H0012069")
    gnd_id: str = ""       # Gemeinsame Normdatei
    geonames_id: str = ""  # GeoNames
    viaf_id: str = ""      # VIAF
    lat: Optional[float] = None
    lon: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "type": self.entity_type}
        if self.gnd_id:
            d["gnd"] = self.gnd_id
        if self.geonames_id:
            d["geonames"] = self.geonames_id
        if self.humboldt_id:
            d["humboldt_id"] = self.humboldt_id
        if self.lat is not None:
            d["lat"] = self.lat
            d["lon"] = self.lon
        return d


@dataclass
class Measurement:
    """A scientific measurement with original and (optionally) converted units."""
    value: float
    unit: str
    context: str = ""
    value_metric: Optional[float] = None
    unit_metric: str = ""

    # Conversion factors for common Humboldt-era units
    CONVERSIONS = {
        "toise": ("meter", 1.949),
        "pied_de_roi": ("meter", 0.3248),
        "reaumur": ("celsius", lambda r: r * 1.25),  # °Ré → °C
        "ligne": ("millimeter", 2.2558),
        "pouce": ("centimeter", 2.707),
    }

    def convert_to_metric(self) -> "Measurement":
        if self.unit in self.CONVERSIONS:
            target_unit, factor = self.CONVERSIONS[self.unit]
            if callable(factor):
                self.value_metric = round(factor(self.value), 2)
            else:
                self.value_metric = round(self.value * factor, 2)
            self.unit_metric = target_unit
        return self

    def to_dict(self) -> dict:
        d = {"value": self.value, "unit": self.unit, "context": self.context}
        if self.value_metric is not None:
            d["value_metric"] = self.value_metric
            d["unit_metric"] = self.unit_metric
        return d


@dataclass
class HumboldtChunk:
    """
    A semantically meaningful unit of text from a Humboldt document.
    This is the fundamental unit for embedding, indexing, and retrieval.
    """
    # === Identification ===
    doc_id: str              # Document ID, e.g. "H0019734"
    chunk_id: str = ""       # Unique chunk ID (auto-generated)
    doc_title: str = ""
    doc_type: str = ""       # "diary", "letter", "chronology", "literature"

    # === Location within document ===
    folio_start: str = ""    # e.g. "55r"
    folio_end: str = ""      # e.g. "55v" (if chunk spans pages)
    entry_number: str = ""   # Humboldt's own numbering, e.g. "N. 1"
    section_title: str = ""  # If available

    # === URLs ===
    url: str = ""            # Direct link to edition-humboldt.de
    url_xml: str = ""        # Link to XML source
    url_images: str = ""     # Link to facsimile images

    # === Text layers ===
    text_diplomatic: str = ""    # Original text with abbreviations
    text_regularized: str = ""   # Text with expanded abbreviations
    text_editorial: str = ""     # Including editorial notes inline
    text_plain: str = ""         # Clean text for embedding (no markup)

    # === Temporal metadata ===
    date_iso: str = ""           # "1797-06-14"
    date_not_before: str = ""    # For ranges
    date_not_after: str = ""
    date_display: str = ""       # Human-readable: "14. Juni 1797"

    # === Correspondence (letters only, from TEI correspDesc) ===
    sender_id: str = ""          # humboldt_id of sender
    sender_name: str = ""
    recipient_id: str = ""
    recipient_name: str = ""
    origin_place_id: str = ""
    origin_place: str = ""

    # === Extracted entities ===
    persons: list[Entity] = field(default_factory=list)
    places: list[Entity] = field(default_factory=list)
    institutions: list[Entity] = field(default_factory=list)
    plants: list[Entity] = field(default_factory=list)
    bibliography: list[Entity] = field(default_factory=list)

    # === Scientific data ===
    measurements: list[Measurement] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)  # From Humboldt's own index

    # === Editorial apparatus ===
    editorial_notes: list[str] = field(default_factory=list)
    source_desc: str = ""  # Archival shelfmark

    # === For retrieval ===
    parent_chunk_id: str = ""  # ID of parent (for hierarchical chunking)
    child_chunk_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.chunk_id:
            # Generate deterministic ID from content + position
            content = (
                f"{self.doc_id}:{self.folio_start}:{self.folio_end}:"
                f"{self.entry_number}:{self.parent_chunk_id}:"
                f"{self.text_regularized[:300]}"
            )
            self.chunk_id = hashlib.md5(content.encode()).hexdigest()[:12]

        # Auto-convert measurements
        for m in self.measurements:
            m.convert_to_metric()

    @property
    def embedding_text(self) -> str:
        """
        Text optimized for embedding generation.
        Includes key metadata as natural-language prefix for better retrieval.
        """
        parts = []

        if self.doc_title:
            parts.append(f"Quelle: {self.doc_title}")
        if self.date_display or self.date_iso:
            parts.append(f"Datum: {self.date_display or self.date_iso}")
        if self.places:
            place_names = ", ".join(p.name for p in self.places[:5])
            parts.append(f"Orte: {place_names}")
        if self.topics:
            parts.append(f"Themen: {', '.join(self.topics)}")

        parts.append(self.text_regularized)

        return "\n".join(parts)

    @property
    def citation_label(self) -> str:
        """Human-readable citation for this chunk."""
        type_labels = {
            "diary": "Tagebuch",
            "letter": "Brief",
            "chronology": "Chronologie",
            "document": "Dokument",
            "research_article": "Forschungsbeitrag",
            "editorial_note": "Editorischer Kommentar",
        }
        if self.doc_type.startswith("register_") or self.doc_type in (
            "register_glossary", "register_plant"
        ):
            return self.doc_title or self.doc_id
        parts = []
        if self.doc_type in type_labels:
            parts.append(type_labels[self.doc_type])
        if self.folio_start:
            parts.append(f"Bl. {self.folio_start}")
        if self.date_display:
            parts.append(self.date_display)
        elif self.date_iso:
            parts.append(self.date_iso)
        return ", ".join(parts) if parts else self.doc_id

    def to_dict(self) -> dict:
        d = {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "doc_type": self.doc_type,
            "folio_start": self.folio_start,
            "folio_end": self.folio_end,
            "entry_number": self.entry_number,
            "url": self.url,
            "date_iso": self.date_iso,
            "date_display": self.date_display,
            "text_regularized": self.text_regularized,
            "text_plain": self.text_plain,
            "embedding_text": self.embedding_text,
            "citation_label": self.citation_label,
            "persons": [p.to_dict() for p in self.persons],
            "places": [p.to_dict() for p in self.places],
            "institutions": [p.to_dict() for p in self.institutions],
            "plants": [p.to_dict() for p in self.plants],
            "measurements": [m.to_dict() for m in self.measurements],
            "topics": self.topics,
            "editorial_notes": self.editorial_notes,
            "source_desc": self.source_desc,
            "parent_chunk_id": self.parent_chunk_id,
        }
        # correspondence fields only on letters — keeps the other 24k chunks lean
        if self.sender_id or self.recipient_id:
            d.update(
                sender_id=self.sender_id,
                sender_name=self.sender_name,
                recipient_id=self.recipient_id,
                recipient_name=self.recipient_name,
                origin_place_id=self.origin_place_id,
                origin_place=self.origin_place,
            )
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "HumboldtChunk":
        def _entities(key: str, default_type: str) -> list[Entity]:
            return [Entity(name=p.get("name", ""), entity_type=p.get("type", default_type),
                           humboldt_id=p.get("humboldt_id", ""), gnd_id=p.get("gnd", ""),
                           geonames_id=p.get("geonames", ""), viaf_id=p.get("viaf", ""),
                           lat=p.get("lat"), lon=p.get("lon")) if isinstance(p, dict) else p
                    for p in d.get(key, [])]
        persons = _entities("persons", "person")
        places = _entities("places", "place")
        measurements = [Measurement(**m) if isinstance(m, dict) else m for m in d.get("measurements", [])]
        return cls(
            chunk_id=d.get("chunk_id", ""),
            doc_id=d["doc_id"],
            doc_title=d.get("doc_title", ""),
            doc_type=d.get("doc_type", ""),
            folio_start=d.get("folio_start", ""),
            folio_end=d.get("folio_end", ""),
            entry_number=d.get("entry_number", ""),
            url=d.get("url", ""),
            date_iso=d.get("date_iso", ""),
            date_display=d.get("date_display", ""),
            sender_id=d.get("sender_id", ""),
            sender_name=d.get("sender_name", ""),
            recipient_id=d.get("recipient_id", ""),
            recipient_name=d.get("recipient_name", ""),
            origin_place_id=d.get("origin_place_id", ""),
            origin_place=d.get("origin_place", ""),
            text_regularized=d.get("text_regularized", ""),
            text_plain=d.get("text_plain", ""),
            persons=persons,
            places=places,
            institutions=_entities("institutions", "institution"),
            plants=_entities("plants", "plant"),
            measurements=measurements,
            topics=d.get("topics", []),
            editorial_notes=d.get("editorial_notes", []),
            source_desc=d.get("source_desc", ""),
            parent_chunk_id=d.get("parent_chunk_id", ""),
        )
