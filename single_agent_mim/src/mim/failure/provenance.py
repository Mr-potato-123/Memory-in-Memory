"""Deterministic read-only data preparation for the two diagnosis agents."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


class ProvenanceService:
    """Read exact search steps and source-linked construction history."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def access_search_chain(self, access_run_id: str) -> list[dict[str, Any]]:
        """Return each Access action and all data returned by that action."""
        rows = self._conn.execute(
            """SELECT action_id, step_index, action_type,
                      request_json, response_json
               FROM access_actions
               WHERE access_run_id=?
               ORDER BY step_index, action_id""",
            (access_run_id,),
        ).fetchall()

        steps: list[dict[str, Any]] = []
        for row in rows:
            request = self._json_object(row["request_json"])
            response = self._json_object(row["response_json"])
            returned_memories: list[dict[str, Any]] = []
            if isinstance(response.get("hits"), list):
                returned_memories = [
                    item for item in response["hits"]
                    if isinstance(item, dict)
                ]
            elif isinstance(response.get("versions"), list):
                returned_memories = [
                    item for item in response["versions"]
                    if isinstance(item, dict)
                ]

            steps.append({
                "action_id": row["action_id"],
                "step_index": row["step_index"],
                "action_type": row["action_type"],
                "request": request,
                "response": response,
                "returned_memories": returned_memories,
                "returned_version_ids": [
                    item["version_id"]
                    for item in returned_memories
                    if item.get("version_id")
                ],
            })
        return steps

    def construction_history(
        self,
        *,
        conversation_id: str,
        message_ids: list[str],
        snapshot_commit_id: int,
    ) -> dict[str, Any]:
        """Return the full chronological history affected by source messages."""
        if not message_ids:
            return self._empty_construction_history()

        placeholders = ",".join("?" for _ in message_ids)
        processed_rows = self._conn.execute(
            f"""SELECT ci.message_id, ci.commit_id
                FROM construction_inputs ci
                JOIN construction_commits cc ON cc.commit_id=ci.commit_id
                WHERE cc.conversation_id=?
                  AND ci.message_id IN ({placeholders})
                ORDER BY ci.commit_id, ci.message_id""",
            [conversation_id, *message_ids],
        ).fetchall()

        # LEFT JOIN preserves candidates even when the decision was missing.
        # SKIP decisions are deliberately included.
        candidate_rows = self._conn.execute(
            f"""SELECT cme.message_id, mc.candidate_id, mc.commit_id,
                       mc.memory_kind, mc.subject, mc.content,
                       mc.world_start, mc.world_end,
                       cd.decision_id, cd.decision_index, cd.action,
                       cd.target_memory_id, cd.update_type,
                       cd.result_version_id, cd.reason,
                       cd.validation_status, cd.validation_errors
                FROM candidate_message_edges cme
                JOIN memory_candidates mc
                  ON mc.candidate_id=cme.candidate_id
                LEFT JOIN construction_decisions cd
                  ON cd.candidate_id=mc.candidate_id
                WHERE mc.conversation_id=?
                  AND cme.message_id IN ({placeholders})
                ORDER BY mc.commit_id, mc.candidate_id, cd.decision_index""",
            [conversation_id, *message_ids],
        ).fetchall()

        wanted_messages = set(message_ids)
        raw_change_rows = self._conn.execute(
            """SELECT mce.change_id, mce.commit_id, mce.decision_id,
                      mce.operation, mce.new_version_id,
                      mce.changed_fields_json, mce.direct_message_ids,
                      mce.affected_message_ids, mce.created_at
               FROM memory_change_events mce
               JOIN construction_commits cc ON cc.commit_id=mce.commit_id
               WHERE cc.conversation_id=? AND mce.commit_id<=?
               ORDER BY mce.commit_id, mce.change_id""",
            (conversation_id, snapshot_commit_id),
        ).fetchall()

        change_events: list[dict[str, Any]] = []
        for row in raw_change_rows:
            direct_ids = self._json_list(row["direct_message_ids"])
            affected_ids = self._json_list(row["affected_message_ids"])
            if not wanted_messages.intersection(
                set(direct_ids) | set(affected_ids)
            ):
                continue

            parent_rows = self._conn.execute(
                """SELECT mcp.parent_version_id AS version_id,
                          mv.memory_id, mv.version_no, mv.content,
                          mv.world_start, mv.world_end, mv.memory_kind
                   FROM memory_change_parents mcp
                   JOIN memory_versions mv
                     ON mv.version_id=mcp.parent_version_id
                   WHERE mcp.change_id=?
                   ORDER BY mv.memory_id, mv.version_no""",
                (row["change_id"],),
            ).fetchall()

            after_version = None
            if row["new_version_id"]:
                after_row = self._conn.execute(
                    """SELECT version_id, memory_id, version_no, content,
                              world_start, world_end, memory_kind, update_type
                       FROM memory_versions WHERE version_id=?""",
                    (row["new_version_id"],),
                ).fetchone()
                if after_row is not None:
                    after_version = dict(after_row)

            # Older databases may lack explicit change-parent rows. Recover
            # the immediately preceding version of the same logical memory
            # deterministically so Cons can still compare before and after.
            if (
                not parent_rows
                and str(row["operation"]).upper() in {"UPDATE", "MERGE"}
                and after_version is not None
            ):
                fallback_rows = self._conn.execute(
                    """SELECT version_id, memory_id, version_no, content,
                              world_start, world_end, memory_kind
                       FROM memory_versions
                       WHERE memory_id=?
                         AND version_no<?
                       ORDER BY version_no DESC
                       LIMIT 1""",
                    (
                        after_version["memory_id"],
                        after_version["version_no"],
                    ),
                ).fetchall()
                parent_rows = fallback_rows

            change_events.append({
                "change_id": row["change_id"],
                "commit_id": row["commit_id"],
                "decision_id": row["decision_id"],
                "operation": row["operation"],
                "new_version_id": row["new_version_id"],
                "changed_fields": self._json_object(
                    row["changed_fields_json"]
                ),
                "direct_message_ids": direct_ids,
                "affected_message_ids": affected_ids,
                "created_at": row["created_at"],
                "before_versions": [dict(item) for item in parent_rows],
                "after_version": after_version,
            })

        snapshot_rows = self._conn.execute(
            f"""SELECT DISTINCT mv.version_id, mv.memory_id, mv.version_no,
                       mv.content, mv.memory_kind, mv.subject,
                       mv.world_start, mv.world_end, mv.update_type
                FROM memory_lineage_messages mlm
                JOIN memory_versions mv ON mv.version_id=mlm.version_id
                WHERE mv.conversation_id=?
                  AND mlm.message_id IN ({placeholders})
                  AND mv.system_from_commit<=?
                  AND (mv.system_to_commit IS NULL
                       OR mv.system_to_commit>?)
                ORDER BY mv.memory_id, mv.version_no""",
            [
                conversation_id,
                *message_ids,
                snapshot_commit_id,
                snapshot_commit_id,
            ],
        ).fetchall()

        return {
            "processed_commits": [dict(row) for row in processed_rows],
            "candidates": [dict(row) for row in candidate_rows],
            "change_events": change_events,
            "snapshot_memories": [dict(row) for row in snapshot_rows],
        }

    @staticmethod
    def _empty_construction_history() -> dict[str, list]:
        return {
            "processed_commits": [],
            "candidates": [],
            "change_events": [],
            "snapshot_memories": [],
        }

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        try:
            parsed = json.loads(value or "[]")
            if not isinstance(parsed, list):
                return []
            return [str(item) for item in parsed]
        except (TypeError, json.JSONDecodeError):
            return []
