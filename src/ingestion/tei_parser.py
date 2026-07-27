"""
TEI-XML Parser for edition humboldt digital.

Parses TEI-encoded diaries and letters into structured HumboldtChunk objects.
Handles the specific markup conventions of the edition, including:
- <choice>/<abbr>/<expan> for abbreviation expansion
- <persName>, <placeName> with GND/GeoNames refs
- <measure> with historical units
- <note type="editorial"> for editorial commentary
- <date> with normalized ISO dates
- <pb> for folio tracking
- <div> as natural entry boundaries
"""
from __future__ import annotations

import re
import logging
from pathlib import Path
from lxml import etree

from src.models import HumboldtChunk, Entity, Measurement

logger = logging.getLogger(__name__)

NS = {"tei": "http://www.tei-c.org/ns/1.0"}
XML_NS = "http://www.w3.org/XML/1998/namespace"


class TEIParser:
    """
    Parser for Humboldt TEI-XML documents.

    Usage:
        parser = TEIParser()
        chunks = parser.parse("path/to/H0019734g.xml")
    """

    def __init__(self):
        self.current_folio: str = ""
        self.folio_stack: list[str] = []  # Track folios within a div

    def parse(self, filepath: str | Path) -> list[HumboldtChunk]:
        """Parse a TEI-XML file into a list of HumboldtChunks."""
        filepath = Path(filepath)
        logger.info(f"Parsing {filepath.name}")

        # collect_ids=False: some edition files reuse xml:id values, which
        # lxml would otherwise reject as duplicate IDs.
        tree = etree.parse(str(filepath), etree.XMLParser(collect_ids=False))
        root = tree.getroot()

        # Extract document-level metadata
        doc_meta = self._extract_doc_metadata(root)

        # Parse body: strategy depends on document structure
        body = root.find(".//tei:body", NS)
        if body is None:
            logger.warning(f"No <body> found in {filepath}")
            return []

        doc_type = doc_meta.get("doc_type", "")
        chunks = []

        # Detect structure: does body have <div> children?
        has_divs = any(
            etree.QName(c.tag).localname == "div"
            for c in body if isinstance(c.tag, str)
        )

        if doc_type == "chronology":
            # === Chronology: single event, date in title ===
            chunks = self._parse_chronology(body, doc_meta, root)

        elif doc_type == "letter":
            # === Letter: may have div, opener, closer, postscript ===
            doc_meta.update(self._extract_corresp(root))
            chunks = self._parse_letter(body, doc_meta)

        elif has_divs:
            # === Diary/structured document: iterate divs ===
            current_folio = ""
            for child in body:
                tag = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""
                if tag == "pb":
                    current_folio = child.get("n", "")
                elif tag == "div":
                    div_chunks = self._parse_div(child, doc_meta, current_folio)
                    chunks.extend(div_chunks)
                    pbs_in_div = child.findall(".//tei:pb", NS)
                    if pbs_in_div:
                        current_folio = pbs_in_div[-1].get("n", current_folio)

        else:
            # === Flat document (document, research-article, travel-journal without divs) ===
            chunks = self._parse_flat_body(body, doc_meta)

        logger.info(f"Extracted {len(chunks)} chunks from {filepath.name}")
        return chunks

    def _extract_doc_metadata(self, root) -> dict:
        """Extract document-level metadata from teiHeader."""
        meta = {}

        # Document ID
        meta["doc_id"] = root.get(f"{{{XML_NS}}}id", "unknown")

        # Title (may contain nested elements like <date>)
        title = root.find(".//tei:titleStmt/tei:title", NS)
        if title is not None:
            meta["doc_title"] = re.sub(r"\s+", " ", "".join(title.itertext())).strip()
        else:
            meta["doc_title"] = ""

        # URL — the v11 dump's URLWeb is only the version root
        # (https://edition-humboldt.de/v11/); the document ID must be appended
        # to get a resolvable permalink (…/v11/H0002922[/folio]).
        url_web = root.find(".//tei:idno[@type='URLWeb']", NS)
        base = (url_web.text or "").strip().rstrip("/") if url_web is not None else ""
        doc_id = meta["doc_id"]
        if base and doc_id and doc_id != "unknown" and not base.endswith(doc_id):
            meta["url_base"] = f"{base}/{doc_id}"
        else:
            meta["url_base"] = base

        url_xml = root.find(".//tei:idno[@type='URLXML']", NS)
        meta["url_xml"] = (url_xml.text or "").strip() if url_xml is not None else ""

        url_images = root.find(".//tei:idno[@type='URLImages']", NS)
        meta["url_images"] = (url_images.text or "").strip() if url_images is not None else ""

        # Source description (shelfmark)
        institution = root.find(".//tei:msIdentifier/tei:institution", NS)
        shelfmark = root.find(".//tei:idno[@type='shelfmark']", NS)
        parts = []
        if institution is not None and institution.text:
            parts.append(institution.text.strip())
        if shelfmark is not None and shelfmark.text:
            parts.append(shelfmark.text.strip())
        meta["source_desc"] = ", ".join(parts)

        # Document type from keywords
        keywords = root.findall(".//tei:keywords/tei:term", NS)
        for kw in keywords:
            if kw.text:
                kw_lower = kw.text.lower()
                if "travel-journal" in kw_lower or "journal" in kw_lower:
                    meta["doc_type"] = "diary"
                elif "letter" in kw_lower or "brief" in kw_lower:
                    meta["doc_type"] = "letter"
                elif "chronology" in kw_lower:
                    meta["doc_type"] = "chronology"
                elif "research-article" in kw_lower or "article" in kw_lower:
                    meta["doc_type"] = "research_article"
                elif "document" in kw_lower:
                    meta["doc_type"] = "document"
        meta.setdefault("doc_type", "document")  # default

        # Date range
        creation_date = root.find(".//tei:creation/tei:date", NS)
        if creation_date is not None:
            meta["date_not_before"] = creation_date.get("notBefore", "")
            meta["date_not_after"] = creation_date.get("notAfter", "")

        return meta

    _EDH_URL = "edition-humboldt.de"

    def _extract_corresp(self, root) -> dict:
        """Sender/recipient/origin/date from <correspDesc> (letters only)."""
        out = {}
        cd = root.find(".//tei:correspDesc", NS)
        if cd is None:
            return out

        def _ref_id(el) -> str:
            # @ref carries multiple space-separated URLs (edition, GND, GeoNames)
            for u in (el.get("ref") or "").split():
                if self._EDH_URL in u:
                    return u.rstrip("/").rsplit("/", 1)[-1]
            return ""

        def _name(el) -> str:
            return re.sub(r"\s+", " ", "".join(el.itertext())).strip()

        for action in cd.findall("tei:correspAction", NS):
            kind = action.get("type", "")
            pers = action.findall("tei:persName", NS)
            if pers:
                pid, pname = _ref_id(pers[0]), _name(pers[0])
                if len(pers) > 1:
                    pname += f" (+{len(pers) - 1})"
                if kind == "sent":
                    out["sender_id"], out["sender_name"] = pid, pname
                elif kind == "received":
                    out["recipient_id"], out["recipient_name"] = pid, pname
            if kind == "sent":
                place = action.find("tei:placeName", NS)
                if place is not None:
                    out["origin_place_id"] = _ref_id(place)
                    out["origin_place"] = _name(place)
                date = action.find("tei:date", NS)
                if date is not None:
                    out["corresp_date"] = (date.get("when")
                                           or date.get("notBefore")
                                           or date.get("notAfter") or "")
        return out

    def _parse_div(self, div, doc_meta: dict, entry_folio: str) -> list[HumboldtChunk]:
        """
        Parse a <div> element into one or more chunks.

        Strategy:
        - Each <div> typically corresponds to one of Humboldt's numbered entries
        - Some divs contain sub-divs (nested structure)
        - We create one chunk per div (leaf level)
        """
        # Check for nested divs
        sub_divs = div.findall("tei:div", NS)
        if sub_divs:
            chunks = []
            for sub in sub_divs:
                chunks.extend(self._parse_div(sub, doc_meta, entry_folio))
            return chunks

        # Track folios within this div
        folio_start = entry_folio
        folio_end = entry_folio
        for pb in div.findall(".//tei:pb", NS):
            folio = pb.get("n", "")
            if folio:
                if not folio_start:
                    folio_start = folio
                folio_end = folio

        # Extract entry number from <head>
        entry_number = self._extract_entry_number(div)

        # Extract text layers
        text_regularized = self._extract_text(div, mode="regularized")
        text_diplomatic = self._extract_text(div, mode="diplomatic")
        text_plain = self._normalize_for_search(text_regularized)

        # Skip near-empty chunks
        if len(text_plain.strip()) < 20:
            return []

        # Extract dates
        dates = self._extract_dates(div)
        date_iso = dates[0] if dates else ""
        date_display = self._format_date(date_iso)

        # Extract entities
        ents = self._extract_all_entities(div)
        bibliography = self._extract_bibliography(div)

        # Extract measurements
        measurements = self._extract_measurements(div)

        # Extract editorial notes
        editorial_notes = self._extract_editorial_notes(div)

        # Build URL
        url = doc_meta.get("url_base", "")
        if url and folio_start:
            url = f"{url}/{folio_start}"

        chunk = HumboldtChunk(
            doc_id=doc_meta["doc_id"],
            doc_title=doc_meta.get("doc_title", ""),
            doc_type=doc_meta.get("doc_type", "diary"),
            folio_start=folio_start,
            folio_end=folio_end,
            entry_number=entry_number,
            url=url,
            url_xml=doc_meta.get("url_xml", ""),
            url_images=doc_meta.get("url_images", ""),
            date_iso=date_iso,
            date_not_before=doc_meta.get("date_not_before", ""),
            date_not_after=doc_meta.get("date_not_after", ""),
            date_display=date_display,
            text_diplomatic=text_diplomatic,
            text_regularized=text_regularized,
            text_plain=text_plain,
            **ents,
            bibliography=bibliography,
            measurements=measurements,
            editorial_notes=editorial_notes,
            source_desc=doc_meta.get("source_desc", ""),
        )

        return [chunk]

    # =========================================================================
    # Document-type-specific parsers
    # =========================================================================

    def _parse_chronology(self, body, doc_meta: dict, root) -> list[HumboldtChunk]:
        """
        Parse a chronology entry. Simple structure: date in <title>, text in <body>.
        Each file = one event = one chunk.
        """
        # Extract date from title
        title_date = root.find(".//tei:titleStmt/tei:title/tei:date", NS)
        date_iso = ""
        date_display = ""
        if title_date is not None:
            date_iso = title_date.get("when", title_date.get("notBefore", ""))
            date_display = (title_date.text or "").strip()
        if not date_display and date_iso:
            date_display = self._format_date(date_iso)

        text = self._extract_text(body, mode="regularized")
        if len(text.strip()) < 10:
            return []

        ents = self._extract_all_entities(body)

        url = doc_meta.get("url_base", "")

        return [HumboldtChunk(
            doc_id=doc_meta["doc_id"],
            doc_title=f"Chronologie: {date_display}",
            doc_type="chronology",
            url=url,
            date_iso=date_iso,
            date_display=date_display,
            text_regularized=text,
            text_plain=self._normalize_for_search(text),
            **ents,
            source_desc=doc_meta.get("source_desc", ""),
        )]

    def _parse_letter(self, body, doc_meta: dict) -> list[HumboldtChunk]:
        """
        Parse a letter. Structure: <div> with main text, plus <opener>,
        <closer>, <postscript> siblings.
        """
        # Collect all text from the body (including opener/closer/postscript)
        text = self._extract_text(body, mode="regularized")
        if len(text.strip()) < 20:
            return []

        # Track folios
        folio_start = ""
        folio_end = ""
        for pb in body.findall(".//tei:pb", NS):
            folio = pb.get("n", "")
            if folio:
                if not folio_start:
                    folio_start = folio
                folio_end = folio

        # Dates: correspDesc first — the body extractor picks up ANY <date>,
        # including merely mentioned ones; the editorial correspDesc date is
        # the letter's actual date.
        dates = self._extract_dates(body)
        date_iso = (doc_meta.get("corresp_date", "")
                    or (dates[0] if dates else "")
                    or doc_meta.get("date_not_before", ""))
        date_display = self._format_date(date_iso)

        ents = self._extract_all_entities(body)
        bibliography = self._extract_bibliography(body)
        editorial_notes = self._extract_editorial_notes(body)

        url = doc_meta.get("url_base", "")
        if url and folio_start:
            url = f"{url}/{folio_start}"

        return [HumboldtChunk(
            doc_id=doc_meta["doc_id"],
            doc_title=doc_meta.get("doc_title", ""),
            doc_type="letter",
            folio_start=folio_start,
            folio_end=folio_end,
            url=url,
            date_iso=date_iso,
            date_display=date_display,
            sender_id=doc_meta.get("sender_id", ""),
            sender_name=doc_meta.get("sender_name", ""),
            recipient_id=doc_meta.get("recipient_id", ""),
            recipient_name=doc_meta.get("recipient_name", ""),
            origin_place_id=doc_meta.get("origin_place_id", ""),
            origin_place=doc_meta.get("origin_place", ""),
            text_regularized=text,
            text_plain=self._normalize_for_search(text),
            **ents,
            bibliography=bibliography,
            editorial_notes=editorial_notes,
            source_desc=doc_meta.get("source_desc", ""),
        )]

    def _parse_flat_body(self, body, doc_meta: dict) -> list[HumboldtChunk]:
        """
        Parse documents where <p> elements sit directly in <body> (no <div>).
        Used for: documents, research articles, travel journals without divs.
        Strategy: split by <pb> if present, otherwise treat as single chunk.
        """
        chunks = []

        # Check for page breaks
        pbs = [c for c in body if isinstance(c.tag, str) and etree.QName(c.tag).localname == "pb"]

        if len(pbs) > 1:
            # Split by page breaks — group elements between consecutive <pb>s
            current_folio = ""
            current_elements = []

            for child in body:
                tag = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""

                if tag == "pb":
                    # Flush current group
                    if current_elements and current_folio:
                        chunk = self._elements_to_chunk(
                            current_elements, doc_meta, current_folio
                        )
                        if chunk:
                            chunks.append(chunk)
                    current_folio = child.get("n", "")
                    current_elements = []
                elif tag in ("p", "note", "list", "table", "head", "lg"):
                    current_elements.append(child)

            # Flush last group
            if current_elements:
                chunk = self._elements_to_chunk(
                    current_elements, doc_meta, current_folio
                )
                if chunk:
                    chunks.append(chunk)
        else:
            # No meaningful page breaks — treat entire body as one chunk
            text = self._extract_text(body, mode="regularized")
            if len(text.strip()) < 20:
                return []

            folio = pbs[0].get("n", "") if pbs else ""
            ents = self._extract_all_entities(body)
            bibliography = self._extract_bibliography(body)
            measurements = self._extract_measurements(body)
            editorial_notes = self._extract_editorial_notes(body)

            dates = self._extract_dates(body)
            date_iso = dates[0] if dates else doc_meta.get("date_not_before", "")
            date_display = self._format_date(date_iso)

            url = doc_meta.get("url_base", "")
            if url and folio:
                url = f"{url}/{folio}"

            chunks.append(HumboldtChunk(
                doc_id=doc_meta["doc_id"],
                doc_title=doc_meta.get("doc_title", ""),
                doc_type=doc_meta.get("doc_type", "document"),
                folio_start=folio,
                url=url,
                date_iso=date_iso,
                date_display=date_display,
                text_regularized=text,
                text_plain=self._normalize_for_search(text),
                **ents,
                bibliography=bibliography,
                measurements=measurements,
                editorial_notes=editorial_notes,
                source_desc=doc_meta.get("source_desc", ""),
            ))

        return chunks

    def _elements_to_chunk(self, elements: list, doc_meta: dict, folio: str) -> HumboldtChunk | None:
        """Convert a group of body elements (between page breaks) into a chunk."""
        from copy import deepcopy
        # Build a temporary wrapper (use deepcopy to avoid mutating the tree)
        wrapper = etree.Element("div")
        for elem in elements:
            wrapper.append(deepcopy(elem))

        text = self._extract_text(wrapper, mode="regularized")
        if len(text.strip()) < 20:
            return None

        ents = self._extract_all_entities(wrapper)
        measurements = self._extract_measurements(wrapper)
        editorial_notes = self._extract_editorial_notes(wrapper)

        dates = self._extract_dates(wrapper)
        date_iso = dates[0] if dates else ""
        date_display = self._format_date(date_iso)

        url = doc_meta.get("url_base", "")
        if url and folio:
            url = f"{url}/{folio}"

        return HumboldtChunk(
            doc_id=doc_meta["doc_id"],
            doc_title=doc_meta.get("doc_title", ""),
            doc_type=doc_meta.get("doc_type", "document"),
            folio_start=folio,
            url=url,
            date_iso=date_iso,
            date_display=date_display,
            text_regularized=text,
            text_plain=self._normalize_for_search(text),
            **ents,
            measurements=measurements,
            editorial_notes=editorial_notes,
            source_desc=doc_meta.get("source_desc", ""),
        )

    # =========================================================================
    # Div-based parsing (diaries, some letters)
    # =========================================================================

    def _extract_text(self, elem, mode: str = "regularized") -> str:
        """
        Extract text from a TEI element.

        Modes:
          - "regularized": Prefer <expan> over <abbr>, <reg> over <orig>
          - "diplomatic": Prefer <abbr> over <expan>, <orig> over <reg>
        """
        parts = []
        self._walk_text(elem, parts, mode)
        text = " ".join(parts)
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text).strip()
        # Fix broken hyphenation from line breaks
        text = re.sub(r"(\w)[\u00AD\-]\s+(\w)", r"\1\2", text)
        return text

    def _walk_text(self, node, parts: list, mode: str):
        """Recursive text walker that respects TEI semantics."""
        tag = etree.QName(node.tag).localname if isinstance(node.tag, str) else ""

        # === <choice>: pick based on mode ===
        if tag == "choice":
            if mode == "regularized":
                expan = node.find("tei:expan", NS)
                reg = node.find("tei:reg", NS)
                preferred = expan if expan is not None else reg
            else:
                abbr = node.find("tei:abbr", NS)
                orig = node.find("tei:orig", NS)
                preferred = abbr if abbr is not None else orig

            if preferred is not None:
                self._walk_text(preferred, parts, mode)
            else:
                # Fallback: take first child
                for child in node:
                    self._walk_text(child, parts, mode)
                    break
            return

        # === Skip elements that shouldn't appear in running text ===
        if tag in ("del", "fw", "metamark"):
            return

        # === <note>: never inline into running text ===
        # Editorial notes are extracted separately (_extract_editorial_notes) and
        # indexed as their own retrievable units, so Humboldt's text stays clean
        # and the editorial voice stays attributable.
        if tag == "note":
            return

        # === <supplied>: include with brackets ===
        if tag == "supplied":
            parts.append("[")

        # === <head>: add newline before ===
        if tag == "head" and parts:
            parts.append("\n")

        # === Text content ===
        if node.text:
            parts.append(node.text)

        # === Recurse into children ===
        for child in node:
            self._walk_text(child, parts, mode)
            # Tail text (text after closing tag)
            if child.tail:
                parts.append(child.tail)

        # === Close supplied ===
        if tag == "supplied":
            parts.append("]")

        # === Add spacing after block elements ===
        if tag in ("p", "item", "head"):
            parts.append(" ")

    # =========================================================================
    # Metadata extraction
    # =========================================================================

    def _extract_entry_number(self, div) -> str:
        """Extract Humboldt's entry number from <head> element."""
        head = div.find("tei:head", NS)
        if head is None:
            return ""
        text = "".join(head.itertext())
        # Match patterns like "N. 1", "Numero 2–8", "N. 10."
        match = re.search(r"(?:N(?:umero)?\.?\s*)(\d+(?:[–\-]\d+)?)", text)
        return f"N. {match.group(1)}" if match else text.strip()[:50]

    def _extract_dates(self, elem) -> list[str]:
        """Extract normalized ISO dates, ordered by specificity."""
        dates = []
        for d in elem.findall(".//tei:date", NS):
            when = d.get("when", "")
            if when:
                dates.append(when)
        # Sort: most specific (longest) first
        dates.sort(key=len, reverse=True)
        return dates

    def _format_date(self, iso_date: str) -> str:
        """Convert ISO date to German display format."""
        if not iso_date:
            return ""
        months = {
            "01": "Januar", "02": "Februar", "03": "März", "04": "April",
            "05": "Mai", "06": "Juni", "07": "Juli", "08": "August",
            "09": "September", "10": "Oktober", "11": "November", "12": "Dezember",
        }
        parts = iso_date.split("-")
        if len(parts) == 3:
            return f"{int(parts[2])}. {months.get(parts[1], parts[1])} {parts[0]}"
        elif len(parts) == 2:
            return f"{months.get(parts[1], parts[1])} {parts[0]}"
        return parts[0]

    def _extract_all_entities(self, elem) -> dict:
        """All annotated entity classes of one element, keyed by chunk field."""
        return {
            "persons": self._extract_entities(elem, "persName", "person"),
            "places": self._extract_entities(elem, "placeName", "place"),
            "institutions": self._extract_entities(elem, "orgName", "institution"),
            # plant names carry no ref IDs; @key holds the normalized Latin
            # binomial, which resolves against the plant register at ingest
            "plants": self._extract_entities(
                elem, "name[@type='plant']", "plant", prefer_key=True),
        }

    def _extract_entities(self, elem, tag_name: str, entity_type: str,
                          prefer_key: bool = False) -> list[Entity]:
        """Extract named entities with authority file links."""
        entities = []
        seen = set()

        for ent_elem in elem.findall(f".//tei:{tag_name}", NS):
            if prefer_key and ent_elem.get("key"):
                name = ent_elem.get("key")
            else:
                name = self._extract_text(ent_elem, mode="regularized")
            name = name.strip()
            if not name or len(name) < 2:
                continue

            ref = ent_elem.get("ref", "")
            refs = self._parse_ref_string(ref)

            # Dedup by name + primary ID
            key = (name, refs.get("gnd", ""), refs.get("geonames", ""))
            if key in seen:
                continue
            seen.add(key)

            entities.append(Entity(
                name=name,
                entity_type=entity_type,
                humboldt_id=refs.get("humboldt", ""),
                gnd_id=refs.get("gnd", ""),
                geonames_id=refs.get("geonames", ""),
                viaf_id=refs.get("viaf", ""),
            ))

        return entities

    def _extract_bibliography(self, elem) -> list[Entity]:
        """Extract bibliographic references."""
        bibls = []
        seen = set()
        for bibl in elem.findall(".//tei:bibl", NS):
            text = self._extract_text(bibl, mode="regularized").strip()
            corresp = bibl.get("corresp", "")
            if text and text not in seen:
                seen.add(text)
                bibls.append(Entity(
                    name=text,
                    entity_type="bibliography",
                    humboldt_id=corresp.replace("https://edition-humboldt.de/", ""),
                ))
        return bibls

    def _extract_measurements(self, elem) -> list[Measurement]:
        """Extract <measure> elements with historical units."""
        measurements = []
        for m in elem.findall(".//tei:measure", NS):
            qty = m.get("quantity")
            unit = m.get("unit", "")
            if qty:
                try:
                    measurements.append(Measurement(
                        value=float(qty),
                        unit=unit,
                        context=self._extract_text(m, mode="regularized").strip(),
                    ))
                except ValueError:
                    pass
        return measurements

    def _extract_editorial_notes(self, elem) -> list[str]:
        """Extract editorial notes (type='editorial') with full text."""
        notes = []
        for note in elem.findall(".//tei:note[@type='editorial']", NS):
            # Get text without recursive editorial note processing
            text = "".join(note.itertext())
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                notes.append(text)
        return notes

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _parse_ref_string(ref: str) -> dict:
        """Parse a space-separated ref string into typed IDs."""
        result = {"gnd": "", "geonames": "", "viaf": "", "humboldt": ""}
        if not ref:
            return result
        for part in ref.split():
            part = part.strip()
            if "d-nb.info/gnd/" in part:
                result["gnd"] = part.split("gnd/")[-1]
            elif "geonames.org/" in part:
                result["geonames"] = part.split("geonames.org/")[-1]
            elif "viaf.org/viaf/" in part:
                result["viaf"] = part.split("viaf/")[-1]
            elif "edition-humboldt.de/" in part:
                result["humboldt"] = part.split("edition-humboldt.de/")[-1]
        return result

    @staticmethod
    def _normalize_for_search(text: str) -> str:
        """Normalize text for search indexing / embedding."""
        text = text.lower()
        # Remove editorial markers
        text = re.sub(r"\[anm\..*?\]", "", text)
        text = re.sub(r"[\[\]]", "", text)
        # Normalize unicode
        text = re.sub(r"[–—]", "-", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


# =============================================================================
# CLI entry point
# =============================================================================

if __name__ == "__main__":
    import sys
    import json

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python -m src.ingestion.tei_parser <path-to-xml>")
        sys.exit(1)

    parser = TEIParser()
    chunks = parser.parse(sys.argv[1])

    print(f"\n{'='*70}")
    print(f"Parsed {len(chunks)} chunks")
    print(f"{'='*70}")

    for chunk in chunks:
        print(f"\n--- {chunk.entry_number or 'no entry#'} | "
              f"Folio {chunk.folio_start}–{chunk.folio_end} | "
              f"{chunk.date_display} ---")
        print(f"  URL: {chunk.url}")
        print(f"  Text: {chunk.text_regularized[:200]}...")
        print(f"  Persons: {len(chunk.persons)}, Places: {len(chunk.places)}, "
              f"Measurements: {len(chunk.measurements)}")
        print(f"  Embedding text preview: {chunk.embedding_text[:150]}...")
