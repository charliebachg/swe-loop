"""Read-only checks that the connection is what Settings says it is. Cached for a minute.

Every check names the call it made and says present, missing, or not checked. In replay mode
with no tokens nothing is called; the page says so instead of pretending.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient, DevinError
from swe_loop.store import Store

_CACHE: dict[str, tuple[float, list[Check]]] = {}
TTL = 60.0


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    value: str
    status: str  # ok | missing | skipped
    call: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _gh(token: str, path: str) -> tuple[int, dict[str, Any]]:
    r = httpx.get(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    try:
        return r.status_code, r.json() if r.content else {}
    except ValueError:
        return r.status_code, {}


def run_checks(
    settings: Settings, cfg: TargetConfig, store: Store, client: DevinClient | None = None
) -> list[Check]:
    key = f"{cfg.name}:{settings.mode}"
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    out: list[Check] = []
    gh = settings.github_token

    # repository
    if gh:
        code, d = _gh(gh, f"/repos/{cfg.repo}")
        parent = (d.get("parent") or {}).get("full_name")
        ok = code == 200 and (
            not cfg.upstream or parent == cfg.upstream or cfg.upstream == cfg.repo
        )
        out.append(
            Check(
                "repo",
                "repository",
                cfg.repo,
                "ok" if ok else "missing",
                f"GET /repos/{cfg.repo} -> {code}" + (f", fork of {parent}" if parent else ""),
            )
        )
        code, d = _gh(gh, f"/repos/{cfg.repo}/branches/{cfg.base_branch}")
        sha = ((d.get("commit") or {}).get("sha") or "")[:8]
        out.append(
            Check(
                "branch",
                "branch under repair",
                cfg.base_branch,
                "ok" if code == 200 else "missing",
                f"GET /repos/{cfg.repo}/branches/{cfg.base_branch} -> {code}"
                + (f", head {sha}" if sha else ""),
            )
        )
        code, d = _gh(gh, f"/repos/{cfg.repo}/installation")
        app = d.get("app_slug") or ""
        out.append(
            Check(
                "app",
                "Devin GitHub App",
                "installed on this repository" if code == 200 else "not visible to this token",
                "ok" if code == 200 else "skipped",
                f"GET /repos/{cfg.repo}/installation -> {code}"
                + (
                    f", app {app}"
                    if app
                    else " (a fine-grained token cannot read installations; confirmed in the Devin console)"
                ),
            )
        )
    else:
        for k, lbl, v in (
            ("repo", "repository", cfg.repo),
            ("branch", "branch under repair", cfg.base_branch),
            ("app", "Devin GitHub App", "installed on this repository"),
        ):
            out.append(Check(k, lbl, v, "skipped", "not checked: no GitHub token in this mode"))

    # org
    if settings.live and settings.devin_api_key and client is not None and not client.is_fake:
        try:
            client.t.list_sessions([])
            out.append(
                Check(
                    "org",
                    "Devin org",
                    settings.devin_org_id,
                    "ok",
                    f"GET /v3/organizations/{settings.devin_org_id}/sessions -> 200",
                )
            )
        except DevinError as ex:
            out.append(
                Check(
                    "org",
                    "Devin org",
                    settings.devin_org_id,
                    "missing",
                    f"GET /v3/.../sessions -> {ex.status}",
                )
            )
    else:
        out.append(
            Check(
                "org",
                "Devin org",
                settings.devin_org_id or "replay",
                "skipped",
                "not checked: replay mode, no sessions are created",
            )
        )

    # seam and budget: local
    out.append(
        Check(
            "seam",
            "the seam",
            str(settings.config_path),
            "ok",
            f"parsed: repo, trigger, detector, router ({len(cfg.forbidden_paths)} forbidden paths), session, gate",
        )
    )
    b = store.budget_state()
    out.append(
        Check(
            "budget",
            "budget",
            f"{b['cap']:.0f} ACU cap · {b['per_session_cap']:.0f} per session"
            if b.get("cap")
            else "no cap set",
            "ok" if b.get("cap") else "missing",
            f"budget row; spent {b['spent']}",
        )
    )
    _CACHE[key] = (time.monotonic(), out)
    return out


def clear_cache() -> None:
    _CACHE.clear()
