"""Configuration: the target seam (a YAML file) plus runtime settings from the environment.

The pipeline knows nothing about the target repository except what the YAML says.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    mode: str = "replay"  # replay | live
    config_path: Path = Path("configs/superset-pandas3.yaml")
    db_path: Path = Path("data/live/swe_loop.sqlite")
    replay_dir: Path = Path("data/replay")
    devin_api_key: str = ""
    devin_org_id: str = ""
    github_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("SWE_LOOP_MODE", "replay").strip().lower()
        if mode == "live" and not os.environ.get("DEVIN_API_KEY"):
            mode = "replay"  # no key means no sessions, ever
        return cls(
            mode=mode,
            config_path=Path(os.environ.get("SWE_LOOP_CONFIG", cls.config_path)),
            db_path=Path(os.environ.get("SWE_LOOP_DB", cls.db_path)),
            replay_dir=Path(os.environ.get("SWE_LOOP_REPLAY_DIR", cls.replay_dir)),
            devin_api_key=os.environ.get("DEVIN_API_KEY", ""),
            devin_org_id=os.environ.get("DEVIN_ORG_ID", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
        )

    @property
    def live(self) -> bool:
        return self.mode == "live"


@dataclass(frozen=True)
class TargetConfig:
    """The seam. One file per (repository, migration)."""

    name: str
    repo: str
    upstream: str
    base_branch: str
    default_branch: str
    language: str
    trigger: dict[str, Any] = field(default_factory=dict)
    detector: dict[str, Any] = field(default_factory=dict)
    acceptance: dict[str, Any] = field(default_factory=dict)
    router: dict[str, Any] = field(default_factory=dict)
    session: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> TargetConfig:
        data = yaml.safe_load(Path(path).read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def forbidden_paths(self) -> list[str]:
        return list(self.router.get("forbidden_paths", []))

    @property
    def max_acu_limit(self) -> int:
        return int(self.session.get("max_acu_limit", 6))
