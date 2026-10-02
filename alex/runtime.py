"""Builds the long-lived objects of a running Alex from the settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alex.agent.loop import Agent
from alex.config import Settings, load_settings
from alex.corpus.store import DocumentStore
from alex.llm.base import Provider
from alex.sandbox.runner import Sandbox

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    store: DocumentStore
    sandbox: Sandbox
    agent: Agent


def providers_for(settings: Settings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    kinds = {spec.provider for spec in settings.models}
    if "anthropic" in kinds:
        from alex.llm.anthropic_provider import AnthropicProvider

        providers["anthropic"] = AnthropicProvider(
            settings.utility_model, settings.anthropic_fallbacks
        )
    if "local" in kinds:
        from alex.llm.openai_compatible import OpenAICompatibleProvider

        local = next(spec for spec in settings.models if spec.provider == "local")
        providers["local"] = OpenAICompatibleProvider(
            settings.local_base_url, settings.local_api_key, local.id
        )
    return providers


def create_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or load_settings()
    store = DocumentStore(settings.store_path)
    sandbox = Sandbox(
        settings.sandbox_mode,
        settings.store_path,
        settings.corpus_dir,
        settings.sandbox_url,
        settings.sandbox_token,
        settings.sandbox_timeout_seconds,
        settings.sandbox_memory_mb,
    )
    if not settings.models:
        logger.warning(
            "No model configured: set ANTHROPIC_API_KEY, or LOCAL_LLM_BASE_URL and LOCAL_LLM_MODEL."
        )
    agent = Agent(settings, store, sandbox, providers_for(settings))
    return Runtime(settings, store, sandbox, agent)
