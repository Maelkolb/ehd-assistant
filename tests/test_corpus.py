import pytest

from alex.corpus.store import StoreError, fold, query_terms
from alex.corpus.tei import read_document


def test_document_is_read_whole_with_offsets(corpus):
    document = read_document(corpus / "data" / "travel-journal" / "H0000100.xml")
    text = document.text
    assert "Bonpland sah ich das Crocodil am Ufer" in text, "abbreviation expanded, hyphen joined"
    assert "falsch" not in text, "deletions are not part of the reading text"
    assert "Gemeint ist ein Kaiman" not in text, "editorial notes stay out of the text"
    assert "⟨vide Tafel⟩" in text, "Humboldt's marginal note is inline"
    assert "Ort | Höhe |\nDresden | 86 toises |" in text

    assert [page.label for page in document.pages] == ["1r", "1v"]
    assert text[document.pages[1].start :].startswith("Die Quina")
    assert [section.heading for section in document.sections] == ["Numero 1.", "Numero 2."]

    for mention in document.mentions:
        if mention.note_index < 0:
            assert text[mention.start : mention.end] == mention.surface
    by_surface = {mention.surface: mention for mention in document.mentions}
    assert by_surface["Bonpland"].entity_id == "H0000010"
    assert by_surface["Königstein"].authority["geonames"] == "111"
    assert by_surface["Quina"].key == "Cinchona officinalis"
    assert by_surface["Willdenow"].note_index == 0

    note = document.notes[0]
    assert note.kind == "editorial"
    assert text[note.anchor_start : note.anchor_end] == "das Crocodil"
    assert document.measures[0].quantity == 27 and document.measures[0].unit == "pouce"
    assert document.dates[0].date_from == "1797-06-14"
    assert (document.date_from, document.date_to) == ("1797-06-01", "1798-04-30")
    assert document.url == "https://edition-humboldt.de/v99/H0000100"
    assert document.citation.endswith("URL: https://edition-humboldt.de/v99/H0000100")
    assert sum(p.end - p.start for p in document.paragraphs) > 0.9 * len(text.replace("\n", ""))


def test_letter_metadata(store):
    letter = store.document("H0000200")
    assert letter["sender_id"] == "H0012069"
    assert letter["recipient_id"] == "H0000010"
    assert letter["origin_place"] == "Berlin"
    assert letter["date_from"] == "1828-09-01"


def test_search_is_diacritic_insensitive_and_ranks_full_matches_first(store):
    passages = store.search("konigstein barometer")
    assert passages[0].doc_id == "H0000100"
    assert passages[0].page == "1r"
    assert passages[0].url.endswith("/H0000100/1r")
    assert [note.text for note in passages[0].notes][0].startswith("Gemeint ist ein Kaiman")
    assert store.search("crocodil*", doc_types=["letter"])[0].doc_id == "H0000200"
    assert store.search("Kaiman")[0].kind == "editorial"
    assert store.search("Vesuv", date_from="1805") == []
    assert store.search("") == []


def test_query_terms_and_folding():
    assert fold("Königstein Orénoque") == "konigstein orenoque"
    assert [term for term, _, _ in query_terms('"Río Negro" kroko*')] == ['"rio negro"', '"kroko"*']


def test_grep_counts_and_guards(store):
    found = store.grep(r"\d+\s*toises")
    assert found["total_matches"] == 1
    assert found["documents"][0]["doc_id"] == "H0000100"
    with pytest.raises(StoreError):
        store.grep("(unclosed")


def test_reading_by_page_and_offset(store):
    start, end = store.page_span("H0000100", "1v")
    passage = store.passage("H0000100", start, end)
    assert passage.text.startswith("Die Quina")
    assert store.rendered(passage).startswith("⟦1v⟧ Die Quina")
    with pytest.raises(StoreError, match="no page"):
        store.page_span("H0000100", "9r")
    with pytest.raises(StoreError, match="Unknown doc_id"):
        store.document("H9999999")


def test_entities(store):
    bonpland = store.lookup_entities("bonpland")[0]
    assert bonpland["entity_id"] == "H0000010"
    assert bonpland["mention_count"] == 1, "the letter header is metadata, not a mention"
    assert store.lookup_entities("Aime Bonpland")[0]["entity_id"] == "H0000010"
    assert store.lookup_entities("Bonplandt")[0]["entity_id"] == "H0000010", "close spelling"
    konigstein = store.lookup_entities("Königstein", kind="place")[0]
    assert (konigstein["lat"], konigstein["lon"]) == (50.92, 14.07)
    plant = store.lookup_entities("Cinchona officinalis")[0]
    assert plant["kind"] == "plant" and plant["mention_count"] == 1


def test_entities_missing_from_the_register_are_rebuilt_from_annotations(store):
    loxa = store.entity("H0000099")
    assert loxa["name"] == "Loxa"
    assert loxa["in_register"] == 0
    assert loxa["geonames"] == "222"


def test_mentions_include_editorial_notes(store):
    found = store.mentions("H0000011")
    assert found["total_mentions"] == 1
    assert found["passages"][0].kind == "editorial"


def test_chronology_and_neighbours(store):
    assert [entry["doc_id"] for entry in store.chronology("1805-03")] == ["H0000300"]
    assert store.chronology("1805-04") == []
    assert [entry["doc_id"] for entry in store.chronology_neighbours("1805-04")] == [
        "H0000300",
        "H0000301",
    ]
    assert store.chronology("1805")[0]["places"][0]["name"] == "Berlin"


def test_sql_is_read_only_and_converts_units(store):
    rows = store.query("SELECT unit, si_value, si_unit FROM measurements ORDER BY unit")["rows"]
    assert rows == [["pouce", pytest.approx(73.089), "cm"], ["toise", pytest.approx(167.614), "m"]]
    assert store.query("SELECT sender, recipient FROM letters")["rows"] == [
        ["Humboldt, Alexander von", "Bonpland, Aimé"]
    ]
    for statement in (
        "DELETE FROM documents",
        "PRAGMA table_info(documents)",
        "ATTACH 'other.db' AS other",
        "SELECT load_extension('x')",
    ):
        with pytest.raises(StoreError):
            store.query(statement)
    assert store.meta["corpus_version"] == "99.0.0"
