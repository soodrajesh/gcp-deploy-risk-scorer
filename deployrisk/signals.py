"""Deterministic content signals from the ADDED lines of a unified diff. These produce flags (hard floors on the risk band)
that no model can lower: destructive migrations, widened network/IAM access, disabled safety settings, dependency major bumps."""

from __future__ import annotations

import re

from .models import Band, Flag

PATTERNS: list[tuple[str, Band, str, re.Pattern, str]] = [
    (
        "C001",
        "critical",
        "Destructive SQL in a migration",
        re.compile(
            r"\b(DROP\s+(TABLE|COLUMN|SCHEMA|DATABASE)|TRUNCATE\s+TABLE|DELETE\s+FROM\s+\w+\s*(;|$)|ALTER\s+TABLE\s+\w+\s+DROP)",
            re.I,
        ),
        "Take a verified backup first and ship the destructive step separately from the code that stops using the data.",
    ),
    (
        "C002",
        "high",
        "Network or IAM access widened to everyone",
        re.compile(
            r"(0\.0\.0\.0/0|\ballUsers\b|\ballAuthenticatedUsers\b|\"\*\"\s*,?\s*#?\s*principal|roles/(owner|editor)\b)"
        ),
        "Confirm the exposure is intended; prefer specific principals and CIDRs.",
    ),
    (
        "C003",
        "high",
        "A safety setting was disabled",
        re.compile(
            r"(deletion_protection\s*=\s*false|force_destroy\s*=\s*true|prevent_destroy\s*=\s*false|public_access_prevention\s*=\s*\"?inherited|enforce[_a-z]*\s*=\s*false|verify[_a-z]*\s*=\s*(false|False)|ssl[_a-z]*\s*=\s*(false|False))",
            re.I,
        ),
        "Re-enable it, or document why the protection is being removed.",
    ),
    (
        "C004",
        "high",
        "Credentials or private keys appear in the added lines",
        re.compile(
            r"(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}|sk_live_[0-9a-zA-Z]{20,})"
        ),
        "Remove them from the change and history, rotate the credential, and use Secret Manager.",
    ),
    (
        "C005",
        "medium",
        "Pipeline or deploy command changed in a way that can bypass checks",
        re.compile(
            r"(--no-verify|--force(?!-with-lease)\b|-auto-approve|continue-on-error:\s*true|skip[_-]?tests|--no-cpu-throttling)",
            re.I,
        ),
        "Review the pipeline change as carefully as production code.",
    ),
]
DEP_BUMP = re.compile(r"^[-+]\s*\"?([A-Za-z0-9_.\-]+)\"?\s*[=<>~^]{1,2}\s*v?(\d+)\.")


def added_lines(diff: str) -> list[tuple[str, str]]:
    """(file, line) for added lines."""
    out: list[tuple[str, str]] = []
    cur = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = line.rsplit(" b/", 1)[-1]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((cur, line[1:]))
    return out


def content_flags(diff: str) -> list[Flag]:
    flags: list[Flag] = []
    seen: set[str] = set()
    for f, line in added_lines(diff):
        for rule, band, title, rx, gate in PATTERNS:
            if rule in seen or not rx.search(line):
                continue
            if rule == "C001" and not re.search(r"\.sql$|migrat|schema|alembic", f, re.I):
                continue
            seen.add(rule)
            flags.append(Flag(rule=rule, band=band, title=title, detail=f"in {f}", gate=gate))
    olds: dict[str, int] = {}
    news: dict[str, int] = {}
    for line in diff.splitlines():
        m = DEP_BUMP.match(line)
        if m:
            (olds if line[0] == "-" else news)[m.group(1).lower()] = int(m.group(2))
    majors = [p for p, v in news.items() if p in olds and v > olds[p]]
    if majors:
        flags.append(
            Flag(
                rule="C006",
                band="medium",
                title=f"Major version bump of {len(majors)} dependenc{'y' if len(majors) == 1 else 'ies'}",
                detail=", ".join(sorted(majors)[:5]),
                gate="Read the upgrade notes, run the full test suite, and roll out gradually.",
            )
        )
    return flags
