"""FastAPI entry point: the intake endpoint, ticket views, and the dashboard."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from swe_loop.config import Settings, TargetConfig
from swe_loop.intake import NormalizedEvent, normalize, ticket_id_for, verify_github_signature
from swe_loop.store import Store


def build_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    cfg = TargetConfig.load(settings.config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.cfg = cfg
        app.state.store = store or Store(settings.db_path)
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
        status="triaged",
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
