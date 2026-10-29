"""HTTP API and viewer. Auth is Cloud Run IAM. The diff is parsed for per-file counts and content flags and is never stored or sent to a model."""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from importlib import resources

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__
from . import features as F
from .agents import add_note
from .config import Settings
from .llm import Budget, LLMError, make_llm
from .models import Change, FileChange, Score
from .score import load_model, score_change


class ScoreRequest(BaseModel):
    diff: str | None = None
    files: list[FileChange] | None = None  # alternative to a diff: per-file counts
    message: str = ""
    author: str = ""
    timestamp: int | None = None
    traffic_tier: str = "normal"  # normal | high
    llm: str | None = None


class _Mem:
    def __init__(self, cap: int = 40):
        self._d: OrderedDict[str, Score] = OrderedDict()
        self._cap, self._lock, self._spend = cap, threading.Lock(), {}

    def save(self, r):
        with self._lock:
            self._d[r.id] = r
            while len(self._d) > self._cap:
                self._d.popitem(last=False)

    def recent(self, n):
        return list(reversed(self._d.values()))[:n]

    def get(self, i):
        return self._d.get(i)

    def spend(self):
        return self._spend.get(datetime.now(UTC).strftime("%Y%m%d"), 0.0)

    def add(self, usd):
        k = datetime.now(UTC).strftime("%Y%m%d")
        with self._lock:
            self._spend[k] = self._spend.get(k, 0.0) + usd


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg, mem = settings or Settings(), _Mem()
    model = load_model("diff_only")
    app = FastAPI(title="deployrisk", version=__version__)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (resources.files("deployrisk") / "ui.html").read_text()

    @app.get("/api/config")
    def config():
        return {
            "version": __version__,
            "provider": cfg.provider,
            "allowed_providers": cfg.allowed_providers,
            "daily_budget_usd": cfg.daily_budget_usd,
            "spend_today_usd": round(mem.spend(), 6),
        }

    @app.get("/api/model")
    def model_info():
        ev = resources.files("deployrisk") / "data" / "eval.json"
        return {
            "variant": "diff_only",
            "meta": {k: v for k, v in model.meta.items() if k != "quantiles"},
            "base_rate": model.base_rate,
            "weights": dict(zip(model.names, [round(w, 3) for w in model.weights], strict=True)),
            "evaluation": json.loads(ev.read_text())["diff_only"]["chronological"]["_pooled"]
            if ev.is_file()
            else None,
        }

    @app.post("/api/score", response_model=Score)
    async def create(req: Request):
        raw = await req.body()
        if len(raw) > cfg.max_diff_bytes:
            raise HTTPException(413, "diff too large")
        try:
            body = ScoreRequest.model_validate_json(raw)
        except ValueError as e:
            raise HTTPException(422, f"invalid request: {str(e)[:300]}") from e
        if (body.diff is None) == (body.files is None):
            raise HTTPException(422, "send exactly one of 'diff' or 'files'")
        files = body.files if body.files is not None else F.parse_unified_diff(body.diff or "")
        if not files:
            raise HTTPException(422, "no files found in the diff")
        provider = body.llm or cfg.provider
        if provider not in cfg.allowed_providers:
            raise HTTPException(
                403, f"provider '{provider}' not allowed here ({', '.join(cfg.allowed_providers)})"
            )
        notes = []
        if provider not in ("none", "mock") and mem.spend() >= cfg.daily_budget_usd:
            provider = "none"
            notes.append(
                f"daily model budget ${cfg.daily_budget_usd:.2f} reached; no model commentary"
            )
        try:
            llm = make_llm(provider, cfg.model if provider == cfg.provider else None)
        except LLMError as e:
            raise HTTPException(503, str(e)) from e
        change = Change(
            files=files,
            message=body.message[:500],
            author=body.author[:100],
            timestamp=body.timestamp,
        )
        s = score_change(
            change, model, "diff_only", diff_text=body.diff, traffic_tier=body.traffic_tier
        )
        s = await add_note(s, llm, Budget(cfg.max_llm_calls, cfg.review_budget_usd))
        s.degraded = notes + s.degraded
        mem.add(sum(t.cost_usd for t in s.trace))
        mem.save(s)
        return s

    @app.get("/api/scores", response_model=list[Score])
    def listing(limit: int = 20):
        return mem.recent(max(1, min(limit, 40)))

    @app.get("/api/scores/{sid}", response_model=Score)
    def get(sid: str):
        r = mem.get(sid)
        if r is None:
            raise HTTPException(404, "no such score")
        return r

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        logging.getLogger("deployrisk").error("unhandled error: %s", type(exc).__name__)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app


def app_factory() -> FastAPI:
    return create_app()
