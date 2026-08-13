"""Productive desktop grants, verification, and risk-boundary regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openjarvis.desktop.controller import (
    DesktopAccessMode,
    DesktopAccessStore,
    DesktopTargetGrant,
    ProductiveDesktopController,
    _resolve_launch_executable,
    desktop_tool_runtimes,
)
from openjarvis.desktop.models import DesktopElement, DesktopRect, DesktopWindow
from openjarvis.desktop.safety import DesktopSafetyError
from openjarvis.desktop.win32 import WindowsDesktopError
from openjarvis.tasks.policy import RiskLevel


def test_launch_executable_resolves_path_command(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "browser.exe"
    executable.write_bytes(b"synthetic")
    monkeypatch.setattr(
        "openjarvis.desktop.controller.shutil.which",
        lambda value: str(executable) if value == "browser.exe" else None,
    )

    assert _resolve_launch_executable("browser.exe") == executable.resolve()


class ProductiveFakeBackend:
    def __init__(self, executable: Path) -> None:
        self.executable = str(executable)
        self.window = DesktopWindow(
            10, 42, "Granted Editor", DesktopRect(-300, 20, 500, 620), 144
        )
        self.screenshots = 0
        self.clicked = False
        self.desktop_name = "Default"
        self.clipboard = "original clipboard"
        self.hotkeys = []
        self.focused = True
        self.modal = False
        self.started_at = 123456
        self.session_id = 7
        self.monitor = "DISPLAY1"
        self.terminate_calls = []
        self.after_text = None
        self.after_click = None
        self.after_window_state = None

    def input_desktop_name(self):
        return self.desktop_name

    def semantic_status(self):
        return "windows_uia"

    def interrupt_semantic(self):
        return None

    def visible_windows(self):
        return (self.window,)

    def process_executable(self, process_id):
        assert process_id in {42, 99}
        return self.executable

    def process_started_at(self, process_id):
        assert process_id in {42, 99}
        return self.started_at if process_id == 42 else 999999

    def process_session_id(self, process_id):
        assert process_id == 42
        return self.session_id

    def monitor_id(self, window):
        return self.monitor

    def is_modal_blocked(self, window):
        return self.modal

    def terminate_owned_process(self, process_id, started_at):
        self.terminate_calls.append((process_id, started_at))

    def is_owned_process(self, process_id, root_process_id):
        return process_id == root_process_id

    def refresh_window(self, window):
        return self.window

    def elements(self, window):
        return (
            DesktopElement(11, 42, "edit", "", 100, DesktopRect(-250, 80, 300, 180)),
            DesktopElement(
                12, 42, "button", "Send message", 101, DesktopRect(-250, 200, -50, 250)
            ),
            DesktopElement(
                13, 42, "button", "Continue", 102, DesktopRect(0, 200, 180, 250)
            ),
        )

    def find_element(self, window, *, automation_id, role):
        return next(
            item
            for item in self.elements(window)
            if item.automation_id == automation_id and item.role == role
        )

    def is_password_element(self, window, element):
        return False

    def focus(self, window):
        self.focused = True
        return self.focused

    def set_window_state(self, window, state):
        if self.after_window_state is not None:
            self.after_window_state()
        return state

    def is_focused(self, window):
        return self.focused

    def set_text(self, window, element, value):
        if self.after_text is not None:
            self.after_text()
        return value

    def click(self, window, element):
        self.clicked = True
        self.screenshots += 1
        if self.after_click is not None:
            self.after_click()

    def screenshot_window(self, window):
        self.screenshots += 1
        return b"BM" + str(self.screenshots).encode()

    def clipboard_read(self):
        return self.clipboard

    def clipboard_write(self, value):
        self.clipboard = value
        return value

    def send_hotkey(self, window, chord):
        self.hotkeys.append(chord)


def _controller(
    tmp_path: Path,
    *,
    browser_service=None,
    allow_browser_input_fallback: bool = False,
):
    executable = tmp_path / "editor.exe"
    executable.write_bytes(b"not executed")
    store = DesktopAccessStore(tmp_path / "desktop-access.json")
    store.put(
        DesktopTargetGrant(
            target_id="editor",
            label="Editor",
            executable=str(executable),
            title_contains="Granted",
            mode=DesktopAccessMode.INTERACT,
            capabilities=(
                "inspect",
                "screenshot",
                "focus",
                "type",
                "click",
                "hotkey",
                "scroll",
                "visual_click",
                "clipboard",
                "window",
                "launch",
            ),
        )
    )
    backend = ProductiveFakeBackend(executable)
    return ProductiveDesktopController(
        backend=backend,
        access_store=store,
        artifact_root=tmp_path / "artifacts",
        browser_service=browser_service,
        allow_browser_input_fallback=allow_browser_input_fallback,
    ), backend


def test_persistent_grant_attaches_to_one_existing_matching_process(
    tmp_path: Path,
) -> None:
    controller, _backend = _controller(tmp_path)
    window = controller.connect("editor")
    assert window.process_id == 42
    assert (
        DesktopAccessStore(tmp_path / "desktop-access.json").list()[0].target_id
        == "editor"
    )
    assert controller.inspect("editor")["verified"] is True


def test_normal_click_refuses_a_sensitive_control_without_allow_once(
    tmp_path: Path,
) -> None:
    controller, backend = _controller(tmp_path)
    controller.connect("editor")
    with pytest.raises(WindowsDesktopError, match="approval-scoped"):
        controller.click("editor", 101, "button")
    assert backend.clicked is False


def test_secure_desktop_is_a_hard_boundary(tmp_path: Path) -> None:
    controller, backend = _controller(tmp_path)
    backend.desktop_name = "Winlogon"
    with pytest.raises(WindowsDesktopError, match="Secure Desktop"):
        controller.connect("editor")


def test_tool_risks_keep_normal_and_sensitive_clicks_separate(tmp_path: Path) -> None:
    controller, _backend = _controller(tmp_path)
    manifests = {
        manifest.tool_id: manifest for manifest, _ in desktop_tool_runtimes(controller)
    }
    assert manifests["desktop.click"].risk_level is RiskLevel.REVERSIBLE_WORKSPACE
    assert manifests["desktop.click"].required_approval is False
    assert (
        manifests["desktop.sensitive_click"].risk_level
        is RiskLevel.DESTRUCTIVE_OR_SENSITIVE
    )
    assert manifests["desktop.sensitive_click"].required_approval is False
    assert manifests["desktop.visual_click"].required_approval is False


def test_public_open_tab_uses_owned_cdp_service_without_input_fallback(
    tmp_path: Path,
) -> None:
    class FakeOwnedBrowser:
        def __init__(self) -> None:
            self.calls = []

        def create(self):
            return SimpleNamespace(session_id="browser-session-1", status="ready")

        def open_tab(self, session_id, url, *, timeout=15.0):
            self.calls.append((session_id, url, timeout))
            return {
                "session_id": session_id,
                "target_id": "cdp-target-1",
                "requested_url": url,
                "final_url": "https://example.com/final",
                "ready_state": "complete",
                "navigation_verified": True,
                "verified": True,
                "content_trust": "untrusted",
            }

    browser = FakeOwnedBrowser()
    controller, backend = _controller(tmp_path, browser_service=browser)
    runtimes = {
        manifest.tool_id: runtime
        for manifest, runtime in desktop_tool_runtimes(controller)
    }
    created = runtimes["browser.create_session"].handler({})
    navigated = runtimes["browser.open_tab"].handler(
        {"session_id": created["session_id"], "url": "https://example.com/redirect"}
    )

    assert created == {
        "session_id": "browser-session-1",
        "status": "ready",
        "verified": True,
    }
    assert navigated["verified"] is True
    assert navigated["target_id"] == "cdp-target-1"
    assert browser.calls == [
        ("browser-session-1", "https://example.com/redirect", 15.0)
    ]
    assert backend.hotkeys == []
    manifests = {tool_id: runtime for tool_id, runtime in runtimes.items()}
    assert {
        "browser.windows",
        "browser.create_session",
        "browser.navigate",
        "browser.open_tab",
    }.issubset(manifests)

    legacy = runtimes["browser.open_tab"].handler(
        {"target_id": "browser-session-1", "url": "https://example.com/legacy"}
    )
    assert legacy["verified"] is True


def test_input_fallback_never_claims_verified_navigation(tmp_path: Path) -> None:
    controller, backend = _controller(
        tmp_path, allow_browser_input_fallback=True
    )
    controller.connect("editor")

    result = controller.browser_open_tab("editor", "https://example.com")

    assert result["fallback"] == "focus_clipboard_hotkey"
    assert result["verified"] is False
    assert result["navigation_verified"] is False
    assert backend.hotkeys == ["ctrl+t", "ctrl+l", "ctrl+v", "enter"]
    assert backend.clipboard == "original clipboard"


def test_guard_rejects_focus_identity_dpi_and_modality_changes(
    tmp_path: Path,
) -> None:
    controller, backend = _controller(tmp_path)
    controller.connect("editor")

    backend.modal = True
    with pytest.raises(DesktopSafetyError, match="modal"):
        controller.click("editor", 102, "button")
    assert backend.clicked is False

    backend.modal = False
    backend.after_click = lambda: setattr(backend, "focused", False)
    with pytest.raises(DesktopSafetyError, match="postcondition"):
        controller.click("editor", 102, "button")

    backend.focused = True
    backend.after_click = None
    backend.after_text = lambda: setattr(backend, "started_at", 999999)
    with pytest.raises(DesktopSafetyError, match="identity changed"):
        controller.set_text("editor", 100, "edit", "hello")

    backend.started_at = 123456
    backend.after_text = lambda: setattr(
        backend,
        "window",
        DesktopWindow(
            backend.window.handle,
            backend.window.process_id,
            backend.window.title,
            backend.window.bounds,
            192,
        ),
    )
    with pytest.raises(DesktopSafetyError, match="postcondition"):
        controller.set_text("editor", 100, "edit", "hello")


def test_global_stop_does_not_terminate_connected_foreign_process(
    tmp_path: Path,
) -> None:
    controller, backend = _controller(tmp_path)
    controller.connect("editor")

    controller.global_stop()

    assert backend.terminate_calls == []
    with pytest.raises(WindowsDesktopError, match="interrupted"):
        controller.inspect("editor")


def test_window_state_allows_expected_bounds_change(tmp_path: Path) -> None:
    controller, backend = _controller(tmp_path)
    controller.connect("editor")
    backend.after_window_state = lambda: setattr(
        backend,
        "window",
        DesktopWindow(10, 42, "Granted Editor", DesktopRect(0, 0, 1920, 1080), 144),
    )

    result = controller.set_window_state("editor", "maximized")

    assert result["verified"] is True
    assert result["state"] == "maximized"


def test_launch_never_adopts_same_executable_foreign_process(
    monkeypatch, tmp_path: Path
) -> None:
    controller, backend = _controller(tmp_path)
    spawned = False

    def visible_windows():
        return (backend.window,) if spawned else ()

    class FakeProcess:
        pid = 99

        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal spawned
            spawned = True

    monotonic = iter((0.0, 1.0, 13.0))
    monkeypatch.setattr(backend, "visible_windows", visible_windows)
    monkeypatch.setattr("openjarvis.desktop.controller.subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "openjarvis.desktop.controller.time.monotonic", lambda: next(monotonic)
    )
    monkeypatch.setattr("openjarvis.desktop.controller.time.sleep", lambda _value: None)

    with pytest.raises(WindowsDesktopError, match="did not expose"):
        controller.launch("editor")
    controller.global_stop()

    assert backend.terminate_calls == [(99, 999999)]


def test_launch_application_without_owned_window_never_reports_success(
    monkeypatch, tmp_path: Path
) -> None:
    controller, backend = _controller(tmp_path)
    controller.flow_authority = SimpleNamespace(is_flow=lambda: True)

    class FakeProcess:
        pid = 99

        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monotonic = iter((0.0, 16.0))
    monkeypatch.setattr(backend, "visible_windows", lambda: ())
    monkeypatch.setattr("openjarvis.desktop.controller.subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "openjarvis.desktop.controller.time.monotonic", lambda: next(monotonic)
    )

    with pytest.raises(WindowsDesktopError, match="owned verified window"):
        controller.launch_application(backend.executable)
    controller.global_stop()

    assert backend.terminate_calls == [(99, 999999)]
