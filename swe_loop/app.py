"""FastAPI entry point: the intake endpoint, ticket views, and the dashboard."""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from swe_loop import connect, cost, ops, pages, replay, v2
from swe_loop import reduce as reduce_mod
from swe_loop.cli import run_once
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.intake import NormalizedEvent, normalize, ticket_id_for, verify_github_signature
from swe_loop.store import Store, now

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
        pages.seed_automations(app.state.store, cfg)
        pages.seed_playbooks(app.state.store, cfg)
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
            "intake",
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
            request,
            template,
            {
                **pages.shell(settings, cfg, st, active),
                **v2.frame(settings, cfg, st, active),
                **ctx,
            },
        )

    def _page(request: Request, active: str, template: str, ctx: dict[str, Any]) -> HTMLResponse:
        """A designed page. An HTMX request gets the content block only; the frame stays."""
        st: Store = request.app.state.store
        full = {
            **pages.shell(settings, cfg, st, active),
            **v2.frame(settings, cfg, st, active),
            **ctx,
        }
        if request.headers.get("HX-Request"):
            tpl = TEMPLATES.env.get_template(template)
            html = "".join(tpl.blocks["content"](tpl.new_context({**full, "request": request})))
            return HTMLResponse(html)
        return TEMPLATES.TemplateResponse(request, template, full)

    def _q(request: Request) -> dict[str, str]:
        return {k: v for k, v in request.query_params.items()}

    # ---- the designed pages (registered first, so they answer before the earlier builders)
    @app.get("/", response_class=HTMLResponse)
    def home_v2(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _page(request, "home", "v2/home.html", v2.home(st, cfg, _q(request)))

    @app.get("/automations", response_class=HTMLResponse)
    def automations_v2(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        running = request.app.state.run_lock.locked()
        return _page(
            request,
            "automations",
            "v2/automations.html",
            v2.automations(st, cfg, settings, request.app.state.client, running, _q(request)),
        )

    @app.get("/tickets-page", response_class=HTMLResponse)
    def tickets_v2(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _page(request, "tickets", "v2/tickets.html", v2.tickets(st, cfg, _q(request)))

    @app.get("/tracker", response_class=HTMLResponse)
    def tracker_v2(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _page(request, "tracker", "v2/tracker.html", v2.tracker(st, cfg, _q(request)))

    @app.get("/report", response_class=HTMLResponse)
    def report_v2(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _page(
            request, "report", "v2/report.html", v2.report(st, cfg, INVENTORY, _q(request))
        )

    @app.get("/devin/sessions", response_class=HTMLResponse)
    def sessions_v2(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _page(request, "sessions", "v2/sessions.html", v2.sessions(st, cfg, _q(request)))

    @app.get("/devin/playbooks", response_class=HTMLResponse)
    def playbooks_v2(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _page(
            request,
            "playbooks",
            "v2/playbooks.html",
            v2.playbooks(st, cfg, request.app.state.client, _q(request)),
        )

    def _auto_page(
        request: Request, err: bool = False, name: str = "", sel: str | None = None
    ) -> HTMLResponse:
        st: Store = request.app.state.store
        q = {**_q(request), **({"sel": sel} if sel else {})}
        return _page(
            request,
            "automations",
            "v2/automations.html",
            v2.automations(
                st,
                cfg,
                settings,
                request.app.state.client,
                request.app.state.run_lock.locked(),
                q,
                err=err,
                name=name,
            ),
        )

    @app.post("/automations", response_class=HTMLResponse)
    async def automations_add_v2(request: Request) -> HTMLResponse:
        body = (await request.body()).decode()
        form = {k: v[0].strip() for k, v in parse_qs(body).items()}
        if not form.get("name"):
            return _auto_page(request, err=True, name="")
        aid = _add_automation(request.app.state.store, form)
        return _auto_page(request, sel=aid)

    @app.post("/automations/{aid}/toggle", response_class=HTMLResponse)
    def automations_toggle_v2(aid: str, request: Request) -> HTMLResponse:
        _toggle_automation(request.app.state.store, aid)
        return _auto_page(request, sel=aid)

    @app.post("/automations/{aid}/delete", response_class=HTMLResponse)
    def automations_delete_v2(aid: str, request: Request) -> HTMLResponse:
        _delete_automation(request.app.state.store, aid)
        return _auto_page(request)

    @app.post("/automations/{aid}/run", response_class=HTMLResponse)
    def automations_run_v2(aid: str, request: Request) -> HTMLResponse:
        _run_automation(request, aid)
        return _auto_page(request, sel=aid)

    @app.post("/devin/playbooks", response_class=HTMLResponse)
    async def playbooks_add_v2(request: Request) -> HTMLResponse:
        body = (await request.body()).decode()
        form = {k: v[0] for k, v in parse_qs(body).items()}
        st: Store = request.app.state.store
        if not form.get("name", "").strip() or not form.get("body", "").strip():
            return _page(
                request,
                "playbooks",
                "v2/playbooks.html",
                v2.playbooks(
                    st,
                    cfg,
                    request.app.state.client,
                    _q(request),
                    err=True,
                    name=form.get("name", ""),
                ),
            )
        pid = _add_playbook(st, form)
        return _page(
            request,
            "playbooks",
            "v2/playbooks.html",
            v2.playbooks(st, cfg, request.app.state.client, {**_q(request), "sel": pid}),
        )

    def _home_block(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _page(request, "home", "v2/home.html", v2.home(st, cfg, _q(request)))

    @app.post("/tickets/{ticket_id}/answer", response_class=HTMLResponse)
    async def answer_v2(ticket_id: str, request: Request) -> HTMLResponse:
        """A person's answer to a waiting triage session. The session resumes in the background;
        the page shows it in flight."""
        from swe_loop.triage import run_triage

        form = parse_qs((await request.body()).decode())
        text = (form.get("text") or [""])[0].strip()
        st: Store = request.app.state.store
        if not text or not st.get_ticket(ticket_id):
            raise HTTPException(status_code=400, detail="an answer is required")
        client = request.app.state.client
        pid = st.get_setting("playbook_id.triage-pandas3")
        inv = cfg.triage.get("inventory_url") or None

        def _go() -> None:
            try:
                run_triage(
                    st, client, ticket_id, cfg, inventory_path=inv, playbook_id=pid, answer=text
                )
            except Exception as ex:  # noqa: BLE001 - surfaced on the page
                st.log(
                    "triage",
                    "answer failed",
                    ticket_id=ticket_id,
                    detail=f"{type(ex).__name__}: {ex}"[:200],
                )

        th = threading.Thread(target=_go, name=f"swe-loop-answer-{ticket_id}", daemon=True)
        request.app.state.answer_threads = getattr(request.app.state, "answer_threads", []) + [th]
        th.start()
        if client.is_fake:
            th.join(10)
        return _home_block(request)

    @app.post("/escalations/{eid}/resolve", response_class=HTMLResponse)
    async def resolve_v2(eid: str, request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode())
        note = (form.get("note") or [""])[0].strip()
        st: Store = request.app.state.store
        if st.resolve_escalation(eid, note) is None:
            raise HTTPException(status_code=404)
        return _home_block(request)

    @app.post("/tickets/{ticket_id}/merge-form", response_class=HTMLResponse)
    async def merge_form_v2(ticket_id: str, request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode())
        actor = (form.get("actor") or [""])[0].strip()
        st: Store = request.app.state.store
        if actor:
            try:
                reduce_mod.record_merge(st, ticket_id, actor)
            except ValueError:
                pass  # not ready: the re-rendered row says why
        return _page(
            request, "tracker", "v2/tracker.html", v2.tracker(st, cfg, {"open": ticket_id})
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _render(
            request,
            "settings.html",
            "settings",
            cost=cost.spend(request.app.state.store),
            cost_rows=v2.cost_rows(request.app.state.store),
            s=pages.settings_page(settings, cfg, st, request.app.state.client),
        )

    @app.post("/settings/credits")
    async def settings_credits(request: Request) -> RedirectResponse:
        """A person read the credits figure in the console (Settings > Plans); it calibrates the
        active-minute estimate into dollars."""
        form = parse_qs((await request.body()).decode())
        try:
            usd = float((form.get("credits_usd") or [""])[0])
        except ValueError as ex:
            raise HTTPException(status_code=400, detail="credits must be a number") from ex
        if usd < 0:
            raise HTTPException(status_code=400, detail="credits must be zero or more")
        cost.record_credits(request.app.state.store, usd)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/session-cost")
    async def settings_session_cost(request: Request) -> RedirectResponse:
        """The console's dollars per session, one field per session, blanks ignored."""
        form = parse_qs((await request.body()).decode())
        st: Store = request.app.state.store
        for key, vals in form.items():
            if not key.startswith("usd_") or not (vals and vals[0].strip()):
                continue
            try:
                usd = float(vals[0])
            except ValueError as ex:
                raise HTTPException(status_code=400, detail=f"{key}: not a number") from ex
            st.set_session_cost(key[4:], usd)
        return RedirectResponse("/settings", status_code=303)

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

    @app.get("/partials/ticket/{ticket_id}", response_class=HTMLResponse)
    def ticket_partial(ticket_id: str, request: Request) -> HTMLResponse:
        d = pages.ticket_detail(request.app.state.store, ticket_id)
        if not d:
            raise HTTPException(status_code=404)
        return TEMPLATES.TemplateResponse(request, "ticket_detail.html", {"d": d})

    def _add_automation(st: Store, form: dict[str, str]) -> str:
        name = form.get("name", "")
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        trig = form.get("trigger", "github:pull_request")
        source, _, event = trig.partition(":")
        match: dict[str, str] = {}
        for part in (form.get("match") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                match[k.strip()] = v.strip()
        trigger: dict[str, Any] = {"source": source, "event": event, "match": match}
        if "label" in match:
            trigger["issue_label"] = match.pop("label")
        aid = st.upsert_automation(
            name=name[:80],
            kind="custom",
            enabled=False,
            availability="live",
            trigger=trigger,
            target=form.get("target") or cfg.repo,
            playbook=form.get("playbook") or None,
            max_acu=float(form.get("max_acu") or cfg.max_acu_limit),
            concurrency=int(form.get("concurrency") or 4),
            schedule=form.get("schedule") or None,
            notes=(form.get("notes") or "")[:200] or None,
        )
        st.log("automation", f"added {name[:40]}", detail=trig)
        return aid

    def _toggle_automation(st: Store, aid: str) -> None:
        a = st.get_automation(aid)
        if not a:
            raise HTTPException(status_code=404)
        if a["availability"] == "next":
            raise HTTPException(status_code=409, detail="not available yet")
        st.set_automation(aid, enabled=0 if a["enabled"] else 1)
        st.log("automation", f"{a['name']} " + ("disabled" if a["enabled"] else "enabled"))

    def _delete_automation(st: Store, aid: str) -> None:
        a = st.get_automation(aid)
        if not a:
            raise HTTPException(status_code=404)
        if a["kind"] != "custom":
            raise HTTPException(status_code=409, detail="the built-in automations stay")
        st.delete_automation(aid)
        st.log("automation", f"removed {a['name']}")

    def _run_automation(request: Request, aid: str) -> None:
        st: Store = request.app.state.store
        a = st.get_automation(aid)
        if not a:
            raise HTTPException(status_code=404)
        if a["kind"] != "repair" or a["availability"] != "live":
            raise HTTPException(
                status_code=409, detail="only the Repair automation runs in this version"
            )
        if not a["enabled"]:
            raise HTTPException(status_code=409, detail="the automation is disabled")
        lock: threading.Lock = request.app.state.run_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a pass is already running")
        client = request.app.state.client
        st.log(
            "automation",
            f"{a['name']}: run now",
            detail="one pass: route, dispatch, poll, gate, reduce",
        )

        def _go() -> None:
            try:
                out = run_once(settings, cfg, st, client, log=lambda m: None)
                st.set_automation(aid, last_run=now(), last_result=out)
            except Exception as ex:  # noqa: BLE001 - surfaced on the page, never silent
                st.log("automation", "run failed", detail=f"{type(ex).__name__}: {ex}"[:300])
                st.set_automation(aid, last_run=now(), last_result={"error": type(ex).__name__})
            finally:
                lock.release()

        th = threading.Thread(target=_go, name="swe-loop-run", daemon=True)
        request.app.state.run_thread = th
        th.start()

    @app.get("/partials/playbook/{pid}", response_class=HTMLResponse)
    def playbook_partial(pid: str, request: Request) -> HTMLResponse:
        d = pages.playbook_detail(request.app.state.store, pid)
        if not d:
            raise HTTPException(status_code=404)
        return TEMPLATES.TemplateResponse(request, "playbook_detail.html", {"d": d})

    def _add_playbook(st: Store, form: dict[str, str]) -> str:
        name, body = form.get("name", "").strip(), form.get("body", "").strip()
        if not name or not body:
            raise HTTPException(status_code=400, detail="name and body are required")
        schema = None
        if form.get("schema", "").strip():
            try:
                schema = json.loads(form["schema"])
            except ValueError as ex:
                raise HTTPException(status_code=400, detail="schema is not valid JSON") from ex
        pid = st.upsert_playbook(
            name=name[:80],
            agent=(form.get("agent") or "custom session")[:40],
            body=body,
            schema=schema,
            max_acu=float(form.get("max_acu") or cfg.max_acu_limit),
            source="user",
        )
        st.log("playbook", f"added {name[:40]}")
        return pid

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
