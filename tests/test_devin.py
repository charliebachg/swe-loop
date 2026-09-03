import json

import httpx
import pytest

from swe_loop.config import Settings
from swe_loop.devin import (
    DevinClient,
    DevinError,
    FakeTransport,
    HttpTransport,
    SessionSpec,
    SessionState,
)

SCHEMA = {"type": "object", "properties": {"self_reported_done": {"type": "boolean"}}}


def spec(**over):
    base = {
        "prompt": "Fix all to_datetime-mixed-tz call sites in superset/models/helpers.py",
        "tags": ("swe-loop", "D", "to_datetime-mixed-tz"),
        "repos": ("charliebachg/superset",),
        "max_acu_limit": 6,
        "structured_output_schema": SCHEMA,
        "playbook_id": "pb-repair",
    }
    base.update(over)
    return SessionSpec(**base)


def test_payload_carries_the_contract():
    p = spec().to_payload()
    assert p["structured_output_required"] is True
    assert p["max_acu_limit"] == 6 and p["repos"] == ["charliebachg/superset"]
    assert p["playbook_id"] == "pb-repair" and p["devin_mode"] == "normal"
    assert "snapshot_id" not in p and "idempotent" not in p  # dropped in v3


def test_terminal_rules():
    s = lambda st, d, **k: SessionState("x", "u", st, d, **k)
    assert not s("running", "working").terminal
    assert not s("new", None).terminal
    assert (
        s("running", "waiting_for_user").terminal
        and s("running", "waiting_for_user").needs_attention
    )
    assert s("exit", "finished").terminal and s("exit", "finished").succeeded
    assert (
        s("exit", "usage_limit_exceeded").too_large
        and not s("exit", "usage_limit_exceeded").succeeded
    )
    assert s("error", None).terminal and not s("error", None).succeeded


def test_fake_transport_runs_a_session_to_completion(tmp_path):
    c = DevinClient(FakeTransport(tmp_path))
    st = c.start(spec())
    assert st.session_id.startswith("fake-") and not st.terminal
    seen = [st]
    while not seen[-1].terminal:
        seen.append(c.status(st.session_id))
    final = seen[-1]
    assert final.succeeded and final.structured_output["self_reported_done"] is True
    assert final.pull_requests and final.acus_consumed == 2.1
    c.message(st.session_id, "T1 failed: 2 hits remain")
    c.terminate(st.session_id)
    kinds = [k for k, _ in c.t.calls]
    assert kinds[0] == "create_session" and "send_message" in kinds and kinds[-1] == "terminate"
    assert c.insights([st.session_id])[st.session_id]["session_size"] == "S"


def test_fake_transport_prefers_a_recorded_fixture(tmp_path):
    (tmp_path / "sessions").mkdir()
    fx = {
        "session_id": "devin-recorded-1",
        "url": "https://app.devin.ai/sessions/devin-recorded-1",
        "match_tags": ["D"],
        "timeline": [
            {"status": "running", "status_detail": "working", "acus_consumed": 0.3},
            {"status": "exit", "status_detail": "usage_limit_exceeded", "acus_consumed": 6.0},
        ],
        "insights": {"session_size": "L"},
    }
    (tmp_path / "sessions" / "001.json").write_text(json.dumps(fx))
    c = DevinClient(FakeTransport(tmp_path))
    st = c.start(spec())
    assert st.session_id == "devin-recorded-1"
    final = c.status(st.session_id)
    assert final.terminal and final.too_large and final.structured_output is None


def test_client_is_fake_without_a_key(monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_MODE", "live")
    assert DevinClient.from_settings(Settings.from_env()).is_fake


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_transport_request_shapes():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.method == "POST" and req.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"session_id": "s1", "url": "u", "status": "new"})
        if req.method == "GET":
            return httpx.Response(
                200, json={"session_id": "s1", "status": "exit", "status_detail": "finished"}
            )
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"ok": True})

    t = HttpTransport("cog_test", "org-1", client=_mock_client(handler), sleep=lambda s: None)
    c = DevinClient(t)
    st = c.start(spec())
    assert st.session_id == "s1"
    c.status("s1")
    c.message("s1", "hello")
    c.terminate("s1")
    c.review_pr("https://github.com/charliebachg/superset/pull/7")
    paths = [(r.method, r.url.path) for r in seen]
    assert paths == [
        ("POST", "/v3/organizations/org-1/sessions"),
        ("GET", "/v3/organizations/org-1/sessions/s1"),
        ("POST", "/v3/organizations/org-1/sessions/s1/messages"),
        ("DELETE", "/v3/organizations/org-1/sessions/s1"),
        ("POST", "/v3/organizations/org-1/pr-reviews"),
    ]
    assert all(r.headers["Authorization"] == "Bearer cog_test" for r in seen)
    assert json.loads(seen[0].content)["structured_output_required"] is True
    assert seen[3].url.params["archive"] == "true"  # never a hard delete
    assert json.loads(seen[4].content) == {
        "pr_url": "https://github.com/charliebachg/superset/pull/7"
    }


def test_http_transport_backs_off_on_429_and_fails_on_401():
    calls = {"n": 0}
    slept = []

    def handler(req: httpx.Request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(
            200, json={"session_id": "s1", "status": "running", "status_detail": "working"}
        )

    t = HttpTransport("k", "o", client=_mock_client(handler), sleep=slept.append)
    assert t.get_session("s1")["status"] == "running"
    assert calls["n"] == 3 and slept == [2.0, 2.0]

    t2 = HttpTransport(
        "k",
        "o",
        client=_mock_client(lambda r: httpx.Response(401, text="nope")),
        sleep=slept.append,
    )
    with pytest.raises(DevinError) as ex:
        t2.get_session("s1")
    assert ex.value.status == 401


def test_list_sessions_uses_first_after_and_array_tags():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if "after" not in req.url.params:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"session_id": "a", "status": "running", "tags": ["swe-loop", "wo:1"]}
                    ],
                    "has_next_page": True,
                    "end_cursor": "c1",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [{"session_id": "b", "status": "running", "tags": ["swe-loop"]}],
                "has_next_page": False,
                "end_cursor": None,
            },
        )

    t = HttpTransport("k", "o", client=_mock_client(handler), sleep=lambda s: None)
    got = t.list_sessions(["swe-loop", "wo:1"])
    assert [i["session_id"] for i in got] == ["a"]  # b lacks wo:1: filtered client-side
    q0, q1 = seen[0].url.params, seen[1].url.params
    assert q0["first"] == "100" and "after" not in q0 and "limit" not in q0 and "cursor" not in q0
    assert q0.get_list("tags") == ["swe-loop", "wo:1"]  # repeated array parameter
    assert q1["after"] == "c1"


def test_pagination_stops_when_the_cursor_stops_advancing():
    n = {"calls": 0}

    def handler(req: httpx.Request):
        n["calls"] += 1
        return httpx.Response(200, json={"items": [], "has_next_page": True, "end_cursor": "same"})

    t = HttpTransport("k", "o", client=_mock_client(handler), sleep=lambda s: None)
    t.list_sessions()
    assert n["calls"] == 2  # page one, page "same", then stop


def test_transport_errors_become_devin_errors():
    def handler(req: httpx.Request):
        raise httpx.ReadTimeout("slow", request=req)

    t = HttpTransport("k", "o", client=_mock_client(handler), sleep=lambda s: None, max_retries=1)
    with pytest.raises(DevinError) as ex:
        t.get_session("s1")
    assert ex.value.status == 0 and "ReadTimeout" in ex.value.detail


def test_alive_versus_terminal():
    s = SessionState("x", "u", "running", "waiting_for_user")
    assert s.terminal and s.alive
    s = SessionState("x", "u", "exit", "finished")
    assert s.terminal and not s.alive


def test_http_transport_pages_insights():
    pages = [
        {
            "items": [{"session_id": "a", "session_size": "S"}],
            "has_next_page": True,
            "end_cursor": "c1",
        },
        {
            "items": [{"session_id": "b", "session_size": "L"}],
            "has_next_page": False,
            "end_cursor": None,
        },
    ]

    def handler(req: httpx.Request):
        return httpx.Response(200, json=pages.pop(0))

    seen = []
    orig = handler

    def handler2(req):
        seen.append(req)
        return orig(req)

    t = HttpTransport("k", "o", client=_mock_client(handler2), sleep=lambda s: None)
    got = t.list_insights(["b"])
    assert [i["session_id"] for i in got] == ["a", "b"]  # the server filters; we page
    assert seen[0].url.params.get_list("session_ids") == ["b"]
    assert seen[1].url.params["after"] == "c1"
