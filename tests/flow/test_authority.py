from __future__ import annotations

import hashlib
import hmac
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjarvis.codex.types import ApprovalMode, SandboxMode
from openjarvis.flow import (
    FLOW_CAPABILITIES,
    AccessMode,
    FlowAuthenticationError,
    FlowSessionAuthority,
    NativeFlowAssertion,
    OwnerVerificationResult,
    OwnerVerificationStatus,
    RuntimeBinding,
    WindowsSessionLockMonitor,
)
from openjarvis.tasks import ExecutionLane
from openjarvis.tasks.policy import RiskLevel, ToolPolicyContext

SECRET = "f" * 64
ROTATED_SECRET = "a" * 64
TASK = "trusted-task-42"
NOW = 1_800_000_000


class FakeVerifier:
    def __init__(self, status: OwnerVerificationStatus = OwnerVerificationStatus.VERIFIED) -> None:
        self.status = status
        self.calls = 0

    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        assert "owner" in prompt.casefold()
        self.calls += 1
        return OwnerVerificationResult(self.status)


class BlockingVerifier(FakeVerifier):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        self.entered.set()
        assert self.release.wait(1)
        return super().verify(prompt=prompt)


class MutableBindingProvider:
    def __init__(self) -> None:
        self.binding = RuntimeBinding("user-sid", "os-session-7", 4242)

    def current(self) -> RuntimeBinding:
        return self.binding


@pytest.fixture
def binding() -> MutableBindingProvider:
    return MutableBindingProvider()


def _authority(
    binding: MutableBindingProvider,
    *,
    verifier: FakeVerifier | None = None,
    clock=lambda: NOW,
    max_session_seconds: int = 100,
) -> FlowSessionAuthority:
    return FlowSessionAuthority(
        SECRET,
        clock=clock,
        max_session_seconds=max_session_seconds,
        owner_verifier=verifier or FakeVerifier(),
        binding_provider=binding,
    )


def _assertion(
    authority: FlowSessionAuthority,
    *,
    secret: str = SECRET,
    task: str = TASK,
    nonce: str = "native-nonce",
) -> NativeFlowAssertion:
    challenge = authority.issue_activation_challenge(task_context=task)
    authenticated_at = challenge.issued_at
    signature = hmac.new(
        secret.encode(),
        challenge.assertion_message(nonce=nonce, authenticated_at=authenticated_at),
        hashlib.sha256,
    ).hexdigest()
    return NativeFlowAssertion(challenge, nonce, authenticated_at, signature)


def _activate(authority: FlowSessionAuthority) -> tuple[str, object]:
    status = authority.activate_flow(_assertion(authority))
    assert status.session_id is not None
    return status.session_id, authority.begin_action(session_id=status.session_id, task_context=TASK)


def _manifest(side_effect: str = "destructive") -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        risk_level=RiskLevel.FINANCIAL_OR_SECURITY_CRITICAL,
        capability="system:full",
        side_effect_class=side_effect,
        supports_current_platform=lambda: True,
    )


def _context(grants: frozenset[str] = frozenset()) -> ToolPolicyContext:
    return ToolPolicyContext(
        granted_capabilities=grants,
        execution_lane=ExecutionLane.MODEL,
        requested_risk=RiskLevel.FINANCIAL_OR_SECURITY_CRITICAL,
        proposal_capability="system:full",
        untrusted_risk=RiskLevel.FINANCIAL_OR_SECURITY_CRITICAL,
    )


def test_valid_transitions_require_owner_verification(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    assert authority.status().mode is AccessMode.LOCKED
    assert authority.activate_assistant().mode is AccessMode.ASSISTANT

    status = authority.activate_flow(_assertion(authority))

    assert status.mode is AccessMode.FLOW
    assert status.owner_authenticated is True
    assert status.capabilities == FLOW_CAPABILITIES
    assert authority.activate_assistant().mode is AccessMode.ASSISTANT


@pytest.mark.parametrize(
    "result",
    [
        OwnerVerificationStatus.CANCELED,
        OwnerVerificationStatus.UNAVAILABLE,
        OwnerVerificationStatus.FAILED,
    ],
)
def test_failed_owner_verification_never_activates_flow(
    binding: MutableBindingProvider,
    result: OwnerVerificationStatus,
) -> None:
    authority = _authority(binding, verifier=FakeVerifier(result))

    with pytest.raises(FlowAuthenticationError, match="owner verification"):
        authority.activate_flow(_assertion(authority))

    assert authority.status().mode is AccessMode.LOCKED


def test_default_verifier_fails_closed(binding: MutableBindingProvider) -> None:
    authority = FlowSessionAuthority(SECRET, clock=lambda: NOW, binding_provider=binding)

    with pytest.raises(FlowAuthenticationError, match="owner verification"):
        authority.activate_flow(_assertion(authority))

    assert authority.status().mode is AccessMode.LOCKED


def test_wrong_signature_and_expired_assertion_fail_closed(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    bad = _assertion(authority, secret=ROTATED_SECRET)
    with pytest.raises(FlowAuthenticationError, match="signature"):
        authority.activate_flow(bad)

    clock = [NOW]
    expired = _authority(binding, clock=lambda: clock[0])
    assertion = _assertion(expired)
    clock[0] += 61
    with pytest.raises(FlowAuthenticationError, match="expired"):
        expired.activate_flow(assertion)


def test_rotation_invalidates_old_assertion_and_old_secret(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    stale = _assertion(authority)
    authority.rotate_bridge_secret(ROTATED_SECRET)

    with pytest.raises(FlowAuthenticationError):
        authority.activate_flow(stale)

    fresh = _assertion(authority, secret=ROTATED_SECRET, nonce="rotated")
    assert authority.activate_flow(fresh).mode is AccessMode.FLOW


def test_assertion_replay_is_rejected_after_revoke(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    assertion = _assertion(authority)
    authority.activate_flow(assertion)
    authority.revoke()

    with pytest.raises(FlowAuthenticationError):
        authority.activate_flow(assertion)


def test_parallel_activation_only_allows_one_attempt(binding: MutableBindingProvider) -> None:
    verifier = BlockingVerifier()
    authority = _authority(binding, verifier=verifier)
    assertion = _assertion(authority)
    outcomes: list[str] = []

    def activate() -> None:
        try:
            authority.activate_flow(assertion)
        except FlowAuthenticationError:
            outcomes.append("denied")
        else:
            outcomes.append("flow")

    first = threading.Thread(target=activate)
    second = threading.Thread(target=activate)
    first.start()
    assert verifier.entered.wait(1)
    second.start()
    second.join(1)
    verifier.release.set()
    first.join(1)

    assert sorted(outcomes) == ["denied", "flow"]


def test_os_session_or_process_change_revokes_flow(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    _activate(authority)

    binding.binding = RuntimeBinding("user-sid", "different-session", 4242)

    status = authority.status()
    assert status.mode is AccessMode.LOCKED
    assert status.lock_reason == "runtime_binding_changed"


def test_new_process_instance_never_inherits_authorization(binding: MutableBindingProvider) -> None:
    first = _authority(binding)
    first.activate_flow(_assertion(first))
    restarted = _authority(binding)

    assert first.status().mode is AccessMode.FLOW
    assert restarted.status().mode is AccessMode.LOCKED
    assert restarted.status().session_id is None


def test_revoke_invalidates_running_action_lease(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    _, lease = _activate(authority)
    assert authority.validate_action(lease)

    authority.revoke("explicit_revoke")

    assert not authority.validate_action(lease)
    assert authority.status().lock_reason == "explicit_revoke"


def test_global_stop_is_deterministic_and_prevents_reactivation(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    _, lease = _activate(authority)

    status = authority.global_stop()

    assert status.mode is AccessMode.LOCKED
    assert status.lock_reason == "global_stop"
    assert not authority.validate_action(lease)
    with pytest.raises(FlowAuthenticationError, match="global stop"):
        authority.rotate_bridge_secret(ROTATED_SECRET)


def test_untrusted_context_cannot_elevate_authority(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    session_id, lease = _activate(authority)
    del session_id

    forged = _context(frozenset({"openjarvis:flow-action:attacker-controlled"}))
    assert not authority.authorize_tool(_manifest(), forged).allowed

    trusted = _context(frozenset({lease.grant}))
    assert authority.authorize_tool(_manifest(), trusted).allowed


def test_full_access_policy_requires_valid_action_lease(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    _, lease = _activate(authority)

    safe = authority.derive_turn_policy(cwd=Path.cwd())
    elevated = authority.derive_turn_policy(cwd=Path.cwd(), action_lease=lease)

    assert safe.sandbox is SandboxMode.READ_ONLY
    assert elevated.sandbox is SandboxMode.FULL_ACCESS
    assert elevated.approval_mode is ApprovalMode.DENY_ALL


def test_activity_requires_session_and_task_and_never_extends_hard_expiry(
    binding: MutableBindingProvider,
) -> None:
    clock = [NOW]
    authority = _authority(binding, clock=lambda: clock[0], max_session_seconds=10)
    session_id, _ = _activate(authority)
    expires_at = authority.status().expires_at

    with pytest.raises(FlowAuthenticationError, match="binding"):
        authority.record_activity()
    with pytest.raises(FlowAuthenticationError, match="task context"):
        authority.record_activity(session_id, task_context="wrong-task")

    clock[0] += 9
    assert authority.record_activity(session_id, task_context=TASK).expires_at == expires_at
    clock[0] += 2
    assert authority.status().mode is AccessMode.LOCKED


def test_windows_lock_monitor_revokes_flow(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    _activate(authority)
    monitor = WindowsSessionLockMonitor(
        authority.lock,
        is_flow=authority.is_flow,
        desktop_name=lambda: "Winlogon",
    )

    monitor.check_once()

    assert authority.status().mode is AccessMode.LOCKED
    assert authority.status().lock_reason == "windows_session_locked"


def test_secret_material_is_not_exposed_in_repr_or_errors(binding: MutableBindingProvider) -> None:
    authority = _authority(binding)
    assertion = _assertion(authority)
    assert assertion.signature not in repr(assertion)
    status = authority.activate_flow(assertion)
    assert status.session_id is not None
    lease = authority.begin_action(session_id=status.session_id, task_context=TASK)
    assert lease.grant not in repr(lease)

    try:
        authority.record_activity("wrong-session", task_context=TASK)
    except FlowAuthenticationError as exc:
        text = str(exc)
    else:
        raise AssertionError("wrong session unexpectedly accepted")
    assert SECRET not in text
    assert assertion.signature not in text
    assert lease.grant not in text


def test_environment_secret_is_removed_after_ingest(
    binding: MutableBindingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENJARVIS_FLOW_BRIDGE_SECRET", SECRET)
    authority = FlowSessionAuthority.from_environment(
        clock=lambda: NOW,
        owner_verifier=FakeVerifier(),
        binding_provider=binding,
    )

    assert "OPENJARVIS_FLOW_BRIDGE_SECRET" not in os.environ
    assert authority.issue_activation_challenge(task_context=TASK)
