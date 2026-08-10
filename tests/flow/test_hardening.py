from __future__ import annotations

import hashlib
import hmac
import threading

import pytest

from openjarvis.flow import (
    AccessMode,
    FlowAuthenticationError,
    FlowSessionAuthority,
    NativeFlowAssertion,
    OwnerVerificationResult,
    OwnerVerificationStatus,
    RuntimeBinding,
    WindowsUserConsentVerifier,
)

SECRET = "f" * 64
TASK = "trusted-task-42"
NOW = 1_800_000_000


class BindingProvider:
    def __init__(self) -> None:
        self.binding = RuntimeBinding("user-sid", "session-1", 100)

    def current(self) -> RuntimeBinding:
        return self.binding


class BlockingVerifier:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        del prompt
        self.entered.set()
        assert self.release.wait(1)
        return OwnerVerificationResult(OwnerVerificationStatus.VERIFIED)


class RaisingVerifier:
    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        del prompt
        raise RuntimeError("native verifier failed with implementation detail")


def _assertion(authority: FlowSessionAuthority) -> NativeFlowAssertion:
    challenge = authority.issue_activation_challenge(task_context=TASK)
    signature = hmac.new(
        SECRET.encode(),
        challenge.assertion_message(
            nonce="native-nonce",
            authenticated_at=challenge.issued_at,
        ),
        hashlib.sha256,
    ).hexdigest()
    return NativeFlowAssertion(
        challenge=challenge,
        nonce="native-nonce",
        authenticated_at=challenge.issued_at,
        signature=signature,
    )


def test_global_stop_wins_during_owner_verification() -> None:
    binding = BindingProvider()
    verifier = BlockingVerifier()
    authority = FlowSessionAuthority(
        SECRET,
        clock=lambda: NOW,
        owner_verifier=verifier,
        binding_provider=binding,
    )
    assertion = _assertion(authority)
    outcome: list[str] = []

    def activate() -> None:
        try:
            authority.activate_flow(assertion)
        except FlowAuthenticationError:
            outcome.append("denied")
        else:
            outcome.append("flow")

    worker = threading.Thread(target=activate)
    worker.start()
    assert verifier.entered.wait(1)
    authority.global_stop()
    verifier.release.set()
    worker.join(1)

    assert outcome == ["denied"]
    assert authority.status().mode is AccessMode.LOCKED


def test_verifier_exception_fails_closed_without_leaking_detail() -> None:
    authority = FlowSessionAuthority(
        SECRET,
        clock=lambda: NOW,
        owner_verifier=RaisingVerifier(),
        binding_provider=BindingProvider(),
    )

    with pytest.raises(FlowAuthenticationError) as caught:
        authority.activate_flow(_assertion(authority))

    assert "implementation detail" not in str(caught.value)
    assert authority.status().mode is AccessMode.LOCKED


def test_process_change_revokes_active_flow() -> None:
    binding = BindingProvider()
    verifier = WindowsUserConsentVerifier(lambda prompt: "Verified")
    authority = FlowSessionAuthority(
        SECRET,
        clock=lambda: NOW,
        owner_verifier=verifier,
        binding_provider=binding,
    )
    authority.activate_flow(_assertion(authority))

    binding.binding = RuntimeBinding("user-sid", "session-1", 101)

    assert authority.status().mode is AccessMode.LOCKED
    assert authority.status().lock_reason == "runtime_binding_changed"


def test_bridge_secret_expiry_fails_closed() -> None:
    clock = [NOW]
    authority = FlowSessionAuthority(
        SECRET,
        clock=lambda: clock[0],
        bridge_secret_max_age_seconds=5,
        owner_verifier=WindowsUserConsentVerifier(lambda prompt: "Verified"),
        binding_provider=BindingProvider(),
    )
    clock[0] += 6

    with pytest.raises(FlowAuthenticationError, match="bridge is unavailable"):
        authority.issue_activation_challenge(task_context=TASK)


def test_windows_verifier_normalizes_non_success_results() -> None:
    assert WindowsUserConsentVerifier(lambda prompt: "Verified").verify(prompt="x").verified
    assert (
        WindowsUserConsentVerifier(lambda prompt: "Canceled").verify(prompt="x").status
        is OwnerVerificationStatus.CANCELED
    )
    assert (
        WindowsUserConsentVerifier(lambda prompt: "DeviceNotPresent").verify(prompt="x").status
        is OwnerVerificationStatus.UNAVAILABLE
    )
    assert (
        WindowsUserConsentVerifier(lambda prompt: "RetriesExhausted").verify(prompt="x").status
        is OwnerVerificationStatus.FAILED
    )
