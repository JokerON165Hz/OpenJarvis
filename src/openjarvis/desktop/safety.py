"""Safety primitives for verified Windows desktop mutations.

The module is intentionally platform-neutral: production Win32/UIA adapters provide
snapshots, while cloud tests use fakes. A mutation is permitted only while the
captured stable identity still matches and its postcondition is explicitly observed.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event, RLock
from typing import Callable, Protocol, TypeVar


class DesktopSafetyError(RuntimeError):
    """Raised when a desktop mutation cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class WindowBounds:
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    hwnd: int
    pid: int
    process_started_at: int
    executable: str
    title: str
    session_id: int

    def stable_key(self) -> tuple[int, int, int, str, int]:
        return (
            self.hwnd,
            self.pid,
            self.process_started_at,
            self.executable.casefold(),
            self.session_id,
        )


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    identity: WindowIdentity
    bounds: WindowBounds
    dpi: int
    monitor_id: str
    focused: bool
    modal: bool = False
    secure_desktop: bool = False
    locked_session: bool = False


@dataclass(frozen=True, slots=True)
class VisualEvidence:
    sha256: str
    snapshot: WindowSnapshot
    captured_at: float


class SafetyBackend(Protocol):
    def snapshot(self, hwnd: int) -> WindowSnapshot: ...

    def now(self) -> float: ...


T = TypeVar("T")


class DesktopActionGuard:
    """Validate identity/context immediately before and after mutations."""

    def __init__(self, backend: SafetyBackend, *, screenshot_max_age: float = 1.0) -> None:
        self._backend = backend
        self._screenshot_max_age = screenshot_max_age
        self._stop = Event()
        self._lock = RLock()
        self._owned_processes: set[tuple[int, int]] = set()

    def capture_target(self, hwnd: int) -> WindowSnapshot:
        snapshot = self._backend.snapshot(hwnd)
        self._assert_interactive(snapshot)
        return snapshot

    def request_stop(self) -> None:
        self._stop.set()

    def clear_stop(self) -> None:
        self._stop.clear()

    def register_owned_process(self, pid: int, process_started_at: int) -> None:
        with self._lock:
            self._owned_processes.add((pid, process_started_at))

    def cleanup_owned_processes(
        self, terminate: Callable[[int, int], None]
    ) -> tuple[tuple[int, int], ...]:
        with self._lock:
            owned = tuple(sorted(self._owned_processes))
            self._owned_processes.clear()
        for pid, started_at in owned:
            terminate(pid, started_at)
        return owned

    def validate_before_mutation(
        self,
        planned: WindowSnapshot,
        *,
        require_focus: bool = True,
        allow_modal: bool = False,
    ) -> WindowSnapshot:
        self._assert_not_stopped()
        current = self._backend.snapshot(planned.identity.hwnd)
        self._assert_interactive(current)
        if current.identity.stable_key() != planned.identity.stable_key():
            raise DesktopSafetyError("desktop window identity changed")
        if current.identity.title != planned.identity.title:
            raise DesktopSafetyError("desktop window title changed")
        if require_focus and not current.focused:
            raise DesktopSafetyError("desktop target lost focus")
        if current.modal and not allow_modal:
            raise DesktopSafetyError("desktop target is blocked by a modal dialog")
        return current

    def validate_visual_action(
        self,
        planned: WindowSnapshot,
        evidence: VisualEvidence,
        *,
        x: int,
        y: int,
    ) -> WindowSnapshot:
        current = self.validate_before_mutation(planned)
        if evidence.snapshot.identity.stable_key() != current.identity.stable_key():
            raise DesktopSafetyError("visual evidence belongs to another window identity")
        if self._backend.now() - evidence.captured_at > self._screenshot_max_age:
            raise DesktopSafetyError("visual evidence is stale")
        if evidence.snapshot.bounds != current.bounds:
            raise DesktopSafetyError("window moved or resized after screenshot")
        if evidence.snapshot.dpi != current.dpi:
            raise DesktopSafetyError("window DPI changed after screenshot")
        if evidence.snapshot.monitor_id != current.monitor_id:
            raise DesktopSafetyError("window monitor changed after screenshot")
        b = current.bounds
        if not (b.left <= x < b.right and b.top <= y < b.bottom):
            raise DesktopSafetyError("visual action escaped current window bounds")
        return current

    def run_verified(
        self,
        planned: WindowSnapshot,
        mutate: Callable[[WindowSnapshot], T],
        postcondition: Callable[[WindowSnapshot, T], bool],
        *,
        require_focus: bool = True,
        allow_modal: bool = False,
    ) -> T:
        before = self.validate_before_mutation(
            planned, require_focus=require_focus, allow_modal=allow_modal
        )
        result = mutate(before)
        self._assert_not_stopped()
        after = self._backend.snapshot(before.identity.hwnd)
        self._assert_interactive(after)
        if after.identity.stable_key() != before.identity.stable_key():
            raise DesktopSafetyError("desktop identity changed during action")
        if not postcondition(after, result):
            raise DesktopSafetyError("desktop postcondition was not observed")
        return result

    def _assert_interactive(self, snapshot: WindowSnapshot) -> None:
        if snapshot.secure_desktop:
            raise DesktopSafetyError("Secure Desktop/UAC interaction is not permitted")
        if snapshot.locked_session:
            raise DesktopSafetyError("interactive desktop session is unavailable")

    def _assert_not_stopped(self) -> None:
        if self._stop.is_set():
            raise DesktopSafetyError("desktop action interrupted by global stop")


def ensure_unprotected_field(*, protected: bool) -> None:
    if protected:
        raise DesktopSafetyError("protected input fields are not permitted")
