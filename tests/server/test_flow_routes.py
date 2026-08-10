from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.flow import (
    FlowActivationChallenge,
    FlowSessionAuthority,
    OwnerVerificationResult,
    OwnerVerificationStatus,
    RuntimeBinding,
)
from openjarvis.server.app import create_app
from openjarvis.server.flow_routes import router
from openjarvis.tasks.types import TaskStatus

SECRET = "a" * 64
TASK = "owner-requested-task"
NOW = 1_800_000_000
NATIVE_HEADERS = {"X-OpenJarvis-Native-Bridge": "tauri"}


class FakeVerifier:
    def __init__(
        self,
        status: OwnerVerificationStatus = OwnerVerificationStatus.VERIFIED,
    ) -> None:
        self.status = status
        self.calls = 0

    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        assert "owner" in prompt.casefold()
        self.calls += 1
        return OwnerVerificationResult(self.status)


class MutableBindingProvider:
    def __init__(self) -> None:
        self.binding = RuntimeBinding("trusted-owner", "session-7", 4242)

    def current(self) -> RuntimeBinding:
        return self.binding


def _authority(
    *,
    binding: MutableBindingProvider | None = None,
    verifier: FakeVerifier | None = None,
    clock=lambda: NOW,
) -> FlowSessionAuthority:
    return FlowSessionAuthority(
        SECRET,
        clock=clock,
        owner_verifier=verifier or FakeVerifier(),
        binding_provider=binding or MutableBindingProvider(),
    )


def _app(authority: FlowSessionAuthority) -> FastAPI:
    app = FastAPI()
    app.state.flow_authority = authority
    app.include_router(router)
    return app


def _signed_assertion(
    client: TestClient,
    *,
    task_context: str = TASK,
    secret: str = SECRET,
) -> dict[str, object]:
    challenge_response = client.post(
        "/v1/flow/challenge",
        json={"task_context": task_context},
        headers=NATIVE_HEADERS,
    )
    assert challenge_response.status_code == 200
    challenge_data = challenge_response.json()
    challenge = FlowActivationChallenge(**challenge_data)
    nonce = "native-proof-nonce"
    authenticated_at = challenge.issued_at
    signature = hmac.new(
        secret.encode(),
        challenge.assertion_message(
            nonce=nonce,
            authenticated_at=authenticated_at,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        "challenge": challenge_data,
        "nonce": nonce,
        "authenticated_at": authenticated_at,
        "owner_verification": "verified",
        "signature": signature,
    }


def _activate(client: TestClient, *, task_context: str = TASK) -> dict:
    response = client.post(
        "/v1/flow/activate",
        json=_signed_assertion(client, task_context=task_context),
        headers=NATIVE_HEADERS,
    )
    assert response.status_code == 200
    return response.json()


def test_native_challenge_binds_trusted_runtime_and_owner_verification() -> None:
    binding = MutableBindingProvider()
    verifier = FakeVerifier()
    app = _app(_authority(binding=binding, verifier=verifier))

    with TestClient(app) as client:
        assertion = _signed_assertion(client)
        challenge = assertion["challenge"]
        assert challenge["owner"] == binding.binding.user_id
        assert challenge["os_session_id"] == binding.binding.os_session_id
        assert challenge["process_id"] == binding.binding.process_id
        assert challenge["task_context_digest"] == hashlib.sha256(TASK.encode()).hexdigest()

        response = client.post(
            "/v1/flow/activate",
            json=assertion,
            headers=NATIVE_HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["mode"] == "flow"
        assert response.json()["owner_authenticated"] is True
        assert verifier.calls == 1


def test_browser_and_legacy_flat_assertion_cannot_activate_flow() -> None:
    authority = _authority()
    app = _app(authority)

    with TestClient(app) as client:
        assert (
            client.post("/v1/flow/challenge", json={"task_context": TASK}).status_code
            == 403
        )
        legacy = {
            "nonce": "native-proof-nonce",
            "authenticated_at": NOW,
            "signature": "0" * 64,
            "owner": "request-controlled-owner",
        }
        assert (
            client.post(
                "/v1/flow/activate",
                json=legacy,
                headers=NATIVE_HEADERS,
            ).status_code
            == 422
        )
        assert authority.status().mode.value == "locked"


def test_wrong_stale_and_runtime_mismatched_assertions_fail_closed() -> None:
    clock = [NOW]
    binding = MutableBindingProvider()
    authority = _authority(binding=binding, clock=lambda: clock[0])
    app = _app(authority)

    with TestClient(app) as client:
        wrong = _signed_assertion(client, secret="b" * 64)
        assert (
            client.post(
                "/v1/flow/activate",
                json=wrong,
                headers=NATIVE_HEADERS,
            ).status_code
            == 403
        )

        authority.rotate_bridge_secret(SECRET)
        stale = _signed_assertion(client)
        clock[0] += 61
        assert (
            client.post(
                "/v1/flow/activate",
                json=stale,
                headers=NATIVE_HEADERS,
            ).status_code
            == 403
        )

        authority.rotate_bridge_secret(SECRET)
        mismatched = _signed_assertion(client)
        binding.binding = RuntimeBinding("trusted-owner", "session-8", 4242)
        assert (
            client.post(
                "/v1/flow/activate",
                json=mismatched,
                headers=NATIVE_HEADERS,
            ).status_code
            == 403
        )
        assert authority.status().mode.value == "locked"


def test_restart_never_accepts_an_assertion_from_the_previous_authority() -> None:
    app = _app(_authority())

    with TestClient(app) as client:
        assertion = _signed_assertion(client)
        app.state.flow_authority = _authority()
        response = client.post(
            "/v1/flow/activate",
            json=assertion,
            headers=NATIVE_HEADERS,
        )

        assert response.status_code == 403
        assert app.state.flow_authority.status().mode.value == "locked"


def test_activity_and_route_revoke_preserve_task_and_lease_binding() -> None:
    authority = _authority()
    app = _app(authority)

    with TestClient(app) as client:
        status = _activate(client)
        lease = authority.begin_action(
            session_id=status["session_id"],
            task_context=TASK,
        )
        assert authority.validate_action(lease)

        wrong_task = client.post(
            "/v1/flow/activity",
            json={"session_id": status["session_id"], "task_context": "other-task"},
        )
        assert wrong_task.status_code == 403
        activity = client.post(
            "/v1/flow/activity",
            json={"session_id": status["session_id"], "task_context": TASK},
        )
        assert activity.status_code == 200

        assert client.post("/v1/flow/lock").json()["mode"] == "locked"
        assert not authority.validate_action(lease)


def test_global_stop_revokes_first_and_propagates_in_stable_order() -> None:
    authority = _authority()
    app = _app(authority)
    calls: list[str] = []

    class StopAwareService:
        def __init__(self, label: str, method: str) -> None:
            setattr(self, method, self.stop)
            self.label = label

        def stop(self) -> None:
            assert authority.status().lock_reason == "global_stop"
            calls.append(self.label)

    class Store:
        def list_tasks(self, *, limit: int):
            assert limit >= 3
            return [
                SimpleNamespace(task_id="task-b", status=TaskStatus.RUNNING),
                SimpleNamespace(task_id="task-done", status=TaskStatus.DONE),
                SimpleNamespace(task_id="task-a", status=TaskStatus.PAUSED),
            ]

    class Orchestrator:
        async def cancel(self, task_id: str, *, cause: str, idempotency_key: str) -> None:
            assert authority.status().lock_reason == "global_stop"
            assert cause == "global_stop"
            assert idempotency_key == f"flow-global-stop:{task_id}"
            calls.append(f"task:{task_id}")

    app.state.tool_action_service = StopAwareService("tools", "global_stop")
    app.state.task_service = SimpleNamespace(store=Store())
    app.state.codex_orchestrator = Orchestrator()
    app.state.desktop_controller = StopAwareService("desktop", "interrupt")
    app.state.browser_session_service = StopAwareService("browser", "global_stop")

    with TestClient(app) as client:
        status = _activate(client)
        lease = authority.begin_action(
            session_id=status["session_id"],
            task_context=TASK,
        )
        response = client.post(
            "/v1/flow/global-stop",
            headers=NATIVE_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["flow"]["lock_reason"] == "global_stop"
    assert response.json()["canceled_task_ids"] == ["task-a", "task-b"]
    assert response.json()["propagation_failures"] == []
    assert calls == ["tools", "task:task-a", "task:task-b", "desktop", "browser"]
    assert not authority.validate_action(lease)

    with TestClient(app) as client:
        assert client.post("/v1/flow/assistant").status_code == 403
        assert client.post("/v1/flow/lock?reason=late_lock").json()["lock_reason"] == "global_stop"


def test_global_stop_is_native_only_and_does_not_leak_component_errors() -> None:
    authority = _authority()
    app = _app(authority)

    class FailingActions:
        def global_stop(self) -> None:
            raise RuntimeError("sensitive runtime detail")

    app.state.tool_action_service = FailingActions()
    with TestClient(app) as client:
        assert client.post("/v1/flow/global-stop").status_code == 403
        response = client.post(
            "/v1/flow/global-stop",
            headers=NATIVE_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["flow"]["lock_reason"] == "global_stop"
    assert response.json()["propagation_failures"] == ["tool_action_service"]
    assert "sensitive runtime detail" not in response.text


def test_approval_queue_is_not_mounted_in_the_application() -> None:
    app = _app(_authority())
    with TestClient(app) as client:
        assert client.get("/v1/approvals/pending").status_code == 404


def test_full_application_does_not_mount_legacy_approval_workflows() -> None:
    engine = MagicMock()
    engine.engine_id = "flow-test"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]
    app = create_app(
        engine,
        "test-model",
        flow_authority=_authority(),
    )

    paths = app.openapi()["paths"]
    assert "/v1/approvals/pending" not in paths
    assert "/v1/website-staging/demo" not in paths
    assert "/v1/learning/health" not in paths
