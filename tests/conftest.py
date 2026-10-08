"""A miniature edition, written as TEI the way the real dump is, and the
objects built from it."""

from __future__ import annotations

from pathlib import Path

import pytest

from alex.agent.loop import Agent
from alex.config import ModelSpec, load_settings
from alex.corpus.build import build_store
from alex.corpus.store import DocumentStore
from alex.llm.base import Citation, TextBlock, ToolCall, Turn, Usage
from alex.sandbox.runner import Sandbox

HEADER = """<TEI xmlns="http://www.tei-c.org/ns/1.0" xml:id="{doc_id}"><teiHeader><fileDesc>
<titleStmt><title>{title}</title><editor><persName><surname>Muster</surname>
<forename>Erika</forename></persName></editor></titleStmt>
<editionStmt><p>{title}, hg. v. Erika Muster. URL: <ref target="https://edition-humboldt.de/v99/">https://edition-humboldt.de/v99/</ref></p></editionStmt>
<publicationStmt><idno type="URLWeb">https://edition-humboldt.de/v99/</idno></publicationStmt>
{source}</fileDesc><profileDesc>{profile}</profileDesc></teiHeader><text><body>{body}</body></text></TEI>"""

DIARY_BODY = """
<div><pb n="1r" facs="https://example.org/facs/1"/><head>Numero 1.</head>
<p>Am <date when="1797-06-14">14ten Junius 97</date> Höhenmessungen am
  <placeName ref="https://edition-humboldt.de/H0000001 https://www.geonames.org/111">Königstein</placeName>.
  Das Barometer stand <measure quantity="27" unit="pouce">27 <choice><abbr>Z.</abbr><expan>Zoll</expan></choice></measure>
  <del rendition="#s">falsch</del> hoch. Mit
  <persName ref="https://edition-humboldt.de/H0000010 http://d-nb.info/gnd/118">
    <choice><abbr>Bonpl.</abbr><expan>Bonpland</expan></choice></persName> sah ich
  <ref xml:id="r1" corresp="#n1" type="editorialNote">das Croco<g ref="#typoHyphen"/><lb break="no"/>dil</ref><note
  xml:id="n1" corresp="#r1" type="editorial">Gemeint ist ein Kaiman, vgl.
  <persName ref="https://edition-humboldt.de/H0000011">Willdenow</persName>.</note> am Ufer.
  <note hand="#author" place="left">vide Tafel</note></p>
<pb n="1v"/>
<p>Die <name type="plant" key="Cinchona officinalis">Quina</name> wächst bei
  <placeName ref="https://edition-humboldt.de/H0000099 https://www.geonames.org/222">Loxa</placeName>.</p>
<table><row><cell>Ort</cell><cell>Höhe</cell></row><row><cell>Dresden</cell>
  <cell><measure quantity="86" unit="toise">86 toises</measure></cell></row></table>
</div>
<div><head>Numero 2.</head><p>Zweiter Eintrag über den Vesuv und seine Lava.</p></div>
"""

LETTER_PROFILE = """<textClass><keywords><term>letter</term></keywords></textClass>
<correspDesc><correspAction type="sent">
<persName ref="https://edition-humboldt.de/H0012069">Humboldt, Alexander von</persName>
<placeName ref="https://edition-humboldt.de/H0000002 https://www.geonames.org/333">Berlin</placeName>
<date when="1828-09-01"/></correspAction><correspAction type="received">
<persName ref="https://edition-humboldt.de/H0000010">Bonpland, Aimé</persName>
</correspAction></correspDesc>"""

PERSON = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><publicationStmt>
<idno type="URLWeb">https://edition-humboldt.de/v99/</idno></publicationStmt></fileDesc></teiHeader>
<text><body><listPerson><person xml:id="{entity_id}"><idno type="uri">http://d-nb.info/gnd/118</idno>
<persName type="reg"><surname>{surname}</surname><forename>{forename}</forename></persName>
<birth>1773</birth><death>1858</death><note>{note}</note></person></listPerson></body></text></TEI>"""

PLACE = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><publicationStmt>
<idno type="URLWeb">https://edition-humboldt.de/v99/</idno></publicationStmt></fileDesc></teiHeader>
<text><body><listPlace><place xml:id="{entity_id}"><idno type="uri">https://www.geonames.org/{geonames}</idno>
<placeName type="reg">{name}</placeName><note>{note}</note></place></listPlace></body></text></TEI>"""

PLANTS = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text><body><list>
<item><label type="reg">Cinchona officinalis</label><linkGrp><desc>Wikidata</desc>
<ptr target="https://wikidata.org/wiki/Q164574"/></linkGrp></item></list></body></text></TEI>"""

UNIT = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text><body><list>
<item xml:id="{unit_id}"><label>{label}</label><desc xml:lang="deu">Längenmaß</desc>
<measure type="length" quantity="{factor}" unit="{unit}"/></item></list></body></text></TEI>"""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("corpus")
    data = root / "data"
    journal = (
        "<textClass><keywords><term>travel-journal</term></keywords></textClass>"
        '<creation><date notBefore="1797-06-01" notAfter="1798-04-30"/></creation>'
    )
    manuscript = (
        "<sourceDesc><msDesc><msIdentifier><institution>Staatsbibliothek</institution>"
        '<idno><idno type="shelfmark">Nachl. 1</idno></idno></msIdentifier></msDesc></sourceDesc>'
    )
    write(
        data / "travel-journal" / "H0000100.xml",
        HEADER.format(
            doc_id="H0000100",
            title="Tagebuch der Probe-Reise",
            source=manuscript,
            profile=journal,
            body=DIARY_BODY,
        ),
    )
    write(
        data / "letter" / "H0000200.xml",
        HEADER.format(
            doc_id="H0000200",
            title="Humboldt an Bonpland. Berlin, 1. September 1828",
            source="",
            profile=LETTER_PROFILE,
            body='<div type="writingSession"><pb n="1r"/><p>Mon cher ami, le crocodile de '
            "l’Orénoque m’occupe toujours.</p></div>",
        ),
    )
    chronology = "<textClass><keywords><term>chronology</term></keywords></textClass>"
    for doc_id, date, label, text in (
        (
            "H0000300",
            "1805-03-11",
            "11. März 1805",
            "H. reist nach <placeName "
            'ref="https://edition-humboldt.de/H0000002">Berlin</placeName> ab.',
        ),
        ("H0000301", "1805-05-01", "1. Mai 1805", "H. hält sich in Rom auf."),
    ):
        document = HEADER.format(
            doc_id=doc_id, title="", source="", profile=chronology, body=f"<p>{text}</p>"
        )
        document = document.replace(
            "<title></title>", f'<title><date when="{date}">{label}</date></title>'
        )
        write(data / "chronology" / f"{doc_id}.xml", document)
    for directory in ("document", "research-article", "about"):
        (data / directory).mkdir(parents=True)
    index = data / "index"
    write(
        index / "person" / "H0000010.xml",
        PERSON.format(
            entity_id="H0000010",
            surname="Bonpland",
            forename="Aimé",
            note="französischer Botaniker, Reisebegleiter Humboldts",
        ),
    )
    write(
        index / "person" / "H0012069.xml",
        PERSON.format(
            entity_id="H0012069", surname="Humboldt", forename="Alexander von", note="Naturforscher"
        ),
    )
    write(
        index / "place" / "H0000001.xml",
        PLACE.format(
            entity_id="H0000001", geonames="111", name="Königstein", note="Festung in Sachsen"
        ),
    )
    write(
        index / "place" / "H0000002.xml",
        PLACE.format(entity_id="H0000002", geonames="333", name="Berlin", note=""),
    )
    write(index / "plant" / "C.xml", PLANTS)
    write(
        index / "measure" / "toise.xml",
        UNIT.format(unit_id="toise", label="Toise", factor="1.949", unit="m"),
    )
    write(
        index / "measure" / "pouce.xml",
        UNIT.format(unit_id="pouce", label="Pouce", factor="2.707", unit="cm"),
    )
    for directory in ("institution", "glossary", "bibliographical-abbrevation"):
        (index / directory).mkdir(parents=True)
    write(root / "README.md", "TEI-XML-Datenset (Version 99.0.0 vom 01.01.2026)")
    write(root / "coordinates.json", '{"111": [50.92, 14.07], "333": [52.52, 13.4]}')
    return root


@pytest.fixture(scope="session")
def store_path(corpus, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("store") / "ehd.sqlite"
    build_store(corpus, path, corpus / "coordinates.json")
    return path


@pytest.fixture(scope="session")
def store(store_path) -> DocumentStore:
    return DocumentStore(store_path)


@pytest.fixture(scope="session")
def sandbox(store_path, corpus) -> Sandbox:
    return Sandbox("subprocess", store_path, corpus, timeout_seconds=60)


class ScriptedConversation:
    """Plays back prepared turns; records what the loop sends it."""

    def __init__(self, provider, turns):
        self.provider = provider
        self.turns = list(turns)

    def stream(self, allow_tools=True):
        self.provider.allowed.append(allow_tools)
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(self.provider)
        for block in turn.blocks:
            yield {"type": "block"}
            yield {"type": "text", "text": block.text}
        return turn

    def add_tool_outputs(self, outputs, note=""):
        self.provider.outputs.append(outputs)
        self.provider.notes.append(note)

    def add_note(self, note):
        self.provider.notes.append(note)


class ScriptedProvider:
    native_citations = True

    def __init__(self, turns):
        self.turns = turns
        self.outputs = []
        self.notes = []
        self.allowed = []
        self.system = ""

    def conversation(self, spec, system, context_note, tools, history, query, max_tokens):
        self.system = system
        self.tools = tools
        return ScriptedConversation(self, self.turns)

    def complete(self, system, prompt, max_tokens=300):
        return "Wo war Humboldt 1805?\nWer war Bonpland?\nWelche Pflanzen nennt das Tagebuch?"


def tool_turn(*calls) -> Turn:
    return Turn(
        [],
        [
            ToolCall(f"call{index}", name, arguments)
            for index, (name, arguments) in enumerate(calls)
        ],
        "tools",
        Usage(10, 5),
    )


def answer_turn(text: str, cited: list[str] | None = None) -> Turn:
    return Turn(
        [TextBlock(text, [Citation(source_id, "") for source_id in cited or []])],
        [],
        "end",
        Usage(10, 5),
    )


TEST_MODEL = ModelSpec("scripted", "Scripted", "scripted")


@pytest.fixture
def make_agent(store, store_path, corpus, sandbox, monkeypatch):
    def build(turns, **overrides):
        monkeypatch.setenv("STORE_PATH", str(store_path))
        monkeypatch.setenv("CORPUS_DIR", str(corpus))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        settings = load_settings()
        settings.models = [TEST_MODEL]
        settings.default_model = TEST_MODEL.id
        for name, value in overrides.items():
            setattr(settings, name, value)
        provider = ScriptedProvider(turns)
        return Agent(settings, store, sandbox, {"scripted": provider}), provider, settings

    return build
