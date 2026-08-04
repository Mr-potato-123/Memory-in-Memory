"""Physically isolated storage for official and candidate Skills.

Runtime reads only ``official/``. Skill generation writes only
``candidates/access`` or ``candidates/construction``. A validated batch CRUD
transaction is the only path from candidates into the official Skill Bank.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .models import CandidateStatus, SkillCandidate, SkillPayload


class SkillRecord:
    """One immutable official Skill revision."""

    def __init__(
        self,
        skill_id: str,
        version: int,
        side: str,
        status: str,
        payload: SkillPayload,
        parent_version_id: str | None = None,
        created_from_failure_id: str = "",
        bank_version_created: int | None = None,
        created_at: str = "",
    ):
        self.skill_id = skill_id
        self.version = version
        self.side = side
        self.status = status
        self.payload = payload
        self.parent_version_id = parent_version_id
        self.created_from_failure_id = created_from_failure_id
        self.bank_version_created = bank_version_created
        self.created_at = created_at or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )

    @property
    def version_id(self) -> str:
        return f"{self.skill_id}_v{self.version}"

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "side": self.side,
            "status": self.status,
            "payload": self.payload.model_dump(),
            "parent_version_id": self.parent_version_id,
            "created_from_failure_id": self.created_from_failure_id,
            "bank_version_created": self.bank_version_created,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillRecord":
        return cls(
            skill_id=data["skill_id"],
            version=int(data["version"]),
            side=data["side"],
            status=data["status"],
            payload=SkillPayload(**data.get("payload", {})),
            parent_version_id=data.get("parent_version_id"),
            created_from_failure_id=data.get(
                "created_from_failure_id", ""
            ),
            bank_version_created=data.get("bank_version_created"),
            created_at=data.get("created_at", ""),
        )


class SkillRepository:
    """Official bank plus side-isolated candidate and transaction stores.

    Layout::

        skills/
          official/
            banks/bank_v000.json
            selected.json
          candidates/
            access/<candidate_id>/
            construction/<candidate_id>/
          transactions/<transaction_id>.json
    """

    def __init__(self, skills_dir: str | Path):
        self._dir = Path(skills_dir)
        self._official_dir = self._dir / "official"
        self._banks_dir = self._official_dir / "banks"
        self._candidates_dir = self._dir / "candidates"
        self._transactions_dir = self._dir / "transactions"
        self._banks_dir.mkdir(parents=True, exist_ok=True)
        for side in ("access", "construction"):
            (self._candidates_dir / side).mkdir(
                parents=True, exist_ok=True
            )
        self._transactions_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_layout()
        self._bank_version = self._load_latest_version()
        self._active: dict[str, SkillRecord] = {}
        self._load_current_bank()

    @property
    def current_version(self) -> str:
        return f"v{self._bank_version:03d}"

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def official_directory(self) -> Path:
        return self._official_dir

    @property
    def candidates_directory(self) -> Path:
        return self._candidates_dir

    def get(
        self, skill_id: str, version: int | None = None
    ) -> SkillRecord | None:
        if version is None:
            return self._active.get(skill_id)
        for bank_version in range(self._bank_version, -1, -1):
            path = self._bank_path(bank_version)
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("skills", []):
                if (
                    item.get("skill_id") == skill_id
                    and int(item.get("version", 0)) == version
                ):
                    return SkillRecord.from_dict(item)
        return None

    def list_active(self, side: str | None = None) -> list[SkillRecord]:
        result = list(self._active.values())
        if side is not None:
            result = [record for record in result if record.side == side]
        return result

    def save_candidate(self, candidate: SkillCandidate) -> Path:
        """Save a candidate snapshot without touching official files."""
        candidate.status = CandidateStatus.STAGED
        directory = self._candidate_dir(
            candidate.side, candidate.candidate_id
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "candidate.json"
        self._atomic_json(path, candidate.model_dump(mode="json"))
        return path

    def save_candidate_revision(
        self,
        candidate: SkillCandidate,
        *,
        attempt_id: str,
        revision_kind: str,
    ) -> None:
        directory = self._candidate_dir(
            candidate.side, candidate.candidate_id
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "revisions.jsonl"
        row = {
            "attempt_id": attempt_id,
            "revision_kind": revision_kind,
            "candidate": candidate.model_dump(mode="json"),
            "created_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def list_candidates(self, side: str) -> list[SkillCandidate]:
        if side not in {"access", "construction"}:
            raise ValueError(f"Unknown Skill side: {side}")
        result: list[SkillCandidate] = []
        for path in sorted(
            (self._candidates_dir / side).glob("*/candidate.json")
        ):
            result.append(
                SkillCandidate(
                    **json.loads(path.read_text(encoding="utf-8"))
                )
            )
        return result

    def stage_create(self, candidate: SkillCandidate) -> SkillRecord:
        """Store a candidate and prepare an unpublished create record."""
        self.save_candidate(candidate)
        record = SkillRecord(
            skill_id=candidate.skill_id,
            version=1,
            side=candidate.side,
            status="staged",
            payload=candidate.payload,
            created_from_failure_id=(
                candidate.source_diagnosis_id
                or candidate.source_failure_id
            ),
        )
        self._append_candidate_record(candidate, record)
        return record

    def stage_update(
        self, skill_id: str, candidate: SkillCandidate
    ) -> SkillRecord:
        """Store a candidate and prepare an unpublished update record."""
        existing = self._active.get(skill_id)
        new_version = existing.version + 1 if existing else 1
        candidate.skill_id = skill_id
        candidate.version = new_version
        self.save_candidate(candidate)
        record = SkillRecord(
            skill_id=skill_id,
            version=new_version,
            side=candidate.side,
            status="staged",
            payload=candidate.payload,
            parent_version_id=(
                f"{skill_id}_v{new_version - 1}"
                if new_version > 1
                else None
            ),
            created_from_failure_id=(
                candidate.source_diagnosis_id
                or candidate.source_failure_id
            ),
        )
        self._append_candidate_record(candidate, record)
        return record

    def tombstone(
        self, skill_id: str, reason: str
    ) -> SkillRecord | None:
        existing = self._active.get(skill_id)
        if existing is None:
            return None
        record = copy.deepcopy(existing)
        record.status = "tombstoned"
        return record

    def publish(self, staged_record: SkillRecord) -> str:
        """Compatibility wrapper around one-record batch publication."""
        return self.publish_batch(
            [staged_record],
            transaction_id=f"single_{staged_record.version_id}",
        )

    def seed_initial(self, records: list[SkillRecord]) -> None:
        """Install a prior published Bank as immutable version zero.

        This is one initialization operation, not one publication per Skill.
        Later versions therefore describe only the current learning round.
        """
        if self._bank_version != 0 or self._active:
            raise RuntimeError("Initial Bank can only seed an empty repository.")
        active: dict[str, SkillRecord] = {}
        for record in records:
            if record.status != "active":
                continue
            if record.side not in {"access", "construction"}:
                raise ValueError(f"Unknown Skill side: {record.side}")
            if record.skill_id in active:
                raise ValueError(f"Duplicate initial Skill: {record.skill_id}")
            active[record.skill_id] = copy.deepcopy(record)
        self._save_bank(0, list(active.values()))
        self._active = active
        self._update_selected()

    def publish_batch(
        self,
        staged_records: list[SkillRecord],
        *,
        transaction_id: str,
        tombstone_skill_ids: list[str] | None = None,
        transaction_payload: dict | None = None,
    ) -> str:
        """Publish many creates/updates/deletes as one official version."""
        new_active = copy.deepcopy(self._active)
        for skill_id in tombstone_skill_ids or []:
            new_active.pop(skill_id, None)
        for record in staged_records:
            record.status = "active"
            record.bank_version_created = self._bank_version + 1
            new_active[record.skill_id] = record

        new_version = self._bank_version + 1
        self._save_bank(new_version, list(new_active.values()))
        self._active = new_active
        self._bank_version = new_version
        self._update_selected()
        self._save_transaction(
            transaction_id,
            {
                "transaction_id": transaction_id,
                "base_bank_version": f"v{new_version - 1:03d}",
                "published_bank_version": f"v{new_version:03d}",
                "published_skill_version_ids": [
                    record.version_id for record in staged_records
                ],
                "deleted_skill_ids": tombstone_skill_ids or [],
                "payload": transaction_payload or {},
            },
        )
        return self.current_version

    def select_version(self, version: int) -> Path:
        source = self._bank_path(version)
        if not source.exists():
            raise FileNotFoundError(
                f"Skill bank version does not exist: {source}"
            )
        destination = self._official_dir / "selected.json"
        shutil.copy2(source, destination)
        return destination

    def build_staging_bank(
        self, candidate: SkillCandidate
    ) -> "SkillRepository":
        """Build an isolated repository for replay; never mutate official."""
        import tempfile

        repository = SkillRepository(tempfile.mkdtemp(prefix="mim_skill_"))
        repository._active = copy.deepcopy(self._active)
        repository._bank_version = self._bank_version
        existing = repository._active.get(candidate.skill_id)
        record = SkillRecord(
            skill_id=candidate.skill_id,
            version=(
                existing.version + 1
                if existing is not None
                else candidate.version
            ),
            side=candidate.side,
            status="staged",
            payload=candidate.payload,
            created_from_failure_id=(
                candidate.source_diagnosis_id
                or candidate.source_failure_id
            ),
        )
        repository._active[record.skill_id] = record
        return repository

    @contextmanager
    def staging_bank(self, candidate: SkillCandidate):
        repository = self.build_staging_bank(candidate)
        try:
            yield repository
        finally:
            shutil.rmtree(repository.directory, ignore_errors=True)

    def _load_latest_version(self) -> int:
        versions = []
        for path in self._banks_dir.glob("bank_v*.json"):
            try:
                versions.append(int(path.stem.removeprefix("bank_v")))
            except ValueError:
                continue
        if not versions:
            self._save_bank(0, [])
            return 0
        return max(versions)

    def _load_current_bank(self) -> None:
        path = self._bank_path(self._bank_version)
        data = json.loads(path.read_text(encoding="utf-8"))
        self._active = {
            record.skill_id: record
            for record in (
                SkillRecord.from_dict(item)
                for item in data.get("skills", [])
            )
            if record.status == "active"
        }

    def _save_bank(
        self, version: int, skills: list[SkillRecord]
    ) -> None:
        self._atomic_json(
            self._bank_path(version),
            {
                "version": version,
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "previous_version": version - 1 if version > 0 else None,
                "skills": [record.to_dict() for record in skills],
            },
        )

    def _update_selected(self) -> None:
        shutil.copy2(
            self._bank_path(self._bank_version),
            self._official_dir / "selected.json",
        )

    def _append_candidate_record(
        self, candidate: SkillCandidate, record: SkillRecord
    ) -> None:
        directory = self._candidate_dir(
            candidate.side, candidate.candidate_id
        )
        path = directory / "staged_records.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
            )

    def _candidate_dir(self, side: str, candidate_id: str) -> Path:
        if side not in {"access", "construction"}:
            raise ValueError(f"Unknown Skill side: {side}")
        return self._candidates_dir / side / candidate_id

    def _bank_path(self, version: int) -> Path:
        return self._banks_dir / f"bank_v{version:03d}.json"

    def _save_transaction(
        self, transaction_id: str, payload: dict
    ) -> Path:
        safe = "".join(
            char
            for char in transaction_id
            if char.isalnum() or char in "-_"
        )
        if not safe:
            raise ValueError("transaction_id contains no safe characters")
        path = self._transactions_dir / f"{safe}.json"
        self._atomic_json(path, payload)
        return path

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = str(path) + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    def _migrate_legacy_layout(self) -> None:
        """Copy old runtime files into official/ without deleting originals."""
        legacy_banks = self._dir / "banks"
        if legacy_banks.exists() and not any(
            self._banks_dir.glob("bank_v*.json")
        ):
            for source in legacy_banks.glob("bank_v*.json"):
                shutil.copy2(source, self._banks_dir / source.name)
        legacy_selected = self._dir / "selected.json"
        official_selected = self._official_dir / "selected.json"
        if legacy_selected.exists() and not official_selected.exists():
            shutil.copy2(legacy_selected, official_selected)
