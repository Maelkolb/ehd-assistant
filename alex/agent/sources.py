"""Everything the model was shown during one answer, as citable sources."""

from __future__ import annotations

from dataclasses import dataclass, field

from alex.corpus.store import DocumentStore, Passage
from alex.llm.base import CitableText

NOTE_CHARS = 700

LABELS = {
    "de": {
        "diary": "Tagebuch",
        "letter": "Brief",
        "document": "Dokument",
        "chronology": "Chronologie",
        "research_article": "Forschungsbeitrag",
        "about": "Über die Edition",
        "folio": "Bl. {}",
        "page": "S. {}",
        "editorial": "Editorische Anmerkung",
        "footnote": "Fußnote",
        "introduction": "Editorische Einführung",
        "register": "Register",
        "data": "Datenabfrage",
        "computation": "Berechnung",
        "wikipedia": "Wikipedia",
    },
    "en": {
        "diary": "Journal",
        "letter": "Letter",
        "document": "Document",
        "chronology": "Chronology",
        "research_article": "Research article",
        "about": "About the edition",
        "folio": "fol. {}",
        "page": "p. {}",
        "editorial": "Editorial note",
        "footnote": "Footnote",
        "introduction": "Editorial introduction",
        "register": "Register",
        "data": "Data query",
        "computation": "Computation",
        "wikipedia": "Wikipedia",
    },
}
NOTE_PREFIX = {"editorial": "Editorial note by the editors", "footnote": "Footnote"}


@dataclass
class Source:
    id: str
    kind: str
    title: str
    label: str
    url: str = ""
    doc_id: str = ""
    doc_type: str = ""
    page: str = ""
    date: str = ""
    start: int | None = None
    end: int | None = None
    note_id: int | None = None
    entity_id: str = ""
    data: dict | None = None
    computation: dict | None = None
    wiki: dict | None = None

    def public(self) -> dict:
        optional = {
            key: value
            for key, value in self.__dict__.items()
            if value not in (None, "") and key not in ("id", "kind", "title", "label")
        }
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "label": self.label,
            **optional,
        }


@dataclass
class SourceRegistry:
    store: DocumentStore
    language: str = "de"
    sources: dict[str, Source] = field(default_factory=dict)
    keys: dict[tuple, str] = field(default_factory=dict)

    @property
    def words(self) -> dict[str, str]:
        return LABELS.get(self.language, LABELS["de"])

    def register(self, key: tuple, **fields) -> tuple[Source, bool]:
        """Returns the source and whether it is new in this answer."""
        if key in self.keys:
            return self.sources[self.keys[key]], False
        source = Source(id=f"S{len(self.sources) + 1}", **fields)
        self.sources[source.id] = source
        self.keys[key] = source.id
        return source, True

    def passage(self, passage: Passage, max_notes: int = 8) -> tuple[Source, CitableText, bool]:
        words = self.words
        page = self.page_label(passage.page)
        if passage.kind == "text":
            label = page or passage.date or words[passage.doc_type]
            key = ("text", passage.doc_id, passage.start, passage.end)
        else:
            label = ", ".join(part for part in (words[passage.kind], page) if part)
            key = ("note", passage.note_id)
        source, new = self.register(
            key,
            kind=passage.kind,
            title=passage.title,
            label=label,
            url=passage.url,
            doc_id=passage.doc_id,
            doc_type=passage.doc_type,
            page=passage.page,
            date=passage.date,
            start=passage.start,
            end=passage.end,
            note_id=passage.note_id,
        )

        location = f"{passage.doc_id}"
        if passage.page:
            location += f", page {passage.page}"
        if passage.kind == "text":
            location += f", chars {passage.start}–{passage.end}"
            heading = f"{passage.title} [{location}]"
            text = self.store.rendered(passage)
            notes = [
                self.note_line(note.kind, note.anchor_text, note.text)
                for note in passage.notes[:max_notes]
            ]
        else:
            kind = {
                "editorial": "Editorial note",
                "footnote": "Footnote",
                "introduction": "Editorial introduction",
            }[passage.kind]
            heading = f"{kind} in: {passage.title} [{location}]"
            text = passage.text
            if passage.anchor_text:
                text = f"(on “{passage.anchor_text}”) {text}"
            notes = []
        return source, CitableText(source.id, heading, passage.url, text, notes), new

    def page_label(self, page: str) -> str:
        """'56v' is a manuscript leaf, '73' a printed page."""
        if not page:
            return ""
        leaf = page.rstrip("]").lower().endswith(("r", "v"))
        return self.words["folio" if leaf else "page"].format(page)

    @staticmethod
    def note_line(kind: str, anchor: str, text: str) -> str:
        prefix = NOTE_PREFIX.get(kind, "Note")
        target = f" on “{anchor[:80]}”" if anchor else ""
        clipped = text if len(text) <= NOTE_CHARS else text[:NOTE_CHARS].rsplit(" ", 1)[0] + " …"
        return f"[{prefix}{target}] {clipped}"

    def entity(self, entity: dict) -> tuple[Source, CitableText, bool]:
        kind_words = self.words["register"]
        source, new = self.register(
            ("entity", entity["entity_id"]),
            kind="register",
            title=entity["name"],
            label=kind_words,
            url=entity.get("url", ""),
            entity_id=entity["entity_id"],
        )
        life = " – ".join(part for part in (entity.get("born"), entity.get("died")) if part)
        facts = [entity["name"] + (f" ({life})" if life else "")]
        if entity.get("alternative_names"):
            facts.append("Also: " + "; ".join(entity["alternative_names"][:6]))
        if entity.get("description"):
            facts.append(entity["description"][:900])
        annotated = (
            f"Annotated {entity['mention_count']} times in {entity['document_count']} "
            "documents of the edition"
        )
        if not entity.get("in_register", 1):
            annotated += " (known from annotations only; no register entry in this data release)"
        facts.append(annotated)
        if entity.get("lat") is not None:
            facts.append(f"Coordinates {entity['lat']:.4f}, {entity['lon']:.4f}")
        for authority in ("gnd", "geonames", "wikidata"):
            if entity.get(authority):
                facts.append(f"{authority} {entity[authority]}")
        heading = f"Register entry ({entity['kind']}): {entity['name']} [{entity['entity_id']}]"
        return source, CitableText(source.id, heading, entity.get("url", ""), ". ".join(facts)), new

    def data(
        self, title: str, sql: str, columns: list[str], rows: list[list], truncated: bool, text: str
    ) -> tuple[Source, CitableText]:
        source, _ = self.register(
            ("data", sql),
            kind="data",
            title=title,
            label=self.words["data"],
            data={"sql": sql, "columns": columns, "rows": rows[:100], "truncated": truncated},
        )
        return source, CitableText(source.id, f"Query result: {title}", "", text)

    def computation(self, title: str, code: str, output: str) -> tuple[Source, CitableText]:
        source, _ = self.register(
            ("computation", code),
            kind="computation",
            title=title,
            label=self.words["computation"],
            computation={"code": code, "output": output[:6000]},
        )
        return source, CitableText(
            source.id, f"Python output: {title}", "", output or "(no printed output)"
        )

    def wikipedia(self, info: dict) -> tuple[Source, CitableText]:
        source, _ = self.register(
            ("wikipedia", info["url"]),
            kind="wikipedia",
            title=info["title"],
            label=f"Wikipedia ({info['lang']})",
            url=info["url"],
            wiki={
                "lang": info["lang"],
                "title": info["title"],
                "extract": info["extract"][:2500],
                "url": info["url"],
                "description": info.get("description", ""),
            },
        )
        heading = f"Wikipedia ({info['lang']}): {info['title']} — external, not part of the edition"
        return source, CitableText(source.id, heading, info["url"], info["extract"][:2000])
