"""Turn a change (a list of file changes + message + optional history) into numeric features.

Two feature sets: `diff_only` uses only what is in the diff and message (works anywhere, including the hosted API);
`full` adds repository history (how often these files were touched and fixed before, and by whom)."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from .models import Change, FileChange

TEST_PAT = re.compile(
    r"(^|/)(tests?|spec|__tests__|testing)(/|$)|(^|/)test_[^/]*|_test\.(py|go|js|ts)$|\.test\.(js|ts|tsx)$|\.spec\.(js|ts)$",
    re.I,
)
INFRA_PAT = re.compile(
    r"\.(tf|tfvars|hcl)$|(^|/)(terraform|infra|deploy|helm|k8s|kubernetes)/|(^|/)Dockerfile|docker-compose|\.ya?ml$",
    re.I,
)
IAM_PAT = re.compile(r"(iam|rbac|permission|policy|auth|oauth|acl|role)", re.I)
MIGRATION_PAT = re.compile(r"(^|/)(migrations?|alembic|db/migrate)/|\.sql$|schema", re.I)
DEPS_PAT = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|poetry\.lock|package(-lock)?\.json|yarn\.lock|go\.(mod|sum)|Cargo\.(toml|lock)|Gemfile(\.lock)?|pom\.xml|setup\.(py|cfg))$",
    re.I,
)
CI_PAT = re.compile(
    r"(^|/)(\.github/workflows|\.gitlab-ci|\.circleci|Jenkinsfile|cloudbuild)", re.I
)
CONFIG_PAT = re.compile(
    r"(^|/)(\.env|config|settings)[^/]*$|\.(ini|cfg|toml|conf|properties)$", re.I
)
DOC_PAT = re.compile(r"\.(md|rst|txt)$|(^|/)docs?/|(^|/)(LICENSE|CHANGELOG|README)", re.I)
GENERATED_PAT = re.compile(
    r"(\.lock$|-lock\.json$|\.min\.(js|css)$|(^|/)(dist|build|vendor|node_modules)/|\.snap$|_pb2\.py$)",
    re.I,
)
FIX_MSG = re.compile(
    r"\b(fix(e[sd])?|hotfix|bug(fix)?|revert(ed)?|regression|oops|typo|patch|broke|broken|repair)\b",
    re.I,
)
WIP_MSG = re.compile(r"\b(wip|tmp|temp|hack|quick|todo|experiment|try|attempt)\b", re.I)

DIFF_ONLY = [
    "log_lines",
    "log_files",
    "dirs_touched",
    "max_file_churn_log",
    "test_ratio",
    "docs_only",
    "infra_files",
    "iam_files",
    "migration_files",
    "deps_files",
    "ci_files",
    "config_files",
    "new_file_ratio",
    "deleted_files",
    "binary_files",
    "fix_message",
    "wip_message",
    "msg_length_log",
    "weekend",
    "friday_late",
    "night",
    "code_files_without_tests",
]
FULL = DIFF_ONLY + ["log_prior_touches", "prior_fix_rate", "author_familiarity"]


def _count(files: list[FileChange], pat: re.Pattern) -> int:
    return sum(1 for f in files if pat.search(f.path))


def _real(files: list[FileChange]) -> list[FileChange]:
    return [f for f in files if not GENERATED_PAT.search(f.path)] or files


def features(c: Change, history: bool = False) -> dict[str, float]:
    files = _real(c.files)
    n = max(len(files), 1)
    lines = sum(f.added + f.deleted for f in files)
    tests = _count(files, TEST_PAT)
    docs = _count(files, DOC_PAT)
    code = [f for f in files if not TEST_PAT.search(f.path) and not DOC_PAT.search(f.path)]
    dirs = {f.path.rsplit("/", 1)[0] if "/" in f.path else "." for f in files}
    ts = datetime.fromtimestamp(c.timestamp, UTC) if c.timestamp else None
    msg = c.message or ""
    out = {
        "log_lines": math.log1p(lines),
        "log_files": math.log1p(len(files)),
        "dirs_touched": math.log1p(len(dirs)),
        "max_file_churn_log": math.log1p(max((f.added + f.deleted for f in files), default=0)),
        "test_ratio": tests / n,
        "docs_only": 1.0 if docs == len(files) and files else 0.0,
        "infra_files": float(min(_count(files, INFRA_PAT), 5)),
        "iam_files": float(min(_count(files, IAM_PAT), 5)),
        "migration_files": float(min(_count(files, MIGRATION_PAT), 5)),
        "deps_files": float(min(_count(files, DEPS_PAT), 3)),
        "ci_files": float(min(_count(files, CI_PAT), 3)),
        "config_files": float(min(_count(files, CONFIG_PAT), 5)),
        "new_file_ratio": sum(1 for f in files if f.status == "A") / n,
        "deleted_files": float(min(sum(1 for f in files if f.status == "D"), 10)),
        "binary_files": float(min(sum(1 for f in files if f.binary), 5)),
        "fix_message": 1.0 if FIX_MSG.search(msg) else 0.0,
        "wip_message": 1.0 if WIP_MSG.search(msg) else 0.0,
        "msg_length_log": math.log1p(len(msg)),
        "weekend": 1.0 if ts and ts.weekday() >= 5 else 0.0,
        "friday_late": 1.0 if ts and ts.weekday() == 4 and ts.hour >= 15 else 0.0,
        "night": 1.0 if ts and (ts.hour >= 22 or ts.hour < 5) else 0.0,
        "code_files_without_tests": 1.0 if code and tests == 0 else 0.0,
    }
    if history:
        pt = c.prior_touches or 0
        out["log_prior_touches"] = math.log1p(pt)
        out["prior_fix_rate"] = (c.prior_fixes or 0) / pt if pt else 0.0
        out["author_familiarity"] = (c.author_prior_touches or 0) / pt if pt else 0.0
    return out


def stats(c: Change) -> dict[str, int | float]:
    files = _real(c.files)
    return {
        "files": len(c.files),
        "lines_added": sum(f.added for f in files),
        "lines_deleted": sum(f.deleted for f in files),
        "test_files": _count(files, TEST_PAT),
        "infra_files": _count(files, INFRA_PAT),
        "migration_files": _count(files, MIGRATION_PAT),
        "dependency_files": _count(files, DEPS_PAT),
        "ci_files": _count(files, CI_PAT),
    }


UNIFIED_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def parse_unified_diff(text: str) -> list[FileChange]:
    """Per-file added/deleted counts, new/deleted/renamed status and binary flags from a unified diff (`git diff`)."""
    files: list[FileChange] = []
    cur: FileChange | None = None
    for line in text.splitlines():
        m = UNIFIED_FILE.match(line)
        if m:
            cur = FileChange(path=m.group(2))
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "A"
        elif line.startswith("deleted file mode"):
            cur.status = "D"
        elif line.startswith("rename from"):
            cur.status = "R"
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith("+") and not line.startswith("+++"):
            cur.added += 1
        elif line.startswith("-") and not line.startswith("---"):
            cur.deleted += 1
    return files
