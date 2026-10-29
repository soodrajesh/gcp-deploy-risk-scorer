"""CLI.  Exit codes for `score`: 0 low, 1 medium, 2 high, 3 critical, 4 error.

deployrisk score --repo . --rev HEAD            # full model: uses the repo's own history (hotspots, author familiarity)
git diff main... | deployrisk score --diff -    # diff-only model
deployrisk train repoA repoB ...                # retrain the bundled models from git histories (needs numpy)
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from .agents import add_note
from .history import read_log
from .llm import Budget, LLMError, make_llm
from .models import BANDS, Change, Score
from .score import load_model, score_change

COLOR = {"low": "32", "medium": "33", "high": "31", "critical": "1;31"}


def render(s: Score, color: bool) -> str:
    def c(code, t):
        return f"\033[{code}m{t}\033[0m" if color else t

    g = s.gates
    out = [
        c(
            COLOR[s.band],
            f"RISK: {s.band.upper()}  (score {s.score}/100, {round(s.probability * 100)}% chance of a follow-up fix, typical {round(s.baseline_probability * 100)}%)",
        ),
        s.band_reason,
        "",
    ]
    out += [
        f"  {'+' if f.contribution > 0 else '-'} {f.why} ({f.contribution:+.2f})" for f in s.factors
    ]
    out += [""] + [f"  FLAG {x.rule} [{x.band}] {x.title} {x.detail}" for x in s.flags]
    out += [
        "",
        f"Gates: canary {g.canary_percent}%, bake {g.bake_minutes} min, {g.reviewers} reviewer(s), window: {g.deploy_window}, rollback plan {'required' if g.rollback_plan_required else 'optional'}",
    ]
    out += [f"  - {n}" for n in g.notes]
    if s.note:
        out += ["", f"note: {s.note}"]
    return "\n".join(out + [f"! {d}" for d in s.degraded])


def change_from_repo(repo: str, rev: str) -> Change:
    from .history import change_at

    sha = subprocess.run(
        ["git", "-C", repo, "rev-parse", rev], capture_output=True, text=True, check=True
    ).stdout.strip()
    commits = read_log(repo)
    idx = next((i for i, c in enumerate(commits) if c["sha"] == sha), None)
    if idx is None:
        raise ValueError("commit not found in non-merge history")
    return change_at(commits, idx)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deployrisk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score", help="score a change")
    sc.add_argument("--repo", help="git repository (uses history features)")
    sc.add_argument("--rev", default="HEAD")
    sc.add_argument("--diff", help="unified diff file, or - for stdin (diff-only model)")
    sc.add_argument("--message", default="")
    sc.add_argument("--llm", default="none", choices=["none", "mock", "vertex", "openai"])
    sc.add_argument("--format", default="text", choices=["text", "json"])
    tr = sub.add_parser("train", help="retrain the bundled models from git histories")
    tr.add_argument("repos", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "train":
        from .train import main as train_main

        return train_main(["train", *a.repos])
    try:
        if a.diff:
            from . import features as F

            text = sys.stdin.read() if a.diff == "-" else Path(a.diff).read_text()
            change = Change(files=F.parse_unified_diff(text), message=a.message)
            s = score_change(change, load_model("diff_only"), "diff_only", diff_text=text)
        elif a.repo:
            change = change_from_repo(a.repo, a.rev)
            diff = subprocess.run(
                ["git", "-C", a.repo, "show", "--format=", a.rev],
                capture_output=True,
                text=True,
                errors="replace",
            ).stdout
            s = score_change(change, load_model("full"), "full", diff_text=diff)
        else:
            print("error: give --repo or --diff", file=sys.stderr)
            return 4
        s = asyncio.run(add_note(s, make_llm(a.llm), Budget()))
    except (OSError, ValueError, LLMError, subprocess.CalledProcessError) as e:
        print(f"error: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        return 4
    print(s.model_dump_json(indent=1) if a.format == "json" else render(s, sys.stdout.isatty()))
    return BANDS.index(s.band)


if __name__ == "__main__":
    sys.exit(main())
