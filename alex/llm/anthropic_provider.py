"""Claude through the Anthropic API.

Sources go to the model as search_result blocks with citations enabled, so
every cited sentence carries the exact quoted span. The transcript is
append-only: Claude Opus 5.5 and Sonnet 5.5 keep their thinking blocks valid
only while the system prompt, the tools and all earlier messages are unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator

import anthropic

from alex.config import ModelSpec
from alex.llm.base import (
    Citation,
    ProviderError,
    TextBlock,
    ToolCall,
    ToolOutput,
    ToolSpec,
    Turn,
    Usage,
)

logger = logging.getLogger(__name__)

SOURCE_MARK = "#alex-source="
SOURCE_ID = re.compile(re.escape(SOURCE_MARK) + r"([\w-]+)")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
STOP_REASONS = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tools",
    "max_tokens": "length",
    "refusal": "refusal",
    "pause_turn": "pause",
}


def source_id_of(citation) -> str:
    match = SOURCE_ID.search(getattr(citation, "source", "") or "")
    return match.group(1) if match else ""


def as_citation(citation) -> Citation | None:
    source_id = source_id_of(citation)
    if not source_id:
        return None
    return Citation(
        source_id,
        getattr(citation, "cited_text", "") or "",
        (getattr(citation, "start_block_index", 0) or 0) >= 1,
    )


class AnthropicConversation:
    def __init__(
        self,
        client: anthropic.Anthropic,
        spec: ModelSpec,
        system: str,
        context_note: str,
        tools: list[ToolSpec],
        history: list[dict],
        query: str,
        max_tokens: int,
        fallbacks: bool,
    ):
        self.client = client
        self.spec = spec
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self.system = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ]
        if context_note:
            self.system.append({"type": "text", "text": context_note})
        self.tools = [self.tool_definition(tool) for tool in tools]
        self.messages: list[dict] = [*history, {"role": "user", "content": query}]

    @staticmethod
    def tool_definition(tool: ToolSpec) -> dict:
        definition = {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        if tool.long_input:
            definition["eager_input_streaming"] = True
        return definition

    def request(self, allow_tools: bool) -> dict:
        request = {
            "model": self.spec.id,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": self.messages,
            "tools": self.tools,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "cache_control": {"type": "ephemeral"},
        }
        if self.spec.effort:
            request["output_config"] = {"effort": self.spec.effort}
        if not allow_tools:
            request["tool_choice"] = {"type": "none"}
        return request

    def open_stream(self, allow_tools: bool):
        request = self.request(allow_tools)
        if self.fallbacks:
            return self.client.beta.messages.stream(
                **request, betas=[FALLBACK_BETA], fallbacks="default"
            )
        return self.client.messages.stream(**request)

    def stream(self, allow_tools: bool = True) -> Generator[dict, None, Turn]:
        unreadable = 0
        while True:
            try:
                message = yield from self.stream_once(allow_tools)
                break
            except ValueError:
                # tool input the SDK could not parse at all (eager input streaming)
                unreadable += 1
                if unreadable > 1:
                    raise ProviderError("The model produced an unreadable tool call.") from None
                logger.warning("Unparseable tool input; re-issuing the turn")
                yield {"type": "reset"}
            except anthropic.BadRequestError as error:
                if self.fallbacks and "fallback" in str(error).lower():
                    logger.warning("Server-side fallbacks rejected (%s); continuing without", error)
                    self.fallbacks = False
                    continue
                logger.error("Anthropic rejected the request: %s", error)
                raise ProviderError("The model rejected the request.") from error
            except anthropic.RateLimitError as error:
                raise ProviderError("The model is rate-limited right now.", "overloaded") from error
            except anthropic.APIStatusError as error:
                code = "overloaded" if error.status_code >= 500 else "model_error"
                logger.error("Anthropic API error %s: %s", error.status_code, error)
                raise ProviderError("The model service returned an error.", code) from error
            except anthropic.APIConnectionError as error:
                raise ProviderError("The model service is unreachable.", "overloaded") from error

        self.messages.append({"role": "assistant", "content": message.content})
        blocks, tool_calls = [], []
        for block in message.content:
            if block.type == "text":
                citations = [
                    citation for citation in map(as_citation, block.citations or []) if citation
                ]
                blocks.append(TextBlock(block.text, citations))
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                tool_calls.append(ToolCall(block.id, block.name, arguments))
        usage = Usage(
            message.usage.input_tokens or 0,
            message.usage.output_tokens or 0,
            getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
        )
        stop = STOP_REASONS.get(message.stop_reason or "end_turn", "end")
        if stop == "length" and tool_calls:
            # a tool input cut off by max_tokens parses as a partial object; never run it
            for call in tool_calls:
                call.invalid_input = "truncated by the output limit"
            stop = "tools"
        return Turn(blocks, tool_calls, stop, usage, message.model)

    def stream_once(self, allow_tools: bool):
        with self.open_stream(allow_tools) as stream:
            for event in stream:
                if event.type == "content_block_start" and event.content_block.type == "text":
                    yield {"type": "block"}
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta" and delta.text:
                        yield {"type": "text", "text": delta.text}
                    elif delta.type == "thinking_delta" and delta.thinking:
                        yield {"type": "thinking", "text": delta.thinking}
                    elif delta.type == "citations_delta":
                        citation = as_citation(delta.citation)
                        if citation:
                            yield {
                                "type": "citation",
                                "source_id": citation.source_id,
                                "cited_text": citation.cited_text,
                                "in_note": citation.in_note,
                            }
            return stream.get_final_message()

    def add_tool_outputs(self, outputs: list[ToolOutput], note: str = "") -> None:
        """Citable sources travel as search_result blocks. A tool_result cannot
        mix block types, so each tool's plain-text part follows in one text
        block after the results."""
        content: list[dict] = []
        remarks: list[str] = []
        for output in outputs:
            result: dict = {"type": "tool_result", "tool_use_id": output.call_id}
            if output.is_error:
                result["is_error"] = True
            if output.sources and not output.is_error:
                result["content"] = [self.search_result(source) for source in output.sources]
                if output.text:
                    remarks.append(f"[{output.name}] {output.text}")
            else:
                result["content"] = output.text or "(no output)"
            content.append(result)
        trailing = "\n\n".join([*remarks, note] if note else remarks)
        if trailing:
            content.append({"type": "text", "text": trailing})
        self.messages.append({"role": "user", "content": content})

    @staticmethod
    def search_result(source) -> dict:
        texts = [source.text or "(empty)", *source.notes]
        return {
            "type": "search_result",
            "source": f"{source.url or 'https://edition-humboldt.de'}{SOURCE_MARK}{source.source_id}",
            "title": source.title,
            "content": [{"type": "text", "text": text} for text in texts],
            "citations": {"enabled": True},
        }


class AnthropicProvider:
    native_citations = True

    def __init__(self, utility_model: str, fallbacks: bool = True):
        self.client = anthropic.Anthropic()
        self.utility_model = utility_model
        self.fallbacks = fallbacks

    def conversation(
        self,
        spec: ModelSpec,
        system: str,
        context_note: str,
        tools: list[ToolSpec],
        history: list[dict],
        query: str,
        max_tokens: int,
    ) -> AnthropicConversation:
        return AnthropicConversation(
            self.client,
            spec,
            system,
            context_note,
            tools,
            history,
            query,
            max_tokens,
            self.fallbacks,
        )

    def complete(self, system: str, prompt: str, max_tokens: int = 300) -> str:
        message = self.client.messages.create(
            model=self.utility_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")
