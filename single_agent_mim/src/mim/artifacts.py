"""Artifact I/O — run directories, JSON/JSONL/YAML persistence, manifest.

Contains zero Agent business logic.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .schemas import hash_dict


class RunDir:
    """Manages a single run's output directory.

    Guards against overwriting an existing run with the same ID.
    """

    def __init__(self, run_id: str, base_dir: str | Path = "outputs"):
        self.run_id = run_id
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / run_id
        self._manifest: dict[str, Any] = {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def create(cls, run_id: str, base_dir: str | Path = "outputs") -> "RunDir":
        rd = cls(run_id, base_dir)
        if rd.path.exists():
            raise FileExistsError(
                f"Run directory already exists: {rd.path}. "
                f"Use a different --run-id or remove the old directory."
            )
        os.makedirs(rd.path, exist_ok=False)
        return rd

    # ── Sub-directories ────────────────────────────────────────

    def sub(self, name: str) -> Path:
        p = self.path / name
        os.makedirs(p, exist_ok=True)
        return p

    def memory_dir(self, conv_id: str) -> Path:
        p = self.path / "memory" / conv_id
        os.makedirs(p, exist_ok=True)
        return p

    def skills_dir(self) -> Path:
        return self.sub("skills")

    def failures_dir(self) -> Path:
        return self.sub("failures")

    def candidates_dir(self) -> Path:
        return self.sub("candidates")

    def replays_dir(self) -> Path:
        return self.sub("replays")

    # ── Write helpers ──────────────────────────────────────────

    def write_json(self, rel_path: str, data: Any, indent: int = 2):
        full = self.path / rel_path
        os.makedirs(full.parent, exist_ok=True)
        tmp = str(full) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, full)

    def write_jsonl(self, rel_path: str, items: list[dict]):
        full = self.path / rel_path
        os.makedirs(full.parent, exist_ok=True)
        tmp = str(full) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp, full)

    def append_jsonl(self, rel_path: str, item: dict):
        full = self.path / rel_path
        os.makedirs(full.parent, exist_ok=True)
        with open(full, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def write_yaml(self, rel_path: str, data: Any):
        full = self.path / rel_path
        os.makedirs(full.parent, exist_ok=True)
        tmp = str(full) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, full)

    def write_text(self, rel_path: str, text: str):
        full = self.path / rel_path
        os.makedirs(full.parent, exist_ok=True)
        tmp = str(full) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, full)

    # ── Manifest ───────────────────────────────────────────────

    def update_manifest(self, **kwargs: Any):
        self._manifest.update(kwargs)

    def save_manifest(self):
        self._manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.write_json("manifest.json", self._manifest)
