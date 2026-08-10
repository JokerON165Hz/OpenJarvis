from __future__ import annotations

from dataclasses import replace

import pytest

from openjarvis.desktop.safety import (
    DesktopActionGuard,
    DesktopSafetyError,
    VisualEvidence,
    WindowBounds,
    WindowIdentity,
    WindowSnapshot,
    ensure_unprotected_field,
)


class FakeBackend:
    def __init__(self, snapshots: list[WindowSnapshot], *, now: float = 10.0) -> None:
        self.snapshots = list(snapshots)
        self.time = now

    def snapshot(self, hwnd: int) -> WindowSnapshot:
        assert self.snapshots
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        assert value.identity.hwnd == hwnd
        return value

    def now(self) -> float:
        return self.time


def snap(**changes) -> WindowSnapshot:
    base = WindowSnapshot(
        identity=WindowIdentity(100, 200, 123456, r"C:\\App\\app.exe", "Document", 1),
        bounds=WindowBounds(-1200, 100, -200, 800),
        dpi=144,
        monitor_id=r"\\.\DISPLAY2",
        focused=True,
    )
    return replace(base, **changes)


def test_reused_hwnd_rejected() -> None:
    planned = snap()
    reused = replace(
        planned,
        identity=replace(planned.identity, pid=201, process_started_at=999),
    )
    with pytest.raises(DesktopSafetyError, match="identity changed"):
        DesktopActionGuard(FakeBackend([reused])).validate_before_mutation(planned)


def test_same_title_different_process_rejected() -> None:
    planned = snap()
    other = replace(
        planned,
        identity=replace(
            planned.identity, pid=999, executable=r"C:\\Other\\app.exe"
        ),
    )
    with pytest.raises(DesktopSafetyError):
        DesktopActionGuard(FakeBackend([other])).validate_before_mutation(planned)


def test_focus_loss_between_steps_rejected() -> None:
    planned = snap()
    with pytest.raises(DesktopSafetyError, match="lost focus"):
        DesktopActionGuard(
            FakeBackend([replace(planned, focused=False)])
        ).validate_before_mutation(planned)


def test_process_restart_with_same_executable_rejected() -> None:
    planned = snap()
    restarted = replace(
        planned,
        identity=replace(planned.identity, pid=201, process_started_at=123999),
    )
    with pytest.raises(DesktopSafetyError, match="identity changed"):
        DesktopActionGuard(FakeBackend([restarted])).validate_before_mutation(planned)


def test_move_resize_between_screenshot_and_click_rejected() -> None:
    planned = snap()
    evidence = VisualEvidence("abc", planned, 9.5)
    moved = replace(planned, bounds=WindowBounds(-1100, 100, -100, 800))
    with pytest.raises(DesktopSafetyError, match="moved or resized"):
        DesktopActionGuard(FakeBackend([moved])).validate_visual_action(
            planned, evidence, x=-500, y=300
        )


def test_negative_monitor_coordinates_are_valid() -> None:
    planned = snap()
    evidence = VisualEvidence("abc", planned, 9.5)
    current = DesktopActionGuard(FakeBackend([planned])).validate_visual_action(
        planned, evidence, x=-500, y=300
    )
    assert current.monitor_id.endswith("DISPLAY2")


def test_dpi_change_between_screenshot_and_click_rejected() -> None:
    planned = snap()
    evidence = VisualEvidence("abc", planned, 9.5)
    with pytest.raises(DesktopSafetyError, match="DPI changed"):
        DesktopActionGuard(FakeBackend([replace(planned, dpi=192)])).validate_visual_action(
            planned, evidence, x=-500, y=300
        )


def test_modal_dialog_rejected() -> None:
    planned = snap()
    with pytest.raises(DesktopSafetyError, match="modal dialog"):
        DesktopActionGuard(
            FakeBackend([replace(planned, modal=True)])
        ).validate_before_mutation(planned)


def test_failed_postcondition_is_failure() -> None:
    planned = snap()
    guard = DesktopActionGuard(FakeBackend([planned, planned]))
    with pytest.raises(DesktopSafetyError, match="postcondition"):
        guard.run_verified(planned, lambda _w: "done", lambda _w, _result: False)


def test_secure_desktop_rejected() -> None:
    planned = snap()
    with pytest.raises(DesktopSafetyError, match="Secure Desktop"):
        DesktopActionGuard(
            FakeBackend([replace(planned, secure_desktop=True)])
        ).validate_before_mutation(planned)


def test_protected_input_rejected() -> None:
    with pytest.raises(DesktopSafetyError, match="protected input"):
        ensure_unprotected_field(protected=True)


def test_global_stop_during_action_prevents_success() -> None:
    planned = snap()
    guard = DesktopActionGuard(FakeBackend([planned, planned]))

    def mutate(_window):
        guard.request_stop()
        return "mutated"

    with pytest.raises(DesktopSafetyError, match="global stop"):
        guard.run_verified(planned, mutate, lambda _w, _result: True)


def test_cleanup_only_terminates_registered_owned_processes() -> None:
    guard = DesktopActionGuard(FakeBackend([snap()]))
    guard.register_owned_process(10, 1000)
    guard.register_owned_process(20, 2000)
    terminated: list[tuple[int, int]] = []
    cleaned = guard.cleanup_owned_processes(
        lambda pid, started: terminated.append((pid, started))
    )
    assert cleaned == ((10, 1000), (20, 2000))
    assert terminated == [(10, 1000), (20, 2000)]
    assert (30, 3000) not in terminated


def test_locked_session_rejected() -> None:
    planned = snap()
    with pytest.raises(DesktopSafetyError, match="session is unavailable"):
        DesktopActionGuard(
            FakeBackend([replace(planned, locked_session=True)])
        ).validate_before_mutation(planned)


def test_stale_screenshot_rejected() -> None:
    planned = snap()
    evidence = VisualEvidence("abc", planned, 1.0)
    with pytest.raises(DesktopSafetyError, match="stale"):
        DesktopActionGuard(
            FakeBackend([planned], now=10.0), screenshot_max_age=1.0
        ).validate_visual_action(planned, evidence, x=-500, y=300)
