"""Read access to the document store.

Documents are kept whole. Search, grep and entity lookups return passages:
windows into a document identified by (doc_id, start, end), cut at the
document's own paragraph and word boundaries.
"""

from __future__ import annotations

import bisect
import difflib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import regex

from alex.corpus.build import padded_date
from alex.corpus.registers import normalized_name

DOC_TYPES = ("diary", "letter", "document", "chronology", "research_article", "about")
PARAGRAPH_KINDS = ("text", "editorial", "footnote", "introduction")
PAGE_MARK = "⟦{}⟧ "
PAGE_MARK_PATTERN = re.compile(r"⟦[^⟧]*⟧ ?")
QUERY_TOKEN = re.compile(r'"([^"]+)"|(\S+)')
WORD = re.compile(r"[\w]+", re.UNICODE)
FTS_OPERATORS = re.compile(r"\b(AND|OR|NOT|NEAR)\b|[()]")


class StoreError(Exception):
    """A request the store cannot serve; the message is safe to show the model."""


class Folding(dict):
    """Translation table that strips diacritics and case one character at a
    time, so folded text keeps the offsets of the original."""

    def __missing__(self, codepoint: int) -> int:
        character = chr(codepoint)
        base = unicodedata.normalize("NFD", character)[0].lower()
        self[codepoint] = ord(base) if len(base) == 1 else codepoint
        return self[codepoint]


FOLDING = Folding()


def fold(text: str) -> str:
    return text.translate(FOLDING)


@dataclass
class NoteView:
    note_id: int
    kind: str
    label: str
    text: str
    anchor_start: int
    anchor_end: int
    anchor_text: str = ""


@dataclass
class Passage:
    doc_id: str
    doc_type: str
    title: str
    start: int
    end: int
    text: str
    kind: str = "text"
    page: str = ""
    date_from: str = ""
    date_to: str = ""
    url: str = ""
    score: float = 0.0
    note_id: int | None = None
    anchor_text: str = ""
    notes: list[NoteView] = field(default_factory=list)
    truncated_before: bool = False
    truncated_after: bool = False
    match_count: int = 0

    @property
    def date(self) -> str:
        if self.date_from and self.date_to and self.date_from != self.date_to:
            return f"{self.date_from}/{self.date_to}"
        return self.date_from or self.date_to


def date_bounds(date_from: str | None, date_to: str | None) -> tuple[str, str]:
    """ISO prefixes like '1801' or '1801-03' become inclusive bounds that
    compare against documents.date_min / date_max."""
    lower = (date_from or "").strip()
    upper = (date_to or date_from or "").strip()
    return padded_date(lower, "-01-01"), padded_date(upper, "-12-31")


DATE_FILTER = "{0}date_min != '' AND {0}date_max >= ? AND {0}date_min <= ?"


def query_terms(query: str) -> list[tuple[str, list[str], bool]]:
    """Parse a keyword query into (fts5 term, folded words, is_prefix)."""
    parsed = []
    for phrase, word in QUERY_TOKEN.findall(query):
        raw = phrase or word
        if not phrase and raw in ("AND", "OR", "NOT"):
            continue
        prefix = not phrase and raw.endswith("*")
        words = WORD.findall(fold(raw))
        if not words:
            continue
        if phrase:
            parsed.append(('"' + " ".join(words) + '"', words, False))
        else:
            parsed.extend(
                (f'"{token}"' + ("*" if prefix else ""), [token], prefix) for token in words
            )
    return parsed


def term_pattern(words: list[str], prefix: bool) -> re.Pattern:
    body = r"\W+".join(re.escape(word) for word in words) + (r"\w*" if prefix else "")
    return re.compile(rf"(?<!\w){body}(?!\w)")


class DocumentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"Document store {self.path} not found — run `alex build` first."
            )
        self.local = threading.local()
        self.texts: dict[str, str] = {}
        self.folded: dict[str, str] = {}
        self.text_lock = threading.Lock()
        self.entity_names: list[str] | None = None
        self.meta = {row["key"]: row["value"] for row in self.db.execute("SELECT * FROM meta")}

    @property
    def db(self) -> sqlite3.Connection:
        connection = getattr(self.local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            self.local.connection = connection
        return connection

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def document(self, doc_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT doc_id, doc_type, title, date_from, date_to, url, language, editors,"
            " authors, citation, repository, shelfmark, introduction, abstract, sender_id,"
            " sender_name, recipient_id, recipient_name, origin_place, destination_place,"
            " n_chars, n_pages FROM documents WHERE doc_id = ?",
            (doc_id.strip(),),
        ).fetchone()
        if row is None:
            raise StoreError(
                f"Unknown doc_id {doc_id!r}. Use search or list_documents to find document ids."
            )
        return row

    def text(self, doc_id: str) -> str:
        text = self.texts.get(doc_id)
        if text is None:
            row = self.db.execute(
                "SELECT text FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown doc_id {doc_id!r}.")
            with self.text_lock:
                text = self.texts.setdefault(doc_id, row["text"])
        return text

    def folded_text(self, doc_id: str) -> str:
        folded = self.folded.get(doc_id)
        if folded is None:
            folded = self.folded.setdefault(doc_id, fold(self.text(doc_id)))
        return folded

    def pages(self, doc_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT label, facsimile, start FROM pages WHERE doc_id = ? ORDER BY start, seq",
            (doc_id,),
        ).fetchall()

    def sections(self, doc_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT seq, level, heading, kind, start, end FROM sections"
            " WHERE doc_id = ? ORDER BY start, seq",
            (doc_id,),
        ).fetchall()

    def page_at(self, doc_id: str, offset: int) -> str:
        row = self.db.execute(
            "SELECT label FROM pages WHERE doc_id = ? AND start <= ?"
            " ORDER BY start DESC, seq DESC LIMIT 1",
            (doc_id, offset),
        ).fetchone()
        return row["label"] if row else ""

    def page_span(self, doc_id: str, page_from: str, page_to: str | None = None) -> tuple[int, int]:
        pages = self.pages(doc_id)
        labels = [page["label"] for page in pages]

        def position(label: str) -> int:
            wanted = label.strip().strip("[]").lower()
            for index, candidate in enumerate(labels):
                if candidate.strip("[]").lower() == wanted:
                    return index
            sample = ", ".join(labels[:12])
            raise StoreError(
                f"Document {doc_id} has no page {label!r}. Pages: {sample}"
                + (" …" if len(labels) > 12 else "")
            )

        first = position(page_from)
        last = position(page_to) if page_to else first
        if last < first:
            first, last = last, first
        end = pages[last + 1]["start"] if last + 1 < len(pages) else len(self.text(doc_id))
        return pages[first]["start"], end

    def page_url(self, url: str, page: str) -> str:
        if url and page and re.fullmatch(r"[\w.-]+", page):
            return f"{url}/{quote(page)}"
        return url

    def notes_between(self, doc_id: str, start: int, end: int, limit: int = 40) -> list[NoteView]:
        text = self.text(doc_id)
        rows = self.db.execute(
            "SELECT note_id, kind, label, text, anchor_start, anchor_end FROM notes"
            " WHERE doc_id = ? AND kind != 'introduction' AND anchor_start >= ?"
            " AND anchor_start < ? ORDER BY anchor_start, seq LIMIT ?",
            (doc_id, start, end, limit),
        ).fetchall()
        return [
            NoteView(
                row["note_id"],
                row["kind"],
                row["label"],
                row["text"],
                row["anchor_start"],
                row["anchor_end"],
                text[row["anchor_start"] : row["anchor_end"]][:160],
            )
            for row in rows
        ]

    def passage(
        self,
        doc_id: str,
        start: int,
        end: int,
        *,
        with_notes: bool = True,
        score: float = 0.0,
        document: sqlite3.Row | None = None,
    ) -> Passage:
        """The exact window [start, end) of a document as a passage."""
        document = document or self.document(doc_id)
        text = self.text(doc_id)
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        page = self.page_at(doc_id, start)
        return Passage(
            doc_id=doc_id,
            doc_type=document["doc_type"],
            title=document["title"],
            start=start,
            end=end,
            text=text[start:end],
            page=page,
            date_from=document["date_from"],
            date_to=document["date_to"],
            url=self.page_url(document["url"], page),
            score=score,
            notes=self.notes_between(doc_id, start, end) if with_notes else [],
            truncated_before=start > 0,
            truncated_after=end < len(text),
        )

    def rendered(self, passage: Passage) -> str:
        """Passage text with ⟦page⟧ marks at the page breaks it contains."""
        if passage.kind != "text":
            return passage.text
        marks = self.db.execute(
            "SELECT label, start FROM pages WHERE doc_id = ? AND start > ? AND start < ?"
            " ORDER BY start",
            (passage.doc_id, passage.start, passage.end),
        ).fetchall()
        pieces, position = [], passage.start
        if passage.page:
            pieces.append(PAGE_MARK.format(passage.page))
        for mark in marks:
            pieces.append(passage.text[position - passage.start : mark["start"] - passage.start])
            pieces.append(PAGE_MARK.format(mark["label"]))
            position = mark["start"]
        pieces.append(passage.text[position - passage.start :])
        return "".join(pieces)

    def read(
        self, doc_id: str, start: int = 0, max_chars: int = 40000, end: int | None = None
    ) -> Passage:
        """Read from `start`, ending at a paragraph boundary within max_chars."""
        text = self.text(doc_id)
        limit = min(len(text), start + max_chars)
        if end is not None:
            limit = min(limit, end)
        if limit < len(text) and (end is None or limit < end):
            boundary = text.rfind("\n\n", start, limit)
            if boundary > start + max_chars // 2:
                limit = boundary
        return self.passage(doc_id, start, limit)

    def list_documents(
        self,
        doc_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        person: str | None = None,
        title_contains: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        conditions, parameters = [], []
        if doc_type:
            conditions.append("doc_type = ?")
            parameters.append(doc_type)
        lower, upper = date_bounds(date_from, date_to)
        if lower or upper:
            conditions.append(DATE_FILTER.format(""))
            parameters.extend([lower, upper or "9999"])
        if person:
            conditions.append(
                "(sender_id = ? OR recipient_id = ? OR sender_name LIKE ? OR recipient_name LIKE ?)"
            )
            parameters.extend([person, person, f"%{person}%", f"%{person}%"])
        if title_contains:
            conditions.append("title LIKE ?")
            parameters.append(f"%{title_contains}%")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM documents{where}", parameters).fetchone()[0]
        rows = self.db.execute(
            "SELECT doc_id, doc_type, title, date_from, date_to, sender_name, recipient_name,"
            f" origin_place, n_chars, n_pages, url FROM documents{where}"
            " ORDER BY date_min = '', date_min, doc_id LIMIT ? OFFSET ?",
            [*parameters, limit, offset],
        ).fetchall()
        return rows, total

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        doc_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        doc_id: str | None = None,
        limit: int = 8,
        max_chars: int = 1200,
        per_document: int = 3,
    ) -> list[Passage]:
        """Ranked full-text search over paragraphs and notes. Passages that
        contain more of the query's terms come first, BM25 orders the rest."""
        terms = query_terms(query)
        if not terms:
            return []
        conditions, parameters = ["text_index MATCH ?"], []
        if doc_types:
            conditions.append(f"d.doc_type IN ({','.join('?' * len(doc_types))})")
            parameters.extend(doc_types)
        if doc_id:
            conditions.append("p.doc_id = ?")
            parameters.append(doc_id)
        lower, upper = date_bounds(date_from, date_to)
        if lower or upper:
            conditions.append(DATE_FILTER.format("d."))
            parameters.extend([lower, upper or "9999"])
        statement = (
            "SELECT p.paragraph_id, p.doc_id, p.kind, p.start, p.end, p.note_id,"
            " bm25(text_index) AS score FROM text_index"
            " JOIN paragraphs p ON p.paragraph_id = text_index.rowid"
            " JOIN documents d ON d.doc_id = p.doc_id"
            f" WHERE {' AND '.join(conditions)} ORDER BY score LIMIT ?"
        )

        def matching(expression: str) -> list[sqlite3.Row]:
            try:
                return self.db.execute(statement, [expression, *parameters, limit * 6]).fetchall()
            except sqlite3.OperationalError:
                return []

        expressions = [
            " AND ".join(term for term, _, _ in terms),
            " OR ".join(term for term, _, _ in terms),
        ]
        if FTS_OPERATORS.search(query):
            expressions.insert(0, query)
        candidates: dict[int, sqlite3.Row] = {}
        for expression in dict.fromkeys(expressions):
            for row in matching(expression):
                candidates.setdefault(row["paragraph_id"], row)
            if len(candidates) >= limit * 3:
                break

        patterns = [term_pattern(words, prefix) for _, words, prefix in terms]

        def relevance(row: sqlite3.Row) -> tuple[int, float]:
            """Terms covered first; then BM25, damped for scraps of a few words
            that BM25's length normalisation would otherwise put on top."""
            if row["kind"] == "text":
                folded = self.folded_text(row["doc_id"])[row["start"] : row["end"]]
            else:
                folded = fold(
                    self.db.execute(
                        "SELECT text FROM notes WHERE note_id = ?", (row["note_id"],)
                    ).fetchone()[0]
                )
            covered_terms = sum(1 for pattern in patterns if pattern.search(folded))
            substance = min(1.0, (len(folded) / 150) ** 0.5)
            return -covered_terms, row["score"] * substance

        ranked = sorted(candidates.values(), key=relevance)
        passages: list[Passage] = []
        per_doc: dict[str, int] = {}
        covered: dict[str, list[tuple[int, int]]] = {}
        for row in ranked:
            if not doc_id and per_doc.get(row["doc_id"], 0) >= per_document:
                continue
            if row["kind"] == "text":
                passage = self.text_window(
                    row["doc_id"], row["start"], row["end"], patterns, max_chars
                )
                spans = covered.setdefault(row["doc_id"], [])
                if any(passage.start < end and start < passage.end for start, end in spans):
                    continue
                spans.append((passage.start, passage.end))
            else:
                passage = self.note_passage(row["note_id"])
            passage.score = -row["score"]
            passages.append(passage)
            per_doc[row["doc_id"]] = per_doc.get(row["doc_id"], 0) + 1
            if len(passages) >= limit:
                break
        return passages

    def text_window(
        self,
        doc_id: str,
        start: int,
        end: int,
        patterns: list[re.Pattern],
        max_chars: int,
        min_chars: int = 500,
    ) -> Passage:
        """A paragraph as a passage: short ones grow to their neighbours, long
        ones are cut around the densest cluster of query terms."""
        if end - start > max_chars:
            start, end = self.densest_window(doc_id, start, end, patterns, max_chars)
        elif end - start < min_chars:
            start, end = self.grown(doc_id, start, end, min_chars, max_chars)
        return self.passage(doc_id, start, end)

    def grown(
        self, doc_id: str, start: int, end: int, min_chars: int, max_chars: int
    ) -> tuple[int, int]:
        text = self.text(doc_id)
        while end - start < min_chars:
            before = text.rfind("\n\n", 0, max(0, start - 2))
            after = text.find("\n\n", end + 2)
            new_start = 0 if before < 0 else before + 2
            new_end = len(text) if after < 0 else after
            grew = False
            if end < len(text) and new_end - start <= max_chars:
                end, grew = new_end, True
            if start > 0 and end - new_start <= max_chars and end - start < min_chars:
                start, grew = new_start, True
            if not grew:
                break
        return start, end

    def densest_window(
        self, doc_id: str, start: int, end: int, patterns: list[re.Pattern], max_chars: int
    ) -> tuple[int, int]:
        folded = self.folded_text(doc_id)
        positions = sorted(
            match.start() for pattern in patterns for match in pattern.finditer(folded, start, end)
        )
        centre = start
        best = 0
        for index, position in enumerate(positions):
            upto = bisect.bisect_right(positions, position + max_chars * 0.7)
            if upto - index > best:
                best, centre = upto - index, position
        window_start = max(start, centre - max_chars // 5)
        window_end = min(end, window_start + max_chars)
        return self.snapped(doc_id, window_start, window_end, start, end)

    def snapped(
        self, doc_id: str, start: int, end: int, lowest: int, highest: int
    ) -> tuple[int, int]:
        """Move window edges outward to the nearest whitespace."""
        text = self.text(doc_id)
        while start > lowest and not text[start - 1].isspace():
            start -= 1
        while end < highest and not text[end - 1].isspace() and not text[end].isspace():
            end += 1
        return start, end

    def note_passage(self, note_id: int) -> Passage:
        row = self.db.execute(
            "SELECT n.note_id, n.doc_id, n.kind, n.label, n.text, n.anchor_start,"
            " n.anchor_end, d.doc_type, d.title, d.date_from, d.date_to, d.url"
            " FROM notes n JOIN documents d ON d.doc_id = n.doc_id WHERE n.note_id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"Unknown note {note_id}.")
        page = self.page_at(row["doc_id"], row["anchor_start"])
        anchor = self.text(row["doc_id"])[row["anchor_start"] : row["anchor_end"]]
        return Passage(
            doc_id=row["doc_id"],
            doc_type=row["doc_type"],
            title=row["title"],
            start=row["anchor_start"],
            end=row["anchor_end"],
            text=row["text"],
            kind=row["kind"],
            page=page,
            date_from=row["date_from"],
            date_to=row["date_to"],
            url=self.page_url(row["url"], page),
            note_id=row["note_id"],
            anchor_text=anchor[:200],
        )

    # ------------------------------------------------------------------
    # Regular expressions
    # ------------------------------------------------------------------

    def grep(
        self,
        pattern: str,
        doc_types: list[str] | None = None,
        doc_id: str | None = None,
        limit: int = 20,
        context: int = 160,
        ignore_case: bool = True,
        budget_seconds: float = 8.0,
    ) -> dict:
        """Regex over whole document texts. Returns the first `limit` matches as
        passages plus match counts per document."""
        if len(pattern) > 400:
            raise StoreError("Pattern too long (max 400 characters).")
        try:
            compiled = regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)
        except regex.error as error:
            raise StoreError(f"Invalid regular expression: {error}") from error
        conditions, parameters = [], []
        if doc_id:
            conditions.append("doc_id = ?")
            parameters.append(doc_id)
        if doc_types:
            conditions.append(f"doc_type IN ({','.join('?' * len(doc_types))})")
            parameters.extend(doc_types)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        documents = self.db.execute(
            "SELECT doc_id, doc_type, title, date_from, date_to, url, n_chars, n_pages"
            f" FROM documents{where} ORDER BY doc_type, date_from, doc_id",
            parameters,
        ).fetchall()

        deadline = time.monotonic() + budget_seconds
        per_document = max(2, limit // 4)
        passages: list[Passage] = []
        counts: list[dict] = []
        total = 0
        timed_out = False
        for document in documents:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            text = self.text(document["doc_id"])
            try:
                matches = [match.span() for match in compiled.finditer(text, timeout=remaining)]
            except TimeoutError:
                timed_out = True
                break
            if not matches:
                continue
            total += len(matches)
            counts.append(
                {
                    "doc_id": document["doc_id"],
                    "doc_type": document["doc_type"],
                    "title": document["title"],
                    "matches": len(matches),
                }
            )
            last_end = -1
            shown = 0
            for start, end in matches:
                if len(passages) >= limit or (not doc_id and shown >= per_document):
                    break
                if start < last_end:
                    continue
                shown += 1
                window_start, window_end = self.snapped(
                    document["doc_id"],
                    max(0, start - context),
                    min(len(text), end + context),
                    0,
                    len(text),
                )
                passage = self.passage(
                    document["doc_id"],
                    window_start,
                    window_end,
                    with_notes=False,
                    document=document,
                )
                passage.match_count = 1
                passages.append(passage)
                last_end = window_end
        counts.sort(key=lambda entry: -entry["matches"])
        return {
            "passages": passages,
            "documents": counts,
            "total_matches": total,
            "timed_out": timed_out,
        }

    # ------------------------------------------------------------------
    # Registers and annotations
    # ------------------------------------------------------------------

    def entity(self, entity_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM entities WHERE entity_id = ?", (entity_id.strip(),)
        ).fetchone()
        return self.entity_dict(row) if row else None

    @staticmethod
    def entity_dict(row: sqlite3.Row) -> dict:
        entity = dict(row)
        entity["alternative_names"] = json.loads(entity.get("alternative_names") or "[]")
        entity["links"] = json.loads(entity.get("links") or "{}")
        return entity

    def lookup_entities(self, name: str, kind: str | None = None, limit: int = 5) -> list[dict]:
        """Register lookup: exact name, then all words as prefixes, then
        descriptions, then close spellings. Ranked by how often an entity is
        annotated in the texts."""
        direct = self.entity(name)
        if direct:
            return [direct]
        normalized = normalized_name(name)
        if not normalized:
            return []
        kind_filter = " AND e.kind = ?" if kind else ""
        kind_parameter = [kind] if kind else []
        found: dict[str, dict] = {}

        def collect(rows) -> None:
            for row in rows:
                found.setdefault(row["entity_id"], self.entity_dict(row))

        collect(
            self.db.execute(
                "SELECT e.* FROM entity_names n JOIN entities e ON e.entity_id = n.entity_id"
                f" WHERE n.name = ?{kind_filter} ORDER BY e.mention_count DESC LIMIT ?",
                [normalized, *kind_parameter, limit],
            )
        )
        tokens = normalized.split()
        if len(found) < limit:
            prefix_query = "names: (" + " AND ".join(f'"{token}"*' for token in tokens) + ")"
            collect(
                self.db.execute(
                    "SELECT e.* FROM entity_index JOIN entities e"
                    " ON e.entity_no = entity_index.rowid"
                    f" WHERE entity_index MATCH ?{kind_filter}"
                    " ORDER BY e.mention_count DESC LIMIT ?",
                    [prefix_query, *kind_parameter, limit * 3],
                )
            )
        if not found:
            any_query = " OR ".join(f'"{token}"' for token in tokens)
            collect(
                self.db.execute(
                    "SELECT e.* FROM entity_index JOIN entities e"
                    " ON e.entity_no = entity_index.rowid"
                    f" WHERE entity_index MATCH ?{kind_filter} ORDER BY rank LIMIT ?",
                    [any_query, *kind_parameter, limit],
                )
            )
        if not found:
            # misspellings: replace each word by the register words closest to it
            alternatives = [
                difflib.get_close_matches(token, self.name_words(), n=3, cutoff=0.8)
                for token in tokens
            ]
            if all(alternatives):
                close_query = (
                    "names: ("
                    + " AND ".join(
                        "(" + " OR ".join(f'"{word}"' for word in words) + ")"
                        for words in alternatives
                    )
                    + ")"
                )
                collect(
                    self.db.execute(
                        "SELECT e.* FROM entity_index JOIN entities e"
                        " ON e.entity_no = entity_index.rowid"
                        f" WHERE entity_index MATCH ?{kind_filter}"
                        " ORDER BY e.mention_count DESC LIMIT ?",
                        [close_query, *kind_parameter, limit],
                    )
                )

        def rank(entity: dict) -> tuple:
            names = [entity["name"], *entity["alternative_names"]]
            exact = any(normalized_name(candidate) == normalized for candidate in names)
            return (not exact, -entity["mention_count"])

        return sorted(found.values(), key=rank)[:limit]

    def name_words(self) -> list[str]:
        if self.entity_names is None:
            words = set()
            for (name,) in self.db.execute("SELECT DISTINCT name FROM entity_names"):
                words.update(word for word in name.split() if len(word) >= 4)
            self.entity_names = sorted(words)
        return self.entity_names

    def mentions(
        self,
        entity_id: str,
        doc_types: list[str] | None = None,
        doc_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 8,
        offset: int = 0,
        context: int = 320,
    ) -> dict:
        """Where an entity is annotated: counts per document, and passages
        around the mentions, taken in turn from the documents."""
        conditions, parameters = ["m.entity_id = ?"], [entity_id]
        if doc_types:
            conditions.append(f"d.doc_type IN ({','.join('?' * len(doc_types))})")
            parameters.extend(doc_types)
        if doc_id:
            conditions.append("m.doc_id = ?")
            parameters.append(doc_id)
        lower, upper = date_bounds(date_from, date_to)
        if lower or upper:
            conditions.append(DATE_FILTER.format("d."))
            parameters.extend([lower, upper or "9999"])
        where = " AND ".join(conditions)
        documents = self.db.execute(
            "SELECT m.doc_id, d.doc_type, d.title, d.date_from, d.date_to, COUNT(*) AS mentions"
            " FROM mentions m JOIN documents d ON d.doc_id = m.doc_id"
            f" WHERE {where} GROUP BY m.doc_id ORDER BY mentions DESC",
            parameters,
        ).fetchall()
        rows = self.db.execute(
            "SELECT m.doc_id, m.start, m.end, m.note_id, n.anchor_start FROM mentions m"
            " JOIN documents d ON d.doc_id = m.doc_id LEFT JOIN notes n ON n.note_id = m.note_id"
            f" WHERE {where} ORDER BY m.doc_id, COALESCE(n.anchor_start, m.start)",
            parameters,
        ).fetchall()

        by_document: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_document.setdefault(row["doc_id"], []).append(row)
        queues = [by_document[document["doc_id"]] for document in documents]
        ordered: list[sqlite3.Row] = []
        depth = 0
        while any(depth < len(queue) for queue in queues):
            ordered.extend(queue[depth] for queue in queues if depth < len(queue))
            depth += 1

        passages: list[Passage] = []
        covered: dict[str, list[tuple[int, int]]] = {}
        skipped = 0
        for row in ordered:
            if len(passages) >= limit:
                break
            if row["note_id"] is not None:
                passage = self.note_passage(row["note_id"])
                key_span = (-row["note_id"], -row["note_id"] + 1)
            else:
                text = self.text(row["doc_id"])
                start, end = self.snapped(
                    row["doc_id"],
                    max(0, row["start"] - context),
                    min(len(text), row["end"] + context),
                    0,
                    len(text),
                )
                passage = None
                key_span = (start, end)
            spans = covered.setdefault(row["doc_id"], [])
            if any(key_span[0] < end and start < key_span[1] for start, end in spans):
                continue
            spans.append(key_span)
            if skipped < offset:
                skipped += 1
                continue
            passages.append(passage or self.passage(row["doc_id"], *key_span, with_notes=False))
        return {
            "documents": [dict(document) for document in documents],
            "total_mentions": len(rows),
            "passages": passages,
        }

    def cooccurring(
        self, entity_id: str, kinds: list[str] | None = None, limit: int = 20, min_shared: int = 2
    ) -> dict:
        """Entities annotated in the same paragraphs as the focus entity,
        ranked by distinctiveness rather than raw frequency."""
        kind_filter = f" AND e.kind IN ({','.join('?' * len(kinds))})" if kinds else ""
        rows = self.db.execute(
            "SELECT e.entity_id, e.name, e.kind, e.url, e.mention_count,"
            " COUNT(DISTINCT a.paragraph_id) AS shared FROM mentions a"
            " JOIN mentions b ON b.paragraph_id = a.paragraph_id AND b.entity_id != a.entity_id"
            " JOIN entities e ON e.entity_id = b.entity_id"
            f" WHERE a.entity_id = ?{kind_filter} GROUP BY e.entity_id",
            [entity_id, *(kinds or [])],
        ).fetchall()

        def distinctiveness(row) -> float:
            return row["shared"] / max(row["mention_count"], 1) ** 0.5

        ranked = sorted(rows, key=distinctiveness, reverse=True)
        neighbours = [row for row in ranked if row["shared"] >= min_shared][:limit] or ranked[
            :limit
        ]
        ids = [row["entity_id"] for row in neighbours]
        links = []
        if len(ids) > 1:
            marks = ",".join("?" * len(ids))
            links = self.db.execute(
                "SELECT b.entity_id AS source, c.entity_id AS target,"
                " COUNT(DISTINCT a.paragraph_id) AS shared FROM mentions a"
                " JOIN mentions b ON b.paragraph_id = a.paragraph_id"
                " JOIN mentions c ON c.paragraph_id = a.paragraph_id AND c.entity_id > b.entity_id"
                f" WHERE a.entity_id = ? AND b.entity_id IN ({marks}) AND c.entity_id IN ({marks})"
                " GROUP BY 1, 2 HAVING shared >= ? ORDER BY shared DESC LIMIT ?",
                [entity_id, *ids, *ids, min_shared, limit * 2],
            ).fetchall()
        return {
            "neighbours": [dict(row) for row in neighbours],
            "links": [dict(row) for row in links],
        }

    CHRONOLOGY_COLUMNS = "doc_id, title, date_from, date_to, url, text"

    def chronology(self, date_from: str, date_to: str | None = None, limit: int = 30) -> list[dict]:
        lower, upper = date_bounds(date_from, date_to)
        rows = self.db.execute(
            f"SELECT {self.CHRONOLOGY_COLUMNS} FROM documents"
            " WHERE doc_type = 'chronology' AND date_min != '' AND date_max >= ?"
            " AND date_min <= ? ORDER BY date_min, doc_id LIMIT ?",
            (lower, upper, limit),
        ).fetchall()
        return [self.chronology_entry(row) for row in rows]

    def chronology_neighbours(
        self, date_from: str, date_to: str | None = None, each_side: int = 2
    ) -> list[dict]:
        """The entries just before and just after a range that has none."""
        lower, upper = date_bounds(date_from, date_to)
        before = self.db.execute(
            f"SELECT {self.CHRONOLOGY_COLUMNS} FROM documents WHERE doc_type = 'chronology'"
            " AND date_max != '' AND date_max < ? ORDER BY date_max DESC LIMIT ?",
            (lower, each_side),
        ).fetchall()
        after = self.db.execute(
            f"SELECT {self.CHRONOLOGY_COLUMNS} FROM documents WHERE doc_type = 'chronology'"
            " AND date_min > ? ORDER BY date_min LIMIT ?",
            (upper, each_side),
        ).fetchall()
        return [self.chronology_entry(row) for row in [*before[::-1], *after]]

    def chronology_entry(self, row: sqlite3.Row) -> dict:
        places = self.db.execute(
            "SELECT DISTINCT e.entity_id, e.name, e.lat, e.lon FROM mentions m"
            " JOIN entities e ON e.entity_id = m.entity_id"
            " WHERE m.doc_id = ? AND m.kind = 'place' ORDER BY m.start",
            (row["doc_id"],),
        )
        return {**dict(row), "places": [dict(place) for place in places]}

    def introduction(self, doc_id: str) -> Passage | None:
        """The editors' introductory note in a document's header, if it has one."""
        row = self.db.execute(
            "SELECT note_id FROM notes WHERE doc_id = ? AND kind = 'introduction'", (doc_id,)
        ).fetchone()
        return self.note_passage(row["note_id"]) if row else None

    # ------------------------------------------------------------------
    # Who, where and when inside the texts
    # ------------------------------------------------------------------

    def entities_in(
        self,
        doc_id: str,
        start: int,
        end: int,
        kinds: list[str] | None = None,
        include_notes: bool = False,
        limit: int = 60,
    ) -> list[dict]:
        """Everything annotated in [start, end) of a document, one row per
        entity (or per unregistered animal, mineral or reference), in order
        of first appearance. Returns up to limit + 1 rows so callers can
        tell when the span holds more."""
        conditions = ["m.doc_id = ?", "m.start >= ?", "m.start < ?"]
        parameters: list = [doc_id, start, end]
        if kinds:
            conditions.append(f"m.kind IN ({','.join('?' * len(kinds))})")
            parameters.extend(kinds)
        if not include_notes:
            conditions.append("m.note_id IS NULL")
        rows = self.db.execute(
            "SELECT COALESCE(m.entity_id, m.kind || ':' || COALESCE(m.key, m.surface)) AS ident,"
            " m.kind, COALESCE(e.name, m.key, m.surface) AS name, COUNT(*) AS mentions,"
            " MIN(m.start) AS first, e.entity_id, e.lat, e.lon, e.url, e.born, e.died,"
            " e.description, SUM(m.note_id IS NOT NULL) AS in_notes"
            " FROM mentions m LEFT JOIN entities e ON e.entity_id = m.entity_id"
            f" WHERE {' AND '.join(conditions)} GROUP BY ident ORDER BY first LIMIT ?",
            [*parameters, limit + 1],
        ).fetchall()
        return [{**dict(row), "page": self.page_at(doc_id, row["first"])} for row in rows]

    def chronology_stations(
        self, date_from: str, date_to: str | None = None, limit: int = 200
    ) -> tuple[list[dict], int]:
        """Places annotated in the chronology entries of a date range, in the
        order of the entries; a place repeated in consecutive entries is one
        station."""
        lower, upper = date_bounds(date_from, date_to)
        rows = self.db.execute(
            "SELECT d.doc_id, d.date_from, d.date_to, d.title, e.entity_id, e.name, e.lat,"
            " e.lon, e.url, m.start FROM documents d"
            " JOIN mentions m ON m.doc_id = d.doc_id AND m.kind = 'place'"
            " JOIN entities e ON e.entity_id = m.entity_id"
            " WHERE d.doc_type = 'chronology' AND d.date_min != '' AND d.date_max >= ?"
            " AND d.date_min <= ? ORDER BY d.date_min, d.doc_id, m.start",
            (lower, upper),
        ).fetchall()
        return self.collapsed_stations(rows, limit)

    def document_stations(self, doc_id: str, limit: int = 300) -> tuple[list[dict], int]:
        """Places named in a document's own text, in text order."""
        rows = self.db.execute(
            "SELECT m.doc_id, '' AS date_from, '' AS date_to, '' AS title, e.entity_id,"
            " e.name, e.lat, e.lon, e.url, m.start FROM mentions m"
            " JOIN entities e ON e.entity_id = m.entity_id"
            " WHERE m.doc_id = ? AND m.kind = 'place' AND m.note_id IS NULL ORDER BY m.start",
            (doc_id,),
        ).fetchall()
        stations, total = self.collapsed_stations(rows, limit)
        for station in stations:
            station["page"] = self.page_at(doc_id, station["start"])
        return stations, total

    @staticmethod
    def collapsed_stations(rows, limit: int) -> tuple[list[dict], int]:
        stations: list[dict] = []
        for row in rows:
            if stations and stations[-1]["entity_id"] == row["entity_id"]:
                stations[-1]["mentions"] += 1
                if row["date_to"]:
                    stations[-1]["date_to"] = row["date_to"]
                continue
            stations.append({**dict(row), "mentions": 1})
        return stations[:limit], len(stations)

    def dated_passages(
        self,
        date_from: str,
        date_to: str | None = None,
        doc_types: list[str] | None = None,
        doc_id: str | None = None,
        limit: int = 12,
        context: int = 320,
    ) -> dict:
        """Passages around the dates the editors annotated inside the texts
        that fall in a range. Returns the passages, each with the annotated
        date, and the total number of matching dates."""
        lower, upper = date_bounds(date_from, date_to)
        conditions = ["x.date_from != ''", "substr(x.date_from, 1, 4) BETWEEN ? AND ?"]
        parameters: list = [lower[:4], upper[:4] if upper else "9999"]
        if doc_types:
            conditions.append(f"d.doc_type IN ({','.join('?' * len(doc_types))})")
            parameters.extend(doc_types)
        if doc_id:
            conditions.append("x.doc_id = ?")
            parameters.append(doc_id)
        rows = self.db.execute(
            "SELECT x.doc_id, x.start, x.date_from, x.date_to, x.surface FROM dates x"
            f" JOIN documents d ON d.doc_id = x.doc_id WHERE {' AND '.join(conditions)}"
            " ORDER BY x.date_from, x.doc_id, x.start",
            parameters,
        ).fetchall()
        matching = [
            row
            for row in rows
            if padded_date(row["date_from"], "-01-01") <= (upper or "9999")
            and padded_date(row["date_to"] or row["date_from"], "-12-31") >= lower
        ]
        # a date that lies inside the range outranks a vaguer one ('1799') that merely overlaps it
        matching.sort(
            key=lambda row: (
                not (
                    padded_date(row["date_from"], "-01-01") >= lower
                    and padded_date(row["date_to"] or row["date_from"], "-12-31")
                    <= (upper or "9999")
                ),
                row["date_from"],
                row["doc_id"],
                row["start"],
            )
        )
        per_document = max(2, limit // 3) if not doc_id else limit
        shown: dict[str, int] = {}
        covered: dict[str, list[tuple[int, int]]] = {}
        passages: list[tuple[Passage, dict]] = []
        for row in matching:
            if len(passages) >= limit:
                break
            if shown.get(row["doc_id"], 0) >= per_document:
                continue
            text = self.text(row["doc_id"])
            start, end = self.snapped(
                row["doc_id"],
                max(0, row["start"] - context),
                min(len(text), row["start"] + len(row["surface"]) + context),
                0,
                len(text),
            )
            spans = covered.setdefault(row["doc_id"], [])
            if any(start < span_end and span_start < end for span_start, span_end in spans):
                continue
            spans.append((start, end))
            shown[row["doc_id"]] = shown.get(row["doc_id"], 0) + 1
            passages.append((self.passage(row["doc_id"], start, end, with_notes=False), dict(row)))
        return {"passages": passages, "total": len(matching)}

    # ------------------------------------------------------------------
    # Read-only SQL
    # ------------------------------------------------------------------

    ALLOWED_ACTIONS = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    FORBIDDEN_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts5"}

    def query(self, sql: str, max_rows: int = 200, budget_seconds: float = 6.0) -> dict:
        """Run one read-only SELECT with a time and row budget."""
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        deadline = time.monotonic() + budget_seconds

        def authorize(action, first, second, *_):
            if action not in self.ALLOWED_ACTIONS:
                return sqlite3.SQLITE_DENY
            if (
                action == sqlite3.SQLITE_FUNCTION
                and (second or "").lower() in self.FORBIDDEN_FUNCTIONS
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        connection.set_progress_handler(lambda: time.monotonic() > deadline, 20000)
        try:
            cursor = connection.execute(sql)
            if cursor.description is None:
                raise StoreError("Only SELECT statements are allowed.")
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.DatabaseError as error:
            message = str(error)
            if "interrupted" in message:
                message = f"Query exceeded the {budget_seconds:.0f} s time budget; simplify it."
            elif "not authorized" in message or "prohibited" in message:
                message = "Only read-only SELECT statements on the corpus tables are allowed."
            raise StoreError(f"SQL error: {message}") from error
        except sqlite3.Warning as error:
            raise StoreError(f"SQL error: {error}") from error
        finally:
            connection.close()
        return {
            "columns": columns,
            "rows": [list(row) for row in rows[:max_rows]],
            "truncated": len(rows) > max_rows,
        }

    def edition_links(self, values) -> dict[str, dict]:
        """Which of these table cells name a document or register entity, and
        where it lives in the edition. Ids always count; titles and names only
        when exactly one document or entity carries them."""
        candidates = list(
            dict.fromkeys(
                value for value in values if isinstance(value, str) and 2 < len(value) <= 300
            )
        )[:800]
        links: dict[str, dict] = {}
        for position in range(0, len(candidates), 400):
            chunk = candidates[position : position + 400]
            marks = ",".join("?" * len(chunk))
            lookups = (
                (
                    "document",
                    f"SELECT doc_id AS cell, title, url FROM documents WHERE doc_id IN ({marks})",
                ),
                (
                    "entity",
                    f"SELECT entity_id AS cell, name AS title, url FROM entities"
                    f" WHERE entity_id IN ({marks})",
                ),
                (
                    "document",
                    f"SELECT title AS cell, title, MIN(url) AS url FROM documents"
                    f" WHERE title IN ({marks}) GROUP BY title HAVING COUNT(*) = 1",
                ),
                (
                    "entity",
                    f"SELECT name AS cell, name AS title, MIN(url) AS url FROM entities"
                    f" WHERE name IN ({marks}) GROUP BY name HAVING COUNT(*) = 1",
                ),
            )
            for kind, statement in lookups:
                for row in self.db.execute(statement, chunk):
                    if row["url"]:
                        links.setdefault(
                            row["cell"], {"url": row["url"], "kind": kind, "title": row["title"]}
                        )
        return links

    def statistics(self) -> dict:
        documents = {
            row[0]: row[1]
            for row in self.db.execute("SELECT doc_type, COUNT(*) FROM documents GROUP BY 1")
        }
        entities = {
            row[0]: row[1]
            for row in self.db.execute("SELECT kind, COUNT(*) FROM entities GROUP BY 1")
        }
        return {
            "documents": documents,
            "entities": entities,
            "corpus_version": self.meta.get("corpus_version", ""),
            "corpus_commit": self.meta.get("corpus_commit", ""),
            "corpus_commit_date": self.meta.get("corpus_commit_date", ""),
            "built_at": self.meta.get("built_at", ""),
        }
