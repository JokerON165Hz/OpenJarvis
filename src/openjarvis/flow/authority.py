"""Fail-closed owner authority for Locked, Assistant, and Flow modes.

Flow activation requires a one-time bridge assertion plus successful owner
verification. Grants are bound to the current user, OS session, backend process
instance, and trusted task context. Secret material is never returned in status,
logs, or exceptions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from openjarvis.codex.types import ApprovalMode, SandboxMode
from openjarvis.flow.owner_verification import (
    FailClosedUserConsentVerifier,
    OwnerVerificationStatus,
    UserConsentVerifier,
)
from openjarvis.flow.runtime_binding import LocalRuntimeBindingProvider, RuntimeBinding, RuntimeBindingProvider
from openjarvis.tasks.policy import RiskLevel, ToolPolicyContext, ToolPolicyDecision
from openjarvis.tasks.types import ExecutionLane

FLOW_BRIDGE_SECRET_ENV = "OPENJARVIS_FLOW_BRIDGE_SECRET"
FLOW_MAX_SECONDS = 8 * 60 * 60
ASSERTION_MAX_AGE_SECONDS = 60
BRIDGE_SECRET_MAX_AGE_SECONDS = 5 * 60
CHALLENGE_MAX_AGE_SECONDS = 60
FLOW_VERIFICATION_PROMPT = "Verify owner to activate OpenJarvis Flow Mode"
_FLOW_ACTION_PREFIX = "openjarvis:flow-action:"

FLOW_CAPABILITIES: dict[str, str] = {
    "filesystem": "full_machine",
    "desktop": "full",
    "browser": "full",
    "shell": "elevated",
    "processes": "full",
    "services": "full",
    "registry": "full",
    "network": "full",
    "memory": "read_write",
    "git": "full",
    "package_management": "full",
    "application_control": "full",
}
ASSISTANT_CAPABILITIES: dict[str, str] = {
    "filesystem": "read_only",
    "desktop": "none",
    "browser": "read_only_isolated",
    "shell": "none",
    "processes": "none",
    "services": "none",
    "registry": "none",
    "network": "read_only",
    "memory": "read_only",
    "git": "read_only",
    "package_management": "none",
    "application_control": "none",
}


class AccessMode(str, Enum):
    LOCKED = "locked"
    ASSISTANT = "assistant"
    FLOW = "flow"


class FlowAuthenticationError(PermissionError):
    """Flow activation or use failed closed."""


@dataclass(frozen=True, slots=True)
class FlowStatus:
    mode: AccessMode
    owner_authenticated: bool
    session_id: str | None
    activated_at: str | None
    expires_at: str | None
    last_activity_at: str | None
    remaining_seconds: int
    capabilities: dict[str, str]
    lock_reason: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["mode"] = self.mode.value
        return value


@dataclass(frozen=True, slots=True)
class FlowTurnPolicy:
    sandbox: SandboxMode
    approval_mode: ApprovalMode
    execution_lane: ExecutionLane
    isolated_workspace: Path | None


@dataclass(frozen=True, slots=True)
class FlowActivationChallenge:
    challenge_id: str
    owner: str
    os_session_id: str
    process_id: int
    process_nonce: str
    task_context_digest: str
    issued_at: int
    expires_at: int
    bridge_generation: int

    def assertion_message(
        self,
        *,
        nonce: str,
        authenticated_at: int,
        owner_verification: str = "verified",
    ) -> bytes:
        payload = {
            "authenticated_at": authenticated_at,
            "bridge_generation": self.bridge_generation,
            "challenge_id": self.challenge_id,
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
            "nonce": nonce,
            "os_session_id": self.os_session_id,
            "owner": self.owner,
            "owner_verification": owner_verification,
            "process_id": self.process_id,
            "process_nonce": self.process_nonce,
            "task_context_digest": self.task_context_digest,
            "version": 2,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


@dataclass(frozen=True, slots=True)
class NativeFlowAssertion:
    challenge: FlowActivationChallenge
    nonce: str
    authenticated_at: int
    signature: str = field(repr=False)
    owner_verification: str = "verified"


@dataclass(frozen=True, slots=True)
class FlowActionLease:
    session_id: str
    task_context_digest: str
    authority_epoch: int
    grant: str = field(repr=False)


def generate_bridge_secret() -> str:
    """Generate fresh bridge material for a trusted native launcher."""

    return secrets.token_urlsafe(48)


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FlowSessionAuthority:
    """The only component allowed to grant local Flow capabilities."""

    def __init__(
        self,
        bridge_secret: str | None = None,
        *,
        clock=time.time,
        max_session_seconds: int = FLOW_MAX_SECONDS,
        bridge_secret_max_age_seconds: int = BRIDGE_SECRET_MAX_AGE_SECONDS,
        owner_verifier: UserConsentVerifier | None = None,
        binding_provider: RuntimeBindingProvider | None = None,
        trust_native_owner_verification: bool = False,
    ) -> None:
        self._clock = clock
        self._max_session_seconds = max_session_seconds
        self._bridge_secret_max_age_seconds = bridge_secret_max_age_seconds
        self._owner_verifier = owner_verifier or FailClosedUserConsentVerifier()
        self._trust_native_owner_verification = trust_native_owner_verification
        self._binding_provider = binding_provider or LocalRuntimeBindingProvider()
        self._lock = threading.RLock()
        self._global_stop = threading.Event()
        self._process_nonce = secrets.token_urlsafe(24)
        self._mode = AccessMode.LOCKED
        self._session_id: str | None = None
        self._activated_at: float | None = None
        self._expires_at: float | None = None
        self._last_activity_at: float | None = None
        self._lock_reason = "application_started"
        self._activation_binding: RuntimeBinding | None = None
        self._task_context_digest: str | None = None
        self._pending_challenge: FlowActivationChallenge | None = None
        self._bridge_secret: bytearray | None = None
        self._bridge_expires_at: float | None = None
        self._bridge_generation = 0
        self._authority_epoch = 0
        self._action_grants: dict[str, tuple[str, int]] = {}
        initial_secret = bridge_secret if bridge_secret is not None else os.environ.pop(FLOW_BRIDGE_SECRET_ENV, "")
        if initial_secret:
            self._install_bridge_secret(initial_secret)

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "FlowSessionAuthority":
        return cls(None, **kwargs)

    def status(self) -> FlowStatus:
        with self._lock:
            self._expire_if_needed()
            self._revoke_if_runtime_changed()
            now = self._clock()
            capabilities = (
                dict(FLOW_CAPABILITIES)
                if self._mode is AccessMode.FLOW
                else dict(ASSISTANT_CAPABILITIES)
                if self._mode is AccessMode.ASSISTANT
                else {}
            )
            return FlowStatus(
                mode=self._mode,
                owner_authenticated=self._mode is AccessMode.FLOW,
                session_id=self._session_id,
                activated_at=_iso(self._activated_at),
                expires_at=_iso(self._expires_at),
                last_activity_at=_iso(self._last_activity_at),
                remaining_seconds=(
                    max(0, int((self._expires_at or now) - now)) if self._mode is AccessMode.FLOW else 0
                ),
                capabilities=capabilities,
                lock_reason=self._lock_reason,
            )

    @property
    def mode(self) -> AccessMode:
        return self.status().mode

    def is_flow(self) -> bool:
        return self.mode is AccessMode.FLOW

    def activate_assistant(self) -> FlowStatus:
        with self._lock:
            if self._global_stop.is_set():
                raise FlowAuthenticationError("global stop is active")
            if self._mode is AccessMode.FLOW:
                self._clear_flow("assistant_selected", destroy_bridge=True)
            else:
                self._invalidate_pending_activation()
                self._authority_epoch += 1
            self._mode = AccessMode.ASSISTANT
            self._lock_reason = "assistant_selected"
            return self.status()

    def rotate_bridge_secret(self, bridge_secret: str) -> None:
        """Install fresh one-time bridge material from the trusted native side."""

        with self._lock:
            if self._global_stop.is_set():
                raise FlowAuthenticationError("global stop is active")
            if self._mode is AccessMode.FLOW:
                raise FlowAuthenticationError("cannot rotate bridge during Flow")
            self._invalidate_pending_activation()
            self._destroy_bridge_secret()
            self._authority_epoch += 1
            self._install_bridge_secret(bridge_secret)

    def issue_activation_challenge(self, *, task_context: str) -> FlowActivationChallenge:
        """Create one signed-request target without exposing bridge material."""

        with self._lock:
            if self._global_stop.is_set():
                raise FlowAuthenticationError("global stop is active")
            if self._mode is AccessMode.FLOW:
                raise FlowAuthenticationError("Flow is already active")
            self._require_bridge_secret()
            if not task_context or len(task_context) > 1024:
                raise FlowAuthenticationError("task context is invalid")
            binding = self._current_binding()
            now = int(self._clock())
            challenge = FlowActivationChallenge(
                challenge_id=secrets.token_urlsafe(24),
                owner=binding.user_id,
                os_session_id=binding.os_session_id,
                process_id=binding.process_id,
                process_nonce=self._process_nonce,
                task_context_digest=_digest_text(task_context),
                issued_at=now,
                expires_at=now + CHALLENGE_MAX_AGE_SECONDS,
                bridge_generation=self._bridge_generation,
            )
            self._pending_challenge = challenge
            return challenge

    def activate_flow(self, assertion: NativeFlowAssertion) -> FlowStatus:
        """Activate Flow only after a valid bridge assertion and owner check."""

        with self._lock:
            if self._global_stop.is_set():
                raise FlowAuthenticationError("global stop is active")
            if self._mode is AccessMode.FLOW:
                raise FlowAuthenticationError("Flow is already active")
            challenge = self._verify_native_assertion(assertion)
            attempt_epoch = self._authority_epoch
            binding = self._current_binding()
            self._consume_pending_activation()

        if self._trust_native_owner_verification:
            verification_status = OwnerVerificationStatus.VERIFIED
        else:
            try:
                verification = self._owner_verifier.verify(
                    prompt=FLOW_VERIFICATION_PROMPT
                )
                verification_status = verification.status
            except Exception:
                verification_status = OwnerVerificationStatus.FAILED

        with self._lock:
            if self._global_stop.is_set() or self._authority_epoch != attempt_epoch:
                self._clear_flow("activation_revoked", destroy_bridge=True)
                raise FlowAuthenticationError("Flow activation was revoked")
            current_binding = self._current_binding()
            if current_binding != binding:
                self._clear_flow("runtime_binding_changed", destroy_bridge=True)
                raise FlowAuthenticationError("runtime binding changed")
            if verification_status is not OwnerVerificationStatus.VERIFIED:
                self._clear_flow(f"owner_verification_{verification_status.value}", destroy_bridge=True)
                raise FlowAuthenticationError("owner verification did not succeed")
            now = self._clock()
            if now >= challenge.expires_at:
                self._clear_flow("native_assertion_expired", destroy_bridge=True)
                raise FlowAuthenticationError("native assertion expired")
            self._mode = AccessMode.FLOW
            self._session_id = secrets.token_urlsafe(32)
            self._activated_at = now
            self._expires_at = now + self._max_session_seconds
            self._last_activity_at = now
            self._lock_reason = ""
            self._activation_binding = binding
            self._task_context_digest = challenge.task_context_digest
            self._authority_epoch += 1
            return self.status()

    def begin_action(self, *, session_id: str, task_context: str) -> FlowActionLease:
        """Mint an in-process lease for one trusted runtime action."""

        with self._lock:
            self._require_active_flow(session_id=session_id, task_context=task_context)
            grant = f"{_FLOW_ACTION_PREFIX}{secrets.token_urlsafe(32)}"
            self._action_grants[_digest_text(grant)] = (self._task_context_digest or "", self._authority_epoch)
            return FlowActionLease(
                session_id=session_id,
                task_context_digest=self._task_context_digest or "",
                authority_epoch=self._authority_epoch,
                grant=grant,
            )

    def validate_action(
        self,
        lease: FlowActionLease,
        *,
        task_context: str | None = None,
    ) -> bool:
        with self._lock:
            self._expire_if_needed()
            self._revoke_if_runtime_changed()
            if self._mode is not AccessMode.FLOW or lease.authority_epoch != self._authority_epoch:
                return False
            if not hmac.compare_digest(lease.session_id, self._session_id or ""):
                return False
            if not hmac.compare_digest(lease.task_context_digest, self._task_context_digest or ""):
                return False
            if task_context is not None and not hmac.compare_digest(
                lease.task_context_digest,
                _digest_text(task_context),
            ):
                return False
            record = self._action_grants.get(_digest_text(lease.grant))
            return record == (lease.task_context_digest, lease.authority_epoch)

    def end_action(self, lease: FlowActionLease) -> None:
        with self._lock:
            self._action_grants.pop(_digest_text(lease.grant), None)

    def record_activity(self, session_id: str | None = None, *, task_context: str | None = None) -> FlowStatus:
        with self._lock:
            if self._mode is AccessMode.FLOW:
                if session_id is None or task_context is None:
                    raise FlowAuthenticationError("Flow session binding is required")
                self._require_active_flow(session_id=session_id, task_context=task_context)
                self._last_activity_at = self._clock()
            return self.status()

    def revoke(self, reason: str = "owner_revoked") -> FlowStatus:
        with self._lock:
            effective_reason = (
                "global_stop"
                if self._global_stop.is_set()
                else reason or "owner_revoked"
            )
            self._clear_flow(effective_reason, destroy_bridge=True)
            return self.status()

    def lock(self, reason: str = "user_locked") -> FlowStatus:
        return self.revoke(reason or "user_locked")

    def global_stop(self) -> FlowStatus:
        self._global_stop.set()
        with self._lock:
            self._clear_flow("global_stop", destroy_bridge=True)
            return self.status()

    def derive_turn_policy(
        self,
        *,
        cwd: Path,
        action_lease: FlowActionLease | None = None,
        task_context: str | None = None,
    ) -> FlowTurnPolicy:
        del cwd
        if action_lease is not None and self.validate_action(
            action_lease,
            task_context=task_context,
        ):
            return FlowTurnPolicy(
                sandbox=SandboxMode.FULL_ACCESS,
                approval_mode=ApprovalMode.DENY_ALL,
                execution_lane=ExecutionLane.INTERACTIVE,
                isolated_workspace=None,
            )
        return FlowTurnPolicy(
            sandbox=SandboxMode.READ_ONLY,
            approval_mode=ApprovalMode.DENY_ALL,
            execution_lane=ExecutionLane.MODEL,
            isolated_workspace=None,
        )

    def authorize_tool(self, manifest: Any, context: ToolPolicyContext) -> ToolPolicyDecision:
        mode = self.mode
        effective = RiskLevel(
            min(
                int(RiskLevel.FINANCIAL_OR_SECURITY_CRITICAL),
                max(int(manifest.risk_level), int(context.requested_risk), int(context.untrusted_risk)),
            )
        )
        allowed_roots = () if mode is AccessMode.FLOW else tuple(context.allowed_roots)

        def result(allowed: bool, reason: str) -> ToolPolicyDecision:
            return ToolPolicyDecision(
                allowed=allowed,
                status="allowed" if allowed else "denied",
                effective_risk=effective,
                capability=str(manifest.capability),
                reason=reason,
                allowed_roots=allowed_roots,
            )

        if getattr(manifest, "enabled", True) is False:
            return result(False, "tool is disabled")
        if not manifest.supports_current_platform():
            return result(False, "tool is unavailable on this operating system")
        if context.proposal_capability != manifest.capability:
            return result(False, "tool capability does not match its manifest")
        if mode is AccessMode.LOCKED:
            return result(False, "Locked mode does not allow personal tools")
        if mode is AccessMode.ASSISTANT:
            read_only = str(manifest.side_effect_class) in {
                "SideEffectClass.NONE",
                "SideEffectClass.LOCAL_READ",
                "none",
                "local_read",
            }
            return result(
                read_only,
                "Assistant mode allows read-only tools" if read_only else "Assistant mode is read-only",
            )
        if not self._context_has_valid_action_grant(context):
            return result(False, "Flow action lease is missing or stale")
        return result(True, "active owner-authenticated Flow action")

    def _verify_native_assertion(self, assertion: NativeFlowAssertion) -> FlowActivationChallenge:
        self._require_bridge_secret()
        challenge = self._pending_challenge
        if challenge is None or assertion.challenge != challenge:
            raise FlowAuthenticationError("native assertion challenge is invalid")
        if assertion.owner_verification != "verified":
            raise FlowAuthenticationError("native owner verification is invalid")
        now = self._clock()
        if now >= challenge.expires_at:
            self._invalidate_pending_activation()
            raise FlowAuthenticationError("native assertion expired")
        if challenge.bridge_generation != self._bridge_generation:
            raise FlowAuthenticationError("native assertion bridge generation is stale")
        if not assertion.nonce or len(assertion.nonce) > 200 or not isinstance(assertion.authenticated_at, int):
            raise FlowAuthenticationError("native assertion is malformed")
        if abs(now - assertion.authenticated_at) > ASSERTION_MAX_AGE_SECONDS:
            raise FlowAuthenticationError("native assertion expired")
        binding = self._current_binding()
        if (
            challenge.owner != binding.user_id
            or challenge.os_session_id != binding.os_session_id
            or challenge.process_id != binding.process_id
            or challenge.process_nonce != self._process_nonce
        ):
            raise FlowAuthenticationError("native assertion runtime binding is invalid")
        expected = hmac.new(
            bytes(self._bridge_secret or b""),
            challenge.assertion_message(
                nonce=assertion.nonce,
                authenticated_at=assertion.authenticated_at,
                owner_verification=assertion.owner_verification,
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, assertion.signature):
            raise FlowAuthenticationError("native assertion signature is invalid")
        return challenge

    def _consume_pending_activation(self) -> None:
        self._pending_challenge = None
        self._destroy_bridge_secret()

    def _invalidate_pending_activation(self) -> None:
        self._pending_challenge = None

    def _install_bridge_secret(self, bridge_secret: str) -> None:
        encoded = bridge_secret.encode()
        if len(encoded) < 32 or len(encoded) > 4096:
            raise FlowAuthenticationError("native Flow bridge secret is invalid")
        self._bridge_generation += 1
        self._bridge_secret = bytearray(encoded)
        self._bridge_expires_at = self._clock() + self._bridge_secret_max_age_seconds

    def _destroy_bridge_secret(self) -> None:
        if self._bridge_secret is not None:
            for index in range(len(self._bridge_secret)):
                self._bridge_secret[index] = 0
        self._bridge_secret = None
        self._bridge_expires_at = None

    def _require_bridge_secret(self) -> None:
        if (
            self._bridge_secret is None
            or self._bridge_expires_at is None
            or self._clock() >= self._bridge_expires_at
        ):
            self._destroy_bridge_secret()
            self._invalidate_pending_activation()
            raise FlowAuthenticationError("native Flow bridge is unavailable")

    def _current_binding(self) -> RuntimeBinding:
        try:
            binding = self._binding_provider.current()
        except Exception:
            raise FlowAuthenticationError("runtime binding is unavailable") from None
        if not binding.complete():
            raise FlowAuthenticationError("runtime binding is unavailable")
        return binding

    def _require_active_flow(self, *, session_id: str, task_context: str) -> None:
        self._expire_if_needed()
        self._revoke_if_runtime_changed()
        if self._mode is not AccessMode.FLOW:
            raise FlowAuthenticationError("Flow is not active")
        if not hmac.compare_digest(session_id, self._session_id or ""):
            raise FlowAuthenticationError("Flow session does not match")
        if not task_context or not hmac.compare_digest(_digest_text(task_context), self._task_context_digest or ""):
            raise FlowAuthenticationError("Flow task context does not match")

    def _context_has_valid_action_grant(self, context: ToolPolicyContext) -> bool:
        for capability in context.granted_capabilities:
            if not capability.startswith(_FLOW_ACTION_PREFIX):
                continue
            record = self._action_grants.get(_digest_text(capability))
            if record == (self._task_context_digest or "", self._authority_epoch):
                return True
        return False

    def _expire_if_needed(self) -> None:
        if (
            self._mode is AccessMode.FLOW
            and self._expires_at is not None
            and self._clock() >= self._expires_at
        ):
            self._clear_flow("flow_session_expired", destroy_bridge=True)

    def _revoke_if_runtime_changed(self) -> None:
        if self._mode is not AccessMode.FLOW or self._activation_binding is None:
            return
        try:
            binding = self._binding_provider.current()
        except Exception:
            self._clear_flow("runtime_binding_unavailable", destroy_bridge=True)
            return
        if binding != self._activation_binding:
            self._clear_flow("runtime_binding_changed", destroy_bridge=True)

    def _clear_flow(self, reason: str, *, destroy_bridge: bool) -> None:
        self._mode = AccessMode.LOCKED
        self._session_id = None
        self._activated_at = None
        self._expires_at = None
        self._last_activity_at = None
        self._activation_binding = None
        self._task_context_digest = None
        self._action_grants.clear()
        self._pending_challenge = None
        if destroy_bridge:
            self._destroy_bridge_secret()
        self._authority_epoch += 1
        self._lock_reason = reason


__all__ = [
    "ASSERTION_MAX_AGE_SECONDS",
    "ASSISTANT_CAPABILITIES",
    "BRIDGE_SECRET_MAX_AGE_SECONDS",
    "FLOW_BRIDGE_SECRET_ENV",
    "FLOW_CAPABILITIES",
    "AccessMode",
    "FlowActionLease",
    "FlowActivationChallenge",
    "FlowAuthenticationError",
    "FlowSessionAuthority",
    "FlowStatus",
    "FlowTurnPolicy",
    "NativeFlowAssertion",
    "generate_bridge_secret",
]
