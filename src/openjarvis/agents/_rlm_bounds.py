"""Fail-closed bounded file helpers for the RLM REPL."""

from __future__ import annotations

from typing import Any


def _bounded_file_read_params(self, path: str, max_lines: Any) -> dict[str, Any]:
    try:
        limit = int(max_lines)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_lines must be a positive integer") from exc
    if limit <= 0:
        raise ValueError("max_lines must be a positive integer")

    file_read = next(
        (tool for tool in self._tools if tool.spec.name == "file_read"), None
    )
    if file_read is None:
        raise RuntimeError("Tool 'file_read' is not available")
    properties = file_read.spec.parameters.get("properties", {})
    if "max_lines" not in properties:
        raise RuntimeError("file_read does not expose the bounded max_lines contract")
    return {"path": path, "max_lines": limit}


def _repl_read_file(self, path: str, max_lines: int = 120) -> str:
    return self._execute_tool_from_repl(
        "file_read", self._bounded_file_read_params(path, max_lines)
    )


def _repl_read_file_chunk(
    self,
    path: str,
    start_line: int,
    end_line: int,
) -> str:
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError) as exc:
        raise ValueError("file chunk bounds must be integers") from exc
    if start < 1 or end < start:
        raise ValueError("file chunk bounds must satisfy 1 <= start <= end")

    content = self._execute_tool_from_repl(
        "file_read", self._bounded_file_read_params(path, end)
    )
    return "".join(content.splitlines(keepends=True)[start - 1 : end])


def install_rlm_bounds(agent_cls) -> None:
    """Install bounded helpers after the canonical RLM class is imported."""
    agent_cls._bounded_file_read_params = _bounded_file_read_params
    agent_cls._repl_read_file = _repl_read_file
    agent_cls._repl_read_file_chunk = _repl_read_file_chunk
