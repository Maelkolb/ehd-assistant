"""The Claude provider against the real Anthropic SDK and a simulated API.

The SDK serialises the request and parses the event stream exactly as in
production; only the HTTP transport is replaced.
"""

import json

import anthropic
import httpx2
import pytest

from alex.config import ModelSpec
from alex.llm.anthropic_provider import AnthropicConversation
from alex.llm.base import CitableText, ToolOutput, ToolSpec

SPEC = ModelSpec("claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", effort="medium")
SOURCE_URL = "https://edition-humboldt.de/v12/H0019734/55r#alex-source=S1"


def message_start() -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 120,
                "output_tokens": 1,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 30,
            },
        },
    }


def message_end(stop_reason: str) -> list[dict]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 40},
        },
        {"type": "message_stop"},
    ]


TOOL_TURN = [
    message_start(),
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "Erst im Tagebuch suchen."},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "signature_delta", "signature": "sig"},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"query": "König'},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": 'stein Barometer"}'},
    },
    {"type": "content_block_stop", "index": 1},
    *message_end("tool_use"),
]

ANSWER_TURN = [
    message_start(),
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": "", "citations": []},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "citations_delta",
            "citation": {
                "type": "search_result_location",
                "source": SOURCE_URL,
                "title": "Tagebuch",
                "cited_text": "am 14ten Junius 97 Höhenmessungen",
                "search_result_index": 0,
                "start_block_index": 0,
                "end_block_index": 0,
            },
        },
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Am 14. Juni 1797 maß Humboldt am Königstein."},
    },
    {"type": "content_block_stop", "index": 0},
    *message_end("end_turn"),
]


def sse(events: list[dict]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


@pytest.fixture
def api():
    """A fake Messages API: records request bodies, replays the two turns."""
    requests = []
    replies = [TOOL_TURN, ANSWER_TURN]

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            }
        )
        return httpx2.Response(
            200,
            content=sse(replies[len(requests) - 1]),
            headers={"content-type": "text/event-stream"},
        )

    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handle)),
    )
    return client, requests


def converse(client, fallbacks: bool) -> AnthropicConversation:
    tools = [
        ToolSpec(
            "search",
            "Search the edition.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        ToolSpec(
            "run_python",
            "Run code.",
            {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
            long_input=True,
        ),
    ]
    return AnthropicConversation(
        client,
        SPEC,
        "You are Alex.",
        "Interface language: German.",
        tools,
        [],
        "Was maß Humboldt am Königstein?",
        32000,
        fallbacks,
    )


def run_turn(conversation, allow_tools=True):
    events = []
    stream = conversation.stream(allow_tools)
    while True:
        try:
            events.append(next(stream))
        except StopIteration as finished:
            return events, finished.value


@pytest.mark.parametrize("fallbacks", [True, False])
def test_tool_round_and_cited_answer(api, fallbacks):
    client, requests = api
    conversation = converse(client, fallbacks)

    events, turn = run_turn(conversation)
    assert events == [{"type": "thinking", "text": "Erst im Tagebuch suchen."}]
    assert turn.stop == "tools"
    assert (turn.tool_calls[0].name, turn.tool_calls[0].input) == (
        "search",
        {"query": "Königstein Barometer"},
    )
    assert (turn.usage.input_tokens, turn.usage.cache_read_tokens) == (120, 900)

    body = requests[0]["body"]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["system"][1]["text"] == "Interface language: German."
    assert "tool_choice" not in body and "temperature" not in body
    assert [tool.get("eager_input_streaming") for tool in body["tools"]] == [None, True]
    if fallbacks:
        assert body["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in requests[0]["headers"]["anthropic-beta"]
    else:
        assert "fallbacks" not in body

    conversation.add_tool_outputs(
        [
            ToolOutput(
                "toolu_1",
                "search",
                "1 passage.",
                [
                    CitableText(
                        "S1",
                        "Tagebuch [H0019734, page 55r]",
                        "https://edition-humboldt.de/v12/H0019734/55r",
                        "⟦55r⟧ am 14ten Junius 97 Höhenmessungen",
                        ["[Editorial note by the editors] Der Königstein …"],
                    )
                ],
            )
        ]
    )
    events, turn = run_turn(conversation, allow_tools=False)
    assert [event["type"] for event in events] == ["block", "citation", "text"]
    assert events[1] == {
        "type": "citation",
        "source_id": "S1",
        "cited_text": "am 14ten Junius 97 Höhenmessungen",
        "in_note": False,
    }
    assert turn.stop == "end"
    assert turn.blocks[0].text == "Am 14. Juni 1797 maß Humboldt am Königstein."
    assert turn.blocks[0].citations[0].source_id == "S1"

    body = requests[1]["body"]
    assert body["tool_choice"] == {"type": "none"}
    assistant, results = body["messages"][1], body["messages"][2]
    assert [block["type"] for block in assistant["content"]] == ["thinking", "tool_use"]
    assert assistant["content"][0]["signature"] == "sig", "thinking goes back unchanged"
    tool_result = results["content"][0]
    assert tool_result["tool_use_id"] == "toolu_1"
    search_result = tool_result["content"][0]
    assert search_result["type"] == "search_result"
    assert search_result["source"] == SOURCE_URL
    assert search_result["citations"] == {"enabled": True}
    assert [part["text"][:9] for part in search_result["content"]] == ["⟦55r⟧ am ", "[Editoria"]
    hint = tool_result["content"][1]
    assert hint["type"] == "search_result" and "not a source" in hint["title"]
    assert hint["content"] == [{"type": "text", "text": "1 passage."}]
    assert len(results["content"]) == 1, "nothing follows the tool results as loose text"
    assert body["messages"][:1] == requests[0]["body"]["messages"], "the transcript only grows"
    assert body["system"] == requests[0]["body"]["system"]
    assert body["tools"] == requests[0]["body"]["tools"]


def test_harness_note_travels_as_a_system_message(api):
    client, _ = api
    conversation = converse(client, fallbacks=False)
    run_turn(conversation)
    conversation.add_tool_outputs(
        [ToolOutput("toolu_1", "search", "No matches.")], note="Answer now."
    )
    assert conversation.messages[-2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "No matches."}
    ]
    assert conversation.messages[-1] == {"role": "system", "content": "Answer now."}
