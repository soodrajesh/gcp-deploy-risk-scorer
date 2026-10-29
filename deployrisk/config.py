from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, default).split(",") if x.strip())


@dataclass
class Settings:
    provider: str = field(default_factory=lambda: os.environ.get("DEPLOYRISK_LLM", "mock"))
    model: str | None = field(default_factory=lambda: os.environ.get("DEPLOYRISK_MODEL") or None)
    allowed_providers: tuple[str, ...] = field(
        default_factory=lambda: _list("DEPLOYRISK_ALLOWED_PROVIDERS", "mock,none")
    )
    daily_budget_usd: float = field(
        default_factory=lambda: float(os.environ.get("DEPLOYRISK_DAILY_BUDGET_USD", 0.5))
    )
    max_diff_bytes: int = 2_000_000
    max_llm_calls: int = 3
    review_budget_usd: float = 0.02
