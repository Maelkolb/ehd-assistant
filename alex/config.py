"""Settings, read once from the environment (and a .env file in the working directory)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def load_env_file(path: Path) -> None:
    """Real environment variables win over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.split(" #")[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_flag(name: str, default: bool) -> bool:
    value = env(name).lower()
    return default if not value else value in ("1", "true", "yes", "on")


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    provider: str
    tier: str = "standard"
    effort: str = ""
    context_tokens: int = 1_000_000
    input_price: float = 0.0
    output_price: float = 0.0
    cache_read_price: float = 0.0
    cache_write_price: float = 0.0

    @property
    def compact(self) -> bool:
        """Small context windows get shorter tool results."""
        return self.context_tokens < 100_000


def anthropic_models(effort: str) -> list[ModelSpec]:
    return [
        ModelSpec(
            "claude-sonnet-5-5",
            "Claude Sonnet 5.5",
            "anthropic",
            "standard",
            effort,
            input_price=2.0,
            output_price=10.0,
            cache_read_price=0.20,
            cache_write_price=2.5,
        ),
        ModelSpec(
            "claude-opus-5-5",
            "Claude Opus 5.5",
            "anthropic",
            "premium",
            effort,
            input_price=4.0,
            output_price=20.0,
            cache_read_price=0.20,
            cache_write_price=5.0,
        ),
    ]


@dataclass
class Settings:
    data_dir: Path
    store_path: Path
    corpus_dir: Path
    coordinates_path: Path
    corpus_repository: str
    corpus_ref: str

    models: list[ModelSpec]
    default_model: str
    utility_model: str
    anthropic_fallbacks: bool
    local_base_url: str
    local_api_key: str

    max_rounds: int
    max_output_tokens: int
    suggestions: bool

    sandbox_mode: str
    sandbox_url: str
    sandbox_token: str
    sandbox_timeout_seconds: int
    sandbox_memory_mb: int

    rate_limit_per_hour: int
    rate_limit_premium_per_hour: int
    daily_request_budget: int
    max_concurrent_chats: int
    proxy_hops: int

    cors_origins: list[str]
    frame_ancestors: list[str]
    log_queries: bool
    feedback_log: str

    map_tile_url: str
    map_attribution: str
    links: dict[str, str] = field(default_factory=dict)

    def model(self, model_id: str | None) -> ModelSpec | None:
        for spec in self.models:
            if spec.id == (model_id or self.default_model):
                return spec
        return None

    @property
    def fallback_for_premium(self) -> ModelSpec | None:
        return next((spec for spec in self.models if spec.tier == "standard"), None)


def load_settings() -> Settings:
    load_env_file(Path.cwd() / ".env")
    data_dir = Path(env("DATA_DIR", "data"))

    effort = env("EFFORT", "medium").lower()
    if effort not in EFFORT_LEVELS:
        effort = "medium"
    models: list[ModelSpec] = []
    if env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"):
        allowed = env_list("ANTHROPIC_MODELS")
        models.extend(
            spec for spec in anthropic_models(effort) if not allowed or spec.id in allowed
        )
    if env("LOCAL_LLM_BASE_URL") and env("LOCAL_LLM_MODEL"):
        models.append(
            ModelSpec(
                env("LOCAL_LLM_MODEL"),
                env("LOCAL_LLM_LABEL") or env("LOCAL_LLM_MODEL"),
                "local",
                "standard",
                env("LOCAL_LLM_EFFORT"),
                context_tokens=env_int("LOCAL_LLM_CONTEXT_TOKENS", 32_000),
            )
        )
    default_model = env("DEFAULT_MODEL")
    if default_model not in {spec.id for spec in models}:
        default_model = models[0].id if models else ""

    return Settings(
        data_dir=data_dir,
        store_path=Path(env("STORE_PATH") or data_dir / "store" / "ehd.sqlite"),
        corpus_dir=Path(env("CORPUS_DIR") or data_dir / "ehd-source"),
        coordinates_path=Path(env("COORDINATES_PATH") or data_dir / "geo" / "coordinates.json"),
        corpus_repository=env(
            "CORPUS_REPOSITORY", "https://github.com/telota/edition-humboldt-digital.git"
        ),
        corpus_ref=env("CORPUS_REF", "main"),
        models=models,
        default_model=default_model,
        utility_model=env("UTILITY_MODEL", "claude-haiku-4-5"),
        anthropic_fallbacks=env_flag("ANTHROPIC_FALLBACKS", True),
        local_base_url=env("LOCAL_LLM_BASE_URL"),
        local_api_key=env("LOCAL_LLM_API_KEY", "not-needed"),
        max_rounds=env_int("MAX_ROUNDS", 14),
        max_output_tokens=env_int("MAX_OUTPUT_TOKENS", 32_000),
        suggestions=env_flag("SUGGESTIONS", True),
        sandbox_mode=env("SANDBOX_MODE", "subprocess").lower(),
        sandbox_url=env("SANDBOX_URL"),
        sandbox_token=env("SANDBOX_TOKEN"),
        sandbox_timeout_seconds=env_int("SANDBOX_TIMEOUT_SECONDS", 40),
        sandbox_memory_mb=env_int("SANDBOX_MEMORY_MB", 1024),
        rate_limit_per_hour=env_int("RATE_LIMIT_PER_HOUR", 15),
        rate_limit_premium_per_hour=env_int("RATE_LIMIT_PREMIUM_PER_HOUR", 3),
        daily_request_budget=env_int("DAILY_REQUEST_BUDGET", 500),
        max_concurrent_chats=env_int("MAX_CONCURRENT_CHATS", 8),
        proxy_hops=env_int("PROXY_HOPS", 0),
        cors_origins=env_list("CORS_ORIGINS"),
        frame_ancestors=env_list("FRAME_ANCESTORS", "'self'"),
        log_queries=env_flag("LOG_QUERIES", False),
        feedback_log=env("FEEDBACK_LOG"),
        map_tile_url=env("MAP_TILE_URL", "https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png"),
        map_attribution=env("MAP_ATTRIBUTION", "© OpenStreetMap-Mitwirkende"),
        links={
            "edition": env("LINK_EDITION", "https://edition-humboldt.de"),
            "imprint": env("LINK_IMPRINT"),
            "privacy": env("LINK_PRIVACY"),
        },
    )
