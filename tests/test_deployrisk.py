import json
import math
import random

import pytest
from fastapi.testclient import TestClient

from deployrisk import agents, history, signals
from deployrisk import features as F
from deployrisk.api import create_app
from deployrisk.cli import main
from deployrisk.config import Settings
from deployrisk.llm import LLM, LLMError, LLMResult, MockLLM, NullLLM
from deployrisk.model import Logit, auc, brier, precision_at, sigmoid
from deployrisk.models import Change, FileChange
from deployrisk.score import band_for, load_model, path_flags, score_change


def fc(path, a=5, d=1, status="M", binary=False):
    return FileChange(path=path, added=a, deleted=d, status=status, binary=binary)


def change(files, msg="", ts=None, **kw):
    return Change(files=files, message=msg, timestamp=ts, **kw)


# --- diff parsing ---------------------------------------------------------------------------------------

DIFF = """diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 keep
-old
+new
+added
diff --git a/migrations/001.sql b/migrations/001.sql
new file mode 100644
--- /dev/null
+++ b/migrations/001.sql
@@ -0,0 +1,2 @@
+DROP TABLE users;
+CREATE TABLE t (id int);
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


def test_unified_diff_parsing():
    fs = {f.path: f for f in F.parse_unified_diff(DIFF)}
    assert (fs["src/app.py"].added, fs["src/app.py"].deleted) == (2, 1)
    assert (
        fs["migrations/001.sql"].status == "A"
        and fs["old.txt"].status == "D"
        and fs["img.png"].binary
    )
    assert F.parse_unified_diff("not a diff") == []


# --- features ---------------------------------------------------------------------------------------------


def test_features_reflect_the_change_and_never_use_the_future():
    c = change(
        [fc("src/a.py"), fc("tests/test_a.py"), fc("infra/main.tf"), fc("README.md")],
        "fix crash",
        ts=1793160000,
    )  # a Friday evening UTC
    f = F.features(c)
    assert (
        f["test_ratio"] == 0.25
        and f["infra_files"] >= 1
        and f["fix_message"] == 1.0
        and f["code_files_without_tests"] == 0.0
    )
    assert "log_prior_touches" not in f and set(f) == set(F.DIFF_ONLY)
    h = F.features(
        change([fc("a.py")], prior_touches=10, prior_fixes=4, author_prior_touches=2), history=True
    )
    assert math.isclose(h["prior_fix_rate"], 0.4) and math.isclose(h["author_familiarity"], 0.2)


def test_docs_only_and_generated_files():
    assert F.features(change([fc("README.md"), fc("docs/x.md")]))["docs_only"] == 1.0
    lock = F.features(change([fc("src/a.py", 3, 1), fc("poetry.lock", 5000, 4000)]))
    assert lock["log_lines"] < math.log1p(500)  # a huge lockfile does not dominate


def test_time_features():
    sat = F.features(change([fc("a.py")], ts=1793000000 + 86400 * 2))
    assert sat["weekend"] in (0.0, 1.0)
    fri_late = int(
        __import__("datetime")
        .datetime(2026, 10, 30, 17, 0, tzinfo=__import__("datetime").timezone.utc)
        .timestamp()
    )
    sun = int(
        __import__("datetime")
        .datetime(2026, 11, 1, 12, 0, tzinfo=__import__("datetime").timezone.utc)
        .timestamp()
    )
    assert F.features(change([fc("a.py")], ts=fri_late))["friday_late"] == 1.0
    assert F.features(change([fc("a.py")], ts=sun))["weekend"] == 1.0


# --- history and labels ----------------------------------------------------------------------------------


def commits(spec):
    out = []
    for i, (files, subject) in enumerate(spec):
        out.append(
            {
                "sha": f"s{i}",
                "author": "a" if i % 2 == 0 else "b",
                "ts": 1_700_000_000 + i * 3600,
                "subject": subject,
                "files": [fc(f) for f in files],
            }
        )
    return out


def test_label_is_a_later_fix_touching_the_same_files_within_the_horizon():
    cs = commits(
        [(["a.py"], "feat: add a"), (["b.py"], "feat: add b"), (["a.py"], "fix: a crashed")]
        + [([f"z{i}.py"], "chore") for i in range(40)]
    )
    rows = {r.sha: r for r in history.build_rows(cs)}
    assert rows["s0"].label == 1  # s2 fixes a.py soon after
    assert rows["s1"].label == 0  # nothing fixes b.py
    assert rows["s2"].label == 0


def test_a_fix_outside_the_horizon_is_not_a_label():
    spec = (
        [(["a.py"], "feat")]
        + [([f"z{i}.py"], "chore") for i in range(history.HORIZON_COMMITS + 5)]
        + [(["a.py"], "fix: late")]
        + [([f"y{i}.py"], "chore") for i in range(35)]
    )
    assert history.build_rows(commits(spec))[0].label == 0


def test_history_features_use_only_the_past_and_the_tail_is_unlabelled():
    cs = commits(
        [(["a.py"], "feat"), (["a.py"], "feat"), (["a.py"], "fix: x")]
        + [([f"z{i}.py"], "c") for i in range(35)]
    )
    rows = history.build_rows(cs)
    assert (
        rows[0].change.prior_touches == 0
        and rows[1].change.prior_touches == 1
        and rows[2].change.prior_touches == 2
        and rows[2].change.prior_fixes == 0
    )
    assert len(rows) == len(cs) - history.HORIZON_COMMITS
    ch = history.change_at(cs, 2)
    # commits 0 and 1 touched a.py; only commit 0 is by the same author as commit 2
    assert ch.prior_touches == 2 and ch.author_prior_touches == 1


def test_renames_are_normalised():
    assert history._path("src/{old => new}/f.py") == "src/new/f.py"
    assert history._path("a.py => b.py") == "b.py"


# --- model ---------------------------------------------------------------------------------------------------


def synthetic(n=600, seed=1):
    rng = random.Random(seed)
    rows, ys = [], []
    for _ in range(n):
        x = rng.random()
        rows.append({"log_lines": x * 5, "noise": rng.random()})
        ys.append(1 if rng.random() < 0.1 + 0.6 * x else 0)
    return rows, ys


def test_logit_learns_a_signal_and_stays_calibrated():
    rows, ys = synthetic()
    m = Logit(["log_lines", "noise"]).fit(rows, ys)
    p = [m.predict(r) for r in rows]
    assert abs(sum(p) / len(p) - sum(ys) / len(ys)) < 0.01  # mean prediction equals the base rate
    assert auc(p, ys) > 0.68 and abs(m.weights[0]) > 4 * abs(m.weights[1])


def test_numpy_and_pure_python_training_agree(monkeypatch):
    rows, ys = synthetic(300)
    a = Logit(["log_lines", "noise"]).fit(rows, ys, epochs=150)
    import builtins

    real = builtins.__import__
    monkeypatch.setattr(
        builtins,
        "__import__",
        lambda n, *x, **k: (
            (_ for _ in ()).throw(ImportError()) if n == "numpy" else real(n, *x, **k)
        ),
    )
    b = Logit(["log_lines", "noise"]).fit(rows, ys, epochs=150)
    assert all(abs(x - y) < 0.05 for x, y in zip(a.weights, b.weights, strict=True))


def test_metrics():
    assert (
        auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
        and auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
        and auc([0.5, 0.5], [0, 1]) == 0.5
    )
    assert math.isnan(auc([0.1, 0.2], [1, 1]))
    assert (
        brier([1.0, 0.0], [1, 0]) == 0.0
        and precision_at([0.9, 0.1, 0.8, 0.2], [1, 0, 0, 0], 0.25) == 1.0
    )
    assert sigmoid(0) == 0.5 and 0 <= sigmoid(-800) >= 0 and sigmoid(800) == 1.0


def test_model_save_load_roundtrip(tmp_path):
    rows, ys = synthetic(200)
    m = Logit(["log_lines", "noise"]).fit(rows, ys)
    m.save(tmp_path / "m.json")
    m2 = Logit.load(tmp_path / "m.json")
    assert m2.predict(rows[0]) == pytest.approx(m.predict(rows[0]))


# --- flags and scoring ----------------------------------------------------------------------------------------


def test_content_flags_come_from_added_lines_only():
    fl = {f.rule for f in signals.content_flags(DIFF)}
    assert "C001" in fl  # DROP TABLE in a migration
    removed = "diff --git a/m.sql b/m.sql\n--- a/m.sql\n+++ b/m.sql\n@@ -1 +1 @@\n-DROP TABLE old;\n+SELECT 1;\n"
    assert "C001" not in {f.rule for f in signals.content_flags(removed)}
    wide = 'diff --git a/main.tf b/main.tf\n+++ b/main.tf\n@@\n+  source_ranges = ["0.0.0.0/0"]\n+  deletion_protection = false\n'
    assert {"C002", "C003"} <= {f.rule for f in signals.content_flags(wide)}
    sql_in_python = (
        "diff --git a/app.py b/app.py\n+++ b/app.py\n@@\n+cursor.execute('DROP TABLE x')\n"
    )
    assert "C001" not in {
        f.rule for f in signals.content_flags(sql_in_python)
    }  # only migration/schema files count


def test_dependency_major_bumps_and_credentials():
    d = "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n@@\n-flask==2.3.1\n+flask==3.0.0\n-requests==2.31.0\n+requests==2.32.0\n"
    fl = [f for f in signals.content_flags(d) if f.rule == "C006"]
    assert fl and "flask" in fl[0].detail and "requests" not in fl[0].detail
    key = "diff --git a/c.py b/c.py\n+++ b/c.py\n@@\n+K='AKIA" + "QWERTYUIOPASDFGH'\n"
    assert "C004" in {f.rule for f in signals.content_flags(key)}


def test_path_flags():
    r = {
        f.rule
        for f in path_flags(
            change(
                [
                    fc("infra/iam.tf"),
                    fc("main.tf", 0, 30, "D"),
                    fc("db/migrations/0002.sql"),
                    fc(".github/workflows/ci.yml"),
                ]
            )
        )
    }
    assert {"P001", "P002", "P003", "P004"} <= r
    assert "P005" in {f.rule for f in path_flags(change([fc("src/auth/policy.py")]))}
    assert "P005" not in {
        f.rule for f in path_flags(change([fc("src/auth/policy.py"), fc("tests/test_policy.py")]))
    }
    assert "P006" in {
        f.rule for f in path_flags(change([fc(f"f{i}.py", 100, 0) for i in range(50)]))
    }


@pytest.fixture(scope="module")
def model():
    return load_model("diff_only")


def test_a_flag_sets_a_floor_the_model_cannot_lower(model):
    docs_with_drop = change([fc("db/migrations/9.sql", 2, 0), fc("README.md")], "docs: tweak")
    s = score_change(
        docs_with_drop,
        model,
        "diff_only",
        diff_text="diff --git a/db/migrations/9.sql b/db/migrations/9.sql\n+++ b/db/migrations/9.sql\n@@\n+DROP TABLE users;\n",
    )
    assert (
        s.band == "critical"
        and "raised to critical" in s.band_reason
        and s.gates.rollback_plan_required
        and s.gates.reviewers == 2
    )


def test_bands_are_by_rank_and_gates_tighten_with_the_band(model):
    low = score_change(change([fc("README.md", 2, 1)], "docs: typo"), model, "diff_only")
    big = score_change(
        change(
            [fc("infra/iam.tf", 80, 10)] + [fc(f"src/m{i}.py", 60, 20) for i in range(30)],
            "refactor",
        ),
        model,
        "diff_only",
    )
    assert low.band == "low" and big.band in ("high", "critical")
    assert (
        big.gates.canary_percent < low.gates.canary_percent
        and big.gates.bake_minutes > low.gates.bake_minutes
    )
    assert 0 <= low.score < big.score <= 100 and 0 < low.probability < 1
    assert band_for(model, 0.0)[0] == "low" and band_for(model, 1.0)[0] == "critical"


def test_factors_explain_the_score_and_traffic_extends_bake_time(model):
    s = score_change(change([fc("README.md", 2, 1)]), model, "diff_only")
    assert (
        s.factors
        and all(f.why for f in s.factors)
        and s.factors == sorted(s.factors, key=lambda f: -abs(f.contribution))
    )
    hot = score_change(change([fc("src/a.py")]), model, "diff_only", traffic_tier="high")
    base = score_change(change([fc("src/a.py")]), model, "diff_only")
    assert hot.gates.bake_minutes > base.gates.bake_minutes


def test_weekend_and_friday_notes(model):
    import datetime as dt

    sat = int(dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.UTC).timestamp())
    assert any(
        "weekend" in n
        for n in score_change(change([fc("a.py")], ts=sat), model, "diff_only").gates.notes
    )


# --- the model-written note is validated ------------------------------------------------------------------------


class Scripted(LLM):
    name, model = "scripted", "s-1"

    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises

    async def complete_json(self, system, user, max_tokens=1200):
        if self.raises:
            raise self.raises
        return LLMResult(
            self.reply if isinstance(self.reply, str) else json.dumps(self.reply), 10, 5, 0.0001
        )


async def test_note_validation(model):
    base = lambda: score_change(change([fc("README.md", 2, 1)]), model, "diff_only")  # noqa: E731
    ok = await agents.add_note(base(), MockLLM())
    assert ok.note and ok.band == base().band
    for bad, why in (
        ("see https://evil.example", "link"),
        ("It is a 97% risk", "number"),
        ("You can ignore the gates here", "argue"),
    ):
        s = await agents.add_note(base(), Scripted({"note": bad}))
        assert not s.note and any(why in d["reason"] for d in s.dropped), bad
    assert (await agents.add_note(base(), Scripted(raises=LLMError("429")))).degraded
    assert (await agents.add_note(base(), Scripted("not json"))).degraded
    assert (await agents.add_note(base(), NullLLM())).trace[-1].status == "skipped"


async def test_the_model_never_receives_code_or_paths(model):
    seen = []

    class Spy(Scripted):
        async def complete_json(self, system, user, max_tokens=1200):
            seen.append(user)
            return await super().complete_json(system, user, max_tokens)

    s = score_change(
        change([fc("secret_project/very_private_file.py", 9, 1)], "fix: internal codename zeta"),
        model,
        "diff_only",
    )
    await agents.add_note(s, Spy({"note": "ok"}))
    assert (
        seen
        and "very_private_file" not in seen[0]
        and "zeta" not in seen[0]
        and "secret_project" not in seen[0]
    )


# --- API and CLI --------------------------------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(create_app(Settings(provider="mock", allowed_providers=("mock", "none"))))


def test_api_flow_and_validation(client):
    assert client.get("/healthz").json()["ok"] and "Deploy Risk" in client.get("/").text
    m = client.get("/api/model").json()
    assert (
        m["variant"] == "diff_only" and m["evaluation"]["auc"] > 0.6 and "log_lines" in m["weights"]
    )
    r = client.post("/api/score", json={"diff": DIFF, "message": "refactor"})
    assert (
        r.status_code == 200
        and r.json()["band"] == "critical"
        and r.json()["model_variant"] == "diff_only"
    )
    sid = r.json()["id"]
    assert (
        client.get(f"/api/scores/{sid}").status_code == 200
        and len(client.get("/api/scores").json()) == 1
    )
    assert (
        client.post("/api/score", json={"files": [{"path": "README.md", "added": 2}]}).json()[
            "band"
        ]
        == "low"
    )
    assert client.post("/api/score", json={}).status_code == 422
    assert client.post("/api/score", json={"diff": "nothing"}).status_code == 422
    assert client.post("/api/score", json={"diff": DIFF, "files": []}).status_code == 422
    assert client.post("/api/score", json={"diff": DIFF, "llm": "openai"}).status_code == 403
    assert client.post("/api/score", content=b"x").status_code == 422
    assert client.get("/api/scores/none").status_code == 404


def test_api_body_limit():
    s = Settings(provider="none", allowed_providers=("none",))
    s.max_diff_bytes = 100
    assert TestClient(create_app(s)).post("/api/score", json={"diff": DIFF * 5}).status_code == 413


def test_cli_diff_and_exit_codes(tmp_path, capsys):
    p = tmp_path / "x.patch"
    p.write_text(DIFF)
    assert main(["score", "--diff", str(p)]) == 3  # critical: destructive migration
    out = capsys.readouterr().out
    assert "RISK: CRITICAL" in out and "Gates:" in out
    q = tmp_path / "doc.patch"
    q.write_text(
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@\n-a\n+b\n"
    )
    assert main(["score", "--diff", str(q), "--message", "docs: typo"]) == 0
    assert main(["score"]) == 4 and main(["score", "--diff", str(tmp_path / "missing")]) == 4
