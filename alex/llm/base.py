"""What the agent loop needs from a language model, independent of the vendor.

A provider opens a Conversation; the loop alternates `stream()` (one model
turn, yielding display events and returning the finished Turn) with
`add_tool_outputs()`. Each provider keeps the transcript in its own wire
format.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Protocol

from alex.config import ModelSpec


class ProviderError(Exception):
    """A model call failed in a way the user should hear about."""

    def __init__(self, message: str, code: str = "model_error"):
        super().__init__(message)
        self.code = code


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    long_input: bool = False


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    invalid_input: str = ""


@dataclass
class CitableText:
    """One source as shown to the model: the passage plus attached notes."""

    source_id: str
    title: str
    url: str
    text: str
    notes: list[str] = field(default_factory=list)


@dataclass
class ToolOutput:
    call_id: str
    name: str
    text: str
    sources: list[CitableText] = field(default_factory=list)
    is_error: bool = False


@dataclass
class Citation:
    source_id: str
    cited_text: str = ""
    in_note: bool = False


@dataclass
class TextBlock:
    text: str
    citations: list[Citation] = field(default_factory=list)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def cost(self, spec: ModelSpec) -> float:
        return round(
            (
                self.input_tokens * spec.input_price
                + self.output_tokens * spec.output_price
                + self.cache_read_tokens * spec.cache_read_price
                + self.cache_write_tokens * spec.cache_write_price
            )
            / 1_000_000,
            4,
        )


@dataclass
class Turn:
    """stop is one of: end, tools, length, refusal, pause."""

    blocks: list[TextBlock]
    tool_calls: list[ToolCall]
    stop: str
    usage: Usage
    served_by: str = ""


class Conversation(Protocol):
    def stream(self, allow_tools: bool = True) -> Generator[dict, None, Turn]:
        """Yield {"type": "thinking" | "block" | "text" | "citation", ...}; return the Turn."""

    def add_tool_outputs(self, outputs: list[ToolOutput], note: str = "") -> None:
        """Append the results of the last turn's tool calls, optionally with a
        note from the harness to the model."""


class Provider(Protocol):
    native_citations: bool

    def conversation(
        self,
        spec: ModelSpec,
        system: str,
        context_note: str,
        tools: list[ToolSpec],
        history: list[dict],
        query: str,
        max_tokens: int,
    ) -> Conversation: ...

    def complete(self, system: str, prompt: str, max_tokens: int = 300) -> str:
        """A short single completion on the provider's inexpensive model."""
