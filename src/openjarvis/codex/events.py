"""Normalization of SDK and app-server messages into stable Codex events."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from openjarvis.codex.redaction import redact_data
from openjarvis.codex.store import CodexStateStore
from openjarvis.codex.types import (
    CodexBackendKind,
    CodexEvent,
    CodexEventType,
    CodexRunContext,
)

_STEP_START_EVENTS = frozenset(
    {
        CodexEventType.ITEM_STARTED,
        CodexEventType.COMMAND_STARTED,
        CodexEventType.FILE_CHANGE_PROPOSED,
        CodexEventType.TOOL_STARTED,
    }
)

_DIRECT_EVENT_MAP = {
    "thread/started": CodexEventType.THREAD_STARTED,
    "thread/resumed": CodexEventType.THREAD_RESUMED,
    "thread/closed": CodexEventType.THREAD_CLOSED,
    "turn/started": CodexEventType.TURN_STARTED,
    "turn/plan/updated": CodexEventType.PLAN_UPDATED,
    "thread/tokenUsage/updated": CodexEventType.USAGE_UPDATED,
    "error": CodexEventType.ERROR,
    "approval/requested": CodexEventType.APPROVAL_REQUESTED,
    "approval/resolved": CodexEventType.APPROVAL_RESOLVED,
}
_ITEM_DELTA_METHODS = {
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "item/fileChange/outputDelta",
}
_COMMAND_OUTPUT_METHODS = {
    "item/commandExecution/outputDelta",
    "command/exec/outputDelta",
}
_TOOL_ITEM_TYPES = {
    "mcpToolCall",
    "dynamicToolCall",
    "collabAgentToolCall",
}
_REPLAY_SAFE_METHODS = frozenset(
    {
        "thread/started",
        "thread/resumed",
        "thread/closed",
        "turn/started",
        "turn/completed",
        "item/started",
        "item/completed",
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "approval/requested",
        "approval/resolved",
        "thread/tokenUsage/updated",
        "error",
    }
)
_USAGE_KEYS = (
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
    "totalTokens",
)
_USAGE_KEY_ALIASES = {
    "inputTokens": ("inputTokens", "input_tokens"),
    "cachedInputTokens": ("cachedInputTokens", "cached_input_tokens"),
    "cacheWriteInputTokens": (
        "cacheWriteInputTokens",
        "cache_write_input_tokens",
    ),
    "outputTokens": ("outputTokens", "output_tokens"),
    "reasoningOutputTokens": (
        "reasoningOutputTokens",
        "reasoning_output_tokens",
    ),
    "totalTokens": ("totalTokens", "total_tokens"),
}


class CodexEventAdapter:
    """Create ordered, redacted, deduplicated events for one state store."""

    def __init__(self, store: CodexStateStore) -> None:
        self._store = store
        self._usage_baselines: dict[tuple[str, str], dict[str, int]] = {}

    def normalize(
        self,
        raw: Any,
        *,
        context: CodexRunContext,
        backend: CodexBackendKind,
        thread_id: str,
        turn_id: str | None = None,
    ) -> CodexEvent | None:
        """Normalize and persist one SDK notification or wire message."""

        method, params, explicit_event_id = self._unpack(raw)
        observed_thread_id = self._find_id(params, "threadId", "thread_id")
        observed_turn_id = self._find_id(params, "turnId", "turn_id")
        if observed_turn_id is None:
            nested_turn = params.get("turn")
            if isinstance(nested_turn, dict):
                observed_turn_id = self._find_id(nested_turn, "id")

        # The caller binds a stream to one owned thread/turn. Explicit foreign
        # correlation is stale or belongs to another concurrent stream and must
        # never be allowed to mutate this one.
        if observed_thread_id and observed_thread_id != thread_id:
            return None
        if turn_id and observed_turn_id and observed_turn_id != turn_id:
            return None

        actual_thread_id = observed_thread_id or thread_id
        actual_turn_id = observed_turn_id or turn_id
        item_id = self._find_id(params, "itemId", "item_id")
        if item_id is None:
            item = params.get("item")
            if isinstance(item, dict):
                item_id = self._find_id(item, "id")

        if method == "thread/tokenUsage/updated":
            params = self._augment_turn_usage(
                params,
                thread_id=actual_thread_id,
                turn_id=actual_turn_id,
            )
        if method in {"error", "turn/completed"}:
            params = self._augment_structured_error(params, method=method)

        event_type = self._event_type(method, params)
        replay_event_id = None
        if explicit_event_id:
            if self._store.has_event(explicit_event_id):
                return None
        elif method in _REPLAY_SAFE_METHODS:
            replay_event_id = self._stable_replay_event_id(
                method=method,
                thread_id=actual_thread_id,
                turn_id=actual_turn_id,
                item_id=item_id,
                params=params,
            )
            if self._store.has_event(replay_event_id):
                return None

        if event_type is CodexEventType.ERROR and method not in {
            "error",
            "turn/completed",
        }:
            payload: dict[str, Any] = {
                "source_event_type": method,
                "message": "Unsupported Codex event type was ignored safely",
            }
        else:
            payload = redact_data(params)

        sequence = self._store.next_sequence(actual_thread_id)
        event_id = explicit_event_id or replay_event_id or self._derived_event_id(
            method=method,
            thread_id=actual_thread_id,
            turn_id=actual_turn_id,
            item_id=item_id,
            sequence=sequence,
        )
        event = CodexEvent(
            event_id=event_id,
            sequence=sequence,
            occurred_at=self._occurred_at(params),
            task_id=context.task_id,
            session_id=context.session_id,
            thread_id=actual_thread_id,
            turn_id=actual_turn_id,
            item_id=item_id,
            backend=backend,
            event_type=event_type,
            payload=payload,
        )
        if not self._store.save_event(event):
            return None
        return event

    def emit(
        self,
        event_type: CodexEventType,
        *,
        context: CodexRunContext,
        backend: CodexBackendKind,
        thread_id: str,
        turn_id: str | None = None,
        item_id: str | None = None,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> CodexEvent | None:
        """Emit a backend-created lifecycle event through the same safeguards."""

        if event_id and self._store.has_event(event_id):
            return None
        sequence = self._store.next_sequence(thread_id)
        actual_event_id = event_id or self._derived_event_id(
            method=event_type.value,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            sequence=sequence,
        )
        event = CodexEvent(
            event_id=actual_event_id,
            sequence=sequence,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            task_id=context.task_id,
            session_id=context.session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            backend=backend,
            event_type=event_type,
            payload=redact_data(payload or {}),
        )
        if not self._store.save_event(event):
            return None
        return event

    @staticmethod
    def _unpack(raw: Any) -> tuple[str, dict[str, Any], str | None]:
        if isinstance(raw, dict):
            method = str(raw.get("method") or raw.get("event_type") or "")
            payload = raw.get("params", raw.get("payload", {}))
            params = payload if isinstance(payload, dict) else {}
            event_id = raw.get("eventId") or raw.get("event_id")
            return method, dict(params), str(event_id) if event_id else None

        method = str(getattr(raw, "method", ""))
        payload = getattr(raw, "payload", {})
        if hasattr(payload, "model_dump"):
            dumped = payload.model_dump(mode="json", by_alias=True)
            params = dumped if isinstance(dumped, dict) else {}
        elif isinstance(payload, dict):
            params = dict(payload)
        else:
            params = {}
        event_id = getattr(raw, "event_id", None)
        return method, params, str(event_id) if event_id else None

    @classmethod
    def _event_type(
        cls,
        method: str,
        params: dict[str, Any],
    ) -> CodexEventType:
        if method == "turn/completed":
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, dict) else None
            if status == "failed":
                return CodexEventType.TURN_FAILED
            if status == "interrupted":
                return CodexEventType.TURN_INTERRUPTED
            return CodexEventType.TURN_COMPLETED
        if method in _ITEM_DELTA_METHODS:
            return CodexEventType.ITEM_DELTA
        if method in _COMMAND_OUTPUT_METHODS:
            return CodexEventType.COMMAND_OUTPUT
        if method == "item/started":
            return cls._item_event_type(params, completed=False)
        if method == "item/completed":
            return cls._item_event_type(params, completed=True)
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return CodexEventType.APPROVAL_REQUESTED
        return _DIRECT_EVENT_MAP.get(method, CodexEventType.ERROR)

    @staticmethod
    def _item_event_type(
        params: dict[str, Any],
        *,
        completed: bool,
    ) -> CodexEventType:
        item = params.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type == "commandExecution":
            return (
                CodexEventType.COMMAND_COMPLETED
                if completed
                else CodexEventType.COMMAND_STARTED
            )
        if item_type == "fileChange":
            return (
                CodexEventType.FILE_CHANGE_APPLIED
                if completed
                else CodexEventType.FILE_CHANGE_PROPOSED
            )
        if item_type in _TOOL_ITEM_TYPES:
            return (
                CodexEventType.TOOL_COMPLETED
                if completed
                else CodexEventType.TOOL_STARTED
            )
        return (
            CodexEventType.ITEM_COMPLETED
            if completed
            else CodexEventType.ITEM_STARTED
        )

    @staticmethod
    def _find_id(data: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if value:
                return str(value)
        nested_turn = data.get("turn")
        if isinstance(nested_turn, dict):
            for key in keys:
                value = nested_turn.get(key)
                if value:
                    return str(value)
        return None

    @staticmethod
    def _occurred_at(params: dict[str, Any]) -> str:
        for key in ("occurredAt", "occurred_at", "timestamp"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        return datetime.now(timezone.utc).isoformat()

    def _augment_turn_usage(
        self,
        params: dict[str, Any],
        *,
        thread_id: str,
        turn_id: str | None,
    ) -> dict[str, Any]:
        """Add a cumulative turn snapshot beside the upstream thread snapshot."""

        if not turn_id:
            return params
        token_usage = params.get("tokenUsage") or params.get("token_usage")
        if not isinstance(token_usage, dict):
            return params
        total = token_usage.get("total")
        last = token_usage.get("last")
        total_values = self._usage_breakdown(total)
        if not total_values:
            return params

        key = (thread_id, turn_id)
        baseline = self._usage_baselines.get(key)
        if baseline is None:
            baseline = self._restore_usage_baseline(thread_id, turn_id)
        if baseline is None:
            last_values = self._usage_breakdown(last)
            if not last_values:
                # Without a prior persisted turn snapshot or a response delta we
                # cannot safely distinguish current-turn usage from cumulative
                # thread usage. Leave the event as thread-only rather than
                # over-counting the turn.
                return params
            baseline = {
                name: max(0, total_values.get(name, 0) - last_values.get(name, 0))
                for name in _USAGE_KEYS
            }
        self._usage_baselines[key] = baseline
        turn_usage = {
            name: max(0, total_values.get(name, 0) - baseline.get(name, 0))
            for name in _USAGE_KEYS
            if name in total_values or name in baseline
        }
        result = dict(params)
        result["turn_usage"] = turn_usage
        return result

    def _restore_usage_baseline(
        self,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, int] | None:
        try:
            events = self._store.list_events(thread_id)
        except Exception:
            return None
        for event in reversed(events):
            if event.turn_id != turn_id or event.event_type is not CodexEventType.USAGE_UPDATED:
                continue
            payload = event.payload
            token_usage = payload.get("tokenUsage") or payload.get("token_usage")
            turn_usage = payload.get("turn_usage") or payload.get("turnUsage")
            if not isinstance(token_usage, dict) or not isinstance(turn_usage, dict):
                continue
            total_values = self._usage_breakdown(token_usage.get("total"))
            turn_values = self._usage_breakdown(turn_usage)
            if total_values and turn_values:
                return {
                    name: max(
                        0,
                        total_values.get(name, 0) - turn_values.get(name, 0),
                    )
                    for name in _USAGE_KEYS
                }
        return None

    @staticmethod
    def _usage_breakdown(block: Any) -> dict[str, int]:
        if not isinstance(block, dict):
            return {}
        result: dict[str, int] = {}
        for canonical, aliases in _USAGE_KEY_ALIASES.items():
            for alias in aliases:
                value = block.get(alias)
                if isinstance(value, (int, float)) and value >= 0:
                    result[canonical] = int(value)
                    break
        return result

    @classmethod
    def _augment_structured_error(
        cls,
        params: dict[str, Any],
        *,
        method: str,
    ) -> dict[str, Any]:
        result = dict(params)
        if method == "turn/completed":
            turn = result.get("turn")
            if not isinstance(turn, dict):
                return result
            error = turn.get("error")
            if not isinstance(error, dict):
                return result
            normalized = cls._structured_error(error)
            new_turn = dict(turn)
            new_turn["error"] = normalized
            result["turn"] = new_turn
            result["errorCode"] = normalized.get("code")
            return result

        error = result.get("error")
        if not isinstance(error, dict):
            # Older transports may put only a message at params level.
            error = {"message": result.get("message", "Codex backend error")}
        normalized = cls._structured_error(error)
        result["error"] = normalized
        result["errorCode"] = normalized.get("code")
        return result

    @classmethod
    def _structured_error(cls, error: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(error)
        info = error.get("codexErrorInfo") or error.get("codex_error_info")
        code = cls._error_code(info)
        if code:
            normalized["code"] = code
            normalized["retryable"] = code in {
                "http_connection_failed",
                "response_stream_connection_failed",
                "response_stream_disconnected",
            }
        return normalized

    @staticmethod
    def _error_code(info: Any) -> str | None:
        value: str | None = None
        if isinstance(info, str):
            value = info
        elif isinstance(info, dict):
            for key in ("type", "kind", "code"):
                candidate = info.get(key)
                if isinstance(candidate, str) and candidate:
                    value = candidate
                    break
            if value is None and len(info) == 1:
                value = str(next(iter(info)))
        if not value:
            return None
        chars: list[str] = []
        for index, char in enumerate(value):
            if char.isupper() and index and chars[-1] != "_":
                chars.append("_")
            chars.append(char.lower() if char.isalnum() else "_")
        return "".join(chars).strip("_")

    @staticmethod
    def _stable_replay_event_id(
        *,
        method: str,
        thread_id: str,
        turn_id: str | None,
        item_id: str | None,
        params: dict[str, Any],
    ) -> str:
        raw = json.dumps(
            [method, thread_id, turn_id, item_id, redact_data(params)],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"replay:{digest}"

    @staticmethod
    def _derived_event_id(
        *,
        method: str,
        thread_id: str,
        turn_id: str | None,
        item_id: str | None,
        sequence: int,
    ) -> str:
        entropy = uuid.uuid4().hex
        raw = json.dumps(
            [method, thread_id, turn_id, item_id, sequence, entropy],
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_counted_step(event: CodexEvent) -> bool:
    """Count work items, never transport chunks, against the step budget."""

    return event.event_type in _STEP_START_EVENTS


__all__ = ["CodexEventAdapter", "is_counted_step"]
