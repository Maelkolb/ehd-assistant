"""Any model behind an OpenAI-compatible chat completions endpoint.

This is how a locally hosted model plugs in: vLLM, Ollama, llama.cpp server,
TGI and LM Studio all speak this protocol. The model needs tool calling.
There are no native citations here; the model marks its sources as [S12] and
the markers are turned into citations afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Generator

import openai

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

MARKER_GROUP = re.compile(r"\s*((?:\[S\d+(?:\s*[,;–-]\s*S?\d+)*\]\s*)+)")
MARKER_PART = re.compile(r"S?(\d+)(?:\s*[–-]\s*S?(\d+))?")
MAX_RANGE = 12
THINK_OPEN, THINK_CLOSE = "<think>", "</think>"
REMOVED = "[This earlier tool result was removed to save space. Call the tool again if needed.]"
STOP_REASONS = {
    "stop": "end",
    "tool_calls": "tools",
    "function_call": "tools",
    "length": "length",
    "content_filter": "refusal",
}


def marker_ids(group: str) -> list[str]:
    """'[S3, S7]' and '[S2–S4]' as source ids, in order, without repeats."""
    ids: list[str] = []
    for first, last in MARKER_PART.findall(group):
        numbers = range(int(first), int(last) + 1) if last else [int(first)]
        ids.extend(f"S{number}" for number in list(numbers)[:MAX_RANGE])
    return list(dict.fromkeys(ids))


def blocks_from_markers(text: str) -> list[TextBlock]:
    """Split text at [S3] / [S3, S7] markers; each stretch of text before a
    marker group becomes a block cited by that group."""
    blocks: list[TextBlock] = []
    position = 0
    for match in MARKER_GROUP.finditer(text):
        ids = marker_ids(match.group(1))
        trailing_space = match.group(0)[len(match.group(0).rstrip()) :]
        blocks.append(
            TextBlock(text[position : match.start()], [Citation(source_id) for source_id in ids])
        )
        if trailing_space:
            blocks.append(TextBlock(trailing_space))
        position = match.end()
    if position < len(text):
        blocks.append(TextBlock(text[position:]))
    return [block for block in blocks if block.text or block.citations]


class ThinkTagFilter:
    """Routes <think>…</think> spans, which some servers leave in the content
    stream, to thinking events."""

    def __init__(self):
        self.thinking = False
        self.held = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.held += text
        events = []
        while self.held:
            tag = THINK_CLOSE if self.thinking else THINK_OPEN
            position = self.held.find(tag)
            kind = "thinking" if self.thinking else "text"
            if position >= 0:
                if position:
                    events.append((kind, self.held[:position]))
                self.held = self.held[position + len(tag) :]
                self.thinking = not self.thinking
                continue
            keep = next(
                (
                    length
                    for length in range(min(len(tag) - 1, len(self.held)), 0, -1)
                    if tag.startswith(self.held[-length:])
                ),
                0,
            )
            if len(self.held) > keep:
                events.append((kind, self.held[: len(self.held) - keep]))
            self.held = self.held[len(self.held) - keep :]
            break
        return events

    def flush(self) -> list[tuple[str, str]]:
        held, self.held = self.held, ""
        return [("thinking" if self.thinking else "text", held)] if held else []


class OpenAICompatibleConversation:
    def __init__(
        self,
        client: openai.OpenAI,
        spec: ModelSpec,
        system: str,
        context_note: str,
        tools: list[ToolSpec],
        history: list[dict],
        query: str,
        max_tokens: int,
        temperature: float | None,
        extra_body: dict | None,
    ):
        self.client = client
        self.spec = spec
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = extra_body
        prompt = system + ("\n\n" + context_note if context_note else "")
        self.messages: list[dict] = [
            {"role": "system", "content": prompt},
            *history,
            {"role": "user", "content": query},
        ]
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in tools
        ]

    def stream(self, allow_tools: bool = True) -> Generator[dict, None, Turn]:
        request = {
            "model": self.spec.id,
            "messages": self.messages,
            "stream": True,
            "max_tokens": self.max_tokens,
            "stream_options": {"include_usage": True},
        }
        if self.tools:
            request["tools"] = self.tools
            request["tool_choice"] = "auto" if allow_tools else "none"
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if self.extra_body:
            request["extra_body"] = self.extra_body
        if self.spec.effort:
            request["reasoning_effort"] = self.spec.effort
        self.fit_context()
        text_parts: list[str] = []
        pending: dict[int, dict] = {}
        finish = "stop"
        usage = Usage()
        tags = ThinkTagFilter()
        started = False

        def content_events(pieces: list[tuple[str, str]]):
            nonlocal started
            for kind, piece in pieces:
                if kind == "text":
                    if not started:
                        started = True
                        yield {"type": "block"}
                    text_parts.append(piece)
                yield {"type": kind, "text": piece}

        try:
            for chunk in self.client.chat.completions.create(**request):
                if chunk.usage:
                    usage = Usage(
                        chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0
                    )
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    yield {"type": "thinking", "text": reasoning}
                if delta.content:
                    yield from content_events(tags.feed(delta.content))
                for fragment in delta.tool_calls or []:
                    call = pending.setdefault(
                        fragment.index, {"id": "", "name": "", "arguments": ""}
                    )
                    call["id"] = fragment.id or call["id"]
                    if fragment.function:
                        call["name"] = fragment.function.name or call["name"]
                        call["arguments"] += fragment.function.arguments or ""
                if choice.finish_reason:
                    finish = choice.finish_reason
            yield from content_events(tags.flush())
        except openai.RateLimitError as error:
            raise ProviderError("The model is rate-limited right now.", "overloaded") from error
        except openai.APIConnectionError as error:
            raise ProviderError("The model service is unreachable.", "overloaded") from error
        except openai.APIStatusError as error:
            logger.error("Model endpoint error %s: %s", error.status_code, error)
            code = "overloaded" if error.status_code >= 500 else "model_error"
            raise ProviderError("The model service returned an error.", code) from error

        text = "".join(text_parts)
        calls = [pending[index] for index in sorted(pending)]
        tool_calls = []
        for position, call in enumerate(calls):
            call["id"] = call["id"] or f"call_{len(self.messages)}_{position}"
            try:
                arguments = json.loads(call["arguments"] or "{}")
                invalid = "" if isinstance(arguments, dict) else "arguments must be a JSON object"
            except ValueError as error:
                arguments, invalid = {}, f"invalid JSON ({error})"
            tool_calls.append(
                ToolCall(
                    call["id"],
                    call["name"],
                    arguments if isinstance(arguments, dict) else {},
                    invalid,
                )
            )
        assistant: dict = {"role": "assistant", "content": text or None}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
                }
                for call in calls
            ]
        self.messages.append(assistant)
        stop = "tools" if tool_calls else STOP_REASONS.get(finish, "end")
        return Turn(blocks_from_markers(text), tool_calls, stop, usage, self.spec.id)

    def fit_context(self) -> None:
        """Small context windows overflow after a few tool rounds: the oldest
        tool results are then replaced by a stub. About three characters per
        token is a safe estimate for these multilingual texts."""
        budget = (self.spec.context_tokens - self.max_tokens) * 3 * 0.9
        size = len(json.dumps(self.tools)) + sum(
            len(message.get("content") or "") + len(json.dumps(message.get("tool_calls") or ""))
            for message in self.messages
        )
        for message in self.messages:
            if size <= budget:
                break
            if message["role"] == "tool" and message["content"] != REMOVED:
                size -= len(message["content"]) - len(REMOVED)
                message["content"] = REMOVED

    def add_tool_outputs(self, outputs: list[ToolOutput], note: str = "") -> None:
        for output in outputs:
            parts = [output.text] if output.text else []
            for source in output.sources:
                notes = "".join(f"\n  {note_text}" for note_text in source.notes)
                parts.append(f"[{source.source_id}] {source.title}\n{source.text}{notes}")
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": output.call_id,
                    "content": "\n\n".join(parts) or "(no output)",
                }
            )
        if note:
            self.messages.append({"role": "user", "content": note})


class OpenAICompatibleProvider:
    native_citations = False

    def __init__(self, base_url: str, api_key: str, model: str):
        self.client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key or "not-needed",
            timeout=float(os.environ.get("LOCAL_LLM_TIMEOUT", "300")),
        )
        self.model = model
        temperature = os.environ.get("LOCAL_LLM_TEMPERATURE", "").strip()
        self.temperature = float(temperature) if temperature else None
        extra_body = os.environ.get("LOCAL_LLM_EXTRA_BODY", "").strip()
        self.extra_body = json.loads(extra_body) if extra_body else None

    def conversation(
        self,
        spec: ModelSpec,
        system: str,
        context_note: str,
        tools: list[ToolSpec],
        history: list[dict],
        query: str,
        max_tokens: int,
    ) -> OpenAICompatibleConversation:
        return OpenAICompatibleConversation(
            self.client,
            spec,
            system,
            context_note,
            tools,
            history,
            query,
            max_tokens,
            self.temperature,
            self.extra_body,
        )

    def complete(self, system: str, prompt: str, max_tokens: int = 300) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max(max_tokens, 2000),  # reasoning models think before they answer
            extra_body=self.extra_body,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content or ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
