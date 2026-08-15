"""Component-isolated, atomic artifacts for diagnosis runs."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .schemas import DiagnosisStatus, DiagnosisType


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiagnosisArtifactStore:
    """The only writer for one diagnosis component."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        component: str,
        resume: bool,
    ):
        if component not in {"answer", "access", "cons"}:
            raise ValueError(f"Unknown diagnosis component: {component}")
        directory_name = {
            "answer": "answer_failure",
            "access": "access_failure",
            "cons": "cons_failure",
        }[component]
        self.component = component
        self.root = Path(output_root) / directory_name
        if self.root.exists() and not resume and any(self.root.iterdir()):
            raise FileExistsError(
                f"{self.root} already contains artifacts; use --resume."
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self.progress_path = self.root / "progress.jsonl"
        self.errors_path = self.root / "errors.jsonl"
        self.summary_path = self.root / "summary.json"
        self.manifest_path = self.root / "manifest.json"
        self.answer_failures_path = self.root / "answer_failures.jsonl"
        self.packages_root = self.root / "packages"

    def completed_keys(self) -> set[str]:
        completed: set[str] = set()
        for row in self._read_jsonl(self.progress_path):
            # Runner-level data errors (which have no diagnosis type) are
            # deliberately retryable: they commonly come from transient
            # local-store locks.  A component may also intentionally emit a
            # terminal DATA_ERROR report with a diagnosis type (for example,
            # unsupported provenance); that report is safe to skip on resume.
            status = row.get("status")
            retryable_error = (
                status == DiagnosisStatus.DATA_ERROR.value
                and not row.get("diagnosis_type")
            )
            if status == DiagnosisStatus.COMPLETED.value or (
                status == DiagnosisStatus.DATA_ERROR.value and not retryable_error
            ):
                completed.add(self.key(row["conversation_id"], row["qa_id"]))
        return completed

    def publish(self, report: BaseModel) -> None:
        data = report.model_dump(mode="json")
        status = str(data["status"])
        diagnosis_type = str(data["diagnosis_type"])

        progress = {
            "timestamp": utc_now(),
            "component": self.component,
            "diagnosis_id": data["diagnosis_id"],
            "conversation_id": data["conversation_id"],
            "qa_id": data["qa_id"],
            "status": status,
            "diagnosis_type": diagnosis_type,
            "problem_found": bool(data.get("problem_found", False)),
            "review_required": bool(data.get("review_required", False)),
        }
        self._append_jsonl(self.progress_path, progress)

        if status != DiagnosisStatus.COMPLETED.value:
            self._append_jsonl(
                self.errors_path,
                {
                    **progress,
                    "reason": data.get("reason", ""),
                },
            )
            return

        if (
            self.component == "answer"
            and diagnosis_type == DiagnosisType.ANSWER_FAILURE.value
        ):
            self._append_jsonl(self.answer_failures_path, data)

        if (
            self.component in {"answer", "access", "cons"}
            and bool(data.get("problem_found"))
            and data.get("repair_package") is not None
        ):
            package_path = (
                self.packages_root
                / data["conversation_id"]
                / f"{data['qa_id']}_{self.component}_failure.json"
            )
            self._write_json(package_path, data)

    def publish_data_error(
        self,
        *,
        conversation_id: str,
        qa_id: str,
        reason: str,
    ) -> None:
        row = {
            "timestamp": utc_now(),
            "component": self.component,
            "conversation_id": conversation_id,
            "qa_id": qa_id,
            "status": DiagnosisStatus.DATA_ERROR.value,
            "reason": reason,
        }
        self._append_jsonl(self.progress_path, row)
        self._append_jsonl(self.errors_path, row)

    def write_manifest(self, value: dict[str, Any]) -> None:
        self._write_json(self.manifest_path, value)

    def write_summary(self, *, eligible: int, skipped_resume: int) -> None:
        # Recompute from the latest terminal row per QA so a resume converts
        # earlier errors into completed items instead of double-counting them.
        latest: dict[str, dict[str, Any]] = {}
        for row in self._read_jsonl(self.progress_path):
            conversation_id = str(row.get("conversation_id", ""))
            qa_id = str(row.get("qa_id", ""))
            if conversation_id and qa_id:
                latest[self.key(conversation_id, qa_id)] = row
        counts: Counter[str] = Counter()
        for row in latest.values():
            counts["processed"] += 1
            counts[f"status:{row.get('status', 'unknown')}"] += 1
            if row.get("diagnosis_type"):
                counts[f"type:{row['diagnosis_type']}"] += 1
        if self.packages_root.exists():
            counts["packages"] = sum(
                1 for _ in self.packages_root.rglob("*.json")
            )

        self._write_json(
            self.summary_path,
            {
                "component": self.component,
                "eligible": eligible,
                "skipped_resume": skipped_resume,
                "counts": dict(sorted(counts.items())),
                "completed_at": utc_now(),
            },
        )

    @staticmethod
    def key(conversation_id: str, qa_id: str) -> str:
        return f"{conversation_id}\0{qa_id}"

    @staticmethod
    def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
            handle.flush()

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows
