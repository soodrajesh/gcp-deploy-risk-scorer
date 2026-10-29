"""Train and evaluate. Evaluation is chronological (train on the earliest 70% of each repo, test on the latest 30%) and
leave-one-repo-out (train on the other repos, test on a repo the model has never seen)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import features as F
from .history import build_rows, read_log
from .model import Logit, auc, brier, precision_at


def load(repos: list[str], max_commits: int | None = None) -> dict[str, list]:
    return {Path(r).name: build_rows(read_log(r, max_commits)) for r in repos}


def matrix(rows, names, history):
    return [F.features(r.change, history) for r in rows], [r.label for r in rows]


def evaluate(data: dict[str, list], variant: str) -> dict:
    names = F.FULL if variant == "full" else F.DIFF_ONLY
    hist = variant == "full"
    out: dict = {"variant": variant, "chronological": {}, "leave_one_repo_out": {}}
    all_sx, all_sy, all_base = [], [], []
    for repo, rows in data.items():
        cut = int(len(rows) * 0.7)
        tr, te = rows[:cut], rows[cut:]
        x, y = matrix(tr, names, hist)
        m = Logit(names).fit(x, y)
        xt, yt = matrix(te, names, hist)
        p = [m.predict(f) for f in xt]
        out["chronological"][repo] = _metrics(p, yt, m.base_rate, [f["log_lines"] for f in xt])
        all_sx += p
        all_sy += yt
        all_base += [m.base_rate] * len(yt)
    out["chronological"]["_pooled"] = _metrics(all_sx, all_sy, sum(all_base) / len(all_base), None)
    for held in data:
        train_rows = [r for k, v in data.items() if k != held for r in v]
        x, y = matrix(train_rows, names, hist)
        m = Logit(names).fit(x, y)
        xt, yt = matrix(data[held], names, hist)
        p = [m.predict(f) for f in xt]
        out["leave_one_repo_out"][held] = _metrics(p, yt, m.base_rate, [f["log_lines"] for f in xt])
    return out


def _metrics(p, y, base, lines):
    n, pos = len(y), sum(y)
    return {
        "n": n,
        "positives": pos,
        "base_rate": round(pos / n, 4),
        "auc": round(auc(p, y), 4),
        "precision_at_10pct": round(precision_at(p, y, 0.10), 4),
        "lift_at_10pct": round(precision_at(p, y, 0.10) / max(pos / n, 1e-9), 2),
        "brier": round(brier(p, y), 4),
        "brier_baseline": round(brier([base] * n, y), 4),
        "auc_lines_only": round(auc(lines, y), 4) if lines else None,
    }


def fit_final(data: dict[str, list], variant: str, repos: list[str]) -> Logit:
    names = F.FULL if variant == "full" else F.DIFF_ONLY
    rows = [r for v in data.values() for r in v]
    x, y = matrix(rows, names, variant == "full")
    m = Logit(names).fit(x, y)
    scores = sorted(m.predict(f) for f in x)
    step = max(1, len(scores) // 200)
    m.meta = {
        "quantiles": [
            round(v, 5) for v in scores[::step]
        ],  # rank bands: low < p50 < medium < p80 < high < p95 < critical
        "variant": variant,
        "trained_on": repos,
        "commits": len(rows),
        "positives": sum(y),
        "label": "followed by a fix/revert touching the same files within 30 commits and 14 days",
    }
    return m


def main(argv: list[str]) -> int:
    repos = argv[1:]
    data = load(repos)
    for k, v in data.items():
        print(k, len(v), "commits,", sum(r.label for r in v), "positive", flush=True)
    res = {v: evaluate(data, v) for v in ("diff_only", "full")}
    Path("deployrisk/data").mkdir(parents=True, exist_ok=True)
    Path("deployrisk/data/eval.json").write_text(json.dumps(res, indent=1))
    for v in ("diff_only", "full"):
        fit_final(data, v, [Path(r).name for r in repos]).save(
            Path(f"deployrisk/data/model_{v}.json")
        )
    print(json.dumps({v: res[v]["chronological"]["_pooled"] for v in res}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
