"""Backward-compatible ReAct shim plus RLM bounded-read bridge install."""

from __future__ import annotations

from functools import wraps

from openjarvis.agents._rlm_bounds import install_rlm_bounds
from openjarvis.agents.native_react import NativeReActAgent
from openjarvis.agents.rlm import RLMAgent
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool
from openjarvis.tools.file_read import FileReadTool

ReActAgent = NativeReActAgent


class _CanonicalFileReadProxy(BaseTool):
    """Expose the trusted file_read bounds when a legacy runtime omits them."""

    tool_id = FileReadTool.tool_id

    def __init__(self, delegate: BaseTool) -> None:
        self._delegate = delegate
        self._canonical = FileReadTool()

    @property
    def spec(self):
        return self._canonical.spec

    @property
    def manifest(self):
        return self._canonical.manifest

    def execute(self, **params) -> ToolResult:
        limit = int(params["max_lines"])
        if limit <= 0:
            return ToolResult(self.spec.name, "max_lines must be positive", success=False)
        result = self._delegate.execute(path=params["path"], max_lines=limit)
        if not result.success:
            return result
        bounded = "".join(result.content.splitlines(keepends=True)[:limit])
        return ToolResult(
            tool_name=result.tool_name,
            content=bounded,
            success=True,
            usage=result.usage,
            cost_usd=result.cost_usd,
            latency_seconds=result.latency_seconds,
            metadata=result.metadata,
        )


def _canonicalize_file_read(tool: BaseTool) -> BaseTool:
    spec = tool.spec
    canonical = FileReadTool().spec
    if getattr(tool, "tool_id", "") != FileReadTool.tool_id or spec.name != canonical.name:
        return tool
    if "max_lines" in spec.parameters.get("properties", {}):
        return tool
    return _CanonicalFileReadProxy(tool)


if not getattr(RLMAgent, "_w3_bounded_bridge_installed", False):
    original_init = RLMAgent.__init__

    @wraps(original_init)
    def _bounded_init(self, *args, **kwargs):
        tools = kwargs.get("tools")
        if tools is not None:
            kwargs["tools"] = [_canonicalize_file_read(tool) for tool in tools]
        original_init(self, *args, **kwargs)

    RLMAgent.__init__ = _bounded_init
    install_rlm_bounds(RLMAgent)
    RLMAgent._w3_bounded_bridge_installed = True


__all__ = ["NativeReActAgent", "ReActAgent"]
