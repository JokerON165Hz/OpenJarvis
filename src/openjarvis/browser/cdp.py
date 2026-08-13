"""Synchronous, target-bound CDP control for an owned Chromium session."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from openjarvis.browser.models import BrowserSession


class BrowserErrorCode(str, Enum):
    """Stable machine-readable browser control failures."""

    NOT_CONNECTED = "not_connected"
    CONNECTION_LOST = "connection_lost"
    COMMAND_FAILED = "command_failed"
    COMMAND_TIMEOUT = "command_timeout"
    INVALID_RESPONSE = "invalid_response"
    INVALID_URL = "invalid_url"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_CLOSED = "target_closed"
    TARGET_MISMATCH = "target_mismatch"
    NAVIGATION_FAILED = "navigation_failed"
    NAVIGATION_TIMEOUT = "navigation_timeout"


class BrowserControlError(RuntimeError):
    """Structured browser error safe to pass through tool boundaries."""

    def __init__(
        self,
        message: str,
        *,
        code: BrowserErrorCode = BrowserErrorCode.COMMAND_FAILED,
        operation: str | None = None,
        target_id: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation
        self.target_id = target_id
        self.retryable = retryable
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "operation": self.operation,
            "target_id": self.target_id,
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class BrowserTab:
    """One stable CDP page target within a browser session."""

    tab_id: str
    target_id: str
    url: str
    title: str
    active: bool = False
    requested_url: str | None = None
    final_url: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    url: str
    title: str
    ready_state: str
    text: str
    target_id: str = ""
    content_trust: str = "untrusted"


@dataclass(frozen=True, slots=True)
class BrowserExtraction:
    """Structured page data whose provenance and trust are explicit."""

    target_id: str
    url: str
    title: str
    kind: str
    data: Any
    content_trust: str = "untrusted"


@dataclass(slots=True)
class _PendingTabOpen:
    requested_url: str
    marker_url: str
    target_id: str | None = None


def normalize_url(url: str) -> str:
    """Normalize a browser URL for verification without hiding redirects."""

    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise BrowserControlError(
            "browser URL must be an absolute HTTP(S) URL",
            code=BrowserErrorCode.INVALID_URL,
            operation="normalize_url",
        )
    if parsed.username or parsed.password:
        raise BrowserControlError(
            "browser URL credentials are blocked",
            code=BrowserErrorCode.INVALID_URL,
            operation="normalize_url",
        )
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold().rstrip(".")
    port = parsed.port
    default_port = 443 if scheme == "https" else 80
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit(SplitResult(scheme, netloc, path, parsed.query, parsed.fragment))


def _urls_equivalent(first: str, second: str) -> bool:
    try:
        return normalize_url(first) == normalize_url(second)
    except BrowserControlError:
        return first == second


class CdpBrowserAdapter:
    """CDP operations bound to one verifiable page target at a time."""

    def __init__(self) -> None:
        self._socket = None
        self._message_id = 0
        self._session: BrowserSession | None = None
        self._session_id: str | None = None
        self._target_id: str | None = None
        self._tabs: dict[str, BrowserTab] = {}
        self._pending_open: _PendingTabOpen | None = None

    @property
    def active_tab_id(self) -> str | None:
        return self._target_id

    @property
    def active_target_id(self) -> str | None:
        return self._target_id

    def connect(self, session: BrowserSession) -> bool:
        """Connect to an owned session, preserving a still-live active target."""

        previous_target = self._target_id if self._session_id == session.session_id else None
        self._disconnect_socket()
        self._session = session
        self._session_id = session.session_id
        try:
            targets = self._fetch_targets(session)
            target = None
            if previous_target is not None:
                target = self._find_target(targets, previous_target)
            if target is None:
                target = next(item for item in targets if item.get("type") == "page")
            self._connect_target(target)
            self._refresh_tabs(targets)
            return True
        except Exception:
            self._disconnect_socket()
            return False

    def reconnect(self, session: BrowserSession) -> bool:
        """Reconnect without silently switching to another tab."""

        expected_target = self._target_id if self._session_id == session.session_id else None
        self._disconnect_socket()
        self._session = session
        self._session_id = session.session_id
        try:
            targets = self._fetch_targets(session)
            if expected_target is None:
                target = next(item for item in targets if item.get("type") == "page")
            else:
                target = self._find_target(targets, expected_target)
                if target is None:
                    raise self._target_error(expected_target, "reconnect")
            self._connect_target(target)
            self._refresh_tabs(targets)
            return True
        except Exception:
            self._disconnect_socket()
            return False

    def close(self) -> None:
        self._disconnect_socket()
        self._session = None
        self._session_id = None
        self._target_id = None
        self._tabs.clear()
        self._pending_open = None

    def _disconnect_socket(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
        self._socket = None

    def _fetch_targets(self, session: BrowserSession | None = None) -> list[dict]:
        current = session or self._session
        if current is None:
            raise BrowserControlError(
                "CDP adapter is not connected",
                code=BrowserErrorCode.NOT_CONNECTED,
                operation="list_targets",
            )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{current.control_port}/json/list",
                timeout=2,
            ) as response:
                value = json.loads(response.read(512 * 1024).decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BrowserControlError(
                "CDP target list is unavailable",
                code=BrowserErrorCode.CONNECTION_LOST,
                operation="list_targets",
                target_id=self._target_id,
                retryable=True,
            ) from exc
        if not isinstance(value, list):
            raise BrowserControlError(
                "CDP target list is invalid",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="list_targets",
            )
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _find_target(targets: list[dict], target_id: str) -> dict | None:
        return next(
            (
                item
                for item in targets
                if str(item.get("id") or item.get("targetId") or "") == target_id and item.get("type") == "page"
            ),
            None,
        )

    @staticmethod
    def _target_identifier(target: dict) -> str:
        return str(target.get("id") or target.get("targetId") or "")

    def _validate_websocket_url(self, websocket_url: str) -> str:
        if self._session is None:
            raise BrowserControlError(
                "CDP adapter has no browser session",
                code=BrowserErrorCode.NOT_CONNECTED,
                operation="connect_target",
            )
        parsed = urlsplit(websocket_url)
        if (
            parsed.scheme != "ws"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port != self._session.control_port
            or parsed.username
            or parsed.password
        ):
            raise BrowserControlError(
                "CDP websocket escaped loopback control port",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="connect_target",
            )
        return websocket_url

    def _dial_websocket(self, websocket_url: str):
        from websockets.sync.client import connect

        return connect(
            websocket_url,
            open_timeout=2,
            close_timeout=1,
            proxy=None,
        )

    def _connect_target(self, target: dict) -> None:
        target_id = self._target_identifier(target)
        websocket_url = str(target.get("webSocketDebuggerUrl") or "")
        if not target_id or not websocket_url:
            raise BrowserControlError(
                "CDP page target has no stable ID or websocket",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="connect_target",
            )
        self._validate_websocket_url(websocket_url)
        self._disconnect_socket()
        try:
            self._socket = self._dial_websocket(websocket_url)
            self._target_id = target_id
            self._command("Runtime.enable")
            self._command("Page.enable")
        except BrowserControlError:
            self._disconnect_socket()
            raise
        except Exception as exc:
            self._disconnect_socket()
            raise BrowserControlError(
                "CDP websocket connection failed",
                code=BrowserErrorCode.CONNECTION_LOST,
                operation="connect_target",
                target_id=target_id,
                retryable=True,
            ) from exc

    def _refresh_tabs(self, targets: list[dict]) -> None:
        observed: dict[str, BrowserTab] = {}
        for target in targets:
            if target.get("type") != "page":
                continue
            target_id = self._target_identifier(target)
            if not target_id:
                continue
            previous = self._tabs.get(target_id)
            url = str(target.get("url") or "")
            observed[target_id] = BrowserTab(
                tab_id=target_id,
                target_id=target_id,
                url=url,
                title=str(target.get("title") or ""),
                active=target_id == self._target_id,
                requested_url=previous.requested_url if previous else None,
                final_url=(previous.final_url if previous else None) or url or None,
            )
        self._tabs = observed

    def list_tabs(self) -> tuple[BrowserTab, ...]:
        targets = self._fetch_targets()
        self._refresh_tabs(targets)
        return tuple(self._tabs.values())

    def tab(self, target_id: str) -> BrowserTab:
        targets = self._fetch_targets()
        target = self._find_target(targets, target_id)
        if target is None:
            raise self._target_error(target_id, "get_tab")
        self._refresh_tabs(targets)
        return self._tabs[target_id]

    def activate_tab(self, target_id: str) -> BrowserTab:
        targets = self._fetch_targets()
        target = self._find_target(targets, target_id)
        if target is None:
            raise self._target_error(target_id, "activate_tab")
        if self._target_id != target_id or self._socket is None:
            self._connect_target(target)
        self._command("Target.activateTarget", {"targetId": target_id})
        targets = self._fetch_targets()
        if self._find_target(targets, target_id) is None:
            raise self._target_error(target_id, "activate_tab")
        self._refresh_tabs(targets)
        return self._tabs[target_id]

    def _target_error(self, target_id: str, operation: str) -> BrowserControlError:
        known = target_id in self._tabs or target_id == self._target_id
        return BrowserControlError(
            f"browser target is {'closed' if known else 'unknown'}: {target_id}",
            code=(BrowserErrorCode.TARGET_CLOSED if known else BrowserErrorCode.TARGET_NOT_FOUND),
            operation=operation,
            target_id=target_id,
        )

    def _command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        if self._socket is None:
            raise BrowserControlError(
                "CDP adapter is not connected",
                code=BrowserErrorCode.NOT_CONNECTED,
                operation=method,
                target_id=self._target_id,
                retryable=True,
            )
        self._message_id += 1
        message_id = self._message_id
        try:
            self._socket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        except Exception as exc:
            self._disconnect_socket()
            raise BrowserControlError(
                f"CDP connection was lost while sending {method}",
                code=BrowserErrorCode.CONNECTION_LOST,
                operation=method,
                target_id=self._target_id,
                retryable=True,
            ) from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._socket.recv(timeout=max(deadline - time.monotonic(), 0.01))
            except TimeoutError:
                continue
            except Exception as exc:
                self._disconnect_socket()
                raise BrowserControlError(
                    f"CDP connection was lost while waiting for {method}",
                    code=BrowserErrorCode.CONNECTION_LOST,
                    operation=method,
                    target_id=self._target_id,
                    retryable=True,
                ) from exc
            try:
                message = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise BrowserControlError(
                    "CDP returned invalid JSON",
                    code=BrowserErrorCode.INVALID_RESPONSE,
                    operation=method,
                    target_id=self._target_id,
                ) from exc
            if message.get("id") != message_id:
                continue
            if "error" in message:
                error_message = str(message["error"].get("message", "unknown CDP error"))
                target_closed = "target" in error_message.casefold() and any(
                    token in error_message.casefold() for token in ("closed", "not found", "no target")
                )
                raise BrowserControlError(
                    f"{method} failed: {error_message}",
                    code=(BrowserErrorCode.TARGET_CLOSED if target_closed else BrowserErrorCode.COMMAND_FAILED),
                    operation=method,
                    target_id=self._target_id,
                    details={"cdp_error": message["error"]},
                )
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise BrowserControlError(
                    f"{method} returned an invalid result",
                    code=BrowserErrorCode.INVALID_RESPONSE,
                    operation=method,
                    target_id=self._target_id,
                )
            return dict(result)
        raise BrowserControlError(
            f"CDP command timed out: {method}",
            code=BrowserErrorCode.COMMAND_TIMEOUT,
            operation=method,
            target_id=self._target_id,
            retryable=True,
        )

    def _ensure_target(self, target_id: str | None, operation: str) -> str:
        selected = target_id or self._target_id
        if selected is None:
            raise BrowserControlError(
                "CDP adapter is not connected to a page target",
                code=BrowserErrorCode.NOT_CONNECTED,
                operation=operation,
            )
        if selected != self._target_id or self._socket is None:
            self.activate_tab(selected)
        return selected

    def evaluate(
        self,
        expression: str,
        *,
        target_id: str | None = None,
        timeout: float = 10.0,
    ) -> Any:
        selected = self._ensure_target(target_id, "evaluate")
        result = self._command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        remote = result.get("result", {})
        if remote.get("subtype") == "error" or result.get("exceptionDetails"):
            raise BrowserControlError(
                "JavaScript evaluation failed",
                code=BrowserErrorCode.COMMAND_FAILED,
                operation="evaluate",
                target_id=selected,
                details={"exception": result.get("exceptionDetails")},
            )
        return remote.get("value")

    def snapshot(self, *, target_id: str | None = None) -> BrowserObservation:
        selected = self._ensure_target(target_id, "snapshot")
        value = self.evaluate(
            """(() => ({
                url: location.href,
                title: document.title,
                ready_state: document.readyState,
                text: document.body
                    ? document.body.innerText
                    : (document.documentElement
                        ? document.documentElement.textContent
                        : '')
            }))()""",
            target_id=selected,
        )
        if not isinstance(value, dict):
            raise BrowserControlError(
                "browser observation is invalid",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="snapshot",
                target_id=selected,
            )
        observation = BrowserObservation(
            url=str(value.get("url", "")),
            title=str(value.get("title", "")),
            ready_state=str(value.get("ready_state", "")),
            text=str(value.get("text", ""))[:1_000_000],
            target_id=selected,
        )
        self._verify_observation_target(observation)
        return observation

    def _verify_observation_target(self, observation: BrowserObservation) -> None:
        targets = self._fetch_targets()
        target = self._find_target(targets, observation.target_id)
        if target is None:
            raise self._target_error(observation.target_id, "snapshot")
        target_url = str(target.get("url") or "")
        if target_url and target_url != observation.url:
            if not _urls_equivalent(target_url, observation.url):
                raise BrowserControlError(
                    "browser observation came from an unexpected target state",
                    code=BrowserErrorCode.TARGET_MISMATCH,
                    operation="snapshot",
                    target_id=observation.target_id,
                    details={
                        "target_url": target_url,
                        "observation_url": observation.url,
                    },
                )
        self._refresh_tabs(targets)

    def navigate(
        self,
        url: str,
        *,
        timeout: float = 15.0,
        target_id: str | None = None,
    ) -> BrowserObservation:
        requested_url = normalize_url(url)
        selected = self._ensure_target(target_id, "navigate")
        before_url = self.snapshot(target_id=selected).url
        result = self._command("Page.navigate", {"url": requested_url})
        if result.get("errorText"):
            raise BrowserControlError(
                f"navigation failed: {result['errorText']}",
                code=BrowserErrorCode.NAVIGATION_FAILED,
                operation="navigate",
                target_id=selected,
                details={"requested_url": requested_url},
            )
        observation = self._wait_ready(
            timeout,
            target_id=selected,
            requested_url=requested_url,
            previous_url=before_url,
        )
        self._record_navigation(selected, requested_url, observation.url)
        return observation

    def reload(
        self,
        *,
        timeout: float = 15.0,
        target_id: str | None = None,
    ) -> BrowserObservation:
        selected = self._ensure_target(target_id, "reload")
        self._command("Page.reload", {"ignoreCache": True})
        return self._wait_ready(timeout, target_id=selected)

    def _wait_ready(
        self,
        timeout: float,
        *,
        target_id: str,
        requested_url: str | None = None,
        previous_url: str | None = None,
    ) -> BrowserObservation:
        deadline = time.monotonic() + timeout
        last: BrowserObservation | None = None
        while time.monotonic() < deadline:
            try:
                last = self.snapshot(target_id=target_id)
            except BrowserControlError as exc:
                if exc.code in {
                    BrowserErrorCode.TARGET_CLOSED,
                    BrowserErrorCode.TARGET_NOT_FOUND,
                }:
                    raise
                if exc.code is BrowserErrorCode.COMMAND_FAILED:
                    time.sleep(0.05)
                    continue
                if exc.code is BrowserErrorCode.TARGET_MISMATCH:
                    time.sleep(0.05)
                    continue
                raise
            changed = (
                requested_url is None
                or previous_url is None
                or _urls_equivalent(requested_url, previous_url)
                or last.url != previous_url
            )
            if (
                changed
                and last.ready_state in {"interactive", "complete"}
                and last.url
                and not last.url.startswith("about:blank#openjarvis-")
            ):
                return last
            time.sleep(0.05)
        raise BrowserControlError(
            "document did not become ready before the navigation timeout",
            code=BrowserErrorCode.NAVIGATION_TIMEOUT,
            operation="navigate",
            target_id=target_id,
            retryable=True,
            details={
                "requested_url": requested_url,
                "final_url": last.url if last else None,
                "ready_state": last.ready_state if last else None,
            },
        )

    def _record_navigation(self, target_id: str, requested_url: str, final_url: str) -> None:
        tab = self._tabs.get(target_id)
        if tab is None:
            tab = BrowserTab(target_id, target_id, final_url, "")
        self._tabs[target_id] = replace(
            tab,
            url=final_url,
            requested_url=requested_url,
            final_url=final_url,
            active=target_id == self._target_id,
        )

    def element_info(self, selector: str, *, target_id: str | None = None) -> dict[str, Any] | None:
        encoded = json.dumps(selector)
        value = self.evaluate(
            f"""(() => {{
              const e=document.querySelector({encoded});
              if(!e) return null;
              return {{tag:e.tagName.toLowerCase(),type:(e.type||'').toLowerCase(),
                value:e.value||'',visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}};
            }})()""",
            target_id=target_id,
        )
        return value if isinstance(value, dict) else None

    def click(self, selector: str, *, target_id: str | None = None) -> BrowserObservation:
        selected = self._ensure_target(target_id, "click")
        encoded = json.dumps(selector)
        clicked = self.evaluate(
            f"""(() => {{const e=document.querySelector({encoded});
            if(!e) return false;e.click();return true;}})()""",
            target_id=selected,
        )
        if clicked is not True:
            raise BrowserControlError(
                f"element not found: {selector}",
                code=BrowserErrorCode.COMMAND_FAILED,
                operation="click",
                target_id=selected,
            )
        time.sleep(0.05)
        return self.snapshot(target_id=selected)

    def fill(self, selector: str, text: str, *, target_id: str | None = None) -> BrowserObservation:
        selected = self._ensure_target(target_id, "fill")
        encoded_selector = json.dumps(selector)
        encoded_text = json.dumps(text)
        value = self.evaluate(
            f"""(() => {{const e=document.querySelector({encoded_selector});
            if(!e) return null;e.focus();e.value={encoded_text};
            e.dispatchEvent(new Event('input',{{bubbles:true}}));
            e.dispatchEvent(new Event('change',{{bubbles:true}}));
            return e.value;}})()""",
            target_id=selected,
        )
        if value is None:
            raise BrowserControlError(
                f"input not found: {selector}",
                code=BrowserErrorCode.COMMAND_FAILED,
                operation="fill",
                target_id=selected,
            )
        return self.snapshot(target_id=selected)

    def value(self, selector: str, *, target_id: str | None = None) -> str | None:
        encoded = json.dumps(selector)
        value = self.evaluate(
            f"""(() => {{const e=document.querySelector({encoded});
            return e?e.value:null;}})()""",
            target_id=target_id,
        )
        return None if value is None else str(value)

    def select(self, selector: str, value: str, *, target_id: str | None = None) -> BrowserObservation:
        selected = self._ensure_target(target_id, "select")
        encoded_selector = json.dumps(selector)
        encoded_value = json.dumps(value)
        selected_value = self.evaluate(
            f"""(() => {{const e=document.querySelector({encoded_selector});
            if(!e) return null;e.value={encoded_value};
            e.dispatchEvent(new Event('change',{{bubbles:true}}));
            return e.value;}})()""",
            target_id=selected,
        )
        if selected_value != value:
            raise BrowserControlError(
                "select verification failed",
                code=BrowserErrorCode.COMMAND_FAILED,
                operation="select",
                target_id=selected,
            )
        return self.snapshot(target_id=selected)

    def scroll(self, delta_y: int, *, target_id: str | None = None) -> BrowserObservation:
        selected = self._ensure_target(target_id, "scroll")
        self.evaluate(
            f"window.scrollBy(0,{int(delta_y)});window.scrollY",
            target_id=selected,
        )
        return self.snapshot(target_id=selected)

    def screenshot(self, *, target_id: str | None = None) -> bytes:
        selected = self._ensure_target(target_id, "screenshot")
        result = self._command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        try:
            data = base64.b64decode(result["data"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise BrowserControlError(
                "browser screenshot payload is invalid",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="screenshot",
                target_id=selected,
            ) from exc
        self._require_live_target(selected, "screenshot")
        return data

    def extract_dom(
        self,
        *,
        target_id: str | None = None,
        max_nodes: int = 5_000,
    ) -> BrowserExtraction:
        selected = self._ensure_target(target_id, "extract_dom")
        limit = max(1, min(int(max_nodes), 20_000))
        data = self.evaluate(
            f"""(() => {{
              const result=[];
              const walker=document.createTreeWalker(
                document.documentElement, NodeFilter.SHOW_ELEMENT);
              let node=walker.currentNode;
              while(node && result.length < {limit}) {{
                result.push({{
                  tag: node.tagName.toLowerCase(),
                  role: node.getAttribute('role') || '',
                  name: node.getAttribute('aria-label') || '',
                  text: (node.childElementCount ? '' : (node.textContent || ''))
                    .trim().slice(0, 200)
                }});
                node=walker.nextNode();
              }}
              return result;
            }})()""",
            target_id=selected,
        )
        if not isinstance(data, list):
            raise BrowserControlError(
                "DOM extraction is invalid",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="extract_dom",
                target_id=selected,
            )
        observation = self.snapshot(target_id=selected)
        return BrowserExtraction(
            target_id=selected,
            url=observation.url,
            title=observation.title,
            kind="dom",
            data=tuple(item for item in data if isinstance(item, dict)),
        )

    def accessibility_tree(
        self,
        *,
        target_id: str | None = None,
        max_nodes: int = 5_000,
    ) -> BrowserExtraction:
        selected = self._ensure_target(target_id, "accessibility_tree")
        result = self._command("Accessibility.getFullAXTree")
        nodes = result.get("nodes")
        if not isinstance(nodes, list):
            raise BrowserControlError(
                "accessibility tree is invalid",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="accessibility_tree",
                target_id=selected,
            )
        normalized = []
        for node in nodes[: max(1, min(int(max_nodes), 20_000))]:
            if not isinstance(node, dict):
                continue
            normalized.append(
                {
                    "node_id": str(node.get("nodeId") or ""),
                    "role": _ax_value(node.get("role")),
                    "name": _ax_value(node.get("name")),
                    "value": _ax_value(node.get("value")),
                    "child_ids": tuple(str(value) for value in node.get("childIds", ())),
                }
            )
        observation = self.snapshot(target_id=selected)
        return BrowserExtraction(
            target_id=selected,
            url=observation.url,
            title=observation.title,
            kind="accessibility",
            data=tuple(normalized),
        )

    def prepare_downloads(self, download_root: Path) -> None:
        self._command(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(download_root),
                "eventsEnabled": True,
            },
        )

    def prepare_upload(self, selector: str, file_path: Path) -> str:
        encoded = json.dumps(selector)
        evaluated = self._command(
            "Runtime.evaluate",
            {"expression": f"document.querySelector({encoded})"},
        )
        object_id = evaluated.get("result", {}).get("objectId")
        if not object_id:
            raise BrowserControlError("upload input not found")
        node = self._command("DOM.describeNode", {"objectId": object_id})
        backend_node_id = node.get("node", {}).get("backendNodeId")
        if not backend_node_id:
            raise BrowserControlError("upload input has no stable backend node")
        self._command(
            "DOM.setFileInputFiles",
            {
                "files": [str(file_path)],
                "backendNodeId": backend_node_id,
            },
        )
        value = self.value(selector) or ""
        return value.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]

    def open_tab(self, url: str, *, timeout: float = 15.0) -> str:
        """Create, bind, navigate, and verify exactly one new page target."""

        requested_url = normalize_url(url)
        self._ensure_target(None, "open_tab")
        pending = self._pending_open
        if pending is None or pending.requested_url != requested_url:
            marker = f"about:blank#openjarvis-{uuid.uuid4().hex}"
            pending = _PendingTabOpen(requested_url, marker)
            self._pending_open = pending

        target = self._recover_pending_target(pending)
        if target is None:
            try:
                result = self._command("Target.createTarget", {"url": pending.marker_url})
                pending.target_id = str(result.get("targetId") or "") or None
            except BrowserControlError as exc:
                if exc.code not in {
                    BrowserErrorCode.CONNECTION_LOST,
                    BrowserErrorCode.COMMAND_TIMEOUT,
                }:
                    raise
                target = self._reconnect_and_find_pending(pending, timeout=0.75)
                if target is None:
                    result = self._command("Target.createTarget", {"url": pending.marker_url})
                    pending.target_id = str(result.get("targetId") or "") or None
            if target is None and pending.target_id:
                target = self._wait_for_target(pending.target_id, timeout=min(timeout, 2.0))
        if target is None:
            raise BrowserControlError(
                "Target.createTarget did not produce a verifiable page target",
                code=BrowserErrorCode.INVALID_RESPONSE,
                operation="open_tab",
                target_id=pending.target_id,
            )

        target_id = self._target_identifier(target)
        pending.target_id = target_id
        self.activate_tab(target_id)
        observed_target_url = str(target.get("url") or "")
        if observed_target_url and observed_target_url != pending.marker_url:
            # A previous call may have timed out after navigation took effect.
            # Observe that same target to completion instead of sending a
            # duplicate navigation on retry.
            observation = self._wait_ready(
                timeout,
                target_id=target_id,
                requested_url=requested_url,
                previous_url=pending.marker_url,
            )
        else:
            try:
                observation = self.navigate(
                    requested_url,
                    timeout=timeout,
                    target_id=target_id,
                )
            except BrowserControlError as exc:
                if exc.code not in {
                    BrowserErrorCode.CONNECTION_LOST,
                    BrowserErrorCode.COMMAND_TIMEOUT,
                }:
                    raise
                observation = self._recover_navigation(pending, timeout)

        if observation.target_id != target_id:
            raise BrowserControlError(
                "open_tab verification used the wrong browser target",
                code=BrowserErrorCode.TARGET_MISMATCH,
                operation="open_tab",
                target_id=target_id,
            )
        normalize_url(observation.url)
        self._require_live_target(target_id, "open_tab")
        self._record_navigation(target_id, requested_url, observation.url)
        self._pending_open = None
        return target_id

    def _recover_pending_target(self, pending: _PendingTabOpen) -> dict | None:
        targets = self._fetch_targets()
        if pending.target_id:
            target = self._find_target(targets, pending.target_id)
            if target is not None:
                return target
        return next(
            (
                item
                for item in targets
                if item.get("type") == "page" and str(item.get("url") or "") == pending.marker_url
            ),
            None,
        )

    def _reconnect_and_find_pending(self, pending: _PendingTabOpen, *, timeout: float) -> dict | None:
        if self._session is None or not self.reconnect(self._session):
            raise BrowserControlError(
                "CDP reconnect failed while opening a tab",
                code=BrowserErrorCode.CONNECTION_LOST,
                operation="open_tab",
                target_id=pending.target_id,
                retryable=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            target = self._recover_pending_target(pending)
            if target is not None:
                return target
            time.sleep(0.05)
        return None

    def _recover_navigation(self, pending: _PendingTabOpen, timeout: float) -> BrowserObservation:
        target_id = pending.target_id
        if target_id is None or self._session is None:
            raise BrowserControlError(
                "navigation recovery has no stable target",
                code=BrowserErrorCode.TARGET_NOT_FOUND,
                operation="open_tab",
                target_id=target_id,
            )
        if not self.reconnect(self._session):
            raise self._target_error(target_id, "open_tab")
        observation = self.snapshot(target_id=target_id)
        if observation.url == pending.marker_url:
            return self.navigate(
                pending.requested_url,
                timeout=timeout,
                target_id=target_id,
            )
        return self._wait_ready(
            timeout,
            target_id=target_id,
            requested_url=pending.requested_url,
            previous_url=pending.marker_url,
        )

    def _wait_for_target(self, target_id: str, *, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            targets = self._fetch_targets()
            target = self._find_target(targets, target_id)
            if target is not None:
                return target
            time.sleep(0.05)
        return None

    def _require_live_target(self, target_id: str, operation: str) -> dict:
        targets = self._fetch_targets()
        target = self._find_target(targets, target_id)
        if target is None:
            raise self._target_error(target_id, operation)
        self._refresh_tabs(targets)
        return target

    def close_tab(self, target_id: str, *, timeout: float = 2.0) -> bool:
        self._require_live_target(target_id, "close_tab")
        try:
            result = self._command("Target.closeTarget", {"targetId": target_id})
        except BrowserControlError as exc:
            if exc.code not in {
                BrowserErrorCode.CONNECTION_LOST,
                BrowserErrorCode.TARGET_CLOSED,
            }:
                raise
            targets = self._fetch_targets()
            if self._find_target(targets, target_id) is None:
                self._finish_closed_target(target_id, targets)
                return True
            raise
        if not bool(result.get("success")):
            raise BrowserControlError(
                "browser target did not acknowledge close",
                code=BrowserErrorCode.COMMAND_FAILED,
                operation="close_tab",
                target_id=target_id,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            targets = self._fetch_targets()
            if self._find_target(targets, target_id) is None:
                self._finish_closed_target(target_id, targets)
                return True
            time.sleep(0.05)
        raise BrowserControlError(
            "browser target remained open after close acknowledgement",
            code=BrowserErrorCode.COMMAND_TIMEOUT,
            operation="close_tab",
            target_id=target_id,
            retryable=True,
        )

    def _finish_closed_target(self, target_id: str, targets: list[dict]) -> None:
        if self._target_id == target_id:
            self._disconnect_socket()
            self._target_id = None
        self._refresh_tabs(targets)


def _ax_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return ""


__all__ = [
    "BrowserControlError",
    "BrowserErrorCode",
    "BrowserExtraction",
    "BrowserObservation",
    "BrowserTab",
    "CdpBrowserAdapter",
    "normalize_url",
]
