"""Score a change: model probability -> band by rank among historical changes, plus deterministic flags that set a floor, plus gates."""

from __future__ import annotations

import bisect
import uuid
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from . import features as F
from . import signals
from .llm import LLM
from .model import Logit
from .models import BANDS, Change, Factor, Flag, Gates, Score

WHY = {
    "log_lines": "how many lines change",
    "log_files": "how many files change",
    "dirs_touched": "how spread out the change is across directories",
    "max_file_churn_log": "the biggest single-file change",
    "test_ratio": "the share of changed files that are tests",
    "docs_only": "the change touches only documentation",
    "infra_files": "infrastructure/config-as-code files changed",
    "iam_files": "auth/IAM/permission files changed",
    "migration_files": "database migration or schema files changed",
    "deps_files": "dependency manifests changed",
    "ci_files": "CI/CD files changed",
    "config_files": "configuration files changed",
    "new_file_ratio": "the share of brand-new files",
    "deleted_files": "files deleted",
    "binary_files": "binary files changed",
    "fix_message": "the message says fix/revert",
    "wip_message": "the message sounds provisional (wip, hack, quick...)",
    "msg_length_log": "how detailed the commit message is",
    "weekend": "deployed on a weekend",
    "friday_late": "deployed late on a Friday",
    "night": "deployed at night",
    "code_files_without_tests": "code changed without any test change",
    "log_prior_touches": "how often these files were changed before (hotspots)",
    "prior_fix_rate": "how often earlier changes to these files needed a fix",
    "author_familiarity": "how familiar the author is with these files",
}
BAND_GATES = {
    "low": Gates(
        canary_percent=100,
        bake_minutes=5,
        reviewers=1,
        deploy_window="any time",
        rollback_plan_required=False,
    ),
    "medium": Gates(
        canary_percent=25,
        bake_minutes=15,
        reviewers=1,
        deploy_window="business hours",
        rollback_plan_required=False,
    ),
    "high": Gates(
        canary_percent=10,
        bake_minutes=30,
        reviewers=2,
        deploy_window="Tuesday to Thursday, business hours",
        rollback_plan_required=True,
    ),
    "critical": Gates(
        canary_percent=5,
        bake_minutes=60,
        reviewers=2,
        deploy_window="a scheduled change window with the owner and on-call present",
        rollback_plan_required=True,
    ),
}


def load_model(variant: str, path: Path | None = None) -> Logit:
    p = path or Path(str(resources.files("deployrisk") / "data" / f"model_{variant}.json"))
    return Logit.load(p)


def band_for(model: Logit, p: float) -> tuple[str, int]:
    """Band by rank among the training changes: low below the median, medium to the 80th percentile, high to the 95th, critical above."""
    q = model.meta.get("quantiles")
    if not q:
        return "medium", 50
    rank = bisect.bisect_left(q, p) / len(q)
    return (
        "low" if rank < 0.5 else "medium" if rank < 0.8 else "high" if rank < 0.95 else "critical"
    ), int(round(rank * 100))


def path_flags(c: Change) -> list[Flag]:
    out: list[Flag] = []
    files = c.files
    if any(f.path.endswith(".tf") and f.status == "D" for f in files):
        out.append(
            Flag(
                rule="P001",
                band="high",
                title="Terraform files deleted",
                detail="removing infrastructure definitions can destroy resources on apply",
                gate="Review `terraform plan` (for example with gcp-plan-review-agents) before applying.",
            )
        )
    if any(F.INFRA_PAT.search(f.path) and F.IAM_PAT.search(f.path) for f in files):
        out.append(
            Flag(
                rule="P002",
                band="high",
                title="Infrastructure or IAM definitions changed",
                detail="paths look like IAM/policy infrastructure",
                gate="Require the infrastructure owner as a reviewer.",
            )
        )
    if any(F.MIGRATION_PAT.search(f.path) for f in files):
        out.append(
            Flag(
                rule="P003",
                band="medium",
                title="Database migration or schema files changed",
                gate="Make the migration backward compatible and reversible; deploy it before the code that needs it.",
            )
        )
    if any(F.CI_PAT.search(f.path) for f in files):
        out.append(
            Flag(
                rule="P004",
                band="medium",
                title="CI/CD configuration changed",
                gate="Review pipeline changes as production code.",
            )
        )
    code = [f for f in files if not F.TEST_PAT.search(f.path) and not F.DOC_PAT.search(f.path)]
    if any(F.IAM_PAT.search(f.path) and not F.INFRA_PAT.search(f.path) for f in code) and not any(
        F.TEST_PAT.search(f.path) for f in files
    ):
        out.append(
            Flag(
                rule="P005",
                band="high",
                title="Auth or permission code changed without a test change",
                gate="Add a test for the changed behavior.",
            )
        )
    lines = sum(f.added + f.deleted for f in files)
    if len(files) > 40 or lines > 1500:
        out.append(
            Flag(
                rule="P006",
                band="high" if lines <= 4000 else "critical",
                title="Very large change",
                detail=f"{len(files)} files, {lines} lines",
                gate="Split the change; large diffs are not reviewed well.",
            )
        )
    if sum(1 for f in files if f.status == "D") > 5:
        out.append(
            Flag(
                rule="P007",
                band="medium",
                title="Many files deleted",
                gate="Confirm nothing still imports or deploys them.",
            )
        )
    return out


def window_notes(c: Change) -> list[str]:
    from datetime import datetime

    if not c.timestamp:
        return []
    t = datetime.fromtimestamp(c.timestamp, UTC)
    notes = []
    if t.weekday() >= 5:
        notes.append("This is a weekend deploy: fewer people are around to respond.")
    elif t.weekday() == 4 and t.hour >= 15:
        notes.append("Late-Friday deploy: consider waiting until Monday unless it is a fix.")
    if t.hour >= 22 or t.hour < 5:
        notes.append("Night-time deploy: make sure on-call is aware.")
    return notes


def score_change(
    c: Change,
    model: Logit,
    variant: str,
    diff_text: str | None = None,
    traffic_tier: str = "normal",
) -> Score:
    f = F.features(c, history=variant == "full")
    p = model.predict(f)
    band, pct = band_for(model, p)
    contribs = sorted(model.contributions(f), key=lambda t: -abs(t[2]))[:6]
    factors = [
        Factor(name=n, value=round(v, 3), contribution=round(w, 3), why=WHY.get(n, n))
        for n, v, w in contribs
        if abs(w) > 0.01
    ]
    flags: list[Flag] = path_flags(c) + (signals.content_flags(diff_text) if diff_text else [])
    floor = max((BANDS.index(x.band) for x in flags), default=0)
    final = BANDS[max(BANDS.index(band), floor)]
    reason = f"model: riskier than {pct}% of historical changes ({band})" + (
        f"; raised to {final} by deterministic flag(s)" if final != band else ""
    )
    gates = BAND_GATES[final].model_copy(deep=True)
    if traffic_tier == "high":
        gates.bake_minutes = int(gates.bake_minutes * 1.5) + 5
        gates.notes.append("High-traffic service: bake time extended.")
    gates.notes += [x.gate for x in flags if x.gate] + window_notes(c)
    gates.notes = list(dict.fromkeys(gates.notes))
    return Score(
        id=uuid.uuid4().hex[:12],
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider="none",
        model="none",
        model_variant=variant,  # type: ignore[arg-type]
        probability=round(p, 4),
        baseline_probability=round(model.base_rate, 4),
        score=pct,
        band=final,
        band_reason=reason,
        factors=factors,
        flags=flags,
        gates=gates,  # type: ignore[arg-type]
        stats=F.stats(c),
        summary=f"{final.upper()} risk: {reason}. {round(p * 100)}% chance of needing a follow-up fix (typical: {round(model.base_rate * 100)}%).",
    )


async def with_llm(s: Score, llm: LLM) -> Score:
    from .agents import add_note

    return await add_note(s, llm)
