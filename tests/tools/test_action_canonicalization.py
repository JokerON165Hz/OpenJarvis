"""Regression coverage for reload-safe action/manifest enum semantics."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

from openjarvis.tools.action_service import ToolActionService
from openjarvis.tools.manifest import SideEffectClass


class ReloadedSideEffect(str, Enum):
    """Equivalent enum class produced by a module reload/import boundary."""

    LOCAL_READ = "local_read"
    EXTERNAL_WRITE = "external_write"


class ManifestStub:
    tool_id = "file.read"
    capability = "file:read"
    side_effect_class = SideEffectClass.LOCAL_READ
    timeout = 10.0
    version = "1.0.0"

    @staticmethod
    def validate_arguments(arguments):
        return dict(arguments)


def _service_without_runtime() -> ToolActionService:
    service = object.__new__(ToolActionService)
    service._tasks = None
    return service


def test_proposal_side_effect_comparison_is_reload_safe() -> None:
    service = _service_without_runtime()
    proposal = SimpleNamespace(
        arguments={},
        capability="file:read",
        expected_side_effect=ReloadedSideEffect.LOCAL_READ,
        timeout_seconds=5.0,
        task_id="task-1",
    )
    context = SimpleNamespace(proposal_capability="file:read")

    assert service._validate_proposal(proposal, ManifestStub(), context) == ""


def test_execution_binding_uses_canonical_side_effect_value() -> None:
    service = _service_without_runtime()
    action = SimpleNamespace(
        manifest_fingerprint="fingerprint",
        manifest_version="1.0.0",
        tool_id="file.read",
        capability="file:read",
        expected_side_effect=ReloadedSideEffect.LOCAL_READ,
        idempotency_key="idem-1",
    )
    proposal = SimpleNamespace(
        tool_id="file.read",
        capability="file:read",
        expected_side_effect=SideEffectClass.LOCAL_READ,
        idempotency_key="idem-1",
    )

    assert (
        service._validate_execution_binding(
            action,
            proposal,
            ManifestStub(),
            "fingerprint",
        )
        == ""
    )


def test_execution_binding_still_rejects_real_side_effect_drift() -> None:
    service = _service_without_runtime()
    action = SimpleNamespace(
        manifest_fingerprint="fingerprint",
        manifest_version="1.0.0",
        tool_id="file.read",
        capability="file:read",
        expected_side_effect=ReloadedSideEffect.EXTERNAL_WRITE,
        idempotency_key="idem-1",
    )
    proposal = SimpleNamespace(
        tool_id="file.read",
        capability="file:read",
        expected_side_effect=SideEffectClass.LOCAL_READ,
        idempotency_key="idem-1",
    )

    assert service._validate_execution_binding(
        action,
        proposal,
        ManifestStub(),
        "fingerprint",
    ) == "action side effect differs from its proposal or manifest"
