"""Browser automation tools — Playwright-based web interaction."""

from __future__ import annotations

import base64
import uuid
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


class _BrowserSession:
    """Manages a shared Playwright browser session (lazy init)."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None
        self._pages: dict[str, Any] = {}
        self._active_tab_id: str | None = None

    def _ensure_browser(self) -> None:
        if self._page is not None and not _page_is_closed(self._page):
            self._register_page(self._page)
            return
        if self._browser is not None or self._playwright is not None:
            self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ImportError("playwright not installed. Install with: uv sync --extra browser")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._page = self._browser.new_page()
        self._register_page(self._page)

    def _register_page(self, page) -> str:
        for tab_id, known_page in self._pages.items():
            if known_page is page:
                self._active_tab_id = tab_id
                return tab_id
        tab_id = f"browser_tab_{uuid.uuid4().hex}"
        self._pages[tab_id] = page
        self._active_tab_id = tab_id
        return tab_id

    @property
    def page(self):
        self._ensure_browser()
        return self._page

    @property
    def current_tab_id(self) -> str:
        page = self.page
        return self._register_page(page)

    def open_tab(self, url: str, *, wait_until: str = "load") -> str:
        self._ensure_browser()
        page = self._browser.new_page()
        tab_id = self._register_page(page)
        self._page = page
        page.goto(url, wait_until=wait_until)
        if _page_is_closed(page):
            raise RuntimeError("new browser tab closed during navigation")
        return tab_id

    def activate_tab(self, tab_id: str):
        page = self._pages.get(tab_id)
        if page is None or _page_is_closed(page):
            raise RuntimeError(f"browser tab is closed or unknown: {tab_id}")
        page.bring_to_front()
        self._page = page
        self._active_tab_id = tab_id
        return page

    def close_tab(self, tab_id: str) -> None:
        page = self._pages.get(tab_id)
        if page is None:
            raise RuntimeError(f"browser tab is unknown: {tab_id}")
        page.close()
        self._pages.pop(tab_id, None)
        if self._active_tab_id == tab_id:
            self._active_tab_id = next(iter(self._pages), None)
            self._page = self._pages[self._active_tab_id] if self._active_tab_id else None

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            finally:
                self._playwright = self._browser = self._page = None
                self._pages.clear()
                self._active_tab_id = None


_session = _BrowserSession()


def _page_is_closed(page) -> bool:
    checker = getattr(page, "is_closed", None)
    if not callable(checker):
        return False
    try:
        return checker() is True
    except Exception:
        return True


def _page_url(page, fallback: str = "") -> str:
    value = getattr(page, "url", fallback)
    return value if isinstance(value, str) and value else fallback


def _verified_tab(page) -> str:
    if _page_is_closed(page):
        raise RuntimeError("browser tab closed during the operation")
    if _session.page is not page:
        raise RuntimeError("active browser tab changed during the operation")
    tab_id = getattr(_session, "current_tab_id", "")
    return tab_id if isinstance(tab_id, str) else f"page:{id(page):x}"


def _source_metadata(page, **values: Any) -> dict[str, Any]:
    metadata = {
        **values,
        "tab_id": _verified_tab(page),
        "content_trust": "untrusted",
    }
    metadata.setdefault("final_url", _page_url(page))
    return metadata


def _injection_findings(page) -> tuple[str, ...]:
    from openjarvis.browser.actions import WebInjectionGuard

    content = page.inner_text("body")
    if not isinstance(content, str):
        return ()
    return WebInjectionGuard().scan(content).findings


# ---------------------------------------------------------------------------
# Tool 1: BrowserNavigateTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("browser_navigate")
class BrowserNavigateTool(BaseTool):
    """Navigate to a URL in the browser."""

    tool_id = "browser_navigate"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_navigate",
            description=("Navigate to a URL in the browser. Returns the page title and text content."),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to.",
                    },
                    "wait_for": {
                        "type": "string",
                        "description": (
                            "Wait condition: 'load', 'domcontentloaded', or 'networkidle'. Default: 'load'."
                        ),
                    },
                },
                "required": ["url"],
            },
            category="browser",
            required_capabilities=["network:fetch"],
        )

    def execute(self, **params: Any) -> ToolResult:
        url = params.get("url", "")
        if not url:
            return ToolResult(
                tool_name="browser_navigate",
                content="No URL provided.",
                success=False,
            )

        wait_for = params.get("wait_for", "load")
        if wait_for not in ("load", "domcontentloaded", "networkidle"):
            wait_for = "load"

        # SSRF check — never skipped. check_ssrf falls back to a pure-Python
        # implementation when the Rust backend is unavailable, so an
        # uncompiled extension must not silently disable SSRF protection.
        from openjarvis.security.ssrf import check_ssrf

        ssrf_error = check_ssrf(url)
        if ssrf_error:
            return ToolResult(
                tool_name="browser_navigate",
                content=f"SSRF blocked: {ssrf_error}",
                success=False,
            )

        try:
            page = _session.page
            blocked_requests: list[tuple[str, str]] = []

            def guard_request(route) -> None:
                request_url = str(route.request.url)
                if request_url.casefold().startswith(("http://", "https://")):
                    request_error = check_ssrf(request_url)
                    if request_error:
                        blocked_requests.append((request_url, request_error))
                        route.abort()
                        return
                route.continue_()

            page.route("**/*", guard_request)
            try:
                response = page.goto(url, wait_until=wait_for)
            except Exception:
                if blocked_requests:
                    blocked_url, blocked_reason = blocked_requests[0]
                    return ToolResult(
                        tool_name="browser_navigate",
                        content=f"SSRF blocked request: {blocked_reason}",
                        success=False,
                        metadata={"blocked_url": blocked_url},
                    )
                raise
            finally:
                page.unroute("**/*", guard_request)
            final_url = _page_url(page, url)
            redirect_error = check_ssrf(final_url)
            if redirect_error:
                try:
                    page.goto("about:blank", wait_until="load")
                except Exception:
                    _session.close()
                return ToolResult(
                    tool_name="browser_navigate",
                    content=f"Redirect blocked by SSRF policy: {redirect_error}",
                    success=False,
                    metadata={
                        "requested_url": url,
                        "final_url": final_url,
                    },
                )
            title = page.title()
            text_content = page.inner_text("body")
            if len(text_content) > 5000:
                text_content = text_content[:5000] + "\n\n[Content truncated]"

            status = response.status if response else None
            return ToolResult(
                tool_name="browser_navigate",
                content=f"Title: {title}\n\n{text_content}",
                success=True,
                metadata=_source_metadata(
                    page,
                    url=url,
                    requested_url=url,
                    final_url=final_url,
                    title=title,
                    status=status,
                    navigation_verified=True,
                ),
            )
        except ImportError:
            return ToolResult(
                tool_name="browser_navigate",
                content=("playwright not installed. Install with: uv sync --extra browser"),
                success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="browser_navigate",
                content=f"Navigation error: {exc}",
                success=False,
            )


# ---------------------------------------------------------------------------
# Tool 2: BrowserClickTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("browser_click")
class BrowserClickTool(BaseTool):
    """Click an element on the page."""

    tool_id = "browser_click"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_click",
            description=(
                "Click an element on the current page. Use a CSS selector or text content to identify the element."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector or text content of the element.",
                    },
                    "by_text": {
                        "type": "boolean",
                        "description": ("If true, click by text content instead of CSS selector. Default: false."),
                    },
                },
                "required": ["selector"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        selector = params.get("selector", "")
        if not selector:
            return ToolResult(
                tool_name="browser_click",
                content="No selector provided.",
                success=False,
            )

        by_text = params.get("by_text", False)

        try:
            page = _session.page
            findings = _injection_findings(page)
            if findings:
                return ToolResult(
                    tool_name="browser_click",
                    content="Untrusted page instructions blocked the click.",
                    success=False,
                    metadata={
                        "content_trust": "untrusted",
                        "injection_findings": list(findings),
                    },
                )
            if by_text:
                page.get_by_text(selector).click()
            else:
                page.click(selector)

            return ToolResult(
                tool_name="browser_click",
                content=f"Clicked element: {selector}",
                success=True,
                metadata=_source_metadata(
                    page,
                    selector=selector,
                    by_text=by_text,
                    verification="action completed on the intended tab",
                ),
            )
        except ImportError:
            return ToolResult(
                tool_name="browser_click",
                content=("playwright not installed. Install with: uv sync --extra browser"),
                success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="browser_click",
                content=f"Click error: {exc}",
                success=False,
            )


# ---------------------------------------------------------------------------
# Tool 3: BrowserTypeTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("browser_type")
class BrowserTypeTool(BaseTool):
    """Type text into a form field."""

    tool_id = "browser_type"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_type",
            description=(
                "Type text into a form field on the current page."
                " Can clear the field first or append to existing content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector of the input field.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type into the field.",
                    },
                    "clear": {
                        "type": "boolean",
                        "description": ("If true, clear the field before typing. Default: true."),
                    },
                },
                "required": ["selector", "text"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        selector = params.get("selector", "")
        text = params.get("text", "")

        if not selector:
            return ToolResult(
                tool_name="browser_type",
                content="No selector provided.",
                success=False,
            )
        if not text:
            return ToolResult(
                tool_name="browser_type",
                content="No text provided.",
                success=False,
            )

        clear = params.get("clear", True)

        try:
            page = _session.page
            findings = _injection_findings(page)
            if findings:
                return ToolResult(
                    tool_name="browser_type",
                    content="Untrusted page instructions blocked text entry.",
                    success=False,
                    metadata={
                        "content_trust": "untrusted",
                        "injection_findings": list(findings),
                    },
                )
            if clear:
                page.fill(selector, text)
            else:
                page.type(selector, text)
            input_value = getattr(page, "input_value", None)
            observed = None
            if callable(input_value):
                observed = input_value(selector)
                if isinstance(observed, str) and observed != text:
                    raise RuntimeError("typed value did not match the intended text")

            return ToolResult(
                tool_name="browser_type",
                content=f"Typed text into: {selector}",
                success=True,
                metadata=_source_metadata(
                    page,
                    selector=selector,
                    input_verified=isinstance(observed, str),
                ),
            )
        except ImportError:
            return ToolResult(
                tool_name="browser_type",
                content=("playwright not installed. Install with: uv sync --extra browser"),
                success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="browser_type",
                content=f"Type error: {exc}",
                success=False,
            )


# ---------------------------------------------------------------------------
# Tool 4: BrowserScreenshotTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("browser_screenshot")
class BrowserScreenshotTool(BaseTool):
    """Take a screenshot of the current page."""

    tool_id = "browser_screenshot"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_screenshot",
            description=(
                "Take a screenshot of the current browser page. Returns the screenshot as base64-encoded data."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional file path to save the screenshot.",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": ("If true, capture the full scrollable page. Default: false."),
                    },
                },
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        path = params.get("path")
        full_page = params.get("full_page", False)

        try:
            page = _session.page
            screenshot_bytes = page.screenshot(full_page=full_page)

            if path:
                with open(path, "wb") as f:
                    f.write(screenshot_bytes)

            b64_data = base64.b64encode(screenshot_bytes).decode("utf-8")

            description = "Screenshot taken"
            if full_page:
                description += " (full page)"
            if path:
                description += f", saved to {path}"

            return ToolResult(
                tool_name="browser_screenshot",
                content=description,
                success=True,
                metadata=_source_metadata(
                    page,
                    screenshot_base64=b64_data,
                ),
            )
        except ImportError:
            return ToolResult(
                tool_name="browser_screenshot",
                content=("playwright not installed. Install with: uv sync --extra browser"),
                success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="browser_screenshot",
                content=f"Screenshot error: {exc}",
                success=False,
            )


# ---------------------------------------------------------------------------
# Tool 5: BrowserExtractTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("browser_extract")
class BrowserExtractTool(BaseTool):
    """Extract content from the current page."""

    tool_id = "browser_extract"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_extract",
            description=("Extract content from the current browser page. Supports extracting text, links, or tables."),
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": ("CSS selector to extract from. Default: 'body'."),
                    },
                    "extract_type": {
                        "type": "string",
                        "description": ("Type of extraction: 'text', 'links', or 'tables'. Default: 'text'."),
                    },
                },
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        selector = params.get("selector", "body")
        extract_type = params.get("extract_type", "text")

        if extract_type not in ("text", "links", "tables"):
            return ToolResult(
                tool_name="browser_extract",
                content=(f"Invalid extract_type: '{extract_type}'. Must be 'text', 'links', or 'tables'."),
                success=False,
            )

        try:
            page = _session.page

            if extract_type == "text":
                content = page.inner_text(selector)
                if len(content) > 10000:
                    content = content[:10000] + "\n\n[Content truncated]"
                return ToolResult(
                    tool_name="browser_extract",
                    content=content,
                    success=True,
                    metadata=_source_metadata(
                        page,
                        selector=selector,
                        extract_type=extract_type,
                    ),
                )

            elif extract_type == "links":
                links = page.eval_on_selector_all(
                    f"{selector} a[href]",
                    """elements => elements.map(el => ({
                        href: el.href,
                        text: el.innerText.trim()
                    }))""",
                )
                lines = []
                for link in links:
                    text = link.get("text", "")
                    href = link.get("href", "")
                    lines.append(f"- [{text}]({href})")
                content = "\n".join(lines) if lines else "No links found."
                if len(content) > 10000:
                    content = content[:10000] + "\n\n[Content truncated]"
                return ToolResult(
                    tool_name="browser_extract",
                    content=content,
                    success=True,
                    metadata=_source_metadata(
                        page,
                        selector=selector,
                        extract_type=extract_type,
                        num_links=len(links),
                    ),
                )

            else:  # tables
                tables_text = page.eval_on_selector_all(
                    f"{selector} table",
                    """elements => elements.map(el => el.innerText)""",
                )
                if tables_text:
                    content = "\n\n---\n\n".join(tables_text)
                else:
                    content = "No tables found."
                if len(content) > 10000:
                    content = content[:10000] + "\n\n[Content truncated]"
                return ToolResult(
                    tool_name="browser_extract",
                    content=content,
                    success=True,
                    metadata=_source_metadata(
                        page,
                        selector=selector,
                        extract_type=extract_type,
                        num_tables=len(tables_text),
                    ),
                )

        except ImportError:
            return ToolResult(
                tool_name="browser_extract",
                content=("playwright not installed. Install with: uv sync --extra browser"),
                success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="browser_extract",
                content=f"Extract error: {exc}",
                success=False,
            )


__all__ = [
    "BrowserNavigateTool",
    "BrowserClickTool",
    "BrowserTypeTool",
    "BrowserScreenshotTool",
    "BrowserExtractTool",
]
