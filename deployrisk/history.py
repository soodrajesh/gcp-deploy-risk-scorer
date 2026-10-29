"""Read a git repository's history into Changes with chronological history features and SZZ-lite labels.

Label (what the model predicts): a commit is "risky" if, within the next HORIZON_COMMITS commits and HORIZON_DAYS days, a later commit
whose message says fix/revert/hotfix/regression touches at least one of the same files. This is a proxy for "this change needed
follow-up work", not a measured incident; it is noisy by construction and the README says so."""

from __future__ import annotations

import bisect
import re
import subprocess
from dataclasses import dataclass

from .features import FIX_MSG
from .models import Change, FileChange

HORIZON_COMMITS = 30
HORIZON_DAYS = 14
SEP, FIELD = "\x1e", "\x1f"
RENAME = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")


@dataclass
class Row:
    change: Change
    label: int
    sha: str
    index: int


def _path(p: str) -> str:
    m = RENAME.match(p)
    if m:
        return (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
    return p.split(" => ")[-1]


def read_log(repo: str, max_commits: int | None = None) -> list[dict]:
    args = [
        "git",
        "-C",
        repo,
        "log",
        "--no-merges",
        "--reverse",
        "--numstat",
        "--summary",
        f"--format={SEP}%H{FIELD}%an{FIELD}%at{FIELD}%s",
    ]
    if max_commits:
        args.insert(4, f"-n{max_commits}")
    out = subprocess.run(args, capture_output=True, text=True, check=True, errors="replace").stdout
    commits = []
    for block in out.split(SEP)[1:]:
        lines = block.split("\n")
        sha, author, ts, subject = lines[0].split(FIELD)
        files: dict[str, FileChange] = {}
        for ln in lines[1:]:
            if "\t" in ln:
                a, d, p = ln.split("\t", 2)
                path = _path(p)
                files[path] = FileChange(
                    path=path,
                    added=int(a) if a.isdigit() else 0,
                    deleted=int(d) if d.isdigit() else 0,
                    binary=(a == "-"),
                )
            elif ln.startswith(" create mode") and ln.split(" ", 4)[-1]:
                files.setdefault(
                    ln.split(" ", 4)[-1], FileChange(path=ln.split(" ", 4)[-1])
                ).status = "A"
            elif ln.startswith(" delete mode"):
                p = ln.split(" ", 4)[-1]
                files.setdefault(p, FileChange(path=p)).status = "D"
            elif ln.startswith(" rename "):
                m = re.match(r" rename (.*) \(\d+%\)", ln)
                if m:
                    p = _path(m.group(1))
                    if p in files:
                        files[p].status = "R"
        if files:
            commits.append(
                {
                    "sha": sha,
                    "author": author,
                    "ts": int(ts),
                    "subject": subject,
                    "files": list(files.values()),
                }
            )
    return commits


def build_rows(commits: list[dict]) -> list[Row]:
    """Chronological pass: history features use only the past; labels look ahead and the unlabelled tail is dropped."""
    touches: dict[str, int] = {}
    fixes: dict[str, int] = {}
    by_author: dict[tuple[str, str], int] = {}
    fix_index: dict[str, list[int]] = {}
    is_fix = [bool(FIX_MSG.search(c["subject"])) for c in commits]
    for i, c in enumerate(commits):
        if is_fix[i]:
            for f in c["files"]:
                fix_index.setdefault(f.path, []).append(i)
    rows: list[Row] = []
    for i, c in enumerate(commits):
        paths = [f.path for f in c["files"]]
        pt = sum(touches.get(p, 0) for p in paths)
        pf = sum(fixes.get(p, 0) for p in paths)
        ap = sum(by_author.get((c["author"], p), 0) for p in paths)
        change = Change(
            files=c["files"],
            message=c["subject"],
            author=c["author"],
            timestamp=c["ts"],
            prior_touches=pt,
            prior_fixes=pf,
            author_prior_touches=ap,
            repo_commits=i,
        )
        if i + HORIZON_COMMITS < len(commits):
            label = 0
            for p in paths:
                idx = fix_index.get(p, [])
                k = bisect.bisect_right(idx, i)
                if (
                    k < len(idx)
                    and idx[k] - i <= HORIZON_COMMITS
                    and commits[idx[k]]["ts"] - c["ts"] <= HORIZON_DAYS * 86400
                ):
                    label = 1
                    break
            rows.append(Row(change, label, c["sha"], i))
        for p in paths:
            touches[p] = touches.get(p, 0) + 1
            by_author[(c["author"], p)] = by_author.get((c["author"], p), 0) + 1
            if is_fix[i]:
                fixes[p] = fixes.get(p, 0) + 1
    return rows


def change_at(commits: list[dict], idx: int) -> Change:
    """The Change for commit `idx` with history features from the commits before it only (no look-ahead)."""
    c = commits[idx]
    paths = {f.path for f in c["files"]}
    pt = pf = ap = 0
    for prev in commits[:idx]:
        shared = len(paths & {f.path for f in prev["files"]})
        if shared:
            pt += shared
            if FIX_MSG.search(prev["subject"]):
                pf += shared
            if prev["author"] == c["author"]:
                ap += shared
    return Change(
        files=c["files"],
        message=c["subject"],
        author=c["author"],
        timestamp=c["ts"],
        prior_touches=pt,
        prior_fixes=pf,
        author_prior_touches=ap,
        repo_commits=idx,
    )
