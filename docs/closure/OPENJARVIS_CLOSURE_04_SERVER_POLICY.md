# OPENJARVIS CLOSURE 04 — SERVER POLICY / APPROVAL / TASK FOLLOW-UP / TOOL BROWSER

## Status

Worker 4 is functionally complete within its ownership. All focused Worker-4 tests, the Worker-4 diff check, targeted Ruff check and targeted Ruff format check passed on the final functional Worker-4 code head. One explicit cross-worker dependency remains in Worker 1 Action canonicalization. No merge was performed.

- BASE_SHA: `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`
- Branch: `closure/w4-server-policy`
- Final validated functional code head before this documentation-only commit: `198a7b896fef06d2e3e299ac3cf4e075e5ab26fb`
- Final remote SHA: authoritative branch/PR head must be read after this documentation commit. A commit cannot embed its own resulting SHA without changing that SHA; post-commit branch-ref verification is authoritative.
- Draft PR: `#9`
- PR base: `integration/jarvis-operator-final`
- READY_FOR_INTEGRATION: `YES, DEPENDS_ON_W1`

## Frozen-base / coordination verification

Fresh GitHub reads were performed against:

- `coord/openjarvis-closure/docs/closure/README.md`
- `coord/openjarvis-closure/docs/closure/OPENJARVIS_CLOSURE_00_BASELINE_AND_CI_MAP.md`

The coordination baseline continues to freeze Wave A at `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`. Worker 4 owns the legacy server action gateway, confirmation wiring, task follow-up and `src/openjarvis/server/tool_browser_routes.py`; Worker 1 owns `src/openjarvis/tools/action_service.py` and underlying canonical Action semantics. Broad repository formatting remains explicitly prohibited during parallel closure work.

The final functional Worker-4 code head `198a7b896fef06d2e3e299ac3cf4e075e5ab26fb` was verified as 5 commits ahead and 0 behind the frozen base. Its diff is limited to:

- `src/openjarvis/tools/_stubs.py`
- `tests/security/test_tool_confirmation.py`
- `tests/server/test_legacy_action_gateway.py`
- `docs/closure/OPENJARVIS_CLOSURE_04_SERVER_POLICY.md`

No Worker-4 commit changes `src/openjarvis/tools/action_service.py`, `src/openjarvis/tools/manifest.py`, `src/openjarvis/server/task_routes.py`, `src/openjarvis/server/tool_browser_routes.py`, or `.github/workflows/ci.yml`.

## 1. Legacy Action Gateway

### Root cause

The legacy managed assistant gateway already routes model-reachable managed tools through the canonical `ToolActionService`: trusted manifest lookup, proposal persistence, authority/policy validation, task-scoped idempotency, execution only from a validated action and verification before success.

The regression was in the test fixture's authority model. Integrated `FlowSessionAuthority` starts in `LOCKED`. The legacy gateway test models normal read-only Assistant operation, not a locked product and not an active Flow. Constructing the service with an implicit locked authority therefore tested the wrong authoritative product state and denied the read-only assistant action before the intended gateway contract could run.

### Design decision: why Assistant mode is the correct contract

The fixture explicitly activates `ASSISTANT` and injects the same authority into `ToolActionService` and app state because:

1. the tested tool is a read-only MODEL-lane assistant tool;
2. ordinary read-only assistant operation is the Assistant-mode product state;
3. `LOCKED` intentionally means assistant tool execution is unavailable;
4. the test does not activate Flow and therefore receives no Flow-only write/sensitive authority;
5. no approval bit, manifest requirement, policy decision, idempotency guard, verifier or Action-Service path is bypassed;
6. the gateway reports success only after canonical Action validation, execution and verification.

Focused regression:

`tests/server/test_legacy_action_gateway.py::test_managed_tool_routes_through_policy_verification_and_idempotency`

Final result: **PASS — 1 passed**.

## 2. Tool Confirmation Callback

### Root cause

`ToolExecutor` performed strict trusted-manifest argument validation before invoking the confirmation callback. For a confirmation-required tool with an invalid/unknown proposed argument, schema rejection occurred before the callback could receive the descriptive proposed-action message required by the existing callback contract.

### Fix

`src/openjarvis/tools/_stubs.py` now preserves the proposed argument mapping for the confirmation message, keeps the existing availability/platform/boundary/RBAC/taint checks, invokes the confirmation callback once, and then performs strict manifest validation before any tool handler can execute.

Security properties retained:

- callback is invoked exactly once for the covered confirmation-required call;
- the callback message contains the tool identity and proposed arguments;
- denial still stops execution;
- positive confirmation is not schema authorization;
- `manifest.validate_arguments(...)` remains mandatory before handler execution;
- invalid `{"action": "delete"}` is rejected and never reaches the tool handler;
- no Action-, approval- or policy-bypass was introduced.

Final command:

```text
uv run pytest -q tests/security/test_tool_confirmation.py
```

Final result: **PASS — 7 passed**.

## 3. Task Follow-up / Memory Candidate Continuation

### Status

No Worker-4 code defect was found in `src/openjarvis/server/task_routes.py`, so no speculative route patch was made.

The existing route already gives each canonical tool follow-up a fresh orchestration correlation:

```python
turn_correlation_id=f"{correlation_id}:tool-follow-up:{step}"
```

Terminal canonical action results are projected through the existing pending/consumed result path rather than replaying the original user turn, and the memory-candidate continuation remains bound to the same canonical task context.

Final commands/results:

```text
uv run pytest -q tests/server/test_task_routes.py
```

**PASS — 24 passed**.

```text
uv run pytest -q tests/server/test_task_routes.py::test_tool_followup_uses_a_fresh_turn_correlation
```

**PASS — 1 passed**.

```text
uv run pytest -q tests/memory/test_memory_candidates.py
```

**PASS — 24 passed**.

Therefore no Task-Route source change was necessary.

## 4. Tool Browser

### Worker-4 route status

`src/openjarvis/server/tool_browser_routes.py` was inspected and intentionally left unchanged. It preserves task/correlation/idempotency identity, obtains a Flow action lease when required, calls canonical `ToolActionService.create(...)`, executes only a validated action through `ToolActionService.execute(...)`, and ends the lease. It does not synthesize, bypass or weaken Action-Service safety semantics.

Final command:

```text
uv run pytest -q tests/server/test_tool_browser_routes.py
```

Final result: **PASS — 5 passed** on Worker-4 head `198a7b896fef06d2e3e299ac3cf4e075e5ab26fb`.

This confirms no independent Worker-4 Tool-Browser route defect remains in the focused route suite.

## 5. Worker-1 dependency: Action canonicalization

Status: `DEPENDS_ON_W1_ACTION_CANONICALIZATION`

Worker 1 remains the sole owner of `src/openjarvis/tools/action_service.py`. During this closeout the Worker-1 branch advanced to:

`closure/w1-action-safety` @ `ede7a9b3c8a2e48bab804240a25aef29739c208a`

Worker 1 added reload-safe Action canonicalization coverage in `tests/tools/test_action_canonicalization.py` and changed Action-Service side-effect comparisons from enum-object identity semantics (`is` / `is not`) to value equality (`==` / `!=`).

To prove the dependency rather than merely infer it, Worker 1's new canonicalization test file was executed against the unchanged Worker-4 Action-Service core:

```text
git fetch origin closure/w1-action-safety
git show origin/closure/w1-action-safety:tests/tools/test_action_canonicalization.py > /tmp/test_action_canonicalization.py
uv run pytest -q /tmp/test_action_canonicalization.py
```

Result against Worker-4 core: **2 failed, 1 passed**.

Exact dependency failures:

1. `test_proposal_side_effect_comparison_is_reload_safe`
   - `proposal side effect differs from trusted manifest`
2. `test_execution_binding_uses_canonical_side_effect_value`
   - `action side effect differs from its proposal or manifest`

The third test, which ensures real side-effect drift is still rejected, passed.

This is the exact cross-worker defect Worker 1 owns. Worker 4 did not copy, weaken or bypass it. Integration must bring Worker 1's canonicalization change before declaring the integrated Action core closed.

## 6. Policy / Approval focused validation

Final commands/results:

```text
uv run pytest -q tests/tasks/test_policy.py
```

**PASS — 5 passed**.

```text
uv run pytest -q tests/tasks/test_approval.py
```

**PASS — 5 passed**.

```text
uv run pytest -q tests/tasks/test_tool_policy_phase5.py
```

**PASS — 5 passed**.

These runs confirm Worker-4 compatibility with the existing policy and approval contracts without adding bypasses.

## 7. Exact final focused validation environment

Because the local execution environment had no usable repository checkout/network path, focused validation was executed in GitHub Actions through an ephemeral branch `closure/w4-validation-temp`. The temporary workflow was never added to the Worker-4 product branch. It explicitly checked out `closure/w4-server-policy` and asserted the exact validated functional head:

`198a7b896fef06d2e3e299ac3cf4e075e5ab26fb`

Final focused run: GitHub Actions `31614894175`.

Setup:

```text
uv sync --extra dev --extra framework-comparison --extra server
uv run maturin develop --manifest-path rust/crates/openjarvis-python/Cargo.toml
mkdir -p "$HOME/.openjarvis"
```

The standard OpenJarvis config directory is created because server lifespan tests instantiate the default digest store at `$HOME/.openjarvis/digest.db`.

### Final commands and results

```text
git diff --check 26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e HEAD
```

**PASS**.

```text
uv run ruff check \
  src/openjarvis/tools/_stubs.py \
  tests/security/test_tool_confirmation.py \
  tests/server/test_legacy_action_gateway.py
```

**PASS — `All checks passed!`**.

```text
uv run ruff format --check \
  src/openjarvis/tools/_stubs.py \
  tests/security/test_tool_confirmation.py \
  tests/server/test_legacy_action_gateway.py
```

**PASS — `3 files already formatted`**.

```text
uv run pytest -q tests/server/test_legacy_action_gateway.py::test_managed_tool_routes_through_policy_verification_and_idempotency
```

**PASS — 1 passed**.

```text
uv run pytest -q tests/security/test_tool_confirmation.py
```

**PASS — 7 passed**.

```text
uv run pytest -q tests/server/test_task_routes.py
```

**PASS — 24 passed**.

```text
uv run pytest -q tests/server/test_task_routes.py::test_tool_followup_uses_a_fresh_turn_correlation
```

**PASS — 1 passed**.

```text
uv run pytest -q tests/memory/test_memory_candidates.py
```

**PASS — 24 passed**.

```text
uv run pytest -q tests/tasks/test_policy.py
```

**PASS — 5 passed**.

```text
uv run pytest -q tests/tasks/test_approval.py
```

**PASS — 5 passed**.

```text
uv run pytest -q tests/tasks/test_tool_policy_phase5.py
```

**PASS — 5 passed**.

```text
uv run pytest -q tests/server/test_tool_browser_routes.py
```

**PASS — 5 passed**.

## 8. Formal-closeout findings by ownership

### A) Worker-4-owned findings

No functional Worker-4 failure remains.

One formal-closeout issue was found: targeted `ruff format --check` initially reported two Worker-4 Python files requiring formatting. Only those Worker-4 files were formatted; no repository-wide formatter was run. Formatting-only commit:

`198a7b896fef06d2e3e299ac3cf4e075e5ab26fb`

After that commit both targeted Ruff checks and `git diff --check` passed.

### B) Worker-1-only Action canonicalization

Worker 1's reload-canonicalization probe produces exactly two failures against the Worker-4/frozen Action core and passes the real-drift rejection case. The remaining dependency is therefore specifically and reproducibly Worker 1 Action canonicalization, not a second Worker-4 route cause.

### C) Baseline / environment / foreign-worker failures

Before the standard `$HOME/.openjarvis` runtime directory was created, the full Task Routes suite on Worker 4 produced:

- 21 passed
- 3 failed

The three failures were:

- `test_owned_task_runtime_and_trace_store_close_on_server_shutdown`
- `test_server_startup_recovers_incomplete_tool_actions_before_serving`
- `test_lifespan_shutdown_is_idempotent_after_partial_cleanup_failure`

All failed while opening the default digest database at `/home/runner/.openjarvis/digest.db` with:

`sqlite3.OperationalError: unable to open database file`

Those exact three tests were then executed on the untouched Frozen Base `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e` with the same runner condition:

```text
uv run pytest -q \
  tests/server/test_task_routes.py::test_owned_task_runtime_and_trace_store_close_on_server_shutdown \
  tests/server/test_task_routes.py::test_server_startup_recovers_incomplete_tool_actions_before_serving \
  tests/server/test_task_routes.py::test_lifespan_shutdown_is_idempotent_after_partial_cleanup_failure
```

Frozen Base result: **3 failed with the same SQLite/default-directory cause**.

Therefore these were not introduced by Worker 4. With the standard runtime directory present, the final Worker-4 full Task Routes suite passed **24/24**.

Repository-wide Ruff format debt also remains a known frozen-baseline/global integration concern. Worker 4 did not perform broad formatting.

A globally green Python suite is not claimed here and is not required for Worker-4 readiness; this handoff claims only the actually executed focused Worker-4 results above.

## 9. Cross-worker integration requirements

During integration:

1. Integrate Worker 1's Action canonicalization before final Action-core closure.
2. Preserve Worker 4's canonical `service.create(...)` / `service.execute(...)` server paths; do not replace them with direct runtime execution.
3. Preserve task/correlation/idempotency binding and Flow action leases in `tool_browser_routes.py`.
4. Preserve strict manifest execution validation in `ToolExecutor`; confirmation is not schema authorization.
5. Do not reintroduce enum-object identity comparison for persisted/reloaded side-effect values.
6. Re-run the focused Worker-4 suites after combining Worker 1 and Worker 4 in the integration tree.
7. Handle broad Ruff formatting only at the coordinated integration/global-format phase.

## 10. Changed files

Functional / targeted-format Worker-4 files:

- `src/openjarvis/tools/_stubs.py`
- `tests/security/test_tool_confirmation.py`
- `tests/server/test_legacy_action_gateway.py`

Handoff:

- `docs/closure/OPENJARVIS_CLOSURE_04_SERVER_POLICY.md`

Intentionally unchanged in Worker 4:

- `src/openjarvis/server/task_routes.py`
- `src/openjarvis/server/tool_browser_routes.py`
- `src/openjarvis/tools/action_service.py`
- `src/openjarvis/tools/manifest.py`
- `.github/workflows/ci.yml`

## READY_FOR_INTEGRATION

`READY_FOR_INTEGRATION: YES, DEPENDS_ON_W1`

Worker-4-owned functional tests are green, targeted Ruff and format checks are green, and `git diff --check` is green on the final functional code head. The remaining Action canonicalization defect is reproducibly isolated to Worker 1 ownership and is not bypassed by Worker 4.