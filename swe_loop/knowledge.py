"""Knowledge notes and playbooks as files. Loaded from disk; created on the org only in the
apply-config step, never implicitly."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = ROOT / "knowledge"
PLAYBOOK_DIR = ROOT / "playbooks"
SCHEMA_DIR = ROOT / "schemas"
FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Note:
    name: str
    trigger_description: str
    body: str
    path: Path

    def to_payload(self, pinned_repo: str | None = None) -> dict[str, Any]:
        """The v3 create-note body, verified against the published v3 OpenAPI spec: name, body,
        trigger (required); pinned_repo so the note is retrieved for this repository only."""
        p: dict[str, Any] = {
            "name": self.name,
            "body": self.body,
            "trigger": self.trigger_description,
        }
        if pinned_repo:
            p["pinned_repo"] = pinned_repo
        return p


def load_notes(directory: Path | str = KNOWLEDGE_DIR) -> list[Note]:
    notes: list[Note] = []
    for f in sorted(Path(directory).glob("*.md")):
        m = FRONT.match(f.read_text())
        if not m:
            raise ValueError(f"{f}: missing front matter")
        meta = yaml.safe_load(m.group(1)) or {}
        if not meta.get("name") or not meta.get("trigger_description"):
            raise ValueError(f"{f}: front matter needs name and trigger_description")
        notes.append(Note(meta["name"], meta["trigger_description"], m.group(2).strip(), f))
    return notes


@dataclass(frozen=True)
class Playbook:
    name: str
    body: str
    structured_output_schema: dict[str, Any] | None
    path: Path

    def to_payload(self) -> dict[str, Any]:
        """The v3 create-playbook body, verified against the published v3 OpenAPI spec: title and
        body (required), structured_output_schema, and a macro so a prompt can invoke it by name."""
        p: dict[str, Any] = {"title": self.name, "body": self.body, "macro": self.macro}
        if self.structured_output_schema:
            p["structured_output_schema"] = self.structured_output_schema
        return p

    @property
    def macro(self) -> str:
        return "!" + "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in self.path.stem)


def load_playbook(path: Path | str, schema: Path | str | None = None) -> Playbook:
    path = Path(path)
    body = path.read_text()
    title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), path.stem)
    sch = json.loads(Path(schema).read_text()) if schema else None
    return Playbook(title, body, sch, path)
