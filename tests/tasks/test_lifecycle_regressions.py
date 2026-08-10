from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from openjarvis.codex.types import CodexBackendKind, CodexEvent, CodexEventType
from openjarvis.tasks import (
    CodexTaskEventProjector,
    InvalidTaskTransition,
    RecoveryCoordinator,
    RecoveryDecision,
    TaskOutcome,
    TaskStatus,
)
from openjarvis.tasks.orchestrator import _TerminalFacts
from tests.tasks.test_orchestrator import _runtime
from tests.tasks.test_recovery import _event, _running_task


def _codex_event(
    event_id: str,
    *,
    turn_id: str,
    item_id: str | None = None,
    event_type: CodexEventType = CodexEventType.ITEM_COMPLETED,
    sequence: int = 1,
    payload: dict | None = None,
) -> CodexEvent:
    return CodexEvent(
        event_id=event_id,
        sequence=sequence,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        task_id="task",
        session_id="session",
        thread_id="thread",
        turn_id=turn_id,
        item_id=item_id,
        backend=CodexBackendKind.PYTHON_SDK,
        event_type=event_type,
        payload=payload or {},
    )


@pytest.mark.asyncio
async def test_five_stage_task_continues_after_recoverable_tool_failure(
    tmp_path: Path,
) -> None:
    first_turn = [
        (CodexEventType.TOOL_STARTED, "step-1", {}),
        (CodexEventType.TOOL_COMPLETED, "step-1", {"status": "completed"}),
        (CodexEventType.TOOL_STARTED, "step-2", {}),
        (CodexEventType.TOOL_COMPLETED, "step-2", {"status": "completed"}),
        (CodexEventType.TOOL_STARTED, "step-3", {}),
        (
            CodexEventType.TOOL_COMPLETED,
            "step-3",
            {"status": "failed", "error": "synthetic recoverable failure"},
        ),
        (CodexEventType.TURN_FAILED, None, {"message": "step three failed"}),
    ]
    store, service, fake, orchestrator = _runtime(tmp_path, first_turn)
    try:
        first = await orchestrator.execute(
            "task",
            "run five steps",
            cwd=tmp_path,
            turn_correlation_id="turn-one",
        )
        assert first.task.task_id == "task"
        assert first.task.status is TaskStatus.PAUSED
        assert first.task.outcome is None
        assert first.task.error_category is None
        assert service.timeline("task")[-1].payload["error_category"] == (
            "recoverable_tool_failure"
        )

        fake.event_specs = [
            (CodexEventType.TOOL_STARTED, "step-3-retry", {}),
            (
                CodexEventType.TOOL_COMPLETED,
                "step-3-retry",
                {"status": "completed"},
            ),
            (CodexEventType.TOOL_STARTED, "step-4", {}),
            (CodexEventType.TOOL_COMPLETED, "step-4", {"status": "completed"}),
            (CodexEventType.TOOL_STARTED, "step-5", {}),
            (CodexEventType.TOOL_COMPLETED, "step-5", {"status": "completed"}),
            (CodexEventType.TURN_COMPLETED, None, {}),
        ]
        second = await orchestrator.execute(
            "task",
            "retry step three and continue",
            cwd=tmp_path,
            turn_correlation_id="turn-two",
        )
        assert second.task.task_id == first.task.task_id
        assert second.task.status is TaskStatus.DONE
        assert second.task.active_turn_id == "turn-2"
        assert fake.turn_count == 2
        assert service.get("task") == second.task
    finally:
        store.close()


def test_duplicate_source_event_has_only_one_durable_effect(tmp_path: Path) -> None:
    store, service, _, _ = _runtime(tmp_path, [])
    projector = CodexTaskEventProjector(store)
    first_event = _codex_event(
        "duplicate-event",
        turn_id="turn-1",
        item_id="message",
        payload={"item": {"type": "agentMessage", "text": "first"}},
    )
    duplicate = _codex_event(
        "duplicate-event",
        turn_id="turn-1",
        item_id="message",
        sequence=999,
        payload={"item": {"type": "agentMessage", "text": "must not overwrite"}},
    )
    try:
        first = projector.project(first_event)
        version_after_first = service.get("task").version
        second = projector.project(duplicate)

        assert first.inserted is True
        assert second.inserted is False
        assert service.get("task").version == version_after_first
        items = store.list_items("task")
        assert len(items) == 1
        assert items[0].payload["item"]["text"] == "first"
        assert sum(
            event.event_type == CodexEventType.ITEM_COMPLETED.value
            for event in service.timeline("task")
        ) == 1
    finally:
        store.close()


def test_late_old_turn_result_cannot_overwrite_newer_turn(tmp_path: Path) -> None:
    store, service, _, orchestrator = _runtime(tmp_path, [])
    projector = CodexTaskEventProjector(store)
    service.transition(
        "task",
        TaskStatus.RUNNING,
        component="test",
        cause="new_turn_started",
        idempotency_key="bind-new-turn",
        active_thread_id="thread",
        active_turn_id="turn-new",
    )
    old_result = _codex_event(
        "late-old-result",
        turn_id="turn-old",
        item_id="old-message",
        payload={"item": {"type": "agentMessage", "text": "stale answer"}},
    )
    try:
        projected = projector.project(old_result)
        assert projected.stale is True
        assert store.list_items("task") == []

        finished = orchestrator._finish_task(
            "task",
            _TerminalFacts(completed=True, content="stale answer"),
            thread_id="thread",
            turn_id="turn-old",
            transition_key="old-turn",
            finalize_task=True,
        )
        assert finished.status is TaskStatus.RUNNING
        assert finished.active_turn_id == "turn-new"
        assert finished.result == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_between_completed_steps_resumes_next_point(
    tmp_path: Path,
) -> None:
    store, service = _running_task(tmp_path)
    _event(store, "tool.started", "step-1")
    _event(store, "tool.completed", "step-1")
    resumed: list[str] = []

    async def resume(task) -> None:
        resumed.append(task.task_id)

    try:
        report = (await RecoveryCoordinator(store, service).recover_all(
            resume_safe=resume
        ))[0]
        assert report.decision is RecoveryDecision.RESUMED_SAFE
        assert report.final_status is TaskStatus.RUNNING
        assert resumed == ["task"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_with_uncommitted_tool_completion_never_replays_effect(
    tmp_path: Path,
) -> None:
    store, service = _running_task(tmp_path)
    _event(store, "tool.started", "external-step")
    resumed: list[str] = []

    async def resume(task) -> None:
        resumed.append(task.task_id)

    try:
        report = (await RecoveryCoordinator(store, service).recover_all(
            resume_safe=resume
        ))[0]
        assert report.decision is RecoveryDecision.PAUSED_AMBIGUOUS
        assert report.ambiguous_effect is True
        assert report.final_status is TaskStatus.PAUSED
        assert resumed == []
        checks = store.list_recovery_checks("task")
        assert checks[-1]["facts"]["active_tools"] == ["external-step"]
    finally:
        store.close()


def test_canceled_task_cannot_be_revived_by_late_completion(tmp_path: Path) -> None:
    store, service, _, orchestrator = _runtime(tmp_path, [])
    service.transition(
        "task",
        TaskStatus.RUNNING,
        component="test",
        cause="turn_started",
        idempotency_key="running",
        active_thread_id="thread",
        active_turn_id="turn-1",
    )
    service.transition(
        "task",
        TaskStatus.CANCELED,
        component="test",
        cause="global_stop",
        idempotency_key="cancel",
        outcome=TaskOutcome.CANCELED,
    )
    try:
        finished = orchestrator._finish_task(
            "task",
            _TerminalFacts(completed=True, content="too late"),
            thread_id="thread",
            turn_id="turn-1",
            transition_key="late",
            finalize_task=True,
        )
        assert finished.status is TaskStatus.CANCELED
        with pytest.raises(InvalidTaskTransition):
            service.transition(
                "task",
                TaskStatus.RUNNING,
                component="test",
                cause="late_event",
                idempotency_key="revive",
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_terminal_failure_stays_terminal_while_tool_failure_is_resumable(
    tmp_path: Path,
) -> None:
    store, service, _, orchestrator = _runtime(
        tmp_path,
        [(CodexEventType.TURN_FAILED, None, {"message": "terminal"})],
    )
    try:
        result = await orchestrator.execute("task", "fail", cwd=tmp_path)
        assert result.task.status is TaskStatus.FAILED
        with pytest.raises(InvalidTaskTransition):
            service.transition(
                "task",
                TaskStatus.RECOVERING,
                component="test",
                cause="must_not_revive",
                idempotency_key="recover-terminal",
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_task_never_completes_with_unverified_active_tool(tmp_path: Path) -> None:
    store, _, _, orchestrator = _runtime(
        tmp_path,
        [
            (CodexEventType.TOOL_STARTED, "unverified-tool", {}),
            (CodexEventType.TURN_COMPLETED, None, {}),
        ],
    )
    try:
        result = await orchestrator.execute("task", "question", cwd=tmp_path)
        assert result.task.status is TaskStatus.PAUSED
        assert result.task.outcome is None
    finally:
        store.close()
