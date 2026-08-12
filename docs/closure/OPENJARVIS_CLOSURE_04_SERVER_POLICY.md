# OPENJARVIS CLOSURE 04 — SERVER POLICY / APPROVAL / TASK FOLLOW-UP / TOOL BROWSER

## Status

Worker 4 is functionally complete within its ownership, with one explicit cross-worker dependency and one validation-observability limitation documented below. No merge was performed.

- BASE_SHA: `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`
- Branch: `closure/w4-server-policy`
- Pre-handoff validated code head: `be95038b1dd780b1d6a92c10c8a2e9c0aceea423`
- Final remote SHA: authoritative branch/PR head must be read after this handoff commit. A Git commit cannot embed the SHA of the commit that contains that text without changing the commit SHA itself; the post-commit branch-ref verification is therefore the authoritative final SHA.
- Validation Draft PR: `#9`

## Frozen-base / coordination verification

Fresh GitHub reads were performed against `coord/openjarvis-closure` before closure. The coordination baseline still freezes Wave A at `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e` and assigns Worker 4 the legacy server action gateway, confirmation wiring, task follow-up and `tool_browser_routes.py`, while Worker 1 owns `src/openjarvis/tools/action_service.py` and the underlying canonical Action semantics.

The Worker-4 branch was verified as exactly 3 commits ahead and 0 behind the frozen base before this handoff commit.

## 1. Legacy Action Gateway

### Root cause

The legacy managed assistant gateway already routes model-reachable managed tools through the canonical `ToolActionService`: proposal creation, trusted manifest lookup, policy/authority validation, idempotency, execution only from a validated action, and verification before success.

The regression was in the test fixture's authority model. The integrated `FlowSessionAuthority` defaults to `LOCKED`. The legacy gateway test represents the ordinary read-only assistant product path, not a locked product and not an active Flow. Constructing the service with an implicit locked authority therefore tested the wrong authoritative product state and denied a read-only assistant action before the gateway contract could be exercised.

### Design decision

The fixture now explicitly calls `FlowSessionAuthority.activate_assistant()` and passes that authority into `ToolActionService` and app state.

Why `ASSISTANT` is the correct contract:

1. The tested tool is a read-only MODEL-lane assistant tool.
2. Ordinary assistant operation is authorized by Assistant mode; `LOCKED` means no assistant tool execution at all.
3. The test does not activate Flow and therefore does not grant Flow-only write/sensitive authority.
4. No approval bit, policy decision, manifest requirement, verifier or idempotency guard is bypassed.
5. The gateway still succeeds only after canonical Action Service validation, execution and verification.

Changed test: `tests/server/test_legacy_action_gateway.py::test_managed_tool_routes_through_policy_verification_and_idempotency`.

## 2. Tool Confirmation Callback

### Root cause

`ToolExecutor` performed strict trusted-manifest argument validation before invoking a confirmation callback. For a confirmation-required tool with a proposed unknown argument, the call failed schema validation before the callback could receive the descriptive proposed action message, violating the existing confirmation-callback contract.

### Fix

`src/openjarvis/tools/_stubs.py` now preserves the proposed argument mapping for the confirmation message, performs the existing availability/platform/boundary/RBAC/taint checks, invokes the confirmation callback once, and then performs strict manifest validation before any tool handler can execute.

Security properties retained:

- callback is invoked exactly once for the covered confirmation-required call;
- message contains the tool identity and proposed arguments;
- denial still stops execution;
- positive confirmation is not schema approval;
- strict `manifest.validate_arguments(...)` remains mandatory before execution;
- the invalid `{"action": "delete"}` proposal is rejected and the tool handler is not executed;
- no Action/approval/policy bypass was added.

Updated test: `tests/security/test_tool_confirmation.py::TestToolConfirmation::test_confirmation_callback_receives_message` now requires one callback invocation, tool/argument text in the message, and a fail-closed `Manifest validation failed` result.

## 3. Task Follow-up / Memory Candidate Continuation

### Root cause / status

No Worker-4 code defect was found in the frozen implementation, so no speculative patch was made.

`src/openjarvis/server/task_routes.py` already creates a fresh orchestration correlation for each canonical tool follow-up:

`turn_correlation_id=f"{correlation_id}:tool-follow-up:{step}"`

The route also consumes terminal canonical tool results through the existing pending/consumed action-result projection instead of replaying the original user turn. The existing memory-candidate path remains connected to the same canonical task context.

Required contract test present and executed as part of the GitHub Python full-suite run:

- `tests/server/test_task_routes.py::test_tool_followup_uses_a_fresh_turn_correlation`

Relevant continuation coverage included by the same full-suite run:

- `tests/server/test_task_routes.py`
- `tests/memory/test_memory_candidates.py`

No source change to `task_routes.py` was made because the inspected contract already matches the required fresh-turn/no-double-turn design.

## 4. Tool Browser

### Route status

`src/openjarvis/server/tool_browser_routes.py` was inspected and intentionally left unchanged. The route verifies task/correlation/idempotency identity, obtains a Flow action lease when appropriate, calls `ToolActionService.create(...)`, executes only a `validated` action through `ToolActionService.execute(...)`, and always ends the lease. It does not synthesize or weaken Action Service safety semantics.

### Current dependency

`BLOCKED_ON_WORKER_1_ACTION_CANONICALIZATION`

The known failure class is:

`action side effect differs from its proposal or manifest`

The affected Tool Browser route coverage includes:

- `tests/server/test_tool_browser_routes.py::test_action_creation_is_idempotent_and_schema_validated`
- `tests/server/test_tool_browser_routes.py::test_sensitive_action_executes_directly_in_flow`

The failure originates in the canonical Action execution binding check owned by Worker 1 (`src/openjarvis/tools/action_service.py`). Worker 4 did not copy, relax or replace that logic.

At handoff time `closure/w1-action-safety` was freshly checked and still pointed at the frozen base `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`; no Worker-1 handoff/fix was yet available to rebase or semantically revalidate against.

## 5. Cross-worker compatibility requirements

Worker 1 remains the sole owner of Action Service canonicalization. During integration:

1. Apply/merge Worker 1's Action Service fix before interpreting Tool Browser failures as Worker-4 route defects.
2. Do not replace Worker 4's canonical `service.create(...)` / `service.execute(...)` call path with direct runtime execution.
3. Preserve task/correlation/idempotency binding and Flow action leases in `tool_browser_routes.py`.
4. Preserve strict manifest execution validation in `ToolExecutor`; confirmation must not become schema authorization.
5. Re-run the focused Worker-4 suites after Worker 1 lands.

## 6. Changed files

Functional Worker-4 diff from frozen base before this handoff:

- `src/openjarvis/tools/_stubs.py`
- `tests/security/test_tool_confirmation.py`
- `tests/server/test_legacy_action_gateway.py`

Handoff:

- `docs/closure/OPENJARVIS_CLOSURE_04_SERVER_POLICY.md`

Intentionally unchanged after inspection:

- `src/openjarvis/server/task_routes.py`
- `src/openjarvis/server/tool_browser_routes.py`
- `src/openjarvis/tools/action_service.py`
- `src/openjarvis/tools/manifest.py`
- `.github/workflows/ci.yml`

## 7. Validation

### Actual GitHub execution

Because the available local execution environment could not resolve GitHub and did not contain the repository checkout, a Draft PR was temporarily targeted at `main` solely to trigger the repository's existing CI on exact Worker-4 head `be95038b1dd780b1d6a92c10c8a2e9c0aceea423`. No workflow file was modified and no merge was performed.

GitHub Actions run `31608855248` executed the repository Python suite on that exact head.

Observed results:

- `Ruff check` (`src/ tests/`): **PASS**
- `Ruff format check` (`src/ tests/`): **FAIL**, known baseline-wide formatter debt. Baseline already reports 834 files would be reformatted; coordination explicitly defers broad normalization.
- Python full test job: **FAIL**
- Windows Python 3.12 lane: **PASS**
- Windows Python 3.13 lane: **PASS**
- Rust job: **PASS**
- Frontend CI: **PASS**
- Deploy Documentation: **PASS**
- Installer integration: **PASS**
- Desktop Build & Release validation: **PASS**

The full Python test command includes all of the following requested suites/tests:

- `tests/server/test_legacy_action_gateway.py`
- `tests/security/test_tool_confirmation.py`
- `tests/server/test_task_routes.py`
- `tests/server/test_task_routes.py::test_tool_followup_uses_a_fresh_turn_correlation`
- `tests/memory/test_memory_candidates.py`
- `tests/server/test_tool_browser_routes.py`
- `tests/tools/test_action_service.py`

### Per-test observability limitation

The connected GitHub interface exposes workflow/job/step conclusions but does not expose the underlying Pytest log text for this run; the job log endpoint returns no text through the connector. Therefore this handoff does **not** falsely label individual tests as PASS when the aggregate Python job is red. The tests above were definitely executed by the full-suite job, but their individual result lines cannot be independently extracted in this environment.

This is especially important for the Tool Browser dependency: the known canonicalization error is documented above, but the current run cannot be decomposed into exact per-test failure lines through the available connector.

### Local-only checks requested by coordination

- focused `pytest` invocations: **NOT AVAILABLE LOCALLY** because no repository checkout can be obtained in the local runner; covered by the GitHub full-suite execution instead, but without per-test log extraction.
- Ruff check own files: covered by global CI `Ruff check` PASS.
- Ruff format check own files: **NOT SEPARATELY OBSERVABLE**; global format check remains baseline-red.
- `git diff --check`: **NOT RUN LOCALLY** because the local environment has no checkout and GitHub network resolution is unavailable.

These limitations are recorded rather than hidden.

## 8. Remaining failures / blockers

1. `BLOCKED_ON_WORKER_1_ACTION_CANONICALIZATION` for Tool Browser Action binding, known error `action side effect differs from its proposal or manifest`.
2. Repository-wide Ruff format gate remains red from frozen baseline and is explicitly deferred by closure coordination.
3. Aggregate Python full suite remains red; connected Actions metadata does not provide the Pytest log lines needed to prove that every independent Worker-4 focused test is green after the fixes.

No second Worker-4 route root cause was found by semantic inspection, but condition (3) prevents claiming the stronger integration-ready state required by the closure contract.

## READY_FOR_INTEGRATION

`READY_FOR_INTEGRATION: NO`

Reason: Worker-4 functional changes are scoped and safety-preserving, but the current tool access cannot independently prove all Worker-4 focused tests green, and Worker 1's canonicalization fix has not landed. Once Worker 1 lands, rerun the focused Worker-4 suites plus own-file Ruff format and `git diff --check`; if those are green and Tool Browser failures disappear (or only the documented W1 dependency remains during a staged integration), this handoff can be promoted to `YES, DEPENDS_ON_W1` / `YES` as appropriate.
