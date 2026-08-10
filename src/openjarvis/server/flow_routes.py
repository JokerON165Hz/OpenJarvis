"""Local API for the fail-closed Flow authority boundary.

Only the trusted native launcher may issue or satisfy activation challenges.
The request never supplies owner, session, process, or authority data: those
values come from :class:`FlowSessionAuthority` and its runtime binding provider.
"""

from __future__ import annotations

import inspect
import ipaddress
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.flow import (
    FlowActivationChallenge,
    FlowAuthenticationError,
    NativeFlowAssertion,
)

router = APIRouter(prefix="/v1/flow", tags=["flow"])

_NATIVE_BRIDGE_HEADER = "X-OpenJarvis-Native-Bridge"
_NATIVE_BRIDGE_VALUE = "tauri"
_NONTERMINAL_TASK_STATES = {
    "pending",
    "running",
    "waiting_approval",
    "paused",
    "recovering",
}


def _require_local(request: Request) -> None:
    client = request.client
    host = client.host if client is not None else ""
    if host == "testclient":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        if host.lower() == "localhost":
            return
    raise HTTPException(status_code=403, detail="Local access required")


def _require_native(request: Request, native_bridge: str | None) -> None:
    _require_local(request)
    if native_bridge != _NATIVE_BRIDGE_VALUE:
        raise HTTPException(
            status_code=403,
            detail="Flow control must originate in the native desktop process",
        )


class ActivationChallengeRequest(BaseModel):
    task_context: str = Field(min_length=1, max_length=1024)


class ActivationChallenge(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=256)
    os_session_id: str = Field(min_length=1, max_length=256)
    process_id: int = Field(gt=0)
    process_nonce: str = Field(min_length=1, max_length=200)
    task_context_digest: str = Field(min_length=64, max_length=64)
    issued_at: int
    expires_at: int
    bridge_generation: int = Field(gt=0)


class NativeFlowAssertionRequest(BaseModel):
    challenge: ActivationChallenge
    nonce: str = Field(min_length=1, max_length=200)
    authenticated_at: int
    signature: str = Field(min_length=64, max_length=64)
    owner_verification: str = Field(pattern=r"^verified$")


class ActivityRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=200)
    task_context: str | None = Field(default=None, max_length=1024)


def _authority(request: Request):
    authority = getattr(request.app.state, "flow_authority", None)
    if authority is None:
        raise HTTPException(status_code=503, detail="Flow authority is unavailable")
    return authority


async def _invoke(service: Any, method_name: str, *args: Any, **kwargs: Any) -> None:
    method = getattr(service, method_name)
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        await result


@router.get("/status")
async def flow_status(request: Request) -> dict:
    _require_local(request)
    return _authority(request).status().as_dict()


@router.get("/capabilities")
async def flow_capabilities(request: Request) -> dict:
    _require_local(request)
    status = _authority(request).status()
    return {"mode": status.mode.value, "capabilities": status.capabilities}


@router.post("/challenge")
async def issue_activation_challenge(
    body: ActivationChallengeRequest,
    request: Request,
    native_bridge: Annotated[
        str | None, Header(alias=_NATIVE_BRIDGE_HEADER)
    ] = None,
) -> dict:
    """Return a short-lived target bound to trusted runtime and task state."""

    _require_native(request, native_bridge)
    try:
        challenge = _authority(request).issue_activation_challenge(
            task_context=body.task_context
        )
    except FlowAuthenticationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return asdict(challenge)


@router.post("/activate")
async def activate_flow(
    assertion: NativeFlowAssertionRequest,
    request: Request,
    native_bridge: Annotated[
        str | None, Header(alias=_NATIVE_BRIDGE_HEADER)
    ] = None,
) -> dict:
    """Consume one native assertion and perform trusted owner verification."""

    _require_native(request, native_bridge)
    try:
        challenge = FlowActivationChallenge(**assertion.challenge.model_dump())
        native_assertion = NativeFlowAssertion(
            challenge=challenge,
            nonce=assertion.nonce,
            authenticated_at=assertion.authenticated_at,
            signature=assertion.signature,
            owner_verification=assertion.owner_verification,
        )
        return _authority(request).activate_flow(native_assertion).as_dict()
    except FlowAuthenticationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/assistant")
async def activate_assistant(request: Request) -> dict:
    _require_local(request)
    try:
        return _authority(request).activate_assistant().as_dict()
    except FlowAuthenticationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/lock")
async def lock_flow(request: Request, reason: str = "user_locked") -> dict:
    _require_local(request)
    return _authority(request).lock(reason).as_dict()


@router.post("/activity")
async def flow_activity(body: ActivityRequest, request: Request) -> dict:
    _require_local(request)
    try:
        return (
            _authority(request)
            .record_activity(body.session_id, task_context=body.task_context)
            .as_dict()
        )
    except FlowAuthenticationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/global-stop")
async def global_stop(
    request: Request,
    native_bridge: Annotated[
        str | None, Header(alias=_NATIVE_BRIDGE_HEADER)
    ] = None,
) -> dict[str, Any]:
    """Revoke authority first, then stop all owned runtime work.

    Propagation is intentionally sequential and task IDs are sorted so a
    partial failure has a stable, auditable result. Component exception details
    are not returned because native/runtime errors can contain sensitive data.
    """

    _require_native(request, native_bridge)
    status = _authority(request).global_stop()
    stopped: list[str] = []
    failures: list[str] = []

    action_service = getattr(request.app.state, "tool_action_service", None)
    if action_service is not None:
        try:
            await _invoke(action_service, "global_stop")
        except Exception:
            failures.append("tool_action_service")
        else:
            stopped.append("tool_action_service")

    task_service = getattr(request.app.state, "task_service", None)
    orchestrator = getattr(request.app.state, "codex_orchestrator", None)
    canceled_task_ids: list[str] = []
    if task_service is not None and orchestrator is not None:
        try:
            tasks = task_service.store.list_tasks(limit=1_000_000)
        except Exception:
            failures.append("task_list")
        else:
            for task in sorted(tasks, key=lambda item: item.task_id):
                status_value = getattr(task.status, "value", str(task.status))
                if status_value not in _NONTERMINAL_TASK_STATES:
                    continue
                try:
                    await _invoke(
                        orchestrator,
                        "cancel",
                        task.task_id,
                        cause="global_stop",
                        idempotency_key=f"flow-global-stop:{task.task_id}",
                    )
                except Exception:
                    failures.append(f"task:{task.task_id}")
                else:
                    canceled_task_ids.append(task.task_id)

    desktop = getattr(request.app.state, "desktop_controller", None)
    if desktop is not None:
        try:
            await _invoke(desktop, "interrupt")
        except Exception:
            failures.append("desktop_controller")
        else:
            stopped.append("desktop_controller")

    browser = getattr(request.app.state, "browser_session_service", None)
    if browser is not None:
        try:
            await _invoke(browser, "global_stop")
        except Exception:
            failures.append("browser_session_service")
        else:
            stopped.append("browser_session_service")

    return {
        "flow": status.as_dict(),
        "stopped_components": stopped,
        "canceled_task_ids": canceled_task_ids,
        "propagation_failures": failures,
    }


__all__ = ["router"]
