import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swe_loop.app import build_app
from swe_loop.config import Settings, TargetConfig
from swe_loop.intake import normalize, parse_work_order, verify_github_signature
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
REPO = {"full_name": "charliebachg/superset"}

ISSUE_BODY = """Why this matters.

### Work order
```yaml
# swe-loop work order
shard: D
route: devin
review: normal
files:
  - superset/models/helpers.py
tests:
  - tests/unit_tests/common/test_time_shifts.py
classes:
  - to_datetime-mixed-tz
acceptance:
  pandas_3_0_5: ".venv-p3/bin/python -m pytest tests/unit_tests/common/test_time_shifts.py"
max_acu_limit: 6
```
"""


def dependabot_pr(action="opened", author="dependabot[bot]", head="dependabot/pip/pandas-3.0.5"):
    return {
        "action": action,
        "repository": REPO,
        "pull_request": {
            "number": 42671,
            "title": "chore(deps): bump pandas from 2.3.3 to 3.0.5",
            "user": {"login": author},
            "head": {"ref": head},
            "base": {"ref": "master"},
            "labels": [{"name": "dependabot"}],
            "draft": False,
            "html_url": "https://github.com/charliebachg/superset/pull/42671",
        },
    }


def issue_event(
    number=4, labels=("swe-loop", "pandas-3", "route:devin"), body=ISSUE_BODY, action="opened"
):
    return {
        "action": action,
        "repository": REPO,
        "issue": {
            "number": number,
            "title": "pandas 3: mixed-timezone parsing in models/helpers.py needs utc=True",
            "user": {"login": "charliebachg"},
            "labels": [{"name": lbl} for lbl in labels],
            "body": body,
        },
    }


def test_dependabot_pr_matches_and_others_do_not():
    ev = normalize("github", dependabot_pr(), CFG)
    assert ev and ev.kind == "pull_request" and ev.number == 42671
    assert (
        ev.ref == "dependabot/pip/pandas-3.0.5" and ev.external_ref == "charliebachg/superset#42671"
    )
    assert normalize("github", dependabot_pr(author="someone"), CFG) is None
    assert normalize("github", dependabot_pr(head="feature/x"), CFG) is None
    assert normalize("github", dependabot_pr(action="closed"), CFG) is None


def test_issue_with_label_and_work_order():
    ev = normalize("github", issue_event(), CFG)
    assert ev and ev.kind == "issue" and ev.work_order["shard"] == "D"
    assert ev.work_order["acceptance"]["pandas_3_0_5"].startswith(".venv-p3")
    assert normalize("github", issue_event(labels=("bug",)), CFG) is None


def test_work_order_block_is_optional():
    assert parse_work_order("no block here") is None
    assert parse_work_order("```yaml\n# swe-loop work order\nshard: A\n```")["shard"] == "A"


def test_check_run_failure_on_dependabot_branch():
    payload = {
        "action": "completed",
        "repository": REPO,
        "check_run": {
            "name": "unit-tests (current)",
            "conclusion": "failure",
            "check_suite": {"head_branch": "dependabot/pip/pandas-3.0.5"},
            "pull_requests": [{"number": 42671}],
        },
    }
    ev = normalize("github", payload, CFG)
    assert ev and ev.kind == "check_run" and ev.number == 42671
    payload["check_run"]["conclusion"] = "success"
    assert normalize("github", payload, CFG) is None


def test_signature_fails_closed_when_secret_set():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_github_signature("s3cret", body, sig)
    assert not verify_github_signature("s3cret", body, "sha256=deadbeef")
    assert not verify_github_signature("s3cret", body, None)
    assert verify_github_signature("", body, None)  # no secret configured: local only


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_MODE", "live")  # must be forced back to replay: no key
    settings = Settings.from_env()
    assert settings.mode == "replay"
    st = Store(tmp_path / "t.sqlite")
    app = build_app(settings, st, seed_replay=False)
    with TestClient(app) as c:
        yield c, st


def test_intake_issue_creates_ticket_and_work_order(client):
    c, st = client
    r = c.post("/intake/github", json=issue_event())
    assert r.status_code == 200 and r.json()["accepted"] and r.json()["ticket_id"] == "tkt_D"
    t = c.get("/tickets/tkt_D").json()
    assert t["status"] == "triaged" and t["external_ref"] == "charliebachg/superset#4"
    assert len(t["work_orders"]) == 1 and t["work_orders"][0]["files"] == [
        "superset/models/helpers.py"
    ]
    # the same event again does not duplicate the work order
    c.post("/intake/github", json=issue_event(action="edited"))
    assert len(c.get("/tickets/tkt_D").json()["work_orders"]) == 1
    assert st.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_intake_pr_creates_untriaged_ticket(client):
    c, _ = client
    r = c.post("/intake/github", json=dependabot_pr())
    assert r.json()["accepted"] and r.json()["ticket_id"] == "tkt_pu42671"
    t = c.get("/tickets/tkt_pu42671").json()
    assert t["status"] == "new" and t["triage_verdict_json"] is None and t["work_orders"] == []


def test_intake_rejects_bad_signature_when_secret_set(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(dependabot_pr()).encode()
    r = c.post("/intake/github", content=body, headers={"X-Hub-Signature-256": "sha256=nope"})
    assert r.status_code == 401
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    r = c.post(
        "/intake/github",
        content=body,
        headers={"X-Hub-Signature-256": good, "Content-Type": "application/json"},
    )
    assert r.status_code == 200


def test_intake_filters_unmatched(client):
    c, st = client
    r = c.post("/intake/github", json=dependabot_pr(author="human"))
    assert r.json()["accepted"] is False
    assert st.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1  # still logged


def test_manual_intake_for_replay(client):
    c, _ = client
    ev = {
        "kind": "issue",
        "repo": "charliebachg/superset",
        "number": 4,
        "title": "D",
        "body": ISSUE_BODY,
    }
    r = c.post("/intake/manual", json=ev)
    assert r.json()["accepted"] and r.json()["ticket_id"] == "tkt_D"
    m = c.get("/metrics").json()
    assert m["funnel"]["tickets"] == 1
