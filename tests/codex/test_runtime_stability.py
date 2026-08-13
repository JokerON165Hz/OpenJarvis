from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from openjarvis.codex import (
    ApprovalMode,
    CodexAppServerBackend,
    CodexBackendKind,
    CodexEventAdapter,
    CodexEventType,
    CodexModelConfig,
    CodexRunContext,
    CodexStateStore,
    CodexTimeoutError,
    SandboxMode,
    ThreadResumeRequest,
    ThreadStartRequest,
    TurnStartRequest,
)
from openjarvis.codex.redaction import redact_data, redact_text


class LifecycleTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.thread_ids: list[str] = []
        self.turn_ids: list[str] = []
        self.histories: dict[str, dict[str, Any]] = {}
        self.running = True
        self.reconnected = 0

    async def start(self) -> None:
        self.running = True

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append((method, params, timeout))
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "private@example.test"}}
        if method == "thread/start":
            return {"thread": {"id": self.thread_ids.pop(0)}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": self.turn_ids.pop(0)}}
        if method == "thread/read":
            return self.histories.get(
                params["threadId"],
                {"thread": {"id": params["threadId"], "turns": []}},
            )
        return {}

    async def next_message(self, *, timeout: float) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self.messages.get(), timeout)
        except asyncio.TimeoutError as exc:
            raise CodexTimeoutError("test transport timeout") from exc

    async def reconnect(self, *, safe: bool) -> None:
        assert safe is True
        self.reconnected += 1
        self.running = True

    async def close(self) -> None:
        self.running = False


def _context(
    workspace: Path,
    correlation_id: str,
    *,
    task_id: str = "task-1",
    session_id: str = "session-1",
    timeout_seconds: float = 2.0,
    token_limit: int | None = 1000,
) -> CodexRunContext:
    return CodexRunContext(
        task_id=task_id,
        session_id=session_id,
        correlation_id=correlation_id,
        cwd=workspace,
        sandbox=SandboxMode.READ_ONLY,
        approval_mode=ApprovalMode.DENY_ALL,
        model=CodexModelConfig(model="test-model", effort="medium", service_tier=None),
        timeout_seconds=timeout_seconds,
        step_limit=100,
        token_limit=token_limit,
        developer_instructions=None,
        isolated_workspace=None,
    )


async def _thread(
    backend: CodexAppServerBackend,
    transport: LifecycleTransport,
    context: CodexRunContext,
    thread_id: str,
):
    transport.thread_ids.append(thread_id)
    return await backend.start_thread(ThreadStartRequest(context=context))


async def _turn(
    backend: CodexAppServerBackend,
    transport: LifecycleTransport,
    context: CodexRunContext,
    thread_id: str,
    turn_id: str,
):
    transport.turn_ids.append(turn_id)
    return await backend.start_turn(
        TurnStartRequest(context=context, thread_id=thread_id, prompt="continue")
    )


async def _collect(backend: CodexAppServerBackend, turn_id: str):
    return [event async for event in backend.stream_events(turn_id)]


def _terminal(thread_id: str, turn_id: str, status: str = "completed") -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "turn": {"id": turn_id, "status": status},
        },
    }


@pytest.mark.asyncio
async def test_five_sequential_turns_reuse_one_thread(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    thread = await _thread(backend, transport, _context(tmp_path, "thread"), "thread-1")
    seen: list[str] = []
    for index in range(5):
        turn_id = f"turn-{index}"
        turn = await _turn(backend, transport, _context(tmp_path, f"c-{index}"), thread.thread_id, turn_id)
        await transport.messages.put(_terminal(thread.thread_id, turn_id))
        assert (await _collect(backend, turn.turn_id))[-1].event_type is CodexEventType.TURN_COMPLETED
        seen.append(turn.turn_id)
    assert seen == [f"turn-{index}" for index in range(5)]
    assert {event.thread_id for event in store.list_events("thread-1")} == {"thread-1"}
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_two_parallel_threads_never_mix_events(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    a = await _thread(backend, transport, _context(tmp_path, "a", task_id="a", session_id="a"), "thread-a")
    b = await _thread(backend, transport, _context(tmp_path, "b", task_id="b", session_id="b"), "thread-b")
    ta = await _turn(backend, transport, _context(tmp_path, "ta", task_id="a", session_id="a"), a.thread_id, "turn-a")
    tb = await _turn(backend, transport, _context(tmp_path, "tb", task_id="b", session_id="b"), b.thread_id, "turn-b")
    await transport.messages.put({"method": "error", "params": {"message": "ambiguous"}})
    await transport.messages.put({
        "method": "item/completed",
        "params": {"threadId": "thread-b", "turnId": "turn-b", "item": {"id": "b-msg", "type": "agentMessage"}},
    })
    await transport.messages.put(_terminal("thread-a", "turn-a"))
    events_a = await _collect(backend, ta.turn_id)
    await transport.messages.put(_terminal("thread-b", "turn-b"))
    events_b = await _collect(backend, tb.turn_id)
    assert all(event.thread_id == "thread-a" for event in events_a)
    assert all(event.thread_id == "thread-b" for event in events_b)
    assert any(event.item_id == "b-msg" for event in events_b)
    assert all(event.payload.get("message") != "ambiguous" for event in [*events_a, *events_b])
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_tool_follow_up_keeps_turn_correlation(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    thread = await _thread(backend, transport, _context(tmp_path, "thread"), "thread-1")
    turn = await _turn(backend, transport, _context(tmp_path, "turn"), thread.thread_id, "turn-1")
    for method, item in (
        ("item/started", {"id": "tool-1", "type": "mcpToolCall"}),
        ("item/completed", {"id": "tool-1", "type": "mcpToolCall"}),
        ("item/completed", {"id": "answer-1", "type": "agentMessage", "text": "done"}),
    ):
        await transport.messages.put(
            {
                "method": method,
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": item,
                },
            }
        )
    await transport.messages.put(_terminal("thread-1", "turn-1"))
    events = await _collect(backend, turn.turn_id)
    assert [event.event_type for event in events[:2]] == [CodexEventType.TOOL_STARTED, CodexEventType.TOOL_COMPLETED]
    assert any(event.item_id == "answer-1" for event in events)
    assert all(event.turn_id == "turn-1" for event in events)
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_duplicate_item_completed_without_event_id_is_idempotent(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    context = _context(tmp_path, "thread")
    await _thread(backend, transport, context, "thread-1")
    adapter = CodexEventAdapter(store)
    raw = {
        "method": "item/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {"id": "item-1", "type": "agentMessage"},
        },
    }
    first = adapter.normalize(
        raw,
        context=context,
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    duplicate = adapter.normalize(
        raw,
        context=context,
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert first is not None and duplicate is None
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_reconnect_mid_stream_does_not_duplicate_assistant_message(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    thread = await _thread(backend, transport, _context(tmp_path, "thread"), "thread-1")
    turn = await _turn(backend, transport, _context(tmp_path, "turn"), thread.thread_id, "turn-1")
    item = {"id": "answer-1", "type": "agentMessage", "text": "one answer"}
    await transport.messages.put(
        {
            "method": "item/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "item": item},
        }
    )
    transport.histories["thread-1"] = {
        "thread": {
            "id": "thread-1",
            "turns": [{"id": "turn-1", "status": "completed", "items": [item]}],
        }
    }
    original_next, calls = transport.next_message, 0

    async def disconnect_after_first(*, timeout: float) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original_next(timeout=timeout)
        transport.running = False
        raise CodexTimeoutError("stdio disconnected")

    transport.next_message = disconnect_after_first  # type: ignore[method-assign]
    events = await _collect(backend, turn.turn_id)
    assert len([event for event in events if event.item_id == "answer-1"]) == 1
    assert transport.reconnected == 1
    assert events[-1].event_type is CodexEventType.TURN_COMPLETED
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_late_old_turn_event_cannot_overwrite_new_turn(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    context = _context(tmp_path, "thread")
    await _thread(backend, transport, context, "thread-1")
    late = CodexEventAdapter(store).normalize(
        _terminal("thread-1", "turn-old"),
        context=context,
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-new",
    )
    assert late is None
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_cancel_and_timeout_target_exact_turn(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    thread = await _thread(backend, transport, _context(tmp_path, "thread"), "thread-1")
    cancel_turn = await _turn(backend, transport, _context(tmp_path, "cancel"), thread.thread_id, "turn-cancel")
    task = asyncio.create_task(_collect(backend, cancel_turn.turn_id))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    interrupts = [call for call in transport.calls if call[0] == "turn/interrupt"]
    assert interrupts[-1][1] == {"threadId": "thread-1", "turnId": "turn-cancel"}
    timeout_turn = await _turn(
        backend,
        transport,
        _context(tmp_path, "timeout", timeout_seconds=0.02),
        thread.thread_id,
        "turn-timeout",
    )
    with pytest.raises(CodexTimeoutError):
        await _collect(backend, timeout_turn.turn_id)
    interrupts = [call for call in transport.calls if call[0] == "turn/interrupt"]
    assert interrupts[-1][1]["turnId"] == "turn-timeout"
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_usage_limit_error_is_structured_and_thread_survives(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    context = _context(tmp_path, "thread")
    await _thread(backend, transport, context, "thread-1")
    event = CodexEventAdapter(store).normalize(
        {
            "method": "error",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "error": {
                    "message": "quota for private@example.test",
                    "codexErrorInfo": "UsageLimitExceeded",
                },
            },
        },
        context=context,
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert event is not None and event.payload["errorCode"] == "usage_limit_exceeded"
    assert event.payload["error"]["code"] == "usage_limit_exceeded"
    assert "private@example.test" not in str(event.payload)
    assert store.get_thread_by_id("thread-1") is not None
    await backend.close()
    store.close()


@pytest.mark.asyncio
async def test_process_restart_resumes_persisted_thread_for_next_turn(tmp_path: Path) -> None:
    store = CodexStateStore(tmp_path / "codex.db")
    first_transport, second_transport = LifecycleTransport(), LifecycleTransport()
    first = CodexAppServerBackend(transport=first_transport, store=store)
    thread = await _thread(first, first_transport, _context(tmp_path, "thread"), "thread-1")
    await first.close()
    second = CodexAppServerBackend(transport=second_transport, store=store)
    resumed = await second.resume_thread(
        ThreadResumeRequest(
            context=_context(tmp_path, "thread"),
            thread_id=thread.thread_id,
        )
    )
    turn = await _turn(
        second,
        second_transport,
        _context(tmp_path, "after-restart"),
        resumed.thread_id,
        "turn-after-restart",
    )
    await second_transport.messages.put(_terminal("thread-1", turn.turn_id))
    assert (await _collect(second, turn.turn_id))[-1].event_type is CodexEventType.TURN_COMPLETED
    assert any(call[0] == "thread/resume" for call in second_transport.calls)
    await second.close()
    store.close()


def test_sensitive_error_and_account_details_are_redacted() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    safe = redact_data({
        "email": "private@example.test",
        "accountId": "acct-private",
        "workspaceId": "ws-private",
        "nested": {"message": f"email=private@example.test token={secret}"},
    })
    rendered = str(safe)
    for value in ("private@example.test", "acct-private", "ws-private", secret):
        assert value not in rendered
    assert "person@example.test" not in redact_text("owner person@example.test")


@pytest.mark.asyncio
async def test_turn_and_thread_usage_are_separate_and_replay_safe(tmp_path: Path) -> None:
    store, transport = CodexStateStore(tmp_path / "codex.db"), LifecycleTransport()
    backend = CodexAppServerBackend(transport=transport, store=store)
    context = _context(tmp_path, "thread")
    await _thread(backend, transport, context, "thread-1")
    adapter = CodexEventAdapter(store)

    def raw(turn_id: str, total: int, last: int) -> dict[str, Any]:
        return {"method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-1",
            "turnId": turn_id,
            "tokenUsage": {
                "total": {"inputTokens": total, "outputTokens": 0, "totalTokens": total},
                "last": {"inputTokens": last, "outputTokens": 0, "totalTokens": last},
            },
        }}

    first = adapter.normalize(
        raw("turn-1", 130, 30),
        context=context,
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    second_raw = raw("turn-1", 190, 60)
    second = adapter.normalize(
        second_raw,
        context=context,
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    duplicate = adapter.normalize(
        second_raw,
        context=context,
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    next_turn = adapter.normalize(
        raw("turn-2", 205, 15),
        context=replace(context, correlation_id="turn-2"),
        backend=CodexBackendKind.APP_SERVER,
        thread_id="thread-1",
        turn_id="turn-2",
    )
    assert first is not None and first.payload["turn_usage"]["totalTokens"] == 30
    assert second is not None and second.payload["turn_usage"]["totalTokens"] == 90
    assert second.payload["tokenUsage"]["total"]["totalTokens"] == 190
    assert duplicate is None
    assert next_turn is not None and next_turn.payload["turn_usage"]["totalTokens"] == 15
    assert next_turn.payload["tokenUsage"]["total"]["totalTokens"] == 205
    await backend.close()
    store.close()
