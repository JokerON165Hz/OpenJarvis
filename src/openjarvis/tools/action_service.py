"""Central, policy-owned execution loop for structured tool actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjarvis.codex.redaction import redact_data
from openjarvis.tasks.lanes import ExecutionLaneScheduler
from openjarvis.tasks.policy import ToolPolicyContext, ToolPolicyDecision
from openjarvis.tasks.service import TaskService
from openjarvis.tools.action_store import ActionStore, ActionStoreError
from openjarvis.tools.actions import (
    ActionStatus,
    ToolAction,
    ToolArtifact,
    ToolEvent,
    ToolProposal,
    VerificationResult,
    VerificationStatus,
)
from openjarvis.tools.manifest import (
    IdempotencyPolicy,
    ManifestValidationError,
    SideEffectClass,
    ToolManifest,
    ToolManifestCatalog,
)

ToolHandler = Callable[[Mapping[str, Any]], Any]
ToolVerifier = Callable[[ToolProposal, Any], VerificationResult]
ToolInterrupt = Callable[[], Any]
ContextFactory = Callable[[ToolProposal], ToolPolicyContext]


class ToolActionError(RuntimeError):
    """Raised when an action cannot safely advance."""


@dataclass(frozen=True, slots=True)
class RegisteredToolRuntime:
    handler: ToolHandler
    verifier: ToolVerifier
    interrupt: ToolInterrupt | None = None


class ToolActionService:
    """Execute registered tools only through policy, lanes, and verification."""

    def __init__(
        self,
        *,
        catalog: ToolManifestCatalog,
        store: ActionStore,
        context_factory: ContextFactory,
        runtimes: Mapping[str, RegisteredToolRuntime],
        artifact_root: str | Path,
        flow_authority=None,
        task_service: TaskService | None = None,
        lanes: ExecutionLaneScheduler | None = None,
        inline_output_limit: int = 16_384,
    ) -> None:
        if inline_output_limit <= 0:
            raise ValueError("inline_output_limit must be positive")
        self.catalog = catalog
        self.store = store
        self._context_factory = context_factory
        self._runtimes = dict(runtimes)
        if flow_authority is None:
            from openjarvis.flow import FlowSessionAuthority

            flow_authority = FlowSessionAuthority.from_environment()
        self._flow_authority = flow_authority
        self._tasks = task_service
        self._lanes = lanes or ExecutionLaneScheduler()
        self._artifact_root = Path(artifact_root).resolve(strict=False)
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._inline_output_limit = inline_output_limit
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = threading.RLock()
        self._interrupt_lock = threading.RLock()
        self._interrupted_tasks: set[str] = set()
        self._active_runtimes: dict[str, tuple[str, RegisteredToolRuntime]] = {}

    @property
    def lanes(self) -> ExecutionLaneScheduler:
        return self._lanes

    def runtime_available(self, tool_id: str) -> bool:
        with self._registry_lock:
            return tool_id in self._runtimes

    def policy_context(self, proposal: ToolProposal) -> ToolPolicyContext:
        """Expose the trusted context for additional code-owned root binding."""

        return self._context_factory(proposal)

    def register_runtime(
        self,
        manifest: ToolManifest,
        runtime: RegisteredToolRuntime,
    ) -> None:
        """Bind one trusted startup runtime without exposing model registration."""

        with self._registry_lock:
            self.catalog.register(manifest)
            existing = self._runtimes.get(manifest.tool_id)
            if existing is not None and existing is not runtime:
                raise ToolActionError(
                    f"tool runtime is already registered: {manifest.tool_id}"
                )
            self._runtimes[manifest.tool_id] = runtime

    def refresh_runtime(
        self,
        manifest: ToolManifest,
        runtime: RegisteredToolRuntime,
    ) -> None:
        """Refresh a reconnectable runtime only when its manifest is unchanged."""

        with self._registry_lock:
            existing = self.catalog.get(manifest.tool_id)
            if existing != manifest:
                raise ToolActionError(
                    f"tool manifest changed during runtime refresh: {manifest.tool_id}"
                )
            self._runtimes[manifest.tool_id] = runtime

    def replace_runtime_policy(
        self,
        manifest: ToolManifest,
        runtime: RegisteredToolRuntime,
    ) -> None:
        """Apply a trusted local policy change without accepting schema drift.

        Existing actions are bound to the prior manifest fingerprint and will
        refuse execution after any policy replacement. They must be recreated.
        """

        with self._registry_lock:
            existing = self.catalog.get(manifest.tool_id)
            mutable_policy_fields = {
                "risk_level",
                "side_effect_class",
                "required_approval",
                "idempotency_policy",
                "undo_strategy",
                "enabled",
                "degraded_reason",
            }
            if existing.model_dump(exclude=mutable_policy_fields) != manifest.model_dump(
                exclude=mutable_policy_fields
            ):
                raise ToolActionError(
                    f"tool schema changed during runtime refresh: {manifest.tool_id}"
                )
            self.catalog.replace(manifest)
            if manifest.enabled:
                self._runtimes[manifest.tool_id] = runtime
            else:
                self._runtimes.pop(manifest.tool_id, None)

    def unregister_runtime(self, tool_id: str) -> None:
        """Remove an external runtime binding while retaining its audit manifest."""

        with self._registry_lock:
            self._runtimes.pop(tool_id, None)

    def begin_task(self, task_id: str) -> None:
        """Clear a prior Stop marker when the owner explicitly starts/resumes work."""

        with self._interrupt_lock:
            self._interrupted_tasks.discard(task_id)

    def interrupt_task(self, task_id: str) -> int:
        """Stop the active tool chain and signal interruptible native runtimes."""

        with self._interrupt_lock:
            self._interrupted_tasks.add(task_id)
            active = tuple(
                runtime
                for active_task_id, runtime in self._active_runtimes.values()
                if active_task_id == task_id
            )
        interrupted = 0
        for runtime in active:
            if runtime.interrupt is None:
                continue
            try:
                runtime.interrupt()
            except Exception:
                continue
            interrupted += 1
        return interrupted

    def _task_interrupted(self, task_id: str) -> bool:
        with self._interrupt_lock:
            return task_id in self._interrupted_tasks

    def create(self, proposal: ToolProposal) -> ToolAction:
        """Validate and persist a proposal without executing before approval."""

        stored = self.store.put_proposal(proposal)
        existing = self.store.get_action_by_proposal(stored.proposal_id)
        if existing is not None:
            return existing
        try:
            with self._registry_lock:
                manifest = self.catalog.get(stored.tool_id)
                fingerprint = self._manifest_fingerprint(manifest)
        except ManifestValidationError as exc:
            raise ToolActionError(str(exc)) from exc
        context = self._context_factory(stored)
        task_risk = None
        if self._tasks is not None:
            task = self._tasks.get(stored.task_id)
            if task is not None:
                task_risk = task.risk_level
        action, created = self.store.put_action_once(
            ToolAction.from_proposal(
                stored,
                manifest_version=manifest.version,
                manifest_fingerprint=fingerprint,
                effective_risk=self._effective_risk(
                    manifest,
                    context,
                    task_risk=task_risk,
                ),
            )
        )
        if not created:
            return action
        self._emit(action, "tool.proposed", {"target": stored.target})

        validation_error = self._validate_proposal(stored, manifest, context)
        if validation_error:
            action = self.store.transition(
                action.action_id,
                ActionStatus.DENIED,
                error=validation_error,
            )
            self._emit(action, "tool.denied", {"reason": validation_error})
            return action

        decision = self._flow_authority.authorize_tool(manifest, context)
        if not decision.allowed:
            action = self.store.transition(
                action.action_id,
                ActionStatus.DENIED,
                error=decision.reason,
            )
            self._emit(action, "tool.denied", self._decision_payload(decision))
            return action
        action = self.store.transition(action.action_id, ActionStatus.VALIDATED)
        self._emit(action, "tool.validated", self._decision_payload(decision))
        return action

    async def execute(self, action_id: str) -> ToolAction:
        """Execute one validated action exactly once."""

        return await self._execute(action_id, allow_failed=False)

    async def _execute(self, action_id: str, *, allow_failed: bool) -> ToolAction:
        lock = self._locks.setdefault(action_id, asyncio.Lock())
        async with lock:
            action = self._require_action(action_id)
            if action.status is ActionStatus.COMPLETED:
                return action
            if action.status is ActionStatus.RECOVERY_REQUIRED:
                raise ToolActionError(
                    "action requires recovery because the prior effect is unknown"
                )
            if action.status is ActionStatus.FAILED and not allow_failed:
                raise ToolActionError("failed actions may only run through retry()")
            if action.status not in {
                ActionStatus.VALIDATED,
                ActionStatus.FAILED,
            }:
                raise ToolActionError(
                    f"action cannot execute from {action.status.value}"
                )
            if self._task_interrupted(action.task_id):
                raise ToolActionError("tool chain was stopped by the owner")

            proposal = self._require_proposal(action.proposal_id)
            with self._registry_lock:
                manifest = self.catalog.get(action.tool_id)
                runtime = self._runtimes.get(action.tool_id)
                manifest_fingerprint = self._manifest_fingerprint(manifest)
            binding_error = self._validate_execution_binding(
                action,
                proposal,
                manifest,
                manifest_fingerprint,
            )
            if binding_error:
                raise ToolActionError(binding_error)
            if runtime is None:
                raise ToolActionError("registered tool has no runtime")

            context = self._context_factory(proposal)
            bounded_context = self._bind_risk_floor(action, context)
            decision = self._flow_authority.authorize_tool(manifest, bounded_context)
            if not decision.allowed:
                raise ToolActionError(decision.reason)
            return await self._lanes.run(
                bounded_context.execution_lane,
                lambda: self._execute_in_lane(
                    action,
                    proposal,
                    manifest,
                    manifest_fingerprint,
                    runtime,
                    allow_failed=allow_failed,
                ),
            )

    def cancel(self, action_id: str) -> ToolAction:
        action = self._require_action(action_id)
        if action.status is ActionStatus.CANCELED:
            return action
        if action.status in {ActionStatus.COMPLETED, ActionStatus.DENIED}:
            raise ToolActionError(f"terminal action is already {action.status.value}")
        effect_known = action.status not in {
            ActionStatus.RUNNING,
            ActionStatus.VERIFYING,
            ActionStatus.RECOVERY_REQUIRED,
        }
        action = self.store.transition(
            action.action_id,
            ActionStatus.CANCELED,
            effect_known=effect_known,
            verification_status=(
                VerificationStatus.UNKNOWN if not effect_known else None
            ),
            error=("action canceled while effect was in flight" if not effect_known else None),
        )
        with self._interrupt_lock:
            active = self._active_runtimes.get(action_id)
        if active is not None and active[1].interrupt is not None:
            try:
                active[1].interrupt()
            except Exception:
                pass
        self._emit(action, "tool.canceled", {"effect_known": effect_known})
        return action

    def recover(self, action_id: str) -> ToolAction:
        """Persist a post-crash unknown-effect state without re-running the handler."""

        action = self._require_action(action_id)
        if action.status is ActionStatus.RECOVERY_REQUIRED:
            return action
        with self._interrupt_lock:
            if action_id in self._active_runtimes:
                raise ToolActionError("cannot recover an action that is active locally")
        if action.status not in {ActionStatus.RUNNING, ActionStatus.VERIFYING}:
            return action
        recovered = self.store.mark_recovery_required(
            action.action_id,
            error="process ended before effect and verification could be reconciled",
        )
        self._emit(
            recovered,
            "tool.recovery_required",
            {"reason": "incomplete_persisted_execution", "effect_known": False},
        )
        return recovered

    async def retry(self, action_id: str) -> ToolAction:
        action = self._require_action(action_id)
        if action.status is not ActionStatus.FAILED:
            raise ToolActionError("only a failed action can be retried")
        manifest = self.catalog.get(action.tool_id)
        if action.manifest_fingerprint != self._manifest_fingerprint(manifest):
            raise ToolActionError("tool policy changed; recreate the action before retry")
        if action.retry_count >= manifest.max_retries:
            raise ToolActionError("maximum retries reached")
        if not action.effect_known:
            raise ToolActionError("retry blocked because the prior effect is unknown")
        if manifest.idempotency_policy is not IdempotencyPolicy.SAFE_RETRY:
            raise ToolActionError("manifest does not permit automatic retry")
        self.store.transition(
            action.action_id,
            ActionStatus.FAILED,
            retry_count=action.retry_count + 1,
        )
        return await self._execute(action.action_id, allow_failed=True)

    async def _execute_in_lane(
        self,
        action: ToolAction,
        proposal: ToolProposal,
        manifest: ToolManifest,
        manifest_fingerprint: str,
        runtime: RegisteredToolRuntime,
        *,
        allow_failed: bool,
    ) -> ToolAction:
        # Re-check the exact policy/runtime snapshot after waiting for the lane.
        with self._registry_lock:
            current_manifest = self.catalog.get(action.tool_id)
            current_runtime = self._runtimes.get(action.tool_id)
            if self._manifest_fingerprint(current_manifest) != manifest_fingerprint:
                raise ToolActionError("tool policy changed before execution")
            if current_runtime is not runtime:
                raise ToolActionError("tool runtime changed before execution")

        run_id = f"run_{uuid.uuid4().hex}"
        action, claimed = self.store.claim_execution(
            action.action_id,
            tool_run_id=run_id,
            allow_failed=allow_failed,
        )
        if not claimed:
            if action.status in {ActionStatus.COMPLETED, ActionStatus.CANCELED}:
                return action
            raise ToolActionError(
                f"action execution is already claimed or blocked: {action.status.value}"
            )

        self._emit(action, "tool.started", {"timeout": manifest.timeout})
        with self._interrupt_lock:
            self._active_runtimes[action.action_id] = (action.task_id, runtime)
        try:
            output = await asyncio.wait_for(
                asyncio.to_thread(runtime.handler, dict(proposal.arguments)),
                timeout=min(proposal.timeout_seconds, manifest.timeout),
            )
        except TimeoutError:
            if runtime.interrupt is not None:
                try:
                    await asyncio.to_thread(runtime.interrupt)
                except Exception:
                    pass
            return self._ambiguous_or_failed(
                action,
                manifest,
                "tool execution timed out",
            )
        except Exception as exc:
            return self._ambiguous_or_failed(
                action,
                manifest,
                f"tool execution failed: {type(exc).__name__}",
            )
        finally:
            with self._interrupt_lock:
                self._active_runtimes.pop(action.action_id, None)

        current = self._require_action(action.action_id)
        if current.status in {ActionStatus.CANCELED, ActionStatus.RECOVERY_REQUIRED}:
            return current
        if self._task_interrupted(action.task_id):
            try:
                action = self.store.transition(
                    action.action_id,
                    ActionStatus.CANCELED,
                    error="tool chain stopped by owner",
                    effect_known=False,
                    verification_status=VerificationStatus.UNKNOWN,
                )
            except ActionStoreError:
                return self._require_action(action.action_id)
            self._emit(action, "tool.canceled", {"reason": "owner_stop"})
            return action
        if current.status is not ActionStatus.RUNNING:
            raise ToolActionError(
                f"action left running state unexpectedly: {current.status.value}"
            )
        action = current

        output_payload = redact_data(output)
        output_summary, artifact = self._store_output(action, output_payload)
        self._emit(
            action,
            "tool.output",
            {
                "summary": output_summary,
                "artifact_id": artifact.artifact_id if artifact else None,
            },
            artifact_id=artifact.artifact_id if artifact else None,
        )
        try:
            action = self.store.transition(
                action.action_id,
                ActionStatus.VERIFYING,
                output_summary=output_summary,
            )
        except ActionStoreError:
            current = self._require_action(action.action_id)
            if current.status in {ActionStatus.CANCELED, ActionStatus.RECOVERY_REQUIRED}:
                return current
            raise
        self._emit(action, "tool.verification_started", {})
        try:
            verification = await asyncio.to_thread(
                runtime.verifier,
                proposal,
                output,
            )
        except Exception as exc:
            return self._ambiguous_or_failed(
                action,
                manifest,
                f"verification failed: {type(exc).__name__}",
            )
        if not isinstance(verification, VerificationResult):
            return self._ambiguous_or_failed(
                action,
                manifest,
                "verifier returned an invalid result",
            )
        if not verification.passed:
            self._emit(
                action,
                "tool.verification_failed",
                {
                    "observed": verification.observed_state,
                    "expected": verification.expected_state,
                },
            )
            if self._effect_can_be_external(manifest):
                return self._recovery_required(
                    action,
                    "postcondition failed after a potentially partial effect",
                )
            return self._fail(
                action,
                "postcondition was not verified",
                effect_known=True,
            )

        current = self._require_action(action.action_id)
        if current.status is ActionStatus.CANCELED:
            return current
        try:
            action = self.store.transition(
                action.action_id,
                ActionStatus.VERIFIED,
                verification_status=VerificationStatus.PASSED,
                effect_known=True,
            )
        except ActionStoreError:
            current = self._require_action(action.action_id)
            if current.status is ActionStatus.CANCELED:
                return current
            raise
        self._emit(
            action,
            "tool.verified",
            {
                "observed": verification.observed_state,
                "expected": verification.expected_state,
                "artifact_ids": list(verification.artifact_ids),
            },
        )
        try:
            action = self.store.transition(action.action_id, ActionStatus.COMPLETED)
        except ActionStoreError:
            current = self._require_action(action.action_id)
            if current.status is ActionStatus.CANCELED:
                return current
            raise
        self._emit(action, "tool.completed", {})
        return action

    def _validate_proposal(
        self,
        proposal: ToolProposal,
        manifest: ToolManifest,
        context: ToolPolicyContext,
    ) -> str:
        try:
            manifest.validate_arguments(proposal.arguments)
        except ManifestValidationError as exc:
            return str(exc)
        if proposal.capability != manifest.capability:
            return "proposal capability differs from trusted manifest"
        if proposal.expected_side_effect is not manifest.side_effect_class:
            return "proposal side effect differs from trusted manifest"
        if proposal.timeout_seconds > manifest.timeout:
            return "proposal timeout exceeds trusted manifest"
        if self._tasks is not None:
            task = self._tasks.get(proposal.task_id)
            if task is None:
                return "proposal references an unknown task"
            if (
                task.session_id != proposal.session_id
                or task.correlation_id != proposal.correlation_id
            ):
                return "proposal identity differs from canonical task"
        if context.proposal_capability != proposal.capability:
            return "trusted context does not match proposal capability"
        return ""

    def _validate_execution_binding(
        self,
        action: ToolAction,
        proposal: ToolProposal,
        manifest: ToolManifest,
        manifest_fingerprint: str,
    ) -> str:
        if not action.manifest_fingerprint:
            return "legacy action has no policy fingerprint; recreate it before execution"
        if action.manifest_fingerprint != manifest_fingerprint:
            return "trusted manifest changed after proposal; recreate the action"
        if action.manifest_version != manifest.version:
            return "trusted manifest version changed after proposal"
        if action.tool_id != proposal.tool_id or action.tool_id != manifest.tool_id:
            return "action tool identity differs from its proposal or manifest"
        if action.capability != proposal.capability or action.capability != manifest.capability:
            return "action capability differs from its proposal or manifest"
        if (
            action.expected_side_effect is not proposal.expected_side_effect
            or action.expected_side_effect is not manifest.side_effect_class
        ):
            return "action side effect differs from its proposal or manifest"
        if action.idempotency_key != proposal.idempotency_key:
            return "action idempotency key differs from its proposal"
        return ""

    @staticmethod
    def _bind_risk_floor(
        action: ToolAction,
        context: ToolPolicyContext,
    ) -> ToolPolicyContext:
        risk_type = type(action.risk_level)
        floor = risk_type(
            max(
                int(action.risk_level),
                int(context.requested_risk),
                int(context.untrusted_risk),
            )
        )
        return ToolPolicyContext(
            granted_capabilities=context.granted_capabilities,
            execution_lane=context.execution_lane,
            requested_risk=floor,
            proposal_capability=context.proposal_capability,
            approved_once=context.approved_once,
            untrusted_risk=floor,
            allowed_roots=context.allowed_roots,
        )

    def _store_output(
        self,
        action: ToolAction,
        output: Any,
    ) -> tuple[str, ToolArtifact | None]:
        encoded = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        if len(encoded) <= self._inline_output_limit:
            return encoded.decode("utf-8"), None
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        path = self._artifact_root / f"{artifact_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)
        artifact = self.store.put_artifact(
            ToolArtifact(
                artifact_id=artifact_id,
                task_id=action.task_id,
                session_id=action.session_id,
                correlation_id=action.correlation_id,
                thread_id=action.thread_id,
                turn_id=action.turn_id,
                item_id=action.item_id,
                proposal_id=action.proposal_id,
                action_id=action.action_id,
                approval_id=action.approval_id,
                tool_run_id=action.tool_run_id or "run_unavailable",
                kind="tool_output",
                path=str(path),
                sha256=hashlib.sha256(encoded).hexdigest(),
                size_bytes=len(encoded),
                media_type="application/json",
                redacted=True,
            )
        )
        if self._tasks is not None:
            self._tasks.store.save_artifact(
                artifact_id=artifact.artifact_id,
                task_id=artifact.task_id,
                kind=artifact.kind,
                media_type=artifact.media_type,
                content=encoded,
                metadata={"action_id": artifact.action_id, "redacted": True},
            )
        return f"output stored as artifact ({len(encoded)} bytes)", artifact

    @staticmethod
    def _effect_can_be_external(manifest: ToolManifest) -> bool:
        return manifest.side_effect_class not in {
            SideEffectClass.NONE,
            SideEffectClass.LOCAL_READ,
        }

    def _ambiguous_or_failed(
        self,
        action: ToolAction,
        manifest: ToolManifest,
        error: str,
    ) -> ToolAction:
        if self._effect_can_be_external(manifest):
            return self._recovery_required(action, error)
        return self._fail(action, error, effect_known=True)

    def _recovery_required(self, action: ToolAction, error: str) -> ToolAction:
        current = self._require_action(action.action_id)
        if current.status is ActionStatus.CANCELED:
            return current
        if current.status is ActionStatus.RECOVERY_REQUIRED:
            return current
        recovered = self.store.mark_recovery_required(
            action.action_id,
            error=error,
        )
        self._emit(
            recovered,
            "tool.recovery_required",
            {"error": error, "effect_known": False},
        )
        return recovered

    def _fail(
        self,
        action: ToolAction,
        error: str,
        *,
        effect_known: bool,
    ) -> ToolAction:
        current = self._require_action(action.action_id)
        if current.status is ActionStatus.CANCELED:
            return current
        action = self.store.transition(
            action.action_id,
            ActionStatus.FAILED,
            verification_status=VerificationStatus.FAILED,
            error=error,
            effect_known=effect_known,
        )
        self._emit(
            action,
            "tool.failed",
            {"error": error, "effect_known": effect_known},
        )
        return action

    def _emit(
        self,
        action: ToolAction,
        event_type: str,
        payload: dict[str, Any],
        *,
        artifact_id: str | None = None,
    ) -> ToolEvent:
        event = self.store.append_event(
            ToolEvent(
                event_type=event_type,
                task_id=action.task_id,
                session_id=action.session_id,
                correlation_id=action.correlation_id,
                thread_id=action.thread_id,
                turn_id=action.turn_id,
                item_id=action.item_id,
                proposal_id=action.proposal_id,
                action_id=action.action_id,
                approval_id=action.approval_id,
                tool_run_id=action.tool_run_id,
                artifact_id=artifact_id,
                payload=redact_data(payload),
            )
        )
        if self._tasks is not None:
            task_event, inserted = self._tasks.store.append_event(
                task_id=action.task_id,
                source_event_id=event.event_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                cause="tool_action_service",
                component="tool_action_service",
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                item_id=event.item_id,
                approval_id=event.approval_id,
                action_id=event.action_id,
                artifact_id=event.artifact_id,
                payload=event.payload,
            )
            if inserted:
                self._tasks.project_committed(task_event)
        return event

    @staticmethod
    def _manifest_fingerprint(manifest: ToolManifest) -> str:
        canonical = json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _effective_risk(
        manifest: ToolManifest,
        context: ToolPolicyContext,
        *,
        task_risk: int | None = None,
    ):
        values = [
            int(manifest.risk_level),
            int(context.requested_risk),
            int(context.untrusted_risk),
        ]
        if task_risk is not None:
            values.append(int(task_risk))
        return type(manifest.risk_level)(max(values))

    @staticmethod
    def _decision_payload(decision: ToolPolicyDecision) -> dict[str, Any]:
        return {
            "allowed": decision.allowed,
            "status": decision.status,
            "risk_level": int(decision.effective_risk),
            "capability": decision.capability,
            "reason": decision.reason,
            "allowed_roots": [str(path) for path in decision.allowed_roots],
        }

    def _require_action(self, action_id: str) -> ToolAction:
        action = self.store.get_action(action_id)
        if action is None:
            raise ToolActionError(f"unknown action: {action_id}")
        return action

    def _require_proposal(self, proposal_id: str) -> ToolProposal:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ToolActionError(f"unknown proposal: {proposal_id}")
        return proposal


__all__ = [
    "RegisteredToolRuntime",
    "ToolActionError",
    "ToolActionService",
]
