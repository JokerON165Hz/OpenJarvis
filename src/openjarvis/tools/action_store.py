"""SQLite persistence for structured tool proposals, actions, and artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from openjarvis.tools.actions import (
    ActionStatus,
    ToolAction,
    ToolArtifact,
    ToolEvent,
    ToolProposal,
    VerificationStatus,
    utc_now,
)


class ActionStoreError(RuntimeError):
    pass


class ActionIdempotencyConflict(ActionStoreError):
    pass


_ACTION_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PROPOSED: frozenset(
        {ActionStatus.VALIDATED, ActionStatus.DENIED, ActionStatus.CANCELED}
    ),
    ActionStatus.VALIDATED: frozenset(
        {
            ActionStatus.WAITING_APPROVAL,
            ActionStatus.RUNNING,
            ActionStatus.DENIED,
            ActionStatus.CANCELED,
        }
    ),
    ActionStatus.WAITING_APPROVAL: frozenset(
        {ActionStatus.RUNNING, ActionStatus.DENIED, ActionStatus.CANCELED}
    ),
    ActionStatus.RUNNING: frozenset(
        {
            ActionStatus.VERIFYING,
            ActionStatus.FAILED,
            ActionStatus.CANCELED,
            ActionStatus.RECOVERY_REQUIRED,
        }
    ),
    ActionStatus.VERIFYING: frozenset(
        {
            ActionStatus.VERIFIED,
            ActionStatus.FAILED,
            ActionStatus.CANCELED,
            ActionStatus.RECOVERY_REQUIRED,
        }
    ),
    ActionStatus.VERIFIED: frozenset({ActionStatus.COMPLETED, ActionStatus.CANCELED}),
    ActionStatus.DENIED: frozenset(),
    ActionStatus.COMPLETED: frozenset(),
    ActionStatus.FAILED: frozenset({ActionStatus.RUNNING, ActionStatus.CANCELED}),
    ActionStatus.CANCELED: frozenset(),
    ActionStatus.RECOVERY_REQUIRED: frozenset({ActionStatus.CANCELED}),
}


class ActionStore:
    """Thread-safe SQLite persistence with cross-process execution claims."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(task_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS tool_actions (
                    action_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL REFERENCES tool_proposals(proposal_id),
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_actions_task
                    ON tool_actions(task_id);
                CREATE INDEX IF NOT EXISTS idx_tool_actions_proposal
                    ON tool_actions(proposal_id);
                CREATE TABLE IF NOT EXISTS tool_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL REFERENCES tool_actions(action_id),
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_artifacts_action
                    ON tool_artifacts(action_id);
                CREATE TABLE IF NOT EXISTS tool_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    action_id TEXT NOT NULL REFERENCES tool_actions(action_id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_events_action
                    ON tool_events(action_id, sequence);
                """
            )

    @staticmethod
    def _json(model) -> str:
        return model.model_dump_json()

    @staticmethod
    def _hash_json(payload_json: str) -> str:
        value = json.loads(payload_json)
        # Generated identifiers and timestamps do not change operation identity.
        value.pop("proposal_id", None)
        value.pop("created_at", None)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _begin_immediate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.commit()

    def _rollback(self) -> None:
        self._conn.rollback()

    def _action_locked(self, action_id: str) -> ToolAction | None:
        row = self._conn.execute(
            "SELECT payload_json FROM tool_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return ToolAction.model_validate_json(row[0]) if row else None

    def _proposal_action_locked(self, proposal_id: str) -> ToolAction | None:
        row = self._conn.execute(
            "SELECT payload_json FROM tool_actions WHERE proposal_id = ? "
            "ORDER BY rowid LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return ToolAction.model_validate_json(row[0]) if row else None

    def _save_action_locked(self, action: ToolAction) -> None:
        self._conn.execute(
            "UPDATE tool_actions SET payload_json = ? WHERE action_id = ?",
            (self._json(action), action.action_id),
        )

    @staticmethod
    def _with_changes(
        current: ToolAction,
        status: ActionStatus,
        *,
        verification_status: VerificationStatus | None = None,
        approval_id: str | None = None,
        tool_run_id: str | None = None,
        output_summary: str | None = None,
        error: str | None = None,
        effect_known: bool | None = None,
        retry_count: int | None = None,
        clear_error: bool = False,
    ) -> ToolAction:
        changes: dict[str, object] = {"status": status, "updated_at": utc_now()}
        optional = {
            "verification_status": verification_status,
            "approval_id": approval_id,
            "tool_run_id": tool_run_id,
            "output_summary": output_summary,
            "error": error,
            "effect_known": effect_known,
            "retry_count": retry_count,
        }
        changes.update({key: value for key, value in optional.items() if value is not None})
        if clear_error:
            changes["error"] = ""
        return current.model_copy(update=changes)

    @staticmethod
    def _check_transition(current: ToolAction, status: ActionStatus) -> None:
        if status == current.status:
            return
        if status not in _ACTION_TRANSITIONS[current.status]:
            raise ActionStoreError(
                "invalid action transition "
                f"{current.status.value} -> {status.value}"
            )

    def put_proposal(self, proposal: ToolProposal) -> ToolProposal:
        """Atomically bind a task-scoped idempotency key to one proposal payload."""

        payload = self._json(proposal)
        payload_hash = self._hash_json(payload)
        with self._lock:
            self._begin_immediate()
            try:
                existing = self._conn.execute(
                    "SELECT payload_hash, payload_json FROM tool_proposals "
                    "WHERE task_id = ? AND idempotency_key = ?",
                    (proposal.task_id, proposal.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise ActionIdempotencyConflict(
                            "idempotency key was reused with a different proposal"
                        )
                    result = ToolProposal.model_validate_json(existing["payload_json"])
                    self._commit()
                    return result
                self._conn.execute(
                    "INSERT INTO tool_proposals "
                    "(proposal_id, task_id, idempotency_key, payload_hash, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        proposal.proposal_id,
                        proposal.task_id,
                        proposal.idempotency_key,
                        payload_hash,
                        payload,
                    ),
                )
                self._commit()
                return proposal
            except Exception:
                self._rollback()
                raise

    def get_proposal(self, proposal_id: str) -> ToolProposal | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM tool_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return ToolProposal.model_validate_json(row[0]) if row else None

    def put_action_once(self, action: ToolAction) -> tuple[ToolAction, bool]:
        """Insert one canonical action for a proposal, returning whether it was new."""

        with self._lock:
            self._begin_immediate()
            try:
                existing = self._action_locked(action.action_id)
                if existing is None:
                    existing = self._proposal_action_locked(action.proposal_id)
                if existing is not None:
                    if (
                        existing.task_id != action.task_id
                        or existing.idempotency_key != action.idempotency_key
                        or existing.tool_id != action.tool_id
                    ):
                        raise ActionStoreError("stable action id collided with another action")
                    self._commit()
                    return existing, False
                try:
                    self._conn.execute(
                        "INSERT INTO tool_actions "
                        "(action_id, task_id, proposal_id, payload_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            action.action_id,
                            action.task_id,
                            action.proposal_id,
                            self._json(action),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    existing = self._action_locked(action.action_id)
                    if existing is None:
                        existing = self._proposal_action_locked(action.proposal_id)
                    if existing is None:
                        raise ActionStoreError(str(exc)) from exc
                    self._commit()
                    return existing, False
                self._commit()
                return action, True
            except Exception:
                self._rollback()
                raise

    def put_action(self, action: ToolAction) -> ToolAction:
        return self.put_action_once(action)[0]

    def get_action(self, action_id: str) -> ToolAction | None:
        with self._lock:
            return self._action_locked(action_id)

    def get_action_by_proposal(self, proposal_id: str) -> ToolAction | None:
        with self._lock:
            return self._proposal_action_locked(proposal_id)

    def list_actions(self, task_id: str) -> tuple[ToolAction, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM tool_actions WHERE task_id = ? "
                "ORDER BY rowid",
                (task_id,),
            ).fetchall()
        return tuple(ToolAction.model_validate_json(row[0]) for row in rows)

    def list_actions_by_status(
        self,
        statuses: frozenset[ActionStatus] | set[ActionStatus],
    ) -> tuple[ToolAction, ...]:
        """Return persisted actions in the requested states in stable order."""

        if not statuses:
            return ()
        values = tuple(sorted(status.value for status in statuses))
        placeholders = ", ".join("?" for _value in values)
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM tool_actions "
                f"WHERE json_extract(payload_json, '$.status') IN ({placeholders}) "
                "ORDER BY rowid",
                values,
            ).fetchall()
        return tuple(ToolAction.model_validate_json(row[0]) for row in rows)

    def transition(
        self,
        action_id: str,
        status: ActionStatus,
        *,
        verification_status: VerificationStatus | None = None,
        approval_id: str | None = None,
        tool_run_id: str | None = None,
        output_summary: str | None = None,
        error: str | None = None,
        effect_known: bool | None = None,
        retry_count: int | None = None,
        clear_error: bool = False,
    ) -> ToolAction:
        """Apply one state transition under a cross-process SQLite write lock."""

        with self._lock:
            self._begin_immediate()
            try:
                current = self._action_locked(action_id)
                if current is None:
                    raise ActionStoreError(f"unknown action: {action_id}")
                self._check_transition(current, status)
                updated = self._with_changes(
                    current,
                    status,
                    verification_status=verification_status,
                    approval_id=approval_id,
                    tool_run_id=tool_run_id,
                    output_summary=output_summary,
                    error=error,
                    effect_known=effect_known,
                    retry_count=retry_count,
                    clear_error=clear_error,
                )
                self._save_action_locked(updated)
                self._commit()
                return updated
            except Exception:
                self._rollback()
                raise

    def claim_execution(
        self,
        action_id: str,
        *,
        tool_run_id: str,
        allow_failed: bool = False,
    ) -> tuple[ToolAction, bool]:
        """Atomically claim execution so only one process can start the effect."""

        with self._lock:
            self._begin_immediate()
            try:
                current = self._action_locked(action_id)
                if current is None:
                    raise ActionStoreError(f"unknown action: {action_id}")
                allowed = current.status == ActionStatus.VALIDATED or (
                    allow_failed
                    and current.status == ActionStatus.FAILED
                    and current.effect_known
                )
                if not allowed:
                    self._commit()
                    return current, False
                self._check_transition(current, ActionStatus.RUNNING)
                updated = current.model_copy(
                    update={
                        "status": ActionStatus.RUNNING,
                        "verification_status": VerificationStatus.PENDING,
                        "tool_run_id": tool_run_id,
                        "error": "",
                        "updated_at": utc_now(),
                    }
                )
                self._save_action_locked(updated)
                self._commit()
                return updated, True
            except Exception:
                self._rollback()
                raise

    def mark_recovery_required(self, action_id: str, *, error: str) -> ToolAction:
        """Persist an ambiguous effect without ever re-running it automatically."""

        with self._lock:
            self._begin_immediate()
            try:
                current = self._action_locked(action_id)
                if current is None:
                    raise ActionStoreError(f"unknown action: {action_id}")
                if current.status == ActionStatus.RECOVERY_REQUIRED:
                    self._commit()
                    return current
                if current.status not in {ActionStatus.RUNNING, ActionStatus.VERIFYING}:
                    raise ActionStoreError(
                        "recovery can only capture running or verifying actions"
                    )
                self._check_transition(current, ActionStatus.RECOVERY_REQUIRED)
                updated = current.model_copy(
                    update={
                        "status": ActionStatus.RECOVERY_REQUIRED,
                        "verification_status": VerificationStatus.UNKNOWN,
                        "error": error,
                        "effect_known": False,
                        "updated_at": utc_now(),
                    }
                )
                self._save_action_locked(updated)
                self._commit()
                return updated
            except Exception:
                self._rollback()
                raise

    def put_artifact(self, artifact: ToolArtifact) -> ToolArtifact:
        with self._lock, self._conn:
            if self.get_action(artifact.action_id) is None:
                raise ActionStoreError(f"unknown action: {artifact.action_id}")
            self._conn.execute(
                "INSERT INTO tool_artifacts "
                "(artifact_id, action_id, payload_json) VALUES (?, ?, ?)",
                (artifact.artifact_id, artifact.action_id, self._json(artifact)),
            )
        return artifact

    def list_artifacts(self, action_id: str) -> tuple[ToolArtifact, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM tool_artifacts WHERE action_id = ? "
                "ORDER BY rowid",
                (action_id,),
            ).fetchall()
        return tuple(ToolArtifact.model_validate_json(row[0]) for row in rows)

    def append_event(self, event: ToolEvent) -> ToolEvent:
        with self._lock, self._conn:
            if self.get_action(event.action_id) is None:
                raise ActionStoreError(f"unknown action: {event.action_id}")
            self._conn.execute(
                "INSERT INTO tool_events "
                "(event_id, action_id, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.action_id,
                    event.event_type,
                    self._json(event),
                ),
            )
        return event

    def list_events(self, action_id: str) -> tuple[ToolEvent, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM tool_events WHERE action_id = ? "
                "ORDER BY sequence",
                (action_id,),
            ).fetchall()
        return tuple(ToolEvent.model_validate_json(row[0]) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "ActionIdempotencyConflict",
    "ActionStore",
    "ActionStoreError",
]
