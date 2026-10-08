"""The agent loop: the model researches with tools until it can answer.

`Agent.run()` is a generator of the events documented in docs/API.md.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Iterator

from alex.agent import prompts
from alex.agent.sources import SourceRegistry
from alex.agent.tools import ToolBox, ToolContext, failure
from alex.config import ModelSpec, Settings
from alex.corpus.store import DocumentStore
from alex.llm.base import Provider, ProviderError, TextBlock, ToolOutput, Usage
from alex.sandbox.runner import Sandbox

logger = logging.getLogger("alex.agent")

MAX_TOOL_CALLS = 60
REMARK_CHARS = 280
UNCITED_CHARS = 600
WANTS_VISUALISATION = re.compile(
    r"\b(\w*karte|map|zeitleiste|zeitstrahl|timeline|netz\w*|network|diagramm|chart|"
    r"graph|plot|visualisier\w*|visualis\w*|visualiz\w*|zeichnen?|draw|"
    r"kartier\w*|verorte\w*)\b",
    re.IGNORECASE,
)
REFUSAL = {
    "de": "Diese Anfrage kann Alex nicht bearbeiten.",
    "en": "Alex cannot help with this request.",
}


def event_input(arguments: dict) -> dict:
    """Tool input as shown in the activity feed: long strings are clipped."""

    def clipped(value):
        if isinstance(value, str) and len(value) > 6000:
            return value[:6000] + " …"
        if isinstance(value, list) and len(value) > 60:
            return value[:60]
        return value

    return {name: clipped(value) for name, value in arguments.items()}


class Agent:
    def __init__(
        self,
        settings: Settings,
        store: DocumentStore,
        sandbox: Sandbox,
        providers: dict[str, Provider],
    ):
        self.settings = settings
        self.store = store
        self.sandbox = sandbox
        self.providers = providers
        self.statistics = store.statistics()

    def run(
        self,
        query: str,
        spec: ModelSpec,
        history: list[dict] | None = None,
        wikipedia: bool = False,
        language: str = "de",
    ) -> Iterator[dict]:
        started = time.time()
        request_id = uuid.uuid4().hex[:12]
        provider = self.providers[spec.provider]
        wikipedia = wikipedia and self.settings.wikipedia
        toolbox = ToolBox(python=self.sandbox.enabled, wikipedia=wikipedia)
        context = ToolContext(
            store=self.store,
            sandbox=self.sandbox,
            registry=SourceRegistry(self.store, language),
            spec=spec,
            language=language,
        )
        system = prompts.system_prompt(
            self.statistics,
            self.settings.max_rounds,
            self.sandbox.enabled,
            self.settings.sandbox_timeout_seconds,
            provider.native_citations,
        )
        max_tokens = (
            min(self.settings.max_output_tokens, max(2000, spec.context_tokens // 4))
            if spec.compact
            else self.settings.max_output_tokens
        )
        conversation = provider.conversation(
            spec,
            system,
            prompts.context_note(language, wikipedia),
            toolbox.specs,
            history or [],
            query,
            max_tokens,
        )

        yield {
            "type": "start",
            "request_id": request_id,
            "model": {"id": spec.id, "label": spec.label},
        }

        usage = Usage()
        blocks: list[TextBlock] = []
        trace: list[str] = []
        reminded: set[str] = set()
        rounds = tool_calls = 0
        stop = "end"
        try:
            while True:
                allow_tools = rounds < self.settings.max_rounds and tool_calls < MAX_TOOL_CALLS
                turn = yield from self.relayed(conversation.stream(allow_tools), bool(blocks))
                usage.add(turn.usage)
                stop = turn.stop
                if turn.stop == "refusal":
                    yield {
                        "type": "error",
                        "code": "refused",
                        "message": REFUSAL.get(language, REFUSAL["en"]),
                    }
                    return
                written = "".join(block.text for block in turn.blocks).strip()
                calling = bool(turn.tool_calls) and allow_tools
                if calling and not blocks and 0 < len(written) < REMARK_CHARS:
                    # "Let me check the chronology." belongs to the research
                    # trail, not to the answer.
                    yield {"type": "reset"}
                    yield {"type": "thinking", "text": written}
                elif written:
                    if blocks:
                        blocks.append(TextBlock("\n\n"))
                    blocks.extend(turn.blocks)
                if turn.stop == "pause":
                    continue
                if not turn.tool_calls or not allow_tools:
                    reminder = self.reminder(query, blocks, context, reminded, allow_tools)
                    if reminder is None:
                        break
                    kind, note = reminder
                    reminded.add(kind)
                    trace.append(f"reminder:{kind}")
                    conversation.add_note(note)
                    if kind == "citations":
                        blocks = []
                        yield {"type": "reset"}
                    continue
                rounds += 1
                outputs = []
                for call in turn.tool_calls:
                    tool_calls += 1
                    yield {
                        "type": "tool_call",
                        "id": call.id,
                        "name": call.name,
                        "input": event_input(call.input),
                        "round": rounds,
                    }
                    call_started = time.time()
                    try:
                        result = toolbox.run(call, context)
                    except Exception:
                        logger.exception("Tool %s failed", call.name)
                        result = failure("The tool failed unexpectedly.")
                    for artifact in result.artifacts:
                        yield {"type": "artifact", "artifact": artifact}
                    yield {
                        "type": "tool_result",
                        "id": call.id,
                        "name": call.name,
                        "ok": not result.is_error,
                        "summary": result.summary,
                        "count": result.count,
                        "duration_ms": int((time.time() - call_started) * 1000),
                        "sources": [source.public() for source in result.new_sources],
                    }
                    trace.append(f"{call.name}{'!' if result.is_error else ''}")
                    outputs.append(
                        ToolOutput(call.id, call.name, result.text, result.sources, result.is_error)
                    )
                exhausted = rounds >= self.settings.max_rounds or tool_calls >= MAX_TOOL_CALLS
                conversation.add_tool_outputs(outputs, prompts.OUT_OF_ROUNDS if exhausted else "")
        except ProviderError as error:
            logger.error("request %s failed: %s", request_id, error)
            yield {"type": "error", "code": error.code, "message": str(error)}
            return

        answer = self.answer(
            blocks, context, spec, usage, rounds, tool_calls, stop, round(time.time() - started, 1)
        )
        yield {"type": "done", "answer": answer}
        self.log(request_id, query, spec, answer, trace)

        if self.settings.suggestions and answer["text"]:
            questions = self.suggestions(provider, query, answer["text"])
            if questions:
                yield {"type": "suggestions", "questions": questions}

    @staticmethod
    def reminder(
        query: str,
        blocks: list[TextBlock],
        context: ToolContext,
        reminded: set[str],
        allow_tools: bool,
    ) -> tuple[str, str] | None:
        """The model ended its turn. Once per answer, send it back when the
        user asked for a visualisation it did not create, or when a long
        researched answer carries no citation at all."""
        text = "".join(block.text for block in blocks).strip()
        if (
            "visualisation" not in reminded
            and allow_tools
            and not context.artifacts
            and WANTS_VISUALISATION.search(query)
        ):
            return "visualisation", prompts.VISUALISATION_REMINDER
        cited = any(block.citations for block in blocks)
        if (
            "citations" not in reminded
            and not cited
            and len(text) >= UNCITED_CHARS
            and context.registry.sources
        ):
            return "citations", prompts.CITATION_REMINDER
        return None

    @staticmethod
    def relayed(stream, after_earlier_text: bool):
        """Pass a turn's events on. Text that follows text from an earlier
        round starts a new paragraph, as it will in the final answer."""
        separated = not after_earlier_text
        while True:
            try:
                event = next(stream)
            except StopIteration as finished:
                return finished.value
            if not separated and event["type"] == "block":
                separated = True
                yield {"type": "block"}
                yield {"type": "text", "text": "\n\n"}
            yield event

    @staticmethod
    def answer(
        blocks: list[TextBlock],
        context: ToolContext,
        spec: ModelSpec,
        usage: Usage,
        rounds: int,
        tool_calls: int,
        stop: str,
        elapsed: float,
    ) -> dict:
        """Number the cited sources in reading order and drop citations of
        sources the model was never shown."""
        numbers: dict[str, int] = {}
        cited = []
        public_blocks = []
        for block in blocks:
            citations = []
            for citation in block.citations:
                source = context.registry.sources.get(citation.source_id)
                if source is None:
                    continue
                if source.id not in numbers:
                    numbers[source.id] = len(numbers) + 1
                    cited.append({**source.public(), "n": numbers[source.id]})
                entry = {
                    "n": numbers[source.id],
                    "source_id": source.id,
                    "cited_text": citation.cited_text[:800],
                    "in_note": citation.in_note,
                }
                if entry not in citations:
                    citations.append(entry)
            public_blocks.append({"text": block.text, "citations": citations})
        text = "".join(block.text for block in blocks).strip()
        return {
            "text": text,
            "blocks": public_blocks,
            "sources": cited,
            "artifacts": context.artifacts,
            "model": {"id": spec.id, "label": spec.label},
            "usage": {
                "rounds": rounds,
                "tool_calls": tool_calls,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cost_usd": usage.cost(spec),
            },
            "elapsed_s": elapsed,
            "stop_reason": stop,
        }

    def suggestions(self, provider: Provider, query: str, answer: str) -> list[str]:
        try:
            reply = provider.complete(
                prompts.SUGGESTIONS_SYSTEM,
                f"Question: {query}\n\nAnswer (shortened): {answer[:1500]}\n\n"
                "Three follow-up questions:",
            )
        except Exception as error:  # noqa: BLE001  suggestions are optional
            logger.warning("No follow-up suggestions: %s", error)
            return []
        questions = [line.strip().lstrip("-•*0123456789. ").strip() for line in reply.splitlines()]
        return [question for question in questions if len(question) > 8][:3]

    def log(
        self, request_id: str, query: str, spec: ModelSpec, answer: dict, trace: list[str]
    ) -> None:
        record = {
            "request": request_id,
            "model": spec.id,
            "tools": trace,
            "sources": len(answer["sources"]),
            "artifacts": len(answer["artifacts"]),
            "chars": len(answer["text"]),
            **answer["usage"],
            "elapsed_s": answer["elapsed_s"],
            "stop": answer["stop_reason"],
        }
        if self.settings.log_queries:
            record["query"] = query
        logger.info(json.dumps(record, ensure_ascii=False))
