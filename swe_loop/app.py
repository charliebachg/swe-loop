"""FastAPI entry point: the intake endpoint, ticket views, and the dashboard."""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from swe_loop import connect, ops, pages, replay, report
from swe_loop import reduce as reduce_mod
from swe_loop.cli import run_once
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.intake import NormalizedEvent, normalize, ticket_id_for, verify_github_signature
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"


def build_app(
    settings: Settings | None = None, store: Store | None = None, *, seed_replay: bool = True
) -> FastAPI:
    settings = settings or Settings.from_env()
    cfg = TargetConfig.load(settings.config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.cfg = cfg
        app.state.store = store or Store(settings.db_path)
        app.state.client = DevinClient.from_settings(settings)
        app.state.run_lock = threading.Lock()
        app.state.run_thread = None
        if not settings.live and seed_replay:
            replay.seed(
                app.state.store,
                cfg,
                tickets_json=INVENTORY / "tickets.json",
                replay_dir=settings.replay_dir,
            )
        yield

    app = FastAPI(title="swe-loop", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "mode": settings.mode, "target": cfg.name}

    @app.post("/intake/{source}")
    async def intake(source: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        if source == "github":
            secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
            if not verify_github_signature(
                secret, body, request.headers.get("X-Hub-Signature-256")
            ):
                raise HTTPException(status_code=401, detail="bad signature")
        try:
            payload = await request.json()
        except Exception as ex:
            raise HTTPException(status_code=400, detail="body is not JSON") from ex
        st: Store = request.app.state.store
        eid = st.insert_event(source, payload)
        ev = normalize(source, payload, cfg)
        if ev is None:
            return {
                "event_id": eid,
                "accepted": False,
                "reason": "no adapter matched or filtered out",
            }
        tid = ingest(st, ev)
        st.conn.execute("UPDATE events SET ticket_id=? WHERE id=?", (tid, eid))
        st.log(
            "L0 intake",
            f"{source}:{ev.kind} {ev.action or ''}",
            ticket_id=tid,
            detail=ev.external_ref,
        )
        return {"event_id": eid, "accepted": True, "ticket_id": tid, "kind": ev.kind}

    @app.get("/tickets")
    def tickets(status: str | None = None) -> list[dict[str, Any]]:
        return app.state.store.list_tickets(status)

    @app.get("/tickets/{ticket_id}")
    def ticket(ticket_id: str) -> dict[str, Any]:
        st: Store = app.state.store
        t = st.get_ticket(ticket_id)
        if not t:
            raise HTTPException(status_code=404)
        t["work_orders"] = st.work_orders_for(ticket_id)
        return t

    def _render(request: Request, template: str, active: str, **ctx: Any) -> HTMLResponse:
        st: Store = request.app.state.store
        return TEMPLATES.TemplateResponse(
            request, template, {**pages.shell(settings, cfg, st, active), **ctx}
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return _render(request, "home.html", "home", h=pages.home(request.app.state.store))

    @app.get("/partials/home", response_class=HTMLResponse)
    def home_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "home_body.html", {"h": pages.home(request.app.state.store)}
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _render(
            request,
            "settings.html",
            "settings",
            s=pages.settings_page(settings, cfg, st, request.app.state.client),
        )

    @app.post("/settings/budget")
    async def settings_budget(request: Request) -> RedirectResponse:
        form = parse_qs((await request.body()).decode())
        try:
            cap = float(form.get("acu_cap", ["0"])[0])
            per = float(form.get("per_session_cap", ["0"])[0])
        except ValueError as ex:
            raise HTTPException(status_code=400, detail="numbers only") from ex
        if cap <= 0 or per <= 0:
            raise HTTPException(status_code=400, detail="caps must be positive")
        request.app.state.store.set_budget(cap, per)
        connect.clear_cache()
        return RedirectResponse("/settings", status_code=303)

    @app.get("/tickets-page", response_class=HTMLResponse)
    def tickets_page(request: Request) -> HTMLResponse:
        return _render(
            request, "tickets.html", "tickets", tk=pages.tickets(request.app.state.store)
        )

    @app.get("/tracker", response_class=HTMLResponse)
    def tracker_page(request: Request) -> HTMLResponse:
        return _render(
            request, "tracker.html", "tracker", tr=pages.tracker(request.app.state.store)
        )

    @app.get("/partials/tracker", response_class=HTMLResponse)
    def tracker_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "tracker_body.html", {"tr": pages.tracker(request.app.state.store)}
        )

    @app.post("/tickets/{ticket_id}/merge-form", response_class=HTMLResponse)
    async def merge_form(ticket_id: str, request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode())
        actor = (form.get("actor") or [""])[0].strip()
        st: Store = request.app.state.store
        if actor:
            try:
                reduce_mod.record_merge(st, ticket_id, actor)
            except ValueError:
                pass  # not ready: the re-rendered row says why
        return TEMPLATES.TemplateResponse(request, "tracker_body.html", {"tr": pages.tracker(st)})

    @app.get("/devin/sessions", response_class=HTMLResponse)
    def sessions_page(request: Request) -> HTMLResponse:
        return _render(
            request, "sessions.html", "sessions", ss=pages.sessions(request.app.state.store, cfg)
        )

    @app.get("/partials/sessions", response_class=HTMLResponse)
    def sessions_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "sessions_body.html", {"ss": pages.sessions(request.app.state.store, cfg)}
        )

    @app.get("/partials/session/{sid}", response_class=HTMLResponse)
    def session_drawer(sid: str, request: Request) -> HTMLResponse:
        d = ops.session_detail(request.app.state.store, sid)
        if not d:
            raise HTTPException(status_code=404)
        return TEMPLATES.TemplateResponse(request, "session_drawer.html", {"d": d})

    def _auto_ctx(request: Request) -> dict[str, Any]:
        st: Store = request.app.state.store
        running = request.app.state.run_lock.locked()
        return {"a": pages.automations(st, cfg, settings, request.app.state.client, running)}

    @app.get("/automations", response_class=HTMLResponse)
    def automations_page(request: Request) -> HTMLResponse:
        return _render(request, "automations.html", "automations", **_auto_ctx(request))

    @app.get("/partials/automations", response_class=HTMLResponse)
    def automations_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "automations_body.html", _auto_ctx(request))

    @app.post("/automations/toggle", response_class=HTMLResponse)
    def automations_toggle(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        cur = st.get_setting("automation.repair.enabled", "1") == "1"
        st.set_setting("automation.repair.enabled", "0" if cur else "1")
        st.log("automation", "repair " + ("disabled" if cur else "enabled"))
        return TEMPLATES.TemplateResponse(request, "automations_body.html", _auto_ctx(request))

    @app.post("/run-now", response_class=HTMLResponse)
    def run_now(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        lock: threading.Lock = request.app.state.run_lock
        if st.get_setting("automation.repair.enabled", "1") != "1":
            raise HTTPException(status_code=409, detail="the repair automation is disabled")
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a pass is already running")
        client = request.app.state.client
        st.log("automation", "run now", detail="one pass: route, dispatch, poll, gate, reduce")

        def _go() -> None:
            try:
                run_once(settings, cfg, st, client, log=lambda m: None)
            except Exception as ex:  # noqa: BLE001 - surfaced on the page, never silent
                st.log("automation", "run failed", detail=f"{type(ex).__name__}: {ex}"[:300])
                st.set_setting(
                    "automation.repair.last_result", f'{{"error": "{type(ex).__name__}"}}'
                )
            finally:
                lock.release()

        th = threading.Thread(target=_go, name="swe-loop-run", daemon=True)
        request.app.state.run_thread = th
        th.start()
        return TEMPLATES.TemplateResponse(request, "automations_body.html", _auto_ctx(request))

    @app.get("/devin/playbooks", response_class=HTMLResponse)
    def playbooks_page(request: Request) -> HTMLResponse:
        return _render(
            request,
            "playbooks.html",
            "playbooks",
            p=pages.playbooks(request.app.state.store, cfg, request.app.state.client),
        )

    @app.get("/devin/knowledge", response_class=HTMLResponse)
    def knowledge_page(request: Request) -> HTMLResponse:
        return _render(
            request,
            "knowledge.html",
            "knowledge",
            k=pages.knowledge(request.app.state.store, settings),
        )

    @app.get("/devin/insights", response_class=HTMLResponse)
    def insights_page(request: Request) -> HTMLResponse:
        return _render(
            request, "insights.html", "insights", i=pages.insights(request.app.state.store)
        )

    @app.get("/devin/review", response_class=HTMLResponse)
    def review_page(request: Request) -> HTMLResponse:
        return _render(request, "review.html", "review", rv=pages.review(request.app.state.store))

    @app.get("/devin/integrations", response_class=HTMLResponse)
    def integrations_page(request: Request) -> HTMLResponse:
        return _render(
            request,
            "integrations.html",
            "integrations",
            ig=pages.integrations(settings, cfg, request.app.state.store, request.app.state.client),
        )

    @app.get("/devin/next", response_class=HTMLResponse)
    def next_page(request: Request) -> HTMLResponse:
        return _render(request, "next.html", "next", nx=pages.next_page())

    @app.get("/board", response_class=HTMLResponse)
    def board_page(request: Request) -> HTMLResponse:
        o = ops.build(request.app.state.store)
        return TEMPLATES.TemplateResponse(
            request, "ops.html", {"o": o, "mode": settings.mode, "target": cfg.name}
        )

    @app.get("/partials/ops", response_class=HTMLResponse)
    def ops_partial(request: Request) -> HTMLResponse:
        o = ops.build(request.app.state.store)
        return TEMPLATES.TemplateResponse(request, "ops_body.html", {"o": o})

    @app.get("/sessions/{sid}")
    def session(sid: str) -> dict[str, Any]:
        d = ops.session_detail(app.state.store, sid)
        if not d:
            raise HTTPException(status_code=404)
        return d

    @app.get("/timeline")
    def timeline(
        session_id: str | None = None, ticket_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        return app.state.store.timeline(session_id=session_id, ticket_id=ticket_id, limit=limit)

    @app.get("/report", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, sql: int = 0) -> HTMLResponse:
        vm = report.build(request.app.state.store, INVENTORY)
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "h": vm["headline"],
                "bd": vm["burndown"],
                "sql": bool(sql),
                "mode": settings.mode,
                **vm,
            },
        )

    @app.get("/partials/board", response_class=HTMLResponse)
    def board(request: Request) -> HTMLResponse:
        vm = report.build(request.app.state.store, INVENTORY)
        return TEMPLATES.TemplateResponse(request, "board.html", {"board": vm["board"]})

    @app.post("/tickets/{ticket_id}/merge")
    async def merge(ticket_id: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        actor = (body or {}).get("actor")
        if not actor:
            raise HTTPException(
                status_code=400, detail="actor is required; it is hashed, never rendered"
            )
        try:
            return reduce_mod.record_merge(
                request.app.state.store, ticket_id, actor, (body or {}).get("pr_url")
            )
        except ValueError as ex:
            raise HTTPException(status_code=409, detail=str(ex)) from ex

    @app.get("/reduce")
    def reduce_summary() -> dict[str, Any]:
        st: Store = app.state.store
        reduce_mod.detect_conflicts(st)
        return reduce_mod.summary(st)

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        st: Store = app.state.store
        return {"headline": st.metrics(), "funnel": st.funnel()}

    return app


def ingest(store: Store, ev: NormalizedEvent) -> str:
    """One normalised event becomes one ticket (created or updated). Work orders only when the
    event already carries an acceptance command; otherwise the triage session scopes it."""
    tid = ticket_id_for(ev)
    wo = ev.work_order or {}
    route = wo.get("route")
    verdict = None
    if wo.get("acceptance"):
        verdict = {
            "acceptance_cmd": wo["acceptance"],
            "context_sufficient": True,
            "split": "one",
            "est_size": "XS" if len(wo.get("files", [])) <= 1 else "S",
            "needs_human": route == "human",
            "review": wo.get("review"),
        }
    store.upsert_ticket(
        id=tid,
        source=ev.source,
        title=ev.title,
        status="triaged" if verdict else "new",
        external_ref=ev.external_ref,
        cls=",".join(wo.get("classes", [])) or None,
        triage_verdict=verdict,
    )
    if verdict and route == "devin" and not store.work_orders_for(tid):
        store.insert_work_order(
            ticket_id=tid,
            shard_id=str(wo.get("shard", tid)),
            files=list(wo.get("files", [])),
            tests=list(wo.get("tests", [])),
            acceptance=dict(wo["acceptance"]),
            est_size=verdict["est_size"],
        )
    return tid


app = build_app()
