"""Read the edition's registers: persons, places, institutions, glossary,
plants, units of measure and bibliographical sigla."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from lxml import etree

from alex.corpus.tei import PARSER, TEI, XML_ID, XML_LANG, authority_ids, flat_text

PLANT_DETAIL_URL = "https://edition-humboldt.de/register/pflanzen/detail.xql?name="

REGISTER_DIRECTORIES = {
    "person": "person",
    "place": "place",
    "institution": "institution",
    "glossary": "glossary",
    "plant": "plant",
    "bibliographical-abbrevation": "siglum",
}


@dataclass
class Entity:
    entity_id: str
    kind: str
    name: str
    alternative_names: list[str] = field(default_factory=list)
    description: str = ""
    born: str = ""
    died: str = ""
    gnd: str = ""
    geonames: str = ""
    viaf: str = ""
    wikidata: str = ""
    url: str = ""
    links: dict[str, str] = field(default_factory=dict)


@dataclass
class Unit:
    unit_id: str
    label: str
    quantity_kind: str
    si_factor: float | None
    si_unit: str
    description_de: str
    description_en: str
    note: str


def normalized_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", plain.lower())).strip()


def plant_id(latin_name: str) -> str:
    return "plant-" + normalized_name(latin_name).replace(" ", "-")


def register_url(root, entity_id: str) -> str:
    version_root = flat_text(root.find(f".//{TEI}idno[@type='URLWeb']")).rstrip("/")
    return f"{version_root}/{entity_id}" if version_root else ""


def authority_fields(element) -> dict[str, str]:
    targets = " ".join(flat_text(idno) for idno in element.findall(f"{TEI}idno[@type='uri']"))
    return authority_ids(targets)


def read_person(path: Path) -> list[Entity]:
    root = etree.parse(str(path), PARSER).getroot()
    entities = []
    for person in root.iter(TEI + "person"):
        entity_id = person.get(XML_ID) or path.stem
        names = person.findall(TEI + "persName")
        regular = next(
            (name for name in names if name.get("type") == "reg"), names[0] if names else None
        )
        if regular is None:
            continue
        surname = flat_text(regular.find(TEI + "surname"))
        forename = flat_text(regular.find(TEI + "forename"))
        display = (
            f"{surname}, {forename}" if surname and forename else surname or flat_text(regular)
        )
        alternatives = [flat_text(name) for name in names if name is not regular]
        if surname and forename:
            alternatives.append(f"{forename} {surname}")
        entities.append(
            Entity(
                entity_id=entity_id,
                kind="person",
                name=display,
                alternative_names=[name for name in alternatives if name],
                description=flat_text(person.find(TEI + "note")),
                born=flat_text(person.find(TEI + "birth"))
                or flat_text(person.find(TEI + "floruit")),
                died=flat_text(person.find(TEI + "death")),
                url=register_url(root, entity_id),
                **authority_fields(person),
            )
        )
    return entities


def read_place(path: Path) -> list[Entity]:
    root = etree.parse(str(path), PARSER).getroot()
    entities = []
    for place in root.iter(TEI + "place"):
        entity_id = place.get(XML_ID) or path.stem
        names = place.findall(TEI + "placeName")
        regular = next(
            (name for name in names if name.get("type") == "reg"), names[0] if names else None
        )
        if regular is None:
            continue
        entities.append(
            Entity(
                entity_id=entity_id,
                kind="place",
                name=flat_text(regular),
                alternative_names=[flat_text(name) for name in names if name is not regular],
                description=flat_text(place.find(TEI + "note")),
                url=register_url(root, entity_id),
                **authority_fields(place),
            )
        )
    return entities


def read_institution(path: Path) -> list[Entity]:
    root = etree.parse(str(path), PARSER).getroot()
    entities = []
    for organisation in root.iter(TEI + "org"):
        entity_id = organisation.get(XML_ID) or path.stem
        names = organisation.findall(TEI + "orgName")
        regular = next(
            (name for name in names if name.get("type") == "reg"), names[0] if names else None
        )
        if regular is None:
            continue
        settlement = flat_text(organisation.find(f".//{TEI}settlement"))
        parts = [
            flat_text(organisation.find(TEI + "desc")),
            flat_text(organisation.find(TEI + "note")),
            f"Sitz: {settlement}" if settlement else "",
        ]
        authority = authority_fields(organisation)
        authority.pop("geonames", None)
        entities.append(
            Entity(
                entity_id=entity_id,
                kind="institution",
                name=flat_text(regular),
                alternative_names=[flat_text(name) for name in names if name is not regular],
                description=" ".join(part for part in parts if part),
                url=register_url(root, entity_id),
                **authority,
            )
        )
    return entities


def read_glossary(path: Path) -> list[Entity]:
    root = etree.parse(str(path), PARSER).getroot()
    entities = []
    for item in root.iter(TEI + "item"):
        labels = item.findall(TEI + "label")
        regular = next(
            (label for label in labels if label.get("type") == "reg"), labels[0] if labels else None
        )
        if regular is None:
            continue
        entity_id = item.get(XML_ID) or path.stem
        language = regular.get(XML_LANG, "")
        note = item.find(TEI + "note")
        paragraphs = (
            [flat_text(paragraph) for paragraph in note.findall(TEI + "p")]
            if note is not None
            else []
        )
        description = " ".join(paragraphs) or flat_text(note)
        entities.append(
            Entity(
                entity_id=entity_id,
                kind="glossary",
                name=flat_text(regular),
                alternative_names=[flat_text(label) for label in labels if label is not regular],
                description=(f"({language}) " if language else "") + description,
                url=register_url(root, entity_id),
            )
        )
    return entities


def read_siglum(path: Path) -> list[Entity]:
    root = etree.parse(str(path), PARSER).getroot()
    entities = []
    for item in root.iter(TEI + "item"):
        label = flat_text(item.find(TEI + "label"))
        if label:
            entities.append(
                Entity(
                    entity_id="siglum-" + (item.get(XML_ID) or path.stem),
                    kind="siglum",
                    name=label,
                    description=flat_text(item.find(TEI + "desc")),
                )
            )
    return entities


def read_plants(path: Path) -> list[Entity]:
    root = etree.parse(str(path), PARSER).getroot()
    entities = []
    for item in root.iter(TEI + "item"):
        latin_name = flat_text(item.find(f"{TEI}label[@type='reg']"))
        if not latin_name:
            continue
        links = {}
        for group in item.findall(TEI + "linkGrp"):
            pointer = group.find(TEI + "ptr")
            if pointer is not None and pointer.get("target"):
                links[flat_text(group.find(TEI + "desc"))] = pointer.get("target")
        wikidata = links.get("Wikidata", "").rstrip("/").rsplit("/", 1)[-1]
        entities.append(
            Entity(
                entity_id=plant_id(latin_name),
                kind="plant",
                name=latin_name,
                wikidata=wikidata,
                links=links,
                url=PLANT_DETAIL_URL + quote(latin_name),
            )
        )
    return entities


READERS = {
    "person": read_person,
    "place": read_place,
    "institution": read_institution,
    "glossary": read_glossary,
    "siglum": read_siglum,
    "plant": read_plants,
}


def read_registers(index_directory: Path):
    """Yield every register entity below data/index."""
    for directory_name, kind in REGISTER_DIRECTORIES.items():
        directory = index_directory / directory_name
        for path in sorted(directory.glob("*.xml")):
            yield from READERS[kind](path)


def read_units(index_directory: Path) -> list[Unit]:
    units = []
    for path in sorted((index_directory / "measure").glob("*.xml")):
        root = etree.parse(str(path), PARSER).getroot()
        for item in root.iter(TEI + "item"):
            measure = item.find(TEI + "measure")
            factor = None
            if measure is not None and measure.get("quantity"):
                try:
                    factor = float(measure.get("quantity"))
                except ValueError:
                    factor = None
            descriptions = {
                desc.get(XML_LANG, ""): flat_text(desc) for desc in item.findall(TEI + "desc")
            }
            units.append(
                Unit(
                    unit_id=item.get(XML_ID) or path.stem,
                    label=flat_text(item.find(TEI + "label")),
                    quantity_kind=measure.get("type", "") if measure is not None else "",
                    si_factor=factor,
                    si_unit=measure.get("unit", "") if measure is not None else "",
                    description_de=descriptions.get("deu", ""),
                    description_en=descriptions.get("eng", ""),
                    note=flat_text(item.find(TEI + "note")),
                )
            )
    return units
