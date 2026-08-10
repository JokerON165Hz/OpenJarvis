"""Exactly-once and recovery invariants for the operator action service."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from openjarvis.tasks.policy import RiskLevel, ToolPolicyContext, ToolPolicyDecision
from openjarvis.tasks.types import ExecutionLane
from openjarvis.tools.action_service import (
    RegisteredToolRuntime,
    ToolActionError,
    ToolActionService,
)
from openjarvis.tools.action_store import ActionStore
from openjarvis.tools.actions import (
    ActionStatus,
    ParameterSource,
    ToolProposal,
    VerificationResult,
)
from openjarvis.tools.manifest import (
    IdempotencyPolicy,
    NetworkPolicy,
    SecretPolicy,
    SideEffectClass,
    ToolManifest,
    ToolManifestCatalog,
)


class _AllowAuthority:
    def __init__(self) -> None:
        self.contexts: list[ToolPolicyContext] = []

    def authorize_tool(
        self,
        manifest: ToolManifest,
        context: ToolPolicyContext,
    ) -> ToolPolicyDecision:
        self.contexts.append(context)
        effective = RiskLevel(
            max(
                int(manifest.risk_level),
                int(context.requested_risk),
                int(context.untrusted_risk),
            )
        )
        return ToolPolicyDecision(
            allowed=True,
            status="allowed",
            effective_risk=effective,
            capability=manifest.capability,
            reason="operator test authority",
            allowed_roots=context.allowed_roots,
        )

    def is_flow(self) -> bool:
        return True


def _manifest(
    *,
    side_effect: SideEffectClass = SideEffectClass.LOCAL_READ,
    risk: RiskLevel = RiskLevel.READ_ONLY,
    idempotency: IdempotencyPolicy = IdempotencyPolicy.SAFE_RETRY,
    retries: int = 1,
    timeout: float = 0.5,
) -> ToolManifest:
    return ToolManifest(
        tool_id="operator.test",
        name="operator.test",
        description="Synthetic operator action.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        output_schema={"type": "object"},
        capability="operator:test",
        risk_level=risk,
        allowed_lanes=(ExecutionLane.MODEL,),
        supported_platforms=("windows", "linux", "darwin"),
        timeout=timeout,
        max_retries=retries,
        idempotency_policy=idempotency,
        side_effect_class=side_effect,
        verification_strategy="compare synthetic state",
        undo_strategy="none",
        required_approval=False,
        network_policy=NetworkPolicy.DENY,
        secret_policy=SecretPolicy.REDACT,
        log_redaction_policy="credentials_and_sensitive_values",
    )


def _proposal(manifest: ToolManifest, *, key: str = "operator-once") -> ToolProposal:
    return ToolProposal(
        task_id="operator-task",
        session_id="operator-session",
        correlation_id="operator-correlation",
        thread_id="operator-thread",
        turn_id="operator-turn",
        item_id="operator-item",
        tool_id=manifest.tool_id,
        arguments={"value": "synthetic"},
        expected_result="synthetic",
        expected_side_effect=manifest.side_effect_class,
        risk_level=manifest.risk_level,
        capability=manifest.capability,
        target="synthetic-target",
        verification_plan="compare exact state",
        undo_plan="not applicable",
        idempotency_key=key,
        timeout_seconds=min(0.2, manifest.timeout),
        rationale="operator invariant test",
        parameter_sources={"value": ParameterSource.USER},
    )


def _service(
    db_path: Path,
    artifact_root: Path,
    manifest: ToolManifest,
    *,
    handler,
    verifier=None,
    interrupt=None,
    authority: _AllowAuthority | None = None,
    context_box: dict[str, ToolPolicyContext] | None = None,
) -> tuple[ToolActionService, ActionStore, _AllowAuthority, dict[str, ToolPolicyContext]]:
    authority = authority or _AllowAuthority()
    context_box = context_box or {
        "context": ToolPolicyContext(
            granted_capabilities=frozenset({manifest.capability}),
            execution_lane=ExecutionLane.MODEL,
            requested_risk=manifest.risk_level,
            proposal_capability=manifest.capability,
            allowed_roots=(db_path.parent,),
        )
    }
    store = ActionStore(db_path)
    service = ToolActionService(
        catalog=ToolManifestCatalog((manifest,)),
        store=store,
        context_factory=lambda _proposal: context_box["context"],
        runtimes={
            manifest.tool_id: RegisteredToolRuntime(
                handler=handler,
                verifier=verifier
                or (
                    lambda _proposal, _output: VerificationResult(
                        passed=True,
                        observed_state="synthetic",
                        expected_state="synthetic",
                    )
                ),
                interrupt=interrupt,
            )
        },
        artifact_root=artifact_root,
        flow_authority=authority,
    )
    return service, store, authority, context_box


def test_duplicate_idempotency_key_keeps_one_stable_action(tmp_path: Path) -> None:
    manifest = _manifest()
    service, store, _, _ = _service(
        tmp_path / "actions.db",
        tmp_path / "artifacts",
        manifest,
        handler=lambda arguments: arguments,
    )
    first = service.create(_proposal(manifest))
    repeated = service.create(_proposal(manifest))
    assert repeated.action_id == first.action_id
    assert store.list_actions("operator-task") == (first,)
    store.close()


@pytest.mark.asyncio
async def test_parallel_services_claim_same_action_once(tmp_path: Path) -> None:
    manifest = _manifest()
    db_path = tmp_path / "actions.db"
    calls = 0
    calls_lock = threading.Lock()

    def handler(arguments):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return arguments

    first, first_store, _, _ = _service(
        db_path,
        tmp_path / "artifacts-1",
        manifest,
        handler=handler,
    )
    action = first.create(_proposal(manifest))
    second, second_store, _, _ = _service(
        db_path,
        tmp_path / "artifacts-2",
        manifest,
        handler=handler,
    )

    results = await asyncio.gather(
        first.execute(action.action_id),
        second.execute(action.action_id),
        return_exceptions=True,
    )

    assert calls == 1
    assert sum(
        getattr(result, "status", None) is ActionStatus.COMPLETED
        for result in results
    ) == 1
    assert sum(isinstance(result, ToolActionError) for result in results) == 1
    first_store.close()
    second_store.close()


@pytest.mark.asyncio
async def test_crash_after_effect_requires_recovery_without_reexecution(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.DESTRUCTIVE_OR_SENSITIVE,
        idempotency=IdempotencyPolicy.NEVER_AFTER_UNKNOWN_EFFECT,
        retries=0,
    )
    db_path = tmp_path / "actions.db"
    calls = 0

    def handler(arguments):
        nonlocal calls
        calls += 1
        return arguments

    first, first_store, _, _ = _service(
        db_path,
        tmp_path / "artifacts-1",
        manifest,
        handler=handler,
    )
    action = first.create(_proposal(manifest))

    def crash(*_args, **_kwargs):
        raise SystemExit("simulated crash after external effect")

    first._store_output = crash
    with pytest.raises(SystemExit):
        await first.execute(action.action_id)
    assert first_store.get_action(action.action_id).status is ActionStatus.RUNNING
    first_store.close()

    recovered_service, recovered_store, _, _ = _service(
        db_path,
        tmp_path / "artifacts-2",
        manifest,
        handler=handler,
    )
    recovered = recovered_service.recover(action.action_id)
    assert recovered.status is ActionStatus.RECOVERY_REQUIRED
    assert recovered.effect_known is False
    with pytest.raises(ToolActionError, match="requires recovery"):
        await recovered_service.execute(action.action_id)
    assert calls == 1
    recovered_store.close()


@pytest.mark.asyncio
async def test_startup_recovery_marks_every_inflight_effect_without_reexecution(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.DESTRUCTIVE_OR_SENSITIVE,
        idempotency=IdempotencyPolicy.NEVER_AFTER_UNKNOWN_EFFECT,
        retries=0,
    )
    db_path = tmp_path / "actions.db"
    calls = 0

    def handler(arguments):
        nonlocal calls
        calls += 1
        return arguments

    crashed, crashed_store, _, _ = _service(
        db_path,
        tmp_path / "artifacts-1",
        manifest,
        handler=handler,
    )
    action = crashed.create(_proposal(manifest))

    def crash(*_args, **_kwargs):
        raise SystemExit("simulated crash before state commit")

    crashed._store_output = crash
    with pytest.raises(SystemExit):
        await crashed.execute(action.action_id)
    crashed_store.close()

    restarted, restarted_store, _, _ = _service(
        db_path,
        tmp_path / "artifacts-2",
        manifest,
        handler=handler,
    )
    recovered = restarted.recover_incomplete()

    assert [item.action_id for item in recovered] == [action.action_id]
    assert recovered[0].status is ActionStatus.RECOVERY_REQUIRED
    assert recovered[0].effect_known is False
    assert restarted.recover_incomplete() == ()
    assert calls == 1
    restarted_store.close()


@pytest.mark.asyncio
async def test_global_stop_cancels_inflight_action_and_blocks_late_revival(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.DESTRUCTIVE_OR_SENSITIVE,
        idempotency=IdempotencyPolicy.NEVER_AFTER_UNKNOWN_EFFECT,
        retries=0,
    )
    entered = threading.Event()
    released = threading.Event()
    interrupted = threading.Event()

    def handler(arguments):
        entered.set()
        released.wait(timeout=2)
        return arguments

    service, store, _, _ = _service(
        tmp_path / "actions.db",
        tmp_path / "artifacts",
        manifest,
        handler=handler,
        interrupt=interrupted.set,
    )
    action = service.create(_proposal(manifest))
    execution = asyncio.create_task(service.execute(action.action_id))
    assert await asyncio.to_thread(entered.wait, 1)

    stopped = service.global_stop()
    released.set()
    result = await execution

    assert stopped[0].status is ActionStatus.CANCELED
    assert stopped[0].effect_known is False
    assert interrupted.is_set()
    assert result.status is ActionStatus.CANCELED
    assert store.get_action(action.action_id).status is ActionStatus.CANCELED
    assert "tool.completed" not in {
        event.event_type for event in store.list_events(action.action_id)
    }
    store.close()


@pytest.mark.asyncio
async def test_failed_external_postcondition_is_recovery_not_success(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.DESTRUCTIVE_OR_SENSITIVE,
        idempotency=IdempotencyPolicy.NEVER_AFTER_UNKNOWN_EFFECT,
        retries=0,
    )
    service, store, _, _ = _service(
        tmp_path / "actions.db",
        tmp_path / "artifacts",
        manifest,
        handler=lambda arguments: arguments,
        verifier=lambda _proposal, _output: VerificationResult(
            passed=False,
            observed_state="partial",
            expected_state="complete",
        ),
    )
    action = service.create(_proposal(manifest))
    result = await service.execute(action.action_id)
    assert result.status is ActionStatus.RECOVERY_REQUIRED
    assert result.effect_known is False
    assert "tool.completed" not in {
        event.event_type for event in store.list_events(action.action_id)
    }
    store.close()


@pytest.mark.asyncio
async def test_direct_execute_cannot_bypass_retry_policy(tmp_path: Path) -> None:
    manifest = _manifest()
    calls = 0

    def handler(arguments):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic read failure")
        return arguments

    service, store, _, _ = _service(
        tmp_path / "actions.db",
        tmp_path / "artifacts",
        manifest,
        handler=handler,
    )
    action = service.create(_proposal(manifest))
    failed = await service.execute(action.action_id)
    assert failed.status is ActionStatus.FAILED
    with pytest.raises(ToolActionError, match="retry"):
        await service.execute(action.action_id)
    assert calls == 1
    completed = await service.retry(action.action_id)
    assert completed.status is ActionStatus.COMPLETED
    assert calls == 2
    store.close()


@pytest.mark.asyncio
async def test_persisted_risk_floor_and_manifest_snapshot_cannot_drop(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    authority = _AllowAuthority()
    context_box = {
        "context": ToolPolicyContext(
            granted_capabilities=frozenset({manifest.capability}),
            execution_lane=ExecutionLane.MODEL,
            requested_risk=RiskLevel.READ_ONLY,
            proposal_capability=manifest.capability,
            untrusted_risk=RiskLevel.DESTRUCTIVE_OR_SENSITIVE,
            allowed_roots=(tmp_path,),
        )
    }
    service, store, _, _ = _service(
        tmp_path / "actions.db",
        tmp_path / "artifacts",
        manifest,
        handler=lambda arguments: arguments,
        authority=authority,
        context_box=context_box,
    )
    action = service.create(_proposal(manifest))
    assert action.risk_level is RiskLevel.DESTRUCTIVE_OR_SENSITIVE

    context_box["context"] = replace(
        context_box["context"],
        requested_risk=RiskLevel.READ_ONLY,
        untrusted_risk=RiskLevel.READ_ONLY,
    )
    completed = await service.execute(action.action_id)
    assert completed.status is ActionStatus.COMPLETED
    assert authority.contexts[-1].requested_risk is RiskLevel.DESTRUCTIVE_OR_SENSITIVE

    next_action = service.create(_proposal(manifest, key="policy-change"))
    changed_manifest = manifest.model_copy(update={"timeout": manifest.timeout / 2})
    service.catalog.replace(changed_manifest)
    with pytest.raises(ToolActionError, match="manifest changed"):
        await service.execute(next_action.action_id)
    store.close()


@pytest.mark.asyncio
async def test_tool_output_cannot_lower_policy_and_secrets_are_redacted(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.DESTRUCTIVE_OR_SENSITIVE,
        idempotency=IdempotencyPolicy.NEVER_AFTER_UNKNOWN_EFFECT,
        retries=0,
    )

    def handler(_arguments):
        return {
            "risk_level": 0,
            "required_approval": False,
            "api_key": "sk-super-secret-value",
        }

    service, store, _, _ = _service(
        tmp_path / "actions.db",
        tmp_path / "artifacts",
        manifest,
        handler=handler,
    )
    action = service.create(_proposal(manifest))
    completed = await service.execute(action.action_id)
    assert completed.status is ActionStatus.COMPLETED
    assert completed.risk_level is RiskLevel.DESTRUCTIVE_OR_SENSITIVE
    assert "super-secret" not in completed.output_summary
    audit = " ".join(str(event.payload) for event in store.list_events(action.action_id))
    assert "super-secret" not in audit
    store.close()
