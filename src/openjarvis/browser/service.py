"""In-process owner for bounded browser sessions exposed by the local API."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from openjarvis.browser.actions import PublicBrowserNetworkPolicy, WebInjectionGuard
from openjarvis.browser.cdp import CdpBrowserAdapter, normalize_url
from openjarvis.browser.models import (
    BrowserControlHealth,
    BrowserRecoveryRecord,
    BrowserSession,
    BrowserSessionStatus,
)
from openjarvis.browser.process import BrowserOpenError, BrowserProcessManager
from openjarvis.browser.recovery import BrowserRecoveryController


class OwnedBrowserControl(Protocol):
    """Narrow target-bound CDP surface used by the public browser route."""

    @property
    def active_target_id(self) -> str | None: ...

    def connect(self, session: BrowserSession) -> bool: ...

    def open_tab(self, url: str, *, timeout: float = 15.0) -> str: ...

    def navigate(
        self,
        url: str,
        *,
        timeout: float = 15.0,
        target_id: str | None = None,
    ): ...

    def snapshot(self, *, target_id: str | None = None): ...

    def close_tab(self, target_id: str, *, timeout: float = 2.0) -> bool: ...

    def close(self) -> None: ...


class BrowserServiceNetworkPolicy(Protocol):
    def validate(self, url: str) -> str: ...


class BrowserSessionService:
    """Track only sessions created by one trusted browser manager."""

    def __init__(
        self,
        manager: BrowserProcessManager,
        recovery: BrowserRecoveryController,
        *,
        control_factory: Callable[[], OwnedBrowserControl] = CdpBrowserAdapter,
        network_policy: BrowserServiceNetworkPolicy | None = None,
    ) -> None:
        self.manager = manager
        self.recovery = recovery
        self.control_factory = control_factory
        self.network_policy = network_policy or PublicBrowserNetworkPolicy()
        self._sessions: dict[str, BrowserSession] = {}
        self._controls: dict[str, OwnedBrowserControl] = {}
        self._recovery_records: dict[str, list[BrowserRecoveryRecord]] = {}
        self._lock = threading.RLock()
        self._stopped = threading.Event()

    def create(self) -> BrowserSession:
        if self._stopped.is_set():
            raise BrowserOpenError(
                "browser sessions remain stopped until explicitly resumed"
            )
        with self._lock:
            session = self.manager.create_session()
            self._sessions[session.session_id] = session
        try:
            return self.manager.start(session)
        except Exception:
            # Keep the degraded owned session observable and recoverable.
            raise

    def get(self, session_id: str) -> BrowserSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> tuple[BrowserSession, ...]:
        with self._lock:
            return tuple(self._sessions.values())

    def health(self, session_id: str) -> BrowserControlHealth:
        session = self._require(session_id)
        return self.manager.health(session)

    def health_all(self) -> tuple[BrowserControlHealth, ...]:
        return tuple(self.manager.health(session) for session in self.list())

    def recover(self, session_id: str) -> BrowserRecoveryRecord:
        if self._stopped.is_set():
            raise BrowserOpenError("browser recovery is blocked by global stop")
        session = self._require(session_id)
        record = self.recovery.recover(session)
        with self._lock:
            self._recovery_records.setdefault(session_id, []).append(record)
        return record

    def recovery_records(
        self,
        session_id: str,
    ) -> tuple[BrowserRecoveryRecord, ...]:
        self._require(session_id)
        with self._lock:
            return tuple(self._recovery_records.get(session_id, ()))

    def close(self, session_id: str) -> BrowserSession:
        session = self._require(session_id)
        with self._lock:
            control = self._controls.pop(session_id, None)
        if control is not None:
            control.close()
        self.manager.close(session)
        return session

    def open_tab(
        self,
        session_id: str,
        url: str,
        *,
        timeout: float = 15.0,
    ) -> dict[str, object]:
        """Open and verify one target in an explicitly owned browser session."""

        requested_url = self.network_policy.validate(normalize_url(url))
        control = self._control(session_id)
        self._require_running()
        target_id = control.open_tab(requested_url, timeout=timeout)
        observation = control.snapshot(target_id=target_id)
        self._require_running()
        if observation.target_id != target_id:
            raise BrowserOpenError("browser open verification used another target")
        final_url = self.network_policy.validate(normalize_url(observation.url))
        if observation.ready_state not in {"interactive", "complete"}:
            raise BrowserOpenError("browser target did not reach a loaded state")
        findings = WebInjectionGuard().scan(observation.text).findings
        return {
            "session_id": session_id,
            "target_id": target_id,
            "requested_url": requested_url,
            "final_url": final_url,
            "ready_state": observation.ready_state,
            "title": observation.title,
            "verified": True,
            "navigation_verified": True,
            "content_trust": "untrusted",
            "injection_findings": list(findings),
        }

    def navigate(
        self,
        session_id: str,
        url: str,
        *,
        target_id: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, object]:
        """Navigate one stable target and verify its final URL and ready state."""

        requested_url = self.network_policy.validate(normalize_url(url))
        control = self._control(session_id)
        self._require_running()
        selected = target_id or control.active_target_id
        if not selected:
            raise BrowserOpenError("owned browser session has no active target")
        observation = control.navigate(
            requested_url,
            timeout=timeout,
            target_id=selected,
        )
        self._require_running()
        if observation.target_id != selected:
            raise BrowserOpenError("browser navigation verification used another target")
        final_url = self.network_policy.validate(normalize_url(observation.url))
        if observation.ready_state not in {"interactive", "complete"}:
            raise BrowserOpenError("browser target did not reach a loaded state")
        findings = WebInjectionGuard().scan(observation.text).findings
        return {
            "session_id": session_id,
            "target_id": selected,
            "requested_url": requested_url,
            "final_url": final_url,
            "ready_state": observation.ready_state,
            "title": observation.title,
            "verified": True,
            "navigation_verified": True,
            "content_trust": "untrusted",
            "injection_findings": list(findings),
        }

    def close_tab(
        self,
        session_id: str,
        target_id: str,
        *,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        control = self._control(session_id)
        self._require_running()
        verified = control.close_tab(target_id, timeout=timeout)
        self._require_running()
        return {
            "session_id": session_id,
            "target_id": target_id,
            "verified": bool(verified),
        }

    def cancel_all_owned(self) -> tuple[str, ...]:
        """Stop controls and only browser processes owned by this service."""

        self._stopped.set()
        with self._lock:
            controls = tuple(self._controls.values())
            self._controls.clear()
            sessions = tuple(self._sessions.values())
        for control in controls:
            control.close()
        cancelled = []
        for session in sessions:
            if session.status is BrowserSessionStatus.CLOSED:
                continue
            self.manager.cancel_owned(session)
            cancelled.append(session.session_id)
        return tuple(cancelled)

    def global_stop(self) -> tuple[str, ...]:
        return self.cancel_all_owned()

    def resume_after_stop(self) -> None:
        """Trusted task startup may explicitly allow newly-created sessions again."""

        self._stopped.clear()

    def _control(self, session_id: str) -> OwnedBrowserControl:
        session = self._require(session_id)
        if session.status is not BrowserSessionStatus.READY:
            raise BrowserOpenError("owned browser session is not ready")
        with self._lock:
            control = self._controls.get(session_id)
            if control is None:
                control = self.control_factory()
                if not control.connect(session):
                    raise BrowserOpenError("owned browser CDP connection failed")
                self._controls[session_id] = control
            return control

    def _require_running(self) -> None:
        if self._stopped.is_set():
            raise BrowserOpenError("browser operation interrupted by global stop")

    def _require(self, session_id: str) -> BrowserSession:
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"unknown browser session: {session_id}")
        return session


__all__ = [
    "BrowserServiceNetworkPolicy",
    "BrowserSessionService",
    "OwnedBrowserControl",
]
