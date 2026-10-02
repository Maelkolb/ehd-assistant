from conftest import TEST_MODEL, answer_turn, tool_turn

from alex.agent.sources import SourceRegistry
from alex.agent.tools import ToolBox, ToolContext
from alex.llm.base import ToolCall
from alex.llm.openai_compatible import ThinkTagFilter, blocks_from_markers


def context_for(store, sandbox, language="de"):
    return ToolContext(
        store=store,
        sandbox=sandbox,
        registry=SourceRegistry(store, language),
        spec=TEST_MODEL,
        language=language,
    )


def run_tool(store, sandbox, name, **arguments):
    context = context_for(store, sandbox)
    return ToolBox(python=True, wikipedia=False).run(
        ToolCall("c1", name, arguments), context
    ), context


def test_search_tool_returns_citable_passages(store, sandbox):
    result, context = run_tool(store, sandbox, "search", query="Königstein Barometer")
    assert not result.is_error
    source = result.new_sources[0]
    assert (source.id, source.kind, source.doc_id, source.label) == (
        "S1",
        "text",
        "H0000100",
        "Bl. 1r",
    )
    citable = result.sources[0]
    assert "H0000100, page 1r, chars" in citable.title
    assert citable.text.startswith("⟦1r⟧")
    assert citable.notes[0].startswith("[Editorial note by the editors on “das Crocodil”]")


def test_same_passage_is_one_source(store, sandbox):
    context = context_for(store, sandbox)
    box = ToolBox(python=True, wikipedia=False)
    first = box.run(ToolCall("c1", "search", {"query": "Königstein"}), context)
    second = box.run(ToolCall("c2", "search", {"query": "Barometer Königstein"}), context)
    assert first.sources[0].source_id == second.sources[0].source_id
    assert second.new_sources == []


def test_tool_input_is_validated_and_coerced(store, sandbox):
    result, _ = run_tool(store, sandbox, "search")
    assert result.is_error and "missing required query" in result.text
    result, _ = run_tool(store, sandbox, "search", query="Vesuv", limit="3", doc_types='["diary"]')
    assert not result.is_error and result.count == 1
    result, _ = run_tool(store, sandbox, "search", query="Vesuv", doc_types=["novel"])
    assert result.is_error
    result, _ = run_tool(store, sandbox, "SEARCH", query="Vesuv")
    assert not result.is_error, "a tool name that differs only in case is accepted"
    result, _ = run_tool(store, sandbox, "teleport")
    assert result.is_error and "Unknown tool" in result.text


def test_read_document_reports_how_to_continue(store, sandbox):
    result, _ = run_tool(store, sandbox, "read_document", doc_id="H0000100", max_chars=120)
    assert "read on with start=" in result.text
    assert "Pages: 1r, 1v" in result.text
    result, _ = run_tool(store, sandbox, "read_document", doc_id="H0000100", page_from="1v")
    assert result.sources[0].text.startswith("⟦1v⟧ Die Quina")
    result, _ = run_tool(store, sandbox, "read_document", doc_id="H0000100", page_from="7r")
    assert result.is_error


def test_query_data_is_a_citable_table(store, sandbox):
    result, context = run_tool(
        store, sandbox, "query_data", sql="SELECT doc_type, COUNT(*) AS n FROM documents GROUP BY 1"
    )
    source = result.new_sources[0]
    assert source.kind == "data"
    assert source.data["columns"] == ["doc_type", "n"]
    assert ["chronology", 2] in source.data["rows"]
    assert "chronology | 2" in result.sources[0].text
    result, _ = run_tool(store, sandbox, "query_data", sql="DROP TABLE documents")
    assert result.is_error


def test_chronology_tool_falls_back_to_neighbours(store, sandbox):
    result, _ = run_tool(store, sandbox, "chronology", date_from="1805-04")
    assert "nearest entries" in result.text
    assert len(result.sources) == 2


def test_map_resolves_places_from_the_register(store, sandbox):
    result, context = run_tool(
        store,
        sandbox,
        "create_map",
        title="Reise",
        connect_route=True,
        stations=[
            {"name": "Königstein"},
            {"entity_id": "H0000002"},
            {"name": "Atlantis"},
            {"label": "Rom", "lat": 41.9, "lon": 12.5},
        ],
    )
    artifact = context.artifacts[0]
    assert [station["label"] for station in artifact["stations"]] == ["Königstein", "Berlin", "Rom"]
    assert artifact["id"] == "a1" and artifact["connect_route"] is True
    assert "Atlantis" in result.text


def test_artifact_with_same_title_replaces_the_earlier_one(store, sandbox):
    context = context_for(store, sandbox)
    box = ToolBox(python=True, wikipedia=False)
    for label in ("Erster", "Zweiter"):
        box.run(
            ToolCall(
                "c",
                "create_timeline",
                {"title": "Verlauf", "events": [{"date": "1805", "label": label}]},
            ),
            context,
        )
    assert len(context.artifacts) == 1
    assert context.artifacts[0]["events"][0]["label"] == "Zweiter"


def test_network_from_cooccurrence_and_from_own_nodes(store, sandbox):
    result, context = run_tool(store, sandbox, "create_network", focus="Bonpland", min_shared=1)
    network = context.artifacts[0]
    assert network["nodes"][0]["focus"] is True
    assert {"Königstein"} <= {node["label"] for node in network["nodes"]}
    result, context = run_tool(
        store,
        sandbox,
        "create_network",
        title="Eigenes",
        nodes=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        edges=[{"source": "a", "target": "b"}, {"source": "a", "target": "x"}],
    )
    assert len(context.artifacts[0]["edges"]) == 1


def test_run_python_shows_outputs_and_is_citable(store, sandbox):
    code = (
        "frame = ehd.sql('SELECT doc_type, COUNT(*) AS n FROM documents GROUP BY 1')\n"
        "print(len(frame))\n"
        "figure, axes = plt.subplots()\n"
        "axes.bar(frame.doc_type, frame.n)\n"
        "ehd.show(figure, title='Dokumente')\n"
        "ehd.show(frame, title='Tabelle')"
    )
    result, context = run_tool(store, sandbox, "run_python", code=code)
    assert not result.is_error, result.text
    assert [artifact["type"] for artifact in context.artifacts] == ["image", "table"]
    assert result.new_sources[0].kind == "computation"
    assert result.new_sources[0].computation["output"].strip() == "3"
    assert "figure “Dokumente”" in result.text
    result, _ = run_tool(store, sandbox, "run_python", code="1/0")
    assert result.is_error and "ZeroDivisionError" in result.text


def test_loop_runs_tools_and_numbers_cited_sources(make_agent):
    agent, provider, _ = make_agent(
        [
            tool_turn(
                ("lookup_entity", {"name": "Bonpland"}),
                ("search", {"query": "Königstein Barometer"}),
            ),
            answer_turn("Humboldt maß am Königstein.", cited=["S2", "S99"]),
        ]
    )
    events = list(agent.run("Was maß Humboldt?", TEST_MODEL))
    kinds = [event["type"] for event in events]
    assert kinds[0] == "start" and kinds[-1] == "suggestions"
    assert kinds.count("tool_call") == 2 and kinds.count("tool_result") == 2
    answer = next(event for event in events if event["type"] == "done")["answer"]
    assert answer["text"] == "Humboldt maß am Königstein."
    assert [(source["id"], source["n"]) for source in answer["sources"]] == [("S2", 1)]
    assert answer["blocks"][0]["citations"] == [
        {"n": 1, "source_id": "S2", "cited_text": "", "in_note": False}
    ]
    assert answer["usage"]["rounds"] == 1 and answer["usage"]["tool_calls"] == 2
    assert [output.name for output in provider.outputs[0]] == ["lookup_entity", "search"]
    assert "Königstein" in provider.outputs[0][1].sources[0].text
    assert len(events[-1]["questions"]) == 3


def test_loop_stops_offering_tools_when_rounds_are_used_up(make_agent):
    endless = [tool_turn(("search", {"query": "Vesuv"}))] * 3
    agent, provider, _ = make_agent(
        [*endless, answer_turn("Genug.")], max_rounds=2, suggestions=False
    )
    events = list(agent.run("Vesuv?", TEST_MODEL))
    assert provider.allowed == [True, True, False]
    assert "used all your tool rounds" in provider.notes[1]
    assert events[-1]["type"] == "done"


def test_loop_reports_a_refusal(make_agent):
    from alex.llm.base import Turn, Usage

    agent, _, _ = make_agent([Turn([], [], "refusal", Usage())])
    events = list(agent.run("…", TEST_MODEL, language="en"))
    assert events[-1] == {
        "type": "error",
        "code": "refused",
        "message": "Alex cannot help with this request.",
    }


def test_system_prompt_states_the_corpus_and_the_citation_rule(make_agent):
    agent, provider, _ = make_agent([answer_turn("Hallo.")], suggestions=False)
    list(agent.run("Hallo", TEST_MODEL))
    assert "data release 99.0.0" in provider.system
    assert "`chronology`: 2 dated entries" in provider.system
    assert "Do not write source ids" in provider.system
    assert {"search", "read_document", "query_data", "run_python", "create_map"} <= {
        tool.name for tool in provider.tools
    }
    assert "wikipedia_context" not in {tool.name for tool in provider.tools}


def test_source_markers_become_citations():
    blocks = blocks_from_markers("Er war in Wien [S3]. Danach Salzburg [S4, S7] und Rom.")
    assert "".join(block.text for block in blocks) == "Er war in Wien. Danach Salzburg und Rom."
    assert [
        [citation.source_id for citation in block.citations] for block in blocks if block.citations
    ] == [["S3"], ["S4", "S7"]]
    assert blocks_from_markers("Ohne Beleg.")[0].citations == []


def test_think_tags_are_separated_from_the_answer():
    tags = ThinkTagFilter()
    events = [
        *tags.feed("<thi"),
        *tags.feed("nk>erst suchen</th"),
        *tags.feed("ink>Die Ant"),
        *tags.feed("wort"),
        *tags.flush(),
    ]
    assert "".join(text for kind, text in events if kind == "thinking") == "erst suchen"
    assert "".join(text for kind, text in events if kind == "text") == "Die Antwort"


def test_marker_ranges_and_bare_numbers():
    blocks = blocks_from_markers("Familie [S2–S4]. Zahlen [S1, 5].")
    cited = [
        [citation.source_id for citation in block.citations] for block in blocks if block.citations
    ]
    assert cited == [["S2", "S3", "S4"], ["S1", "S5"]]


def test_tool_summaries_follow_the_interface_language(store, sandbox):
    box = ToolBox(python=True, wikipedia=False)
    german = box.run(ToolCall("c1", "search", {"query": "Vesuv"}), context_for(store, sandbox))
    english = box.run(
        ToolCall("c1", "search", {"query": "Vesuv"}), context_for(store, sandbox, "en")
    )
    nothing = box.run(ToolCall("c1", "search", {"query": "Zeppelin"}), context_for(store, sandbox))
    assert (german.summary, english.summary, nothing.summary) == (
        "1 Passage",
        "1 passage",
        "nichts gefunden",
    )
    source = german.new_sources[0].public()
    assert list(source)[:4] == ["id", "kind", "title", "label"] and source["label"]


def test_short_remark_before_a_tool_call_goes_to_the_trail_not_the_answer(make_agent):
    from alex.llm.base import TextBlock, ToolCall, Turn, Usage

    remark = Turn(
        [TextBlock("Ich sehe in der Chronologie nach.")],
        [ToolCall("c1", "chronology", {"date_from": "1805"})],
        "tools",
        Usage(),
    )
    agent, _, _ = make_agent([remark, answer_turn("Er war in Berlin.")], suggestions=False)
    events = list(agent.run("Wo war Humboldt 1805?", TEST_MODEL))
    kinds = [event["type"] for event in events]
    assert kinds[:6] == ["start", "block", "text", "reset", "thinking", "tool_call"]
    assert events[-1]["answer"]["text"] == "Er war in Berlin."
