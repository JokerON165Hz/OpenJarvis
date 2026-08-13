"""Context-bound overlay for the canonical MCP action bridge."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

_SOURCE = Path(__file__).resolve().parent.parent / "action_bridge.py"
_SPEC = importlib.util.spec_from_file_location("openjarvis.mcp._action_bridge_base", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("canonical MCP action bridge is unavailable")
_BASE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BASE)

_manifest = _BASE._manifest
_runtime = _BASE._runtime


def _clear_cache(app_state: Any) -> None:
    app_state._mcp_tools_cache = None
    app_state._mcp_tools_cache_identity = None


def _context(app_state: Any) -> tuple[str, Any]:
    servers, registry = _BASE._configured_servers(app_state)
    service = getattr(app_state, "tool_action_service", None)
    auth: list[str] = []
    for cfg in servers:
        if not isinstance(cfg, dict):
            continue
        env_name = str(cfg.get("token_env", ""))
        token = cfg.get("token") or (os.environ.get(env_name, "") if env_name else "")
        auth.append(hashlib.sha256(str(token).encode()).hexdigest() if token else "")
    payload = {
        "servers": servers,
        "auth": auth,
        "registry": id(registry) if registry is not None else None,
        "catalog": id(service.catalog) if service is not None else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest(), registry


def _drop_stale_runtimes(app_state: Any, current: set[str]) -> None:
    service = getattr(app_state, "tool_action_service", None)
    if service is None:
        return
    servers, _ = _BASE._configured_servers(app_state)
    capabilities = {
        f"mcp:{_BASE._component(str(cfg.get('server_id') or cfg.get('name') or 'server'), limit=36)}"
        for cfg in servers
        if isinstance(cfg, dict) and bool(cfg.get("enabled", True))
    }
    for manifest in service.catalog.list():
        if (
            manifest.tool_id.startswith("mcp__")
            and manifest.capability in capabilities
            and manifest.tool_id not in current
        ):
            service.unregister_runtime(manifest.tool_id)


def discover_action_tools(
    app_state: Any, *, force: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use canonical discovery while binding cache reuse to trusted context."""
    if getattr(app_state, "_mcp_shutdown", False):
        return [], {}

    identity, _registry = _context(app_state)
    cached = getattr(app_state, "_mcp_tools_cache", None)
    cached_identity = getattr(app_state, "_mcp_tools_cache_identity", None)
    if force:
        _clear_cache(app_state)
    elif cached is not None and cached_identity == identity:
        return cached
    elif cached is not None:
        _clear_cache(app_state)

    tools, adapters = _BASE.discover_action_tools(app_state, force=force)
    statuses = getattr(app_state, "_mcp_status", [])
    unavailable = any(
        isinstance(status, dict) and bool(status.get("last_error")) for status in statuses
    )
    if force or unavailable:
        _drop_stale_runtimes(app_state, set(adapters))

    if adapters and getattr(app_state, "_mcp_tools_cache", None) is not None:
        app_state._mcp_tools_cache_identity = identity
    elif not adapters:
        _clear_cache(app_state)
    return tools, adapters


def disconnect_server(app_state: Any, server_id: str) -> None:
    _BASE.disconnect_server(app_state, server_id)
    _clear_cache(app_state)


def interrupt_active_calls(app_state: Any) -> tuple[str, ...]:
    return _BASE.interrupt_active_calls(app_state)


__all__ = ["disconnect_server", "discover_action_tools", "interrupt_active_calls"]
