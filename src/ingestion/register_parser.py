"""
Register Parser for edition humboldt digital.

Parses the index/register files (persons, places, glossary, plants) into
embeddable HumboldtChunk objects. These are small but critical for answering
questions like "Wer war Bonpland?" or "Was bedeutet 'alii dicunt'?"

Register types and their TEI structure:
- person: <person> → name, birth, death, note, VIAF/GND
- place: <place> → name, note, GeoNames
- glossary: <item> → label (multilingual), note with definition
- plants: <item> → scientific name, document references, BHL/GBIF links
"""
from __future__ import annotations

import re
import logging
from pathlib import Path
from lxml import etree

from src.models import HumboldtChunk, Entity

logger = logging.getLogger(__name__)

NS = {"tei": "http://www.tei-c.org/ns/1.0"}
XML_NS = "http://www.w3.org/XML/1998/namespace"


class RegisterParser:
    """
    Parses register/index XML files into HumboldtChunks.

    Usage:
        parser = RegisterParser()
        chunks = parser.parse_directory("data/registers/person", register_type="person")
    """

    def parse_directory(self, dirpath: str | Path, register_type: str) -> list[HumboldtChunk]:
        """Parse all XML files in a register directory."""
        dirpath = Path(dirpath)
        if not dirpath.exists():
            logger.warning(f"Register directory not found: {dirpath}")
            return []

        xml_files = sorted(dirpath.glob("*.xml"))
        logger.info(f"Parsing {len(xml_files)} {register_type} register files...")

        chunks = []
        for xml_file in xml_files:
            try:
                file_chunks = self.parse_file(xml_file, register_type)
                chunks.extend(file_chunks)
            except Exception as e:
                logger.warning(f"Failed to parse {xml_file.name}: {e}")

        logger.info(f"Extracted {len(chunks)} {register_type} chunks")
        return chunks

    def parse_file(self, filepath: Path, register_type: str) -> list[HumboldtChunk]:
        """Parse a single register XML file."""
        tree = etree.parse(str(filepath))
        root = tree.getroot()

        # Extract common metadata
        url_web = root.find(".//tei:idno[@type='URLWeb']", NS)
        url = (url_web.text or "").strip() if url_web is not None else ""

        dispatch = {
            "person": self._parse_person,
            "place": self._parse_place,
            "glossary": self._parse_glossary,
            "institution": self._parse_institution,
            "plants": self._parse_plants,
        }

        parser_fn = dispatch.get(register_type)
        if parser_fn is None:
            logger.warning(f"Unknown register type: {register_type}")
            return []

        return parser_fn(root, url, filepath.stem)

    # =========================================================================
    # Person register
    # =========================================================================

    def _parse_person(self, root, url: str, file_id: str) -> list[HumboldtChunk]:
        """
        Parse a person register entry.
        Structure: <person> with <persName>, <birth>, <death>, <note>, <idno>
        """
        persons = root.findall(".//tei:person", NS)
        chunks = []

        for person in persons:
            xml_id = person.get(f"{{{XML_NS}}}id", file_id)

            # Name
            persname = person.find("tei:persName[@type='reg']", NS)
            if persname is None:
                persname = person.find("tei:persName", NS)
            
            surname = ""
            forename = ""
            if persname is not None:
                sn = persname.find("tei:surname", NS)
                fn = persname.find("tei:forename", NS)
                surname = (sn.text or "").strip() if sn is not None else ""
                forename = (fn.text or "").strip() if fn is not None else ""
            
            if not surname and not forename:
                # Try plain text
                name = "".join(persname.itertext()).strip() if persname is not None else ""
            else:
                name = f"{forename} {surname}".strip() if forename else surname

            display_name = f"{surname}, {forename}" if forename and surname else name

            # Life dates
            birth = person.find("tei:birth", NS)
            death = person.find("tei:death", NS)
            birth_text = (birth.text or "").strip() if birth is not None else ""
            death_text = (death.text or "").strip() if death is not None else ""
            life_dates = ""
            if birth_text or death_text:
                life_dates = f"({birth_text}–{death_text})"

            # Description
            note = person.find("tei:note", NS)
            description = ""
            if note is not None:
                description = "".join(note.itertext()).strip()
                description = re.sub(r"\s+", " ", description)

            # Authority IDs
            idno = person.find("tei:idno[@type='uri']", NS)
            viaf_id = ""
            gnd_id = ""
            if idno is not None and idno.text:
                uri = idno.text.strip()
                if "viaf.org" in uri:
                    viaf_id = uri.split("viaf/")[-1]
                elif "d-nb.info/gnd" in uri:
                    gnd_id = uri.split("gnd/")[-1]

            # Build natural-language text for embedding
            text_parts = [f"{display_name} {life_dates}".strip()]
            if description:
                text_parts.append(description)

            text = ". ".join(text_parts)

            if not text or len(text) < 5:
                continue

            chunks.append(HumboldtChunk(
                doc_id=xml_id,
                doc_title=f"Personenregister: {display_name}",
                doc_type="register_person",
                url=url,
                text_regularized=text,
                text_plain=text.lower(),
                persons=[Entity(
                    name=name,
                    entity_type="person",
                    humboldt_id=xml_id,
                    viaf_id=viaf_id,
                    gnd_id=gnd_id,
                )],
            ))

        return chunks

    # =========================================================================
    # Place register
    # =========================================================================

    def _parse_place(self, root, url: str, file_id: str) -> list[HumboldtChunk]:
        """
        Parse a place register entry.
        Structure: <place> with <placeName>, <note>, <idno> (GeoNames)
        """
        places = root.findall(".//tei:place", NS)
        chunks = []

        for place in places:
            xml_id = place.get(f"{{{XML_NS}}}id", file_id)

            # Name
            placename = place.find("tei:placeName[@type='reg']", NS)
            if placename is None:
                placename = place.find("tei:placeName", NS)
            name = "".join(placename.itertext()).strip() if placename is not None else ""

            # Description
            note = place.find("tei:note", NS)
            description = ""
            if note is not None:
                description = "".join(note.itertext()).strip()
                description = re.sub(r"\s+", " ", description)

            # GeoNames ID
            idno = place.find("tei:idno[@type='uri']", NS)
            geonames_id = ""
            if idno is not None and idno.text:
                uri = idno.text.strip()
                if "geonames.org" in uri:
                    geonames_id = uri.split("geonames.org/")[-1]

            # Coordinates (if present)
            lat, lon = None, None
            geo = place.find(".//tei:geo", NS)
            if geo is not None and geo.text:
                parts = geo.text.strip().split()
                if len(parts) == 2:
                    try:
                        lat, lon = float(parts[0]), float(parts[1])
                    except ValueError:
                        pass

            # Build text
            text_parts = [f"{name}"]
            if description:
                text_parts.append(description)

            text = ". ".join(text_parts)
            if not text or len(text) < 3:
                continue

            chunks.append(HumboldtChunk(
                doc_id=xml_id,
                doc_title=f"Ortsregister: {name}",
                doc_type="register_place",
                url=url,
                text_regularized=text,
                text_plain=text.lower(),
                places=[Entity(
                    name=name,
                    entity_type="place",
                    humboldt_id=xml_id,
                    geonames_id=geonames_id,
                    lat=lat,
                    lon=lon,
                )],
            ))

        return chunks

    # =========================================================================
    # Institution register
    # =========================================================================

    def _parse_institution(self, root, url: str, file_id: str) -> list[HumboldtChunk]:
        """Parse institution register entries (similar structure to place)."""
        orgs = root.findall(".//tei:org", NS)
        chunks = []

        for org in orgs:
            xml_id = org.get(f"{{{XML_NS}}}id", file_id)

            orgname = org.find("tei:orgName[@type='reg']", NS)
            if orgname is None:
                orgname = org.find("tei:orgName", NS)
            name = "".join(orgname.itertext()).strip() if orgname is not None else ""

            note = org.find("tei:note", NS)
            description = ""
            if note is not None:
                description = "".join(note.itertext()).strip()
                description = re.sub(r"\s+", " ", description)

            text_parts = [name]
            if description:
                text_parts.append(description)
            text = ". ".join(text_parts)

            if not text or len(text) < 3:
                continue

            chunks.append(HumboldtChunk(
                doc_id=xml_id,
                doc_title=f"Institutionenregister: {name}",
                doc_type="register_institution",
                url=url,
                text_regularized=text,
                text_plain=text.lower(),
            ))

        return chunks

    # =========================================================================
    # Glossary register
    # =========================================================================

    def _parse_glossary(self, root, url: str, file_id: str) -> list[HumboldtChunk]:
        """
        Parse glossary register entry.
        Structure: <item> with <label> (term), <note> with <p> (definition)
        """
        items = root.findall(".//tei:item", NS)
        chunks = []

        for item in items:
            xml_id = item.get(f"{{{XML_NS}}}id", file_id)

            # Primary term
            label_reg = item.find("tei:label[@type='reg']", NS)
            term = (label_reg.text or "").strip() if label_reg is not None else ""

            # Language
            lang = ""
            if label_reg is not None:
                lang = label_reg.get(f"{{{XML_NS}}}lang", "")

            # Alternative forms
            alt_labels = item.findall("tei:label[@type='alt']", NS)
            alt_forms = [
                (l.text or "").strip() for l in alt_labels if l.text
            ]

            # Definition from <note>
            note = item.find("tei:note", NS)
            definition = ""
            if note is not None:
                paragraphs = note.findall("tei:p", NS)
                if paragraphs:
                    definition = " ".join(
                        re.sub(r"\s+", " ", "".join(p.itertext())).strip()
                        for p in paragraphs
                    )
                else:
                    definition = "".join(note.itertext()).strip()
                    definition = re.sub(r"\s+", " ", definition)

            # Build text
            text_parts = []
            if term:
                lang_label = f" ({lang})" if lang else ""
                text_parts.append(f"Glossar: {term}{lang_label}")
            if alt_forms:
                text_parts.append(f"Auch: {', '.join(alt_forms)}")
            if definition:
                text_parts.append(definition)

            text = ". ".join(text_parts)
            if not text or len(text) < 5:
                continue

            chunks.append(HumboldtChunk(
                doc_id=xml_id,
                doc_title=f"Glossar: {term}",
                doc_type="register_glossary",
                url=url,
                text_regularized=text,
                text_plain=text.lower(),
            ))

        return chunks

    # =========================================================================
    # Plants register
    # =========================================================================

    def _parse_plants(self, root, url: str, file_id: str) -> list[HumboldtChunk]:
        """
        Parse plant register file. These are alphabetical files (A.xml–Z.xml),
        each containing multiple <item> entries.
        Structure: <item> with <label> (name), <listRef> (doc refs), <linkGrp> (ext links)
        """
        items = root.findall(".//tei:list/tei:item", NS)
        chunks = []

        for item in items:
            # Scientific name
            label = item.find("tei:label[@type='reg']", NS)
            plant_name = (label.text or "").strip() if label is not None else ""
            if not plant_name:
                continue

            # Collect document references (where this plant is mentioned)
            references = []
            for list_ref in item.findall(".//tei:listRef/tei:listRef", NS):
                desc = list_ref.find("tei:desc", NS)
                doc_desc = "".join(desc.itertext()).strip() if desc is not None else ""
                doc_desc = re.sub(r"\s+", " ", doc_desc)

                folios = []
                for ref in list_ref.findall("tei:ref", NS):
                    folio = ref.get("n", "")
                    # Check for vernacular name
                    vernacular = ref.text.strip() if ref.text else ""
                    if folio:
                        folios.append(folio)
                    if vernacular and ref.get("ana") == "#vernacular":
                        references.append(f"Volksname: {vernacular}")

                if doc_desc:
                    ref_text = doc_desc
                    if folios:
                        ref_text += f" (Bl. {', '.join(folios)})"
                    references.append(ref_text)

            # External links (BHL, GBIF, Catalogue of Life)
            ext_links = []
            for link_grp in item.findall("tei:linkGrp", NS):
                desc = link_grp.find("tei:desc", NS)
                source = "".join(desc.itertext()).strip() if desc is not None else ""
                ptrs = link_grp.findall("tei:ptr", NS)
                for ptr in ptrs:
                    target = ptr.get("target", "")
                    link_name = ptr.get("n", "")
                    if target:
                        ext_links.append(f"{source}: {link_name or target}")

            # Build text
            text_parts = [f"Pflanze: {plant_name}"]
            if references:
                text_parts.append(f"Erwähnt in: {'; '.join(references[:5])}")
            if ext_links:
                text_parts.append(f"Externe Referenzen: {'; '.join(ext_links[:3])}")

            text = ". ".join(text_parts)

            chunks.append(HumboldtChunk(
                doc_id=f"plant_{plant_name.replace(' ', '_')}",
                doc_title=f"Pflanzenregister: {plant_name}",
                doc_type="register_plant",
                url=url,
                text_regularized=text,
                text_plain=text.lower(),
            ))

        return chunks


# =============================================================================
# CLI entry point
# =============================================================================

def parse_all_registers(register_base_dir: str = "data/registers") -> list[HumboldtChunk]:
    """Parse all register types and return combined chunk list."""
    base = Path(register_base_dir)
    parser = RegisterParser()
    all_chunks = []

    type_map = {
        "person": "person",
        "place": "place",
        "institution": "institution",
        "glossary": "glossary",
        "plants": "plants",
    }

    for dirname, reg_type in type_map.items():
        dirpath = base / dirname
        if dirpath.exists():
            chunks = parser.parse_directory(dirpath, reg_type)
            all_chunks.extend(chunks)
            logger.info(f"  {reg_type}: {len(chunks)} chunks")

    logger.info(f"Total register chunks: {len(all_chunks)}")
    return all_chunks


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    register_dir = sys.argv[1] if len(sys.argv) > 1 else "data/registers"
    chunks = parse_all_registers(register_dir)

    print(f"\nTotal: {len(chunks)} register chunks")
    for doc_type in set(c.doc_type for c in chunks):
        count = sum(1 for c in chunks if c.doc_type == doc_type)
        sample = next(c for c in chunks if c.doc_type == doc_type)
        print(f"\n  {doc_type}: {count} chunks")
        print(f"    Example: {sample.text_regularized[:150]}...")
