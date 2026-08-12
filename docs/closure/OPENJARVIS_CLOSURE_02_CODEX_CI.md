# OPENJARVIS CLOSURE 02 — CODEX SDK / CI DEPENDENCY CONTRACT

## Assignment

Worker 2 — Codex SDK / CI Dependency Contract

## Authoritative base and branch

- BASE_SHA: `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`
- Branch: `closure/w2-codex-ci`
- Pre-handoff functional remote head: `bd362f483bdbf23f31473ba63d47efc76be1dbd7`
- Draft PR: `#8`
- Required PR base: `integration/jarvis-operator-final`
- Final remote head: the branch tip containing this handoff is verified immediately after the handoff commit and recorded in PR #8 / the final Worker-2 report. A Git commit cannot embed its own SHA in its own contents without changing that SHA; no self-referential SHA is fabricated here.

## Root cause

Closure 00 identified 15 Codex collection errors in the normal Linux test lane. The product metadata already modeled Codex as an optional extra:

```toml
codex = ["openai-codex==0.144.4"]
```

However, the Linux CI job collected the full `tests/` tree while its dependency-install step omitted the `codex` extra. The result was a CI contract mismatch: optional for normal end-user installation, but required by the repository's full CI test contract when Codex tests are collected.

This was a dependency/CI-contract defect, not a reason to weaken or skip Codex tests.

## Design decision

Codex remains an **optional product extra**. It is not promoted into the base runtime dependencies and is not made mandatory for end users.

The full Linux CI lane intentionally tests optional Codex support, so that lane must explicitly install the optional extra before collecting the complete repository test suite.

Decision:

- End-user/default install: Codex remains optional.
- Full Linux repository CI: explicitly install `--extra codex`.
- Lock resolution: enforce `--frozen` so CI cannot silently rewrite or re-resolve the lock contract.
- Codex assertions: unchanged; no skip, xfail, or weakened assertion was introduced.

## Changed files

Functional change:

- `.github/workflows/ci.yml`

Closure evidence:

- `docs/closure/OPENJARVIS_CLOSURE_02_CODEX_CI.md`

Explicitly unchanged by Worker 2:

- `pyproject.toml`
- `uv.lock`
- `tests/codex/**`
- Windows test-selection/platform ownership
- Action/operator implementation
- RLM/MCP implementation
- Speech, migration, Git-security, and other workers' functional areas

## Exact CI/install change

The Linux `test` job changed from:

```yaml
run: uv sync --extra dev --extra framework-comparison --extra server
```

to:

```yaml
run: uv sync --frozen --extra dev --extra framework-comparison --extra server --extra codex
```

No Codex extra was added to the default product dependency set. No Codex extra was added to the lint job or Windows jobs, because those jobs do not collect the complete Codex test contract.

The branch-vs-frozen-base comparison before this handoff was exactly one modified workflow line: one addition / one deletion, with no foreign worker fixes.

## pyproject.toml / uv.lock contract

`pyproject.toml` continues to define:

```toml
codex = ["openai-codex==0.144.4"]
```

The dependency remains optional.

`uv.lock` was not modified by Worker 2. The existing lock already contains the Codex SDK resolution required by that extra. The fresh GitHub Actions step

```text
uv sync --frozen --extra dev --extra framework-comparison --extra server --extra codex
```

completed successfully. Because `--frozen` forbids lock mutation/re-resolution, that successful install is direct CI evidence that the checked-in lock is consistent with the requested Codex extra.

## Validation commands / CI contract

Dependency install:

```bash
uv sync --frozen --extra dev --extra framework-comparison --extra server --extra codex
```

PyO3 build:

```bash
uv run maturin develop --manifest-path rust/crates/openjarvis-python/Cargo.toml
```

Full Linux repository test command:

```bash
COVERAGE_CORE=sysmon uv run pytest tests/ -n auto -q --tb=short -m "not live and not cloud and not hub" \
  --cov=openjarvis \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-fail-under=60
```

Lint contract observed in CI:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

Rust CI:

```bash
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## Final CI evidence for functional head `bd362f483bdbf23f31473ba63d47efc76be1dbd7`

Central CI run: `31606373175`.

### Linux `test`

- Install Dependencies: **SUCCESS**
- Build Rust Extension / PyO3: **SUCCESS**
- Run tests: **FAILURE**, caused by pre-existing/non-Worker-2 functional failure groups listed below
- Coverage upload: **SUCCESS**
- Coverage threshold: **PASS**, 65.98% against required 60%

Final pytest summary:

```text
33 failed, 8232 passed, 61 skipped, 3 warnings in 142.81s (0:02:22)
```

There are **zero Codex collection errors** in this run. None of the 33 failed test IDs is under `tests/codex/`.

### Codex test status

The full Linux command collects `tests/`, including the complete `tests/codex/` suite. The prior 15 `ModuleNotFoundError`/missing-SDK collection failures are gone after installing the extra.

Result for Worker-2's contract:

- Codex dependency install: **PASS**
- Codex test collection: **PASS**
- Codex test failures in final Linux summary: **0**
- Codex collection errors in final Linux summary: **0**
- Codex assertions modified/weakened: **NO**
- Lifecycle/runtime semantics retained by the unchanged Codex tests, including thread start, resume, fork, turn streaming, steer, interrupt, checkpoint, step limits, token limits, usage accounting, credential redaction, sandbox mapping, and durable claim semantics.

### Windows jobs

- `test-windows (3.12)`: **SUCCESS**
  - dependency install: success
  - Windows RAM detection: success
  - Windows-specific hardware/CLI tests: success
  - PyO3 build/import: success
  - CLI smoke test: success
- `test-windows (3.13)`: **SUCCESS**
  - dependency install: success
  - Windows RAM detection: success
  - Windows-specific hardware/CLI tests: success
  - PyO3 build/import: success
  - CLI smoke test: success

Worker 2 did not modify Worker-5-owned Windows test-selection semantics.

### Rust

Rust job: **SUCCESS**

- Clippy: success
- Rust workspace tests: success

### Lint

Lint job: **FAILURE**, but not from Worker-2's functional CI line.

- `ruff check src/ tests/`: **SUCCESS**
- `ruff format --check src/ tests/`: **FAILURE** because the repository has broad pre-existing formatting drift outside Worker-2 ownership.
- No repository-wide Ruff formatting was performed, per closure isolation rules.

## Concrete remaining Linux failures

The 33 remaining failures from the final Linux job are:

### RLM / MCP discovery bridge — other worker ownership

1. `tests/agents/test_rlm.py::TestRLMDirectToolBridge::test_root_repl_can_use_bounded_read_helper`
2. `tests/agents/test_rlm.py::TestRLMDirectToolBridge::test_root_repl_can_use_file_chunk_helper`
3. `tests/mcp/test_action_bridge.py::test_live_policy_change_replaces_policy_but_not_schema`
4. `tests/server/test_mcp_tools_cache.py::test_returns_tools_from_mcp_server`
5. `tests/server/test_mcp_tools_cache.py::test_caches_successful_discovery`
6. `tests/server/test_mcp_tools_cache.py::test_does_not_cache_empty_results`

The first two and MCP discovery/cache failures belong to the Worker-3 RLM/MCP closure area. Worker 2 does not absorb them.

### Linux collecting Windows-only final-launcher tests — Worker 5 ownership

7. `tests/final/test_windows_final_launcher_ownership.py::test_runtime_process_ownership_is_fail_closed[launched-process-is-listener]`
8. `tests/final/test_windows_final_launcher_ownership.py::test_runtime_process_ownership_is_fail_closed[direct-child-is-listener]`
9. `tests/final/test_windows_final_launcher_ownership.py::test_runtime_process_ownership_is_fail_closed[indirect-child-is-listener]`
10. `tests/final/test_windows_final_launcher_ownership.py::test_runtime_process_ownership_is_fail_closed[listener-health-mismatch-fails-closed]`
11. `tests/final/test_windows_final_launcher_ownership.py::test_runtime_process_ownership_is_fail_closed[foreign-listener-fails-closed]`
12. `tests/final/test_windows_final_launcher_ownership.py::test_runtime_process_ownership_is_fail_closed[missing-health-pid-fails-closed]`
13. `tests/final/test_windows_final_launcher_ownership.py::test_runtime_process_ownership_is_fail_closed[nonnumeric-health-pid-fails-closed]`

All fail on Linux because `powershell.exe` is unavailable. Worker 5 owns the functional/platform classification. If a CI selection change is required, Worker 5 must specify it in handoff and Worker 2 can implement only the `.github/workflows/ci.yml` portion later under the workflow ownership rule.

### Action/operator/manifest/security — Worker 1 area or adjacent closure ownership

14. `tests/security/test_tool_confirmation.py::TestToolConfirmation::test_confirmation_callback_receives_message`
15. `tests/server/test_legacy_action_gateway.py::test_managed_tool_routes_through_policy_verification_and_idempotency`
16. `tests/operators/test_operators.py::TestOperativeAgent::test_run_tool_loop`
17. `tests/tools/test_manifest.py::test_manifest_rejects_level_four_execution`
18. `tests/tools/test_operator_action_service.py::test_parallel_services_claim_same_action_once`
19. `tests/tools/test_operator_action_service.py::test_crash_after_effect_requires_recovery_without_reexecution`
20. `tests/tools/test_operator_action_service.py::test_startup_recovery_marks_every_inflight_effect_without_reexecution`
21. `tests/tools/test_operator_action_service.py::test_global_stop_cancels_inflight_action_and_blocks_late_revival`
22. `tests/tools/test_operator_action_service.py::test_failed_external_postcondition_is_recovery_not_success`
23. `tests/tools/test_operator_action_service.py::test_direct_execute_cannot_bypass_retry_policy`
24. `tests/tools/test_operator_action_service.py::test_persisted_risk_floor_and_manifest_snapshot_cannot_drop`
25. `tests/tools/test_operator_action_service.py::test_tool_output_cannot_lower_policy_and_secrets_are_redacted`

Worker 2 does not change these semantics or assertions.

### Other independent closure failure groups

26. `tests/migration/test_backup.py::test_long_target_path_preflight_runs_before_copy_and_removes_destination`
27. `tests/speech/test_local_voice.py::test_chatterbox_timeout_restarts_with_bounded_piper_fallback`
28. `tests/speech/test_local_voice.py::test_piper_timeout_does_not_start_an_unbounded_retry`
29. `tests/speech/test_local_voice.py::test_auditions_must_match_the_current_voice_schema`
30. `tests/tools/test_git_secure.py::test_upstream_push_is_disabled`
31. `tests/tools/test_git_secure.py::test_commit_in_integration_repo_is_blocked`
32. `tests/tools/test_git_secure.py::test_worktree_requires_clean_integration_repo`
33. `tests/tools/test_git_secure.py::test_non_task_branch_and_foreign_worktree_are_blocked`

These are outside Worker-2's Codex dependency/CI contract and were not modified.

## Other workflow status for the same functional head

Fresh checks on `bd362f483bdbf23f31473ba63d47efc76be1dbd7` also showed:

- Build Artifacts: **SUCCESS**
- Build Docker: **SUCCESS**
- SDK Corpus Guard: **SUCCESS**
- Claude Code Review: **SKIPPED**

The central CI workflow remains red only because the Linux full suite still has the 33 non-Codex failures and lint has the repository-wide format-check failure described above.

## Cross-worker dependencies

- **Worker 1:** Action/operator/manifest safety failures remain in the full Linux lane. No Worker-2 changes were made there.
- **Worker 3:** RLM Direct Tool Bridge and MCP discovery/cache failures remain. No Worker-2 changes were made there.
- **Worker 5:** Linux wrongly collecting Windows-only launcher tests remains functionally Worker 5. Worker 2 is sole `.github/workflows/ci.yml` owner and may apply a later CI selection adjustment only from Worker 5's explicit handoff.
- Other migration, speech, and Git-security failures remain outside this assignment and must be resolved by their respective closure owners/integrator.

## Scope / diff safety

Before adding this handoff, comparing `closure/w2-codex-ci` against the frozen base and against `integration/jarvis-operator-final` showed:

- status: ahead
- ahead by: 1
- behind by: 0
- only functional file changed: `.github/workflows/ci.yml`
- workflow diff: exactly one replacement line

No `pyproject.toml`, `uv.lock`, Codex-test, Windows-test, or unrelated source fix is present in Worker-2's functional commit.

## READY_FOR_INTEGRATION

**YES for Worker-2's Codex SDK / CI dependency contract**, provided the final post-push verification confirms that:

1. the remote branch tip contains this handoff,
2. PR #8 targets `integration/jarvis-operator-final`,
3. PR #8 remains Draft,
4. the final PR diff contains only the Worker-2 workflow change plus this handoff document,
5. no merge is performed.

The repository as a whole is not green yet; the remaining 33 Linux failures and the global Ruff-format failure are cross-worker/integration blockers, not Codex dependency failures.