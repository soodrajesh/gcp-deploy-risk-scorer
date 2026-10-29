from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Band = Literal["low", "medium", "high", "critical"]
BANDS = ["low", "medium", "high", "critical"]


class FileChange(BaseModel):
    path: str
    added: int = 0
    deleted: int = 0
    status: Literal["M", "A", "D", "R"] = "M"
    binary: bool = False


class Change(BaseModel):
    """What is about to be deployed, in the form both training and scoring use."""

    files: list[FileChange]
    message: str = ""
    author: str = ""
    timestamp: int | None = None  # unix seconds; used for time-of-week features
    # history features (only when a repo is available; None = unknown)
    prior_touches: int | None = None  # commits that previously touched these files
    prior_fixes: int | None = None  # of those, how many were fixes/reverts
    author_prior_touches: int | None = None  # how many of the prior touches were by this author
    repo_commits: int | None = None  # history length


class Factor(BaseModel):
    name: str
    value: float
    contribution: float  # change in log-odds relative to a typical change; positive raises risk
    why: str = ""


class Flag(BaseModel):
    rule: str
    band: Band
    title: str
    detail: str = ""
    gate: str = ""


class Gates(BaseModel):
    canary_percent: int
    bake_minutes: int
    reviewers: int
    deploy_window: str
    rollback_plan_required: bool
    notes: list[str] = Field(default_factory=list)


class TraceStep(BaseModel):
    agent: str
    action: str
    status: Literal["ok", "skipped", "error"] = "ok"
    start_ms: int = 0
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    note: str = ""


class Score(BaseModel):
    id: str
    created_at: str
    provider: str
    model: str
    model_variant: Literal["full", "diff_only"]
    probability: float  # calibrated chance that this change is followed by a fix or revert
    baseline_probability: float  # the same for an average change in the training data
    score: int  # 0-100
    band: Band
    band_reason: str
    factors: list[Factor]
    flags: list[Flag]
    gates: Gates
    stats: dict[str, int | float] = Field(default_factory=dict)
    summary: str
    note: str = ""
    dropped: list[dict[str, str]] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    duration_ms: int = 0
