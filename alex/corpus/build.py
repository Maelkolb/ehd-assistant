"""Build the document store from the original TEI dump of the edition.

One row per document, holding the whole text. Everything else (pages,
sections, paragraphs, notes, mentions, measurements, dates) points into that
text by character offset; the full-text index is built over each document's
own paragraphs and notes.
"""

from __future__ import annotations

import bisect
import collections
import json
import logging
import re
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from alex.corpus.registers import normalized_name, read_registers, read_units
from alex.corpus.tei import DOC_TYPES, Document, read_document

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "3"
TOKENIZER = "unicode61 remove_diacritics 2"

SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    title TEXT,
    date_from TEXT,
    date_to TEXT,
    url TEXT,
    language TEXT,
    editors TEXT,
    authors TEXT,
    citation TEXT,
    repository TEXT,
    shelfmark TEXT,
    introduction TEXT,
    abstract TEXT,
    sender_id TEXT,
    sender_name TEXT,
    recipient_id TEXT,
    recipient_name TEXT,
    origin_place_id TEXT,
    origin_place TEXT,
    destination_place_id TEXT,
    destination_place TEXT,
    n_chars INTEGER,
    n_pages INTEGER,
    source_file TEXT,
    text TEXT NOT NULL,
    date_min TEXT,
    date_max TEXT
);

CREATE TABLE pages (doc_id TEXT, seq INTEGER, label TEXT, facsimile TEXT, start INTEGER);
CREATE TABLE sections (doc_id TEXT, seq INTEGER, level INTEGER, heading TEXT, kind TEXT,
                       start INTEGER, end INTEGER);
CREATE TABLE notes (note_id INTEGER PRIMARY KEY, doc_id TEXT, seq INTEGER, kind TEXT,
                    label TEXT, anchor_start INTEGER, anchor_end INTEGER, text TEXT);
CREATE TABLE paragraphs (paragraph_id INTEGER PRIMARY KEY, doc_id TEXT, kind TEXT,
                         seq INTEGER, start INTEGER, end INTEGER, page TEXT, note_id INTEGER);

CREATE TABLE entities (
    entity_no INTEGER PRIMARY KEY,
    entity_id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    alternative_names TEXT,
    description TEXT,
    born TEXT,
    died TEXT,
    gnd TEXT,
    geonames TEXT,
    viaf TEXT,
    wikidata TEXT,
    url TEXT,
    lat REAL,
    lon REAL,
    links TEXT,
    in_register INTEGER DEFAULT 1,
    mention_count INTEGER DEFAULT 0,
    document_count INTEGER DEFAULT 0
);
CREATE TABLE entity_names (name TEXT, entity_id TEXT);

CREATE TABLE mentions (
    mention_id INTEGER PRIMARY KEY,
    entity_id TEXT,
    kind TEXT,
    key TEXT,
    surface TEXT,
    doc_id TEXT,
    start INTEGER,
    end INTEGER,
    paragraph_id INTEGER,
    note_id INTEGER
);
CREATE TABLE measures (doc_id TEXT, start INTEGER, quantity REAL, unit TEXT, surface TEXT,
                       paragraph_id INTEGER);
CREATE TABLE units (unit_id TEXT PRIMARY KEY, label TEXT, quantity_kind TEXT, si_factor REAL,
                    si_unit TEXT, description_de TEXT, description_en TEXT, note TEXT);
CREATE TABLE dates (doc_id TEXT, start INTEGER, date_from TEXT, date_to TEXT, surface TEXT,
                    paragraph_id INTEGER);

CREATE VIRTUAL TABLE text_index USING fts5(text, content='', tokenize="{TOKENIZER}");
CREATE VIRTUAL TABLE entity_index USING fts5(names, description, content='',
                                             tokenize="{TOKENIZER}");
"""

INDEXES = """
CREATE INDEX documents_type_date ON documents (doc_type, date_min);
CREATE INDEX pages_doc ON pages (doc_id, start);
CREATE INDEX sections_doc ON sections (doc_id, start);
CREATE INDEX notes_doc ON notes (doc_id, anchor_start);
CREATE INDEX paragraphs_doc ON paragraphs (doc_id, kind, start);
CREATE INDEX entity_names_name ON entity_names (name);
CREATE INDEX mentions_entity ON mentions (entity_id, doc_id, start);
CREATE INDEX mentions_doc ON mentions (doc_id, start);
CREATE INDEX mentions_paragraph ON mentions (paragraph_id);
CREATE INDEX mentions_kind_key ON mentions (kind, key);
CREATE INDEX measures_unit ON measures (unit);
CREATE INDEX measures_doc ON measures (doc_id, start);
CREATE INDEX dates_from ON dates (date_from);
CREATE INDEX dates_doc ON dates (doc_id, start);
"""

VIEWS = """
CREATE VIEW letters AS
    SELECT d.doc_id, d.title, d.date_from, d.date_to,
           d.sender_id, COALESCE(s.name, d.sender_name) AS sender,
           d.recipient_id, COALESCE(r.name, d.recipient_name) AS recipient,
           d.origin_place_id, COALESCE(o.name, d.origin_place) AS origin_place,
           d.destination_place_id, COALESCE(t.name, d.destination_place) AS destination_place,
           d.language, d.n_chars, d.url
    FROM documents d
    LEFT JOIN entities s ON s.entity_id = d.sender_id
    LEFT JOIN entities r ON r.entity_id = d.recipient_id
    LEFT JOIN entities o ON o.entity_id = d.origin_place_id
    LEFT JOIN entities t ON t.entity_id = d.destination_place_id
    WHERE d.doc_type = 'letter';

CREATE VIEW chronology AS
    SELECT doc_id, date_from, date_to, title, text, url
    FROM documents WHERE doc_type = 'chronology';

CREATE VIEW measurements AS
    SELECT m.doc_id, d.doc_type, d.title, m.start, m.quantity, m.unit,
           u.label AS unit_label, u.quantity_kind,
           CASE m.unit WHEN 'reaumur' THEN m.quantity * 1.25
                       WHEN 'fahrenheit' THEN (m.quantity - 32) * 5.0 / 9
                       ELSE m.quantity * u.si_factor END AS si_value,
           CASE WHEN u.quantity_kind = 'temperature' THEN '°C' ELSE u.si_unit END AS si_unit,
           m.surface, m.paragraph_id,
           substr(d.text, MAX(1, m.start - 120), 300) AS context
    FROM measures m
    JOIN documents d ON d.doc_id = m.doc_id
    LEFT JOIN units u ON u.unit_id = m.unit;

CREATE VIEW entity_mentions AS
    SELECT m.mention_id, m.entity_id, e.name, m.kind, m.key, m.surface, m.doc_id, d.doc_type,
           d.title, d.date_from, d.date_to, m.start, m.end, m.paragraph_id,
           m.note_id IS NOT NULL AS in_editorial_note
    FROM mentions m
    JOIN documents d ON d.doc_id = m.doc_id
    LEFT JOIN entities e ON e.entity_id = m.entity_id;
"""


def padded_date(date: str, filler: str) -> str:
    """'1829' -> '1829-01-01' or '1829-12-31': comparable bounds for partial dates."""
    return date + filler[len(date) - 4 :] if 4 <= len(date) < 10 else date


def corpus_revision(corpus_root: Path) -> dict[str, str]:
    def git(*arguments: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(corpus_root), *arguments],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    version = ""
    readme = corpus_root / "README.md"
    if readme.exists():
        match = re.search(r"Version (\d+(?:\.\d+)*)", readme.read_text(encoding="utf-8"))
        version = match.group(1) if match else ""
    return {
        "corpus_commit": git("rev-parse", "HEAD"),
        "corpus_commit_date": git("log", "-1", "--format=%cs"),
        "corpus_version": version,
    }


class StoreBuilder:
    def __init__(self, database_path: Path, coordinates_path: Path | None = None):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.temporary_path = database_path.with_suffix(".building")
        self.database_path = database_path
        self.temporary_path.unlink(missing_ok=True)
        self.db = sqlite3.connect(self.temporary_path)
        self.db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
        self.coordinates = {}
        if coordinates_path and coordinates_path.exists():
            self.coordinates = json.loads(coordinates_path.read_text(encoding="utf-8"))
        self.plant_ids: dict[str, str] = {}
        self.registered: set[str] = set()
        self.unregistered: dict[str, dict] = {}
        self.next_paragraph_id = 1
        self.next_note_id = 1
        self.counts: dict[str, int] = {}

    def add_registers(self, index_directory: Path) -> None:
        for entity in read_registers(index_directory):
            if entity.entity_id in self.registered:
                continue
            self.registered.add(entity.entity_id)
            lat, lon = self.coordinates.get(entity.geonames, (None, None))
            self.db.execute(
                "INSERT INTO entities (entity_id, kind, name, alternative_names, description,"
                " born, died, gnd, geonames, viaf, wikidata, url, lat, lon, links)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity.entity_id,
                    entity.kind,
                    entity.name,
                    json.dumps(entity.alternative_names, ensure_ascii=False),
                    entity.description,
                    entity.born,
                    entity.died,
                    entity.gnd,
                    entity.geonames,
                    entity.viaf,
                    entity.wikidata,
                    entity.url,
                    lat,
                    lon,
                    json.dumps(entity.links, ensure_ascii=False),
                ),
            )
            for name in {entity.name, *entity.alternative_names}:
                self.db.execute(
                    "INSERT INTO entity_names VALUES (?, ?)",
                    (normalized_name(name), entity.entity_id),
                )
            if entity.kind == "plant":
                self.plant_ids[normalized_name(entity.name)] = entity.entity_id
            self.counts[entity.kind] = self.counts.get(entity.kind, 0) + 1
        for unit in read_units(index_directory):
            self.db.execute(
                "INSERT OR IGNORE INTO units VALUES (?,?,?,?,?,?,?,?)",
                (
                    unit.unit_id,
                    unit.label,
                    unit.quantity_kind,
                    unit.si_factor,
                    unit.si_unit,
                    unit.description_de,
                    unit.description_en,
                    unit.note,
                ),
            )

    def plant_for(self, name: str) -> str:
        normalized = normalized_name(name)
        if normalized in self.plant_ids:
            return self.plant_ids[normalized]
        words = normalized.split()
        if len(words) > 2:
            return self.plant_ids.get(" ".join(words[:2]), "")
        return ""

    def add_document(self, document: Document, source_file: str) -> None:
        title = document.title
        if document.doc_type == "chronology":
            title = f"Chronologie: {title}"
        self.db.execute(
            f"INSERT INTO documents VALUES ({','.join('?' * 28)})",
            (
                document.doc_id,
                document.doc_type,
                title,
                document.date_from,
                document.date_to,
                document.url,
                document.language,
                document.editors,
                document.authors,
                document.citation,
                document.repository,
                document.shelfmark,
                document.introduction,
                document.abstract,
                document.sender_id,
                document.sender_name,
                document.recipient_id,
                document.recipient_name,
                document.origin_place_id,
                document.origin_place,
                document.destination_place_id,
                document.destination_place,
                len(document.text),
                len(document.pages),
                source_file,
                document.text,
                padded_date(document.date_from, "-01-01"),
                padded_date(document.date_to, "-12-31"),
            ),
        )

        self.db.executemany(
            "INSERT INTO pages VALUES (?,?,?,?,?)",
            [
                (document.doc_id, seq, page.label, page.facsimile, page.start)
                for seq, page in enumerate(document.pages)
            ],
        )
        self.db.executemany(
            "INSERT INTO sections VALUES (?,?,?,?,?,?,?)",
            [
                (
                    document.doc_id,
                    seq,
                    section.level,
                    section.heading,
                    section.kind,
                    section.start,
                    section.end,
                )
                for seq, section in enumerate(document.sections)
            ],
        )

        page_starts = [page.start for page in document.pages]

        def page_at(offset: int) -> str:
            position = bisect.bisect_right(page_starts, offset) - 1
            return document.pages[position].label if position >= 0 else ""

        paragraph_starts, paragraph_ids = [], []
        for seq, paragraph in enumerate(document.paragraphs):
            paragraph_id = self.next_paragraph_id
            self.next_paragraph_id += 1
            paragraph_starts.append(paragraph.start)
            paragraph_ids.append(paragraph_id)
            self.db.execute(
                "INSERT INTO paragraphs VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    paragraph_id,
                    document.doc_id,
                    "text",
                    seq,
                    paragraph.start,
                    paragraph.end,
                    page_at(paragraph.start),
                ),
            )
            self.db.execute(
                "INSERT INTO text_index (rowid, text) VALUES (?, ?)",
                (paragraph_id, document.text[paragraph.start : paragraph.end]),
            )

        def paragraph_at(offset: int) -> int | None:
            position = bisect.bisect_right(paragraph_starts, offset) - 1
            return paragraph_ids[position] if position >= 0 else None

        notes = list(document.notes)
        note_ids = []
        for seq, note in enumerate(notes):
            note_id = self.next_note_id
            self.next_note_id += 1
            note_ids.append(note_id)
            self.db.execute(
                "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)",
                (
                    note_id,
                    document.doc_id,
                    seq,
                    note.kind,
                    note.label,
                    note.anchor_start,
                    note.anchor_end,
                    note.text,
                ),
            )
            self.add_note_paragraph(
                document.doc_id,
                note.kind,
                seq,
                note.anchor_start,
                note.anchor_end,
                page_at(note.anchor_start),
                note_id,
                note.text,
            )
        if document.introduction:
            note_id = self.next_note_id
            self.next_note_id += 1
            self.db.execute(
                "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)",
                (
                    note_id,
                    document.doc_id,
                    len(notes),
                    "introduction",
                    "",
                    0,
                    0,
                    document.introduction,
                ),
            )
            self.add_note_paragraph(
                document.doc_id,
                "introduction",
                len(notes),
                0,
                0,
                "",
                note_id,
                document.introduction,
            )

        for mention in document.mentions:
            entity_id = mention.entity_id
            if mention.kind == "plant" and not entity_id:
                entity_id = self.plant_for(mention.key or mention.surface)
            elif entity_id and entity_id not in self.registered:
                self.remember_unregistered(mention)
            in_note = mention.note_index >= 0
            note = notes[mention.note_index] if in_note else None
            anchor = note.anchor_start if in_note else mention.start
            self.db.execute(
                "INSERT INTO mentions (entity_id, kind, key, surface, doc_id, start, end,"
                " paragraph_id, note_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    entity_id or None,
                    mention.kind,
                    mention.key,
                    mention.surface[:200],
                    document.doc_id,
                    mention.start,
                    mention.end,
                    paragraph_at(anchor),
                    note_ids[mention.note_index] if in_note else None,
                ),
            )
        self.db.executemany(
            "INSERT INTO measures VALUES (?,?,?,?,?,?)",
            [
                (
                    document.doc_id,
                    measure.start,
                    measure.quantity,
                    measure.unit,
                    measure.surface[:200],
                    paragraph_at(measure.start),
                )
                for measure in document.measures
            ],
        )
        self.db.executemany(
            "INSERT INTO dates VALUES (?,?,?,?,?,?)",
            [
                (
                    document.doc_id,
                    date.start,
                    date.date_from,
                    date.date_to,
                    date.surface[:200],
                    paragraph_at(date.start),
                )
                for date in document.dates
            ],
        )
        self.counts[document.doc_type] = self.counts.get(document.doc_type, 0) + 1

    def remember_unregistered(self, mention) -> None:
        seen = self.unregistered.setdefault(
            mention.entity_id,
            {"kind": mention.kind, "surfaces": collections.Counter(), "authority": {}},
        )
        seen["surfaces"][mention.surface.strip(" .,;:()[]")] += 1
        seen["authority"].update(mention.authority)

    def add_unregistered_entities(self) -> None:
        """Entities the texts reference but the dump's registers lack are
        rebuilt from the annotations: the commonest spelling names them, the
        reference carries their authority ids."""
        for entity_id, seen in self.unregistered.items():
            spellings = [spelling for spelling, _ in seen["surfaces"].most_common(9) if spelling]
            if not spellings:
                continue
            authority = seen["authority"]
            lat, lon = self.coordinates.get(authority.get("geonames", ""), (None, None))
            self.db.execute(
                "INSERT INTO entities (entity_id, kind, name, alternative_names, description,"
                " gnd, geonames, viaf, url, lat, lon, links, in_register)"
                " VALUES (?,?,?,?,'',?,?,?,?,?,?,'{}',0)",
                (
                    entity_id,
                    seen["kind"],
                    spellings[0],
                    json.dumps(spellings[1:], ensure_ascii=False),
                    authority.get("gnd", ""),
                    authority.get("geonames", ""),
                    authority.get("viaf", ""),
                    f"https://edition-humboldt.de/{entity_id}",
                    lat,
                    lon,
                ),
            )
            for spelling in spellings:
                self.db.execute(
                    "INSERT INTO entity_names VALUES (?, ?)", (normalized_name(spelling), entity_id)
                )
            self.counts["unregistered"] = self.counts.get("unregistered", 0) + 1

    def add_note_paragraph(
        self,
        doc_id: str,
        kind: str,
        seq: int,
        start: int,
        end: int,
        page: str,
        note_id: int,
        text: str,
    ) -> None:
        paragraph_id = self.next_paragraph_id
        self.next_paragraph_id += 1
        self.db.execute(
            "INSERT INTO paragraphs VALUES (?,?,?,?,?,?,?,?)",
            (paragraph_id, doc_id, kind, seq, start, end, page, note_id),
        )
        self.db.execute("INSERT INTO text_index (rowid, text) VALUES (?, ?)", (paragraph_id, text))

    def finish(self, metadata: dict[str, str]) -> None:
        self.add_unregistered_entities()
        self.db.executescript(INDEXES)
        self.db.execute("""
            UPDATE entities SET
                mention_count = (SELECT COUNT(*) FROM mentions m
                                 WHERE m.entity_id = entities.entity_id),
                document_count = (SELECT COUNT(DISTINCT doc_id) FROM mentions m
                                  WHERE m.entity_id = entities.entity_id)""")
        for entity_no, entity_id, name, alternatives, description in self.db.execute(
            "SELECT entity_no, entity_id, name, alternative_names, description FROM entities"
        ).fetchall():
            surfaces = [
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT surface FROM mentions WHERE entity_id = ? LIMIT 40",
                    (entity_id,),
                )
            ]
            names = " | ".join([name, *json.loads(alternatives or "[]"), *surfaces])
            self.db.execute(
                "INSERT INTO entity_index (rowid, names, description) VALUES (?,?,?)",
                (entity_no, names, description or ""),
            )
        self.db.executescript(VIEWS)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "counts": json.dumps(self.counts),
            **metadata,
        }
        self.db.executemany("INSERT INTO meta VALUES (?, ?)", metadata.items())
        self.db.commit()
        self.db.execute("INSERT INTO text_index (text_index) VALUES ('optimize')")
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.close()
        self.database_path.unlink(missing_ok=True)
        self.temporary_path.rename(self.database_path)


def build_store(
    corpus_root: Path, database_path: Path, coordinates_path: Path | None = None
) -> dict[str, int]:
    """corpus_root is a checkout of telota/edition-humboldt-digital."""
    started = time.time()
    data_directory = corpus_root / "data"
    if not data_directory.is_dir():
        raise FileNotFoundError(f"No TEI dump at {data_directory} — run `alex fetch` first.")
    builder = StoreBuilder(database_path, coordinates_path)
    builder.add_registers(data_directory / "index")
    failures = 0
    for directory_name in DOC_TYPES:
        for path in sorted((data_directory / directory_name).glob("*.xml")):
            try:
                document = read_document(path)
            except Exception:
                logger.exception("Could not read %s", path.name)
                failures += 1
                continue
            if document is not None:
                builder.add_document(document, f"{directory_name}/{path.name}")
    builder.finish(corpus_revision(corpus_root))
    logger.info(
        "Built %s in %.0f s: %s (%d unreadable files)",
        database_path,
        time.time() - started,
        builder.counts,
        failures,
    )
    return builder.counts
