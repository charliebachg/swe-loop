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
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from swe_loop import connect, cost, ops, pages, replay, rerun, runner, v2
from swe_loop import reduce as reduce_mod
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.intake import ingest, normalize, verify_github_signature
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
        busy = request.app.state.run_lock.locked() or bool(st.live_sessions())
        full = {
            **pages.shell(settings, cfg, st, active),
            **v2.frame(settings, cfg, st, active),
            "refreshUrl": (
                str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")
            )
            if busy
            else "",
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

    @app.get("/tracker")
    def tracker_redirect(request: Request) -> RedirectResponse:
        """The pipeline view lives on the Tickets page now."""
        return RedirectResponse(
            v2.url("/tickets-page", **{**_q(request), "view": "pipeline"}), status_code=303
        )

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
        q = {**_q(request), **({"open": sel} if sel else {})}
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
            from swe_loop.cli import run_once

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
                return
            # the answer is the only thing that was missing: carry on without another click
            lock: threading.Lock = request.app.state.run_lock
            if not lock.acquire(blocking=False):
                st.log(
                    "automation",
                    "a run is already in progress; it will pick this up",
                    ticket_id=ticket_id,
                )
                return
            try:
                st.log(
                    "automation",
                    "continuing after your answer",
                    ticket_id=ticket_id,
                    detail="route, start the repair session, gate, review",
                )
                run_once(settings, cfg, st, client, log=lambda m: None)
            except Exception as ex:  # noqa: BLE001 - surfaced on the page
                st.log(
                    "automation",
                    "run failed",
                    ticket_id=ticket_id,
                    detail=f"{type(ex).__name__}: {ex}"[:200],
                )
            finally:
                lock.release()

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
        """A person merges. The pull request is merged on GitHub and the decision recorded here,
        in that order, so the two can never disagree. Nothing else in the loop can call this."""
        form = parse_qs((await request.body()).decode())
        actor = (form.get("actor") or [""])[0].strip()
        st: Store = request.app.state.store
        note = _do_merge(st, ticket_id, actor, request)
        return _page(
            request,
            "tickets",
            "v2/tickets.html",
            v2.tickets(st, cfg, {**_q(request), "view": "pipeline", "open": ticket_id}, note=note),
        )

    def _do_merge(st: Store, ticket_id: str, actor: str, request: Request) -> str:
        """Returns what to tell the person: empty when it went through."""
        if not actor:
            return "a name is needed; it is hashed and never shown"
        client = request.app.state.client
        live = settings.live and not client.is_fake
        try:
            reduce_mod.readiness(st, ticket_id)
        except Exception:  # noqa: BLE001 - an unknown ticket
            raise HTTPException(status_code=404) from None
        if live:
            results = reduce_mod.merge_on_github(st, ticket_id, settings.github_token)
            refused = [r for r in results if not r["merged"]]
            if refused:
                st.log(
                    "merge",
                    "GitHub refused the merge",
                    ticket_id=ticket_id,
                    detail=refused[0]["why"],
                )
                return "GitHub did not merge it: " + refused[0]["why"]
        try:
            reduce_mod.record_merge(st, ticket_id, actor)
        except ValueError as ex:
            return str(ex)
        return ""

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        st: Store = request.app.state.store
        return _render(
            request,
            "settings.html",
            "settings",
            cost=cost.spend(request.app.state.store),
            cost_rows=v2.cost_rows(request.app.state.store),
            usdCap=v2._usd_cap(st),
            rerun=v2.rerun_ctx(settings, cfg, st),
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

    @app.post("/settings/reset-shard")
    async def settings_reset_shard(request: Request) -> RedirectResponse:
        """Put one shard back to its broken state, on the repository and in the store, so the
        next Run does it again for real."""
        form = parse_qs((await request.body()).decode())
        shard = (form.get("shard") or [""])[0].strip().upper()
        if not shard or shard not in {s["id"] for s in rerun.shards()}:
            raise HTTPException(status_code=400, detail="pick a shard the loop can repair")
        if request.app.state.run_lock.locked():
            raise HTTPException(status_code=409, detail="a run is in progress; reset after it")
        st: Store = request.app.state.store
        client = request.app.state.client
        try:
            rerun.reset_shard(settings, cfg, st, shard, push=settings.live and not client.is_fake)
        except (RuntimeError, ValueError) as ex:
            st.set_setting("rerun.last", json.dumps({"shard": shard, "error": str(ex)[:300]}))
        return RedirectResponse("/settings#rerun", status_code=303)

    @app.post("/settings/budget")
    async def settings_budget(request: Request) -> RedirectResponse:
        form = parse_qs((await request.body()).decode())
        st: Store = request.app.state.store
        b = st.budget_state()
        try:
            cap = float((form.get("acu_cap") or [b.get("cap") or 300])[0])
            per = float(form.get("per_session_cap", ["0"])[0])
            usd_cap = float((form.get("usd_cap") or ["0"])[0] or 0)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail="numbers only") from ex
        if cap <= 0 or per <= 0 or usd_cap < 0:
            raise HTTPException(status_code=400, detail="caps must be positive")
        st.set_budget(cap, per)
        if "usd_cap" in form:
            st.set_setting("usd_cap", str(usd_cap) if usd_cap else "")
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
        trig = form.get("trigger", "github:issues")
        source, _, event = trig.partition(":")
        if source == "manual":
            event = "click"
        if source == "schedule":
            event = "recurring"
        scan = form.get("kind") == "scan"
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
            kind="scan" if scan else "custom",
            enabled=False,
            availability="next" if scan else "live",
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
        if a["kind"] not in ("repair", "custom") or a["availability"] != "live":
            raise HTTPException(
                status_code=409, detail="a scan automation is for the next version; nothing runs"
            )
        if not a["enabled"]:
            raise HTTPException(status_code=409, detail="the automation is disabled")
        lock: threading.Lock = request.app.state.run_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a pass is already running")
        client = request.app.state.client
        st.set_setting("automation.running", aid)
        st.log(
            "automation",
            f"{a['name']}: run",
            detail="issues to tickets, triage, route, repair, gate, review",
        )

        def _go() -> None:
            try:
                runner.run_automation(settings, cfg, st, client, aid, log=lambda m: None)
            except Exception as ex:  # noqa: BLE001 - surfaced on the page, never silent
                st.log("automation", "run failed", detail=f"{type(ex).__name__}: {ex}"[:300])
            finally:
                st.set_setting("automation.running", "")
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
            k=pages.knowledge(request.app.state.store, settings, cfg),
        )

    @app.get("/devin/insights", response_class=HTMLResponse)
    def insights_page(request: Request) -> HTMLResponse:
        return _render(request, "insights.html", "insights", i=v2.insights(request.app.state.store))

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

    @app.get("/evidence/{eid}")
    def evidence_log(eid: str) -> PlainTextResponse:
        """The log of one check, exactly as it was written when the command ran."""
        row = app.state.store._one("SELECT * FROM evidence WHERE id=?", eid)
        if not row or not row["output_path"]:
            raise HTTPException(status_code=404)
        p = Path(row["output_path"]).resolve()
        root = (ROOT / cfg.gate.get("evidence_dir", "data/live/evidence")).resolve()
        if not p.is_relative_to(root) or not p.exists():
            raise HTTPException(status_code=404, detail="the log is not on this machine")
        head = (
            f"# {row['command']}\n# exit {row['exit_code']} · tree {row['tree_hash']}\n"
            f"# digest {row['output_digest']}\n\n"
        )
        return PlainTextResponse(head + p.read_text(errors="replace"))

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


app = build_app()
