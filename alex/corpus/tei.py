"""Read one TEI document of the edition humboldt digital as a whole.

The result is the regularized reading text of the complete document plus
everything the editors annotated, addressed by character offsets into that
text: pages, sections, paragraphs, entity mentions, measurements, dates and
notes. Nothing is cut by length.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

TEI = "{http://www.tei-c.org/ns/1.0}"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
EDITION_HOST = "edition-humboldt.de/"

PARSER = etree.XMLParser(collect_ids=False, remove_comments=True, resolve_entities=False)

PARAGRAPH_BREAK = " "
LINE_BREAK = " "
SEPARATOR_RANK = {" ": 1, "\n": 2, "\n\n": 3}

DOC_TYPES = {
    "travel-journal": "diary",
    "letter": "letter",
    "document": "document",
    "chronology": "chronology",
    "research-article": "research_article",
    "about": "about",
}

UNIT_ELEMENTS = {
    "p",
    "head",
    "table",
    "list",
    "opener",
    "closer",
    "postscript",
    "lg",
    "address",
    "dateline",
    "salute",
    "signed",
    "byline",
    "ab",
    "figure",
}
BLOCK_ELEMENTS = UNIT_ELEMENTS | {"div", "body", "front", "back"}
LINE_ELEMENTS = {"row", "item", "l", "addrLine"}
INLINE_ELEMENTS = {
    "persName",
    "placeName",
    "orgName",
    "name",
    "choice",
    "abbr",
    "expan",
    "orig",
    "reg",
    "sic",
    "corr",
    "measure",
    "date",
    "ref",
    "hi",
    "bibl",
    "supplied",
    "unclear",
    "foreign",
    "add",
    "subst",
    "quote",
}
SKIPPED_ELEMENTS = {"del", "fw", "metamark", "graphic", "teiHeader"}
MENTION_ELEMENTS = {"persName": "person", "placeName": "place", "orgName": "institution"}
NATURE_KINDS = {"plant", "animal", "mineral"}


@dataclass
class Page:
    label: str
    facsimile: str
    start: int


@dataclass
class Section:
    level: int
    heading: str
    kind: str
    start: int
    end: int = 0


@dataclass
class Paragraph:
    start: int
    end: int


@dataclass
class Mention:
    kind: str
    entity_id: str
    key: str
    surface: str
    start: int
    end: int
    note_index: int = -1
    authority: dict[str, str] = field(default_factory=dict)


@dataclass
class Measure:
    quantity: float
    unit: str
    surface: str
    start: int


@dataclass
class DateMention:
    date_from: str
    date_to: str
    surface: str
    start: int


@dataclass
class Note:
    kind: str
    label: str
    text: str
    anchor_start: int
    anchor_end: int


@dataclass
class Document:
    doc_id: str
    doc_type: str
    title: str = ""
    date_from: str = ""
    date_to: str = ""
    url: str = ""
    language: str = ""
    editors: str = ""
    authors: str = ""
    citation: str = ""
    repository: str = ""
    shelfmark: str = ""
    introduction: str = ""
    abstract: str = ""
    sender_id: str = ""
    sender_name: str = ""
    recipient_id: str = ""
    recipient_name: str = ""
    origin_place_id: str = ""
    origin_place: str = ""
    destination_place_id: str = ""
    destination_place: str = ""
    text: str = ""
    pages: list[Page] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    dates: list[DateMention] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)


def local_name(element) -> str:
    return etree.QName(element.tag).localname if isinstance(element.tag, str) else ""


def is_indentation(text: str | None) -> bool:
    return bool(text) and "\n" in text and not text.strip()


def flat_text(element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip() if element is not None else ""


def edition_id(ref: str) -> str:
    for target in (ref or "").split():
        if EDITION_HOST in target:
            return target.rstrip("/").rsplit("/", 1)[-1]
    return ""


def authority_ids(ref: str) -> dict[str, str]:
    found = {}
    for target in (ref or "").split():
        if "d-nb.info/gnd/" in target:
            found["gnd"] = target.rsplit("/", 1)[-1]
        elif "geonames.org/" in target:
            found["geonames"] = target.rstrip("/").rsplit("/", 1)[-1]
        elif "viaf.org/viaf/" in target:
            found["viaf"] = target.rstrip("/").rsplit("/", 1)[-1]
        elif "wikidata.org/" in target:
            found["wikidata"] = target.rstrip("/").rsplit("/", 1)[-1]
    return found


def date_range(element) -> tuple[str, str]:
    if element is None:
        return "", ""
    when = element.get("when", "")
    if when:
        return when, when
    start = element.get("from") or element.get("notBefore") or ""
    end = element.get("to") or element.get("notAfter") or ""
    return start or end, end or start


class TextWalker:
    """Walks a TEI subtree and collects reading text with annotation offsets."""

    def __init__(self):
        self.pieces: list[str] = []
        self.length = 0
        self.pages: list[Page] = []
        self.sections: list[Section] = []
        self.paragraphs: list[Paragraph] = []
        self.mentions: list[Mention] = []
        self.measures: list[Measure] = []
        self.dates: list[DateMention] = []
        self.notes: list[Note] = []
        self.pending_notes: list[tuple[etree._Element, str, int, int]] = []
        self.editorial_spans: dict[str, tuple[int, int]] = {}
        self.unit_depth = 0
        self.unit_start = 0
        self.section_depth = 0

    def write(self, text: str) -> None:
        if text:
            self.pieces.append(text)
            self.length += len(text)

    def paragraph_break(self) -> None:
        self.write(PARAGRAPH_BREAK)

    def walk(self, node) -> None:
        name = local_name(node)
        if not name or name in SKIPPED_ELEMENTS:
            return
        handler = getattr(self, f"visit_{name}", None)
        if handler is not None:
            handler(node)
        else:
            self.visit_generic(node, name)

    def walk_children(self, node) -> None:
        """Indentation the XML editor put directly inside an inline element
        ("d’<placeName>⏎  <choice>…") is layout, not text."""
        inline = local_name(node) in INLINE_ELEMENTS
        if not (inline and is_indentation(node.text)):
            self.write(node.text)
        last = len(node) - 1
        for position, child in enumerate(node):
            self.walk(child)
            if not (inline and position == last and is_indentation(child.tail)):
                self.write(child.tail)

    def visit_generic(self, node, name: str) -> None:
        is_unit = name in UNIT_ELEMENTS
        separator = (
            PARAGRAPH_BREAK
            if name in BLOCK_ELEMENTS
            else LINE_BREAK
            if name in LINE_ELEMENTS
            else ""
        )
        self.write(separator)
        if is_unit:
            self.open_unit()
        start = self.length
        self.walk_children(node)
        if name in MENTION_ELEMENTS:
            reference = node.get("ref", "")
            self.record_mention(
                MENTION_ELEMENTS[name], edition_id(reference), "", start, authority_ids(reference)
            )
        if is_unit:
            self.close_unit()
        self.write(separator)

    def open_unit(self) -> None:
        if self.unit_depth == 0:
            self.unit_start = self.length
        self.unit_depth += 1

    def close_unit(self) -> None:
        self.unit_depth -= 1
        if self.unit_depth == 0 and self.length > self.unit_start:
            self.paragraphs.append(Paragraph(self.unit_start, self.length))

    def record_mention(
        self,
        kind: str,
        entity_id: str,
        key: str,
        start: int,
        authority: dict[str, str] | None = None,
    ) -> None:
        surface = "".join(self.pieces_since(start))
        if surface.strip():
            self.mentions.append(
                Mention(
                    kind, entity_id, key, surface, start, self.length, authority=authority or {}
                )
            )

    def pieces_since(self, start: int) -> list[str]:
        collected, position = [], self.length
        for piece in reversed(self.pieces):
            if position <= start:
                break
            position -= len(piece)
            collected.append(piece if position >= start else piece[start - position :])
        return collected[::-1]

    def visit_choice(self, node) -> None:
        for preferred in ("expan", "reg", "corr"):
            child = node.find(TEI + preferred)
            if child is not None:
                self.walk_children(child)
                return
        for child in node:
            if isinstance(child.tag, str):
                self.walk_children(child)
                return

    def visit_lb(self, node) -> None:
        if node.get("break") != "no":
            self.write(" ")

    def visit_cb(self, node) -> None:
        self.write(" ")

    def visit_space(self, node) -> None:
        self.write(" ")

    def visit_g(self, node) -> None:
        return

    def visit_anchor(self, node) -> None:
        return

    def visit_milestone(self, node) -> None:
        self.paragraph_break()

    def visit_pb(self, node) -> None:
        if node.get("break") != "no":
            self.write(" ")
        self.pages.append(Page(node.get("n", ""), node.get("facs", ""), self.length))

    def visit_gap(self, node) -> None:
        if node.get("reason") != "empty":
            self.write(" […] ")

    def visit_supplied(self, node) -> None:
        content = flat_text(node)
        bracketed = any(character.isalnum() for character in content)
        if bracketed:
            self.write("[")
        self.walk_children(node)
        if bracketed:
            self.write("]")

    def visit_cell(self, node) -> None:
        self.walk_children(node)
        self.write(" | ")

    def visit_figure(self, node) -> None:
        description = flat_text(node.find(TEI + "figDesc")) or flat_text(node.find(TEI + "head"))
        if description:
            self.paragraph_break()
            self.open_unit()
            self.write(f"[Abbildung: {description}]")
            self.close_unit()
            self.paragraph_break()

    def visit_quote(self, node) -> None:
        is_block = "block" in (node.get("rendition", "") + node.get("type", ""))
        if is_block:
            self.paragraph_break()
        self.walk_children(node)
        if is_block:
            self.paragraph_break()

    def visit_div(self, node) -> None:
        self.paragraph_break()
        heading = node.find(TEI + "head")
        section = Section(
            level=self.section_depth + 1,
            heading=reading_text(heading)[:200] if heading is not None else "",
            kind=node.get("type", ""),
            start=self.length,
        )
        self.section_depth += 1
        self.walk_children(node)
        self.section_depth -= 1
        section.end = self.length
        if section.heading or section.kind in ("diaryEntry", "writingSession"):
            self.sections.append(section)
        self.paragraph_break()

    def visit_ref(self, node) -> None:
        start = self.length
        self.walk_children(node)
        if node.get("type") == "editorialNote" and node.get(XML_ID):
            self.editorial_spans["#" + node.get(XML_ID)] = (start, self.length)

    def visit_name(self, node) -> None:
        start = self.length
        self.walk_children(node)
        kinds = set(node.get("type", "").split("|")) & NATURE_KINDS
        if kinds:
            kind = "plant" if "plant" in kinds else sorted(kinds)[0]
            self.record_mention(kind, "", node.get("key", ""), start)

    def visit_bibl(self, node) -> None:
        start = self.length
        self.walk_children(node)
        key = edition_id(node.get("ref", ""))
        if key:
            self.record_mention("bibl", "", key, start)

    def visit_measure(self, node) -> None:
        start = self.length
        self.walk_children(node)
        try:
            quantity = float(node.get("quantity", ""))
        except ValueError:
            return
        surface = "".join(self.pieces_since(start))
        self.measures.append(Measure(quantity, node.get("unit", ""), surface, start))

    def visit_date(self, node) -> None:
        start = self.length
        self.walk_children(node)
        date_from, date_to = date_range(node)
        if date_from:
            surface = "".join(self.pieces_since(start))
            self.dates.append(DateMention(date_from, date_to, surface, start))

    def visit_note(self, node) -> None:
        note_type = node.get("type", "")
        if note_type == "editorial":
            self.pending_notes.append((node, "editorial", self.length, self.length))
        elif node.get("place") == "foot":
            self.pending_notes.append((node, "footnote", self.length, self.length))
        elif note_type in ("auxCalc", "remarkResponsibility"):
            return
        else:
            self.write(" ⟨")
            self.walk_children(node)
            self.write("⟩ ")

    def resolve_notes(self) -> None:
        """Notes are read after the main walk so their text never enters the
        document text; their own entity mentions point back to the note."""
        for node, kind, anchor_start, anchor_end in self.pending_notes:
            span = self.editorial_spans.get(node.get("corresp", ""))
            if span:
                anchor_start, anchor_end = span
            note_walker = TextWalker()
            note_walker.walk_children(node)
            text, offsets = normalize(note_walker.pieces)
            if not text:
                continue
            text = text.replace("\n\n", "  ").replace("\n", " ")
            note_index = len(self.notes)
            self.notes.append(Note(kind, node.get("n", ""), text, anchor_start, anchor_end))
            for mention in note_walker.mentions:
                start, end = trimmed(text, offsets[mention.start], offsets[mention.end])
                self.mentions.append(
                    Mention(
                        mention.kind,
                        mention.entity_id,
                        mention.key,
                        text[start:end],
                        start,
                        end,
                        note_index,
                        mention.authority,
                    )
                )


def normalize(pieces: list[str]) -> tuple[str, list[int]]:
    """Collapse whitespace; return the clean text and a map raw offset -> clean offset."""
    raw = "".join(pieces)
    output: list[str] = []
    offsets = [0] * (len(raw) + 1)
    clean_length = 0
    pending = ""
    for index, character in enumerate(raw):
        offsets[index] = clean_length
        if character.isspace() or character in (PARAGRAPH_BREAK, LINE_BREAK):
            separator = (
                "\n\n" if character == PARAGRAPH_BREAK else "\n" if character == LINE_BREAK else " "
            )
            if output and SEPARATOR_RANK[separator] > SEPARATOR_RANK.get(pending, 0):
                pending = separator
            continue
        if pending:
            output.append(pending)
            clean_length += len(pending)
            pending = ""
            offsets[index] = clean_length
        output.append(character)
        clean_length += 1
    offsets[len(raw)] = clean_length
    return "".join(output), offsets


def trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def reading_text(element) -> str:
    if element is None:
        return ""
    walker = TextWalker()
    walker.walk_children(element)
    return re.sub(r"\s+", " ", normalize(walker.pieces)[0])


def read_document(path: str | Path) -> Document | None:
    path = Path(path)
    root = etree.parse(str(path), PARSER).getroot()
    header = root.find(TEI + "teiHeader")
    body = root.find(TEI + "text")
    if header is None or body is None:
        return None

    doc_id = root.get(XML_ID) or path.stem
    document = Document(doc_id=doc_id, doc_type=DOC_TYPES.get(path.parent.name, "document"))
    read_header(header, document)

    walker = TextWalker()
    walker.walk_children(body)
    walker.resolve_notes()
    text, offsets = normalize(walker.pieces)
    if not text.strip():
        return None

    def point(position: int) -> int:
        return trimmed(text, offsets[position], len(text))[0]

    document.text = text
    document.pages = [
        Page(page.label, page.facsimile, point(page.start)) for page in walker.pages if page.label
    ]

    def span(start: int, end: int) -> tuple[int, int]:
        return trimmed(text, offsets[start], offsets[end])

    document.sections = [
        Section(section.level, section.heading, section.kind, *span(section.start, section.end))
        for section in walker.sections
    ]
    document.paragraphs = cover_with_paragraphs(
        text, [Paragraph(*span(paragraph.start, paragraph.end)) for paragraph in walker.paragraphs]
    )
    document.notes = [
        Note(note.kind, note.label, note.text, *span(note.anchor_start, note.anchor_end))
        for note in walker.notes
    ]
    for mention in walker.mentions:
        if mention.note_index >= 0:
            document.mentions.append(mention)
            continue
        start, end = span(mention.start, mention.end)
        document.mentions.append(
            Mention(
                mention.kind,
                mention.entity_id,
                mention.key,
                text[start:end],
                start,
                end,
                authority=mention.authority,
            )
        )
    document.measures = [
        Measure(
            measure.quantity,
            measure.unit,
            re.sub(r"\s+", " ", measure.surface).strip(),
            point(measure.start),
        )
        for measure in walker.measures
    ]
    document.dates = [
        DateMention(
            date.date_from,
            date.date_to,
            re.sub(r"\s+", " ", date.surface).strip(),
            point(date.start),
        )
        for date in walker.dates
    ]
    return document


def cover_with_paragraphs(text: str, paragraphs: list[Paragraph]) -> list[Paragraph]:
    """Every part of the text belongs to a paragraph: text outside the
    edition's own block elements becomes paragraphs split at blank lines."""
    covered: list[Paragraph] = []
    position = 0
    for paragraph in sorted(paragraphs, key=lambda p: p.start):
        if paragraph.start > position:
            covered.extend(blank_line_paragraphs(text, position, paragraph.start))
        if paragraph.end > max(position, paragraph.start):
            covered.append(Paragraph(max(position, paragraph.start), paragraph.end))
            position = paragraph.end
    covered.extend(blank_line_paragraphs(text, position, len(text)))
    return covered


def blank_line_paragraphs(text: str, start: int, end: int) -> list[Paragraph]:
    found = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text[start:end]):
        if match.group().strip():
            found.append(Paragraph(start + match.start(), start + match.end()))
    return found


def read_header(header, document: Document) -> None:
    title_statement = header.find(f"{TEI}fileDesc/{TEI}titleStmt")
    if title_statement is not None:
        titles = [
            title
            for title in title_statement.findall(TEI + "title")
            if title.get("type") != "series"
        ]
        if titles:
            document.title = flat_text(titles[0])
            title_date = titles[0].find(TEI + "date")
            if title_date is not None:
                document.date_from, document.date_to = date_range(title_date)
        document.editors = "; ".join(
            filter(
                None, (person_name(editor) for editor in title_statement.findall(TEI + "editor"))
            )
        )
        document.authors = "; ".join(
            filter(
                None, (person_name(author) for author in title_statement.findall(TEI + "author"))
            )
        )

    version_root = flat_text(header.find(f".//{TEI}idno[@type='URLWeb']")).rstrip("/")
    if version_root:
        document.url = (
            version_root
            if version_root.endswith(document.doc_id)
            else f"{version_root}/{document.doc_id}"
        )
    citation = flat_text(header.find(f"{TEI}fileDesc/{TEI}editionStmt/{TEI}p"))
    if citation and version_root:
        citation = citation.replace(f"URL: {version_root}/", f"URL: {document.url}")
    document.citation = citation

    manuscript = header.find(f".//{TEI}msIdentifier")
    if manuscript is not None:
        holders = [
            flat_text(manuscript.find(TEI + part))
            for part in ("institution", "repository", "collection")
        ]
        document.repository = ", ".join(holder for holder in holders if holder)
        document.shelfmark = flat_text(manuscript.find(f".//{TEI}idno[@type='shelfmark']"))

    introduction = header.find(f"{TEI}fileDesc/{TEI}notesStmt/{TEI}note[@type='editorial']")
    document.introduction = reading_text(introduction)
    document.abstract = reading_text(header.find(f".//{TEI}abstract"))

    languages = [language.get("ident", "") for language in header.findall(f".//{TEI}language")]
    document.language = ",".join(language for language in languages if language)

    if not document.date_from:
        document.date_from, document.date_to = date_range(
            header.find(f".//{TEI}creation/{TEI}date")
        )
    read_correspondence(header.find(f".//{TEI}correspDesc"), document)


def person_name(element) -> str:
    person = element.find(TEI + "persName")
    target = person if person is not None else element
    forename = flat_text(target.find(TEI + "forename"))
    surname = flat_text(target.find(TEI + "surname"))
    return f"{forename} {surname}".strip() or flat_text(target)


def read_correspondence(description, document: Document) -> None:
    if description is None:
        return
    for action in description.findall(TEI + "correspAction"):
        people = action.findall(TEI + "persName") or action.findall(TEI + "orgName")
        place = action.find(TEI + "placeName")
        person_id = edition_id(people[0].get("ref", "")) if people else ""
        person_label = "; ".join(flat_text(person) for person in people)
        place_id = edition_id(place.get("ref", "")) if place is not None else ""
        place_label = flat_text(place)
        if action.get("type") == "sent":
            document.sender_id, document.sender_name = person_id, person_label
            document.origin_place_id, document.origin_place = place_id, place_label
            date_from, date_to = date_range(action.find(TEI + "date"))
            if date_from:
                document.date_from, document.date_to = date_from, date_to
        elif action.get("type") == "received":
            document.recipient_id, document.recipient_name = person_id, person_label
            document.destination_place_id, document.destination_place = place_id, place_label
