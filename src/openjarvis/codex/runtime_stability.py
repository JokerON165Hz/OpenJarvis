"""Runtime stability helpers for persistent Codex thread/turn lifecycles.

This module is intentionally internal.  It patches the two concrete Codex
backends at package import time without changing the public backend protocol.
The helpers are kept here so the fixes stay inside ``openjarvis.codex`` and do
not require task/server integration changes.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


def _message_correlation(message: Any) -> tuple[str | None, str | None]:
    """Return explicit thread/turn ids from one app-server notification."""

    if not isinstance(message, dict):
        return None, None
    params = message.get("params")
    data = params if isinstance(params, dict) else {}
    thread_id = data.get("threadId") or data.get("thread_id")
    turn_id = data.get("turnId") or data.get("turn_id")
    if not turn_id:
        turn = data.get("turn")
        if isinstance(turn, dict):
            turn_id = turn.get("id")
    if not thread_id:
        thread = data.get("thread")
        if isinstance(thread, dict):
            thread_id = thread.get("id")
    return (
        str(thread_id) if thread_id else None,
        str(turn_id) if turn_id else None,
    )


def _token_total(block: Any) -> int | None:
    if not isinstance(block, dict):
        return None
    for key in ("totalTokens", "total_tokens"):
        value = block.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    input_tokens = None
    output_tokens = None
    for key in ("inputTokens", "input_tokens", "promptTokens", "prompt_tokens"):
        value = block.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            input_tokens = int(value)
            break
    for key in (
        "outputTokens",
        "output_tokens",
        "completionTokens",
        "completion_tokens",
    ):
        value = block.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            output_tokens = int(value)
            break
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0) + (output_tokens or 0)


def turn_token_limit_exceeded(event: Any, limit: int | None) -> bool:
    """Evaluate a per-turn limit without treating thread totals as turn totals."""

    if limit is None:
        return False
    event_type = getattr(event, "event_type", None)
    event_type_value = getattr(event_type, "value", str(event_type or ""))
    if event_type_value != "usage.updated":
        return False
    payload = getattr(event, "payload", {})
    if not isinstance(payload, dict):
        return False

    block = payload.get("turn_usage") or payload.get("turnUsage")
    if not isinstance(block, dict):
        token_usage = payload.get("tokenUsage") or payload.get("token_usage")
        if isinstance(token_usage, dict):
            # ``last`` is a response-level delta. It is a safer fallback than
            # the cumulative thread ``total`` when no synthesized turn total is
            # available (older persisted events / older runtimes).
            block = token_usage.get("last")
    value = _token_total(block)
    return value is not None and value > limit


def _terminal_event_for_turn(store: Any, thread_id: str, turn_id: str) -> Any | None:
    try:
        events = store.list_events(thread_id)
    except Exception:
        return None
    for event in reversed(events):
        if getattr(event, "turn_id", None) != turn_id:
            continue
        event_type = getattr(getattr(event, "event_type", None), "value", "")
        if event_type in {"turn.completed", "turn.failed", "turn.interrupted"}:
            return event
    return None


def _seen_item_ids(store: Any, thread_id: str, turn_id: str) -> set[str]:
    try:
        events = store.list_events(thread_id)
    except Exception:
        return set()
    return {
        str(event.item_id)
        for event in events
        if getattr(event, "turn_id", None) == turn_id
        and getattr(event, "item_id", None)
    }


def _find_turn(result: Any, turn_id: str) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    if not isinstance(thread, dict):
        thread = result
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if isinstance(turn, dict) and str(turn.get("id") or "") == turn_id:
            return turn
    return None


async def _recover_app_server_turn(
    backend: Any,
    transport: Any,
    turn_id: str,
    active: Any,
) -> bool:
    """Reconnect, resume the thread, and reconcile the in-flight turn.

    The original prompt is never re-submitted.  A completed/interrupted/failed
    turn is reconstructed from durable thread history only when its terminal
    event was not already persisted.  This makes recovery at-most-once from the
    caller's perspective.
    """

    await transport.reconnect(safe=True)
    params = backend._thread_params(active.context)
    params["threadId"] = active.thread_id
    await transport.request(
        "thread/resume",
        params,
        timeout=active.context.timeout_seconds,
    )

    result = await transport.request(
        "thread/read",
        {"threadId": active.thread_id, "includeTurns": True},
        timeout=active.context.timeout_seconds,
    )
    turn = _find_turn(result, turn_id)
    if turn is None:
        # A resumed live turn can continue emitting notifications even if the
        # history response has not materialized it yet.
        return False

    status = str(turn.get("status") or "")
    if status not in {"completed", "failed", "interrupted"}:
        return False

    store = backend._get_store()
    if _terminal_event_for_turn(store, active.thread_id, turn_id) is not None:
        return True

    seen_items = _seen_item_ids(store, active.thread_id, turn_id)
    items = turn.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not item_id or str(item_id) in seen_items:
                continue
            backend._backlog.setdefault(turn_id, deque()).append(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": active.thread_id,
                        "turnId": turn_id,
                        "item": item,
                    },
                }
            )

    backend._backlog.setdefault(turn_id, deque()).append(
        {
            "method": "turn/completed",
            "params": {
                "threadId": active.thread_id,
                "turnId": turn_id,
                "turn": turn,
            },
        }
    )
    return False


async def stable_next_turn_message(
    backend: Any,
    transport: Any,
    turn_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Route notifications by both turn and thread before normalization."""

    backlog = backend._backlog.setdefault(turn_id, deque())
    if backlog:
        return backlog.popleft()
    active = backend._turns.get(turn_id)
    expected_thread = getattr(active, "thread_id", None)

    while True:
        message = await transport.next_message(timeout=timeout)
        message_thread, message_turn = _message_correlation(message)
        if not message_thread and not message_turn and len(backend._turns) > 1:
            # With concurrent turns there is no safe owner for an uncorrelated
            # notification. Drop it rather than attach it to whichever stream
            # happened to consume the shared transport queue first.
            continue
        if message_turn and message_turn != turn_id:
            # Preserve only events for another currently owned turn. Unknown or
            # stale turn events are deliberately ignored rather than allowed to
            # overwrite a newer turn.
            if message_turn in backend._turns:
                backend._backlog.setdefault(message_turn, deque()).append(message)
            continue
        if message_thread and expected_thread and message_thread != expected_thread:
            target_turn = next(
                (
                    other_turn_id
                    for other_turn_id, other in backend._turns.items()
                    if getattr(other, "thread_id", None) == message_thread
                ),
                None,
            )
            if target_turn is not None:
                backend._backlog.setdefault(target_turn, deque()).append(message)
            continue
        return message


async def stable_app_server_stream_events(backend: Any, turn_id: str):
    """Stream one turn with bounded transport recovery and exact correlation."""

    import time

    from openjarvis.codex.events import is_counted_step
    from openjarvis.codex.types import (
        CodexBackendError,
        CodexBackendKind,
        CodexCapabilityError,
        CodexEventType,
        CodexPolicyError,
        CodexTimeoutError,
    )

    active = backend._turns.get(turn_id)
    if active is None:
        raise CodexCapabilityError(f"unknown active turn: {turn_id}")
    transport = await backend._get_transport()

    counted_steps: set[str] = set()
    try:
        for persisted in backend._get_store().list_events(active.thread_id):
            if getattr(persisted, "turn_id", None) == turn_id and is_counted_step(
                persisted
            ):
                counted_steps.add(persisted.item_id or persisted.event_id)
    except Exception:
        pass

    terminal = False
    reconnect_attempts = 0
    try:
        while True:
            remaining = active.context.timeout_seconds - (
                time.monotonic() - active.started_monotonic
            )
            if remaining <= 0:
                try:
                    await backend.interrupt(turn_id)
                except Exception:
                    pass
                failure = backend._events().emit(
                    CodexEventType.ERROR,
                    context=active.context,
                    backend=CodexBackendKind.APP_SERVER,
                    thread_id=active.thread_id,
                    turn_id=turn_id,
                    payload={"code": "turn_timeout", "message": "turn timeout exceeded"},
                    event_id=f"app-server:{turn_id}:timeout",
                )
                if failure is not None:
                    yield failure
                raise CodexTimeoutError("app-server turn timeout exceeded")

            # Poll rather than block for the entire turn timeout so a dead stdio
            # child can be detected and reconciled promptly.
            poll_timeout = min(remaining, 1.0)
            try:
                message = await stable_next_turn_message(
                    backend,
                    transport,
                    turn_id,
                    timeout=poll_timeout,
                )
            except CodexTimeoutError:
                remaining_after_poll = active.context.timeout_seconds - (
                    time.monotonic() - active.started_monotonic
                )
                if remaining_after_poll <= 0:
                    continue
                running = getattr(transport, "running", True)
                if running:
                    continue
                if reconnect_attempts >= 1:
                    raise CodexBackendError(
                        "app-server transport disconnected after recovery"
                    )
                already_terminal = await _recover_app_server_turn(
                    backend, transport, turn_id, active
                )
                reconnect_attempts += 1
                if already_terminal:
                    terminal = True
                    break
                continue
            except CodexBackendError:
                if reconnect_attempts >= 1:
                    raise
                already_terminal = await _recover_app_server_turn(
                    backend, transport, turn_id, active
                )
                reconnect_attempts += 1
                if already_terminal:
                    terminal = True
                    break
                continue

            event = backend._events().normalize(
                message,
                context=active.context,
                backend=CodexBackendKind.APP_SERVER,
                thread_id=active.thread_id,
                turn_id=turn_id,
            )
            if event is None:
                continue
            if is_counted_step(event):
                counted_steps.add(event.item_id or event.event_id)
            if len(counted_steps) > active.context.step_limit:
                await backend.interrupt(turn_id)
                failure = backend._events().emit(
                    CodexEventType.ERROR,
                    context=active.context,
                    backend=CodexBackendKind.APP_SERVER,
                    thread_id=active.thread_id,
                    turn_id=turn_id,
                    payload={
                        "code": "turn_step_limit_exceeded",
                        "message": "turn step limit exceeded",
                    },
                    event_id=f"app-server:{turn_id}:step-limit",
                )
                if failure is not None:
                    yield failure
                raise CodexPolicyError("turn step limit exceeded")
            if turn_token_limit_exceeded(event, active.context.token_limit):
                await backend.interrupt(turn_id)
                failure = backend._events().emit(
                    CodexEventType.ERROR,
                    context=active.context,
                    backend=CodexBackendKind.APP_SERVER,
                    thread_id=active.thread_id,
                    turn_id=turn_id,
                    payload={
                        "code": "turn_token_limit_exceeded",
                        "message": "turn token limit exceeded",
                    },
                    event_id=f"app-server:{turn_id}:token-limit",
                )
                if failure is not None:
                    yield failure
                raise CodexPolicyError("turn token limit exceeded")

            yield event
            if event.event_type in {
                CodexEventType.TURN_COMPLETED,
                CodexEventType.TURN_FAILED,
                CodexEventType.TURN_INTERRUPTED,
            }:
                terminal = True
                status = event.event_type.value.rsplit(".", 1)[-1]
                now = backend._now()
                backend._get_store().update_turn(
                    turn_id,
                    status=status,
                    updated_at=now,
                )
                backend._get_store().update_thread(
                    active.thread_id,
                    status="idle",
                    updated_at=now,
                    resume_checkpoint=turn_id,
                )
                break
    except asyncio.CancelledError:
        try:
            await backend.interrupt(turn_id)
        except Exception:
            pass
        raise
    finally:
        # Never leave an in-memory active handle behind after the stream exits.
        # The durable thread identity remains in the store for later resume.
        backend._turns.pop(turn_id, None)
        if terminal:
            backend._backlog.pop(turn_id, None)


def install_backend_stability_patches() -> None:
    """Install idempotent backend patches while preserving public interfaces."""

    import importlib

    app_server_module = importlib.import_module("openjarvis.codex.app_server")
    sdk_backend_module = importlib.import_module("openjarvis.codex.sdk_backend")

    app_cls = app_server_module.CodexAppServerBackend
    sdk_cls = sdk_backend_module.CodexPythonSdkBackend
    if getattr(app_cls, "_openjarvis_stability_v1", False):
        return

    async def _next(self: Any, transport: Any, turn_id: str, *, timeout: float):
        return await stable_next_turn_message(
            self, transport, turn_id, timeout=timeout
        )

    async def _stream(self: Any, turn_id: str):
        async for event in stable_app_server_stream_events(self, turn_id):
            yield event

    app_cls._next_turn_message = _next
    app_cls.stream_events = _stream
    app_cls._token_limit_exceeded = staticmethod(turn_token_limit_exceeded)
    sdk_cls._token_limit_exceeded = staticmethod(turn_token_limit_exceeded)
    app_cls._openjarvis_stability_v1 = True
    sdk_cls._openjarvis_stability_v1 = True


__all__ = [
    "install_backend_stability_patches",
    "stable_next_turn_message",
    "turn_token_limit_exceeded",
]
