# OpenJarvis Closure 05 — Platform / Voice / Migration / Isolation

## Status

- Frozen base / implementation SHA: `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`
- Branch: `closure/w5-platform-voice-migration`
- Target: `integration/jarvis-operator-final`
- Handoff-only remote SHA before this documentation correction: `f755a3f681d27946a67f864d83e3400b77ab93c6`
- Product-code delta from frozen base: none; required code/test writes were blocked before publication
- `.github/workflows/ci.yml`: unchanged; owned by Worker 2 / final integrator
- Draft PR: not opened; PR creation was blocked by the connected GitHub write-safety layer
- `READY_FOR_INTEGRATION: NO`

This handoff records the completed K3 diagnosis and the exact remaining changes. The connected GitHub write boundary blocked publication of the required W5 code/security-test changes, including a migration-only tree created directly from the frozen base. No security control was weakened or bypassed, no force update was attempted, and no merge was performed. The only remote W5 delta is this documentation handoff.

## 1. Platform gating

Root cause confirmed in `tests/final/test_windows_final_launcher_ownership.py`: the seven parametrized live ownership cases launch `powershell.exe` and are therefore target-platform tests, while the same file also contains static source/security checks that remain useful on Linux.

Required change: gate only `test_runtime_process_ownership_is_fail_closed` on non-Windows. Do not skip the whole file, delete the tests, mark them xfail, or remove them from CI. Windows continues to execute all seven live cases.

A minimal collection-gating implementation was prepared that marks only those parametrized runtime cases skipped when `os.name != "nt"`; it was not published because the connected GitHub safety layer blocked the security-test commit.

## 2. Windows process ownership / live CI

The current launcher ownership contract is already fail-closed and passed the dedicated baseline Windows jobs:

- GitHub Actions run `31397553918`
- Windows launcher ownership Python 3.12 job `94144326616`: PASS
- Windows launcher ownership Python 3.13 job `94144326705`: PASS

Verified contract from the current launcher/tests:

- health PID and listener PID must match
- health PID must be present and numeric
- direct/indirect child ownership requires process-tree provenance from the launched runtime
- a foreign listener is rejected
- status/stop use the canonical owned runtime PID plus persisted identity metadata
- no `process on port == ours` shortcut
- no broad process-name kill / `taskkill` fallback

No launcher runtime weakening is required.

## 3. Local voice timeout / fallback

`LocalVoiceBackend` is already bounded at the runtime boundary:

- `_request()` has a hard deadline
- timeout/cancel stops the backend-owned worker
- Chatterbox timeout may trigger one Piper fallback
- the Piper fallback receives its own bounded fallback deadline
- Piper timeout does not enter an automatic retry loop

The two known failures in `tests/speech/test_local_voice.py` are test-double signature drift: their monkeypatched `_request` doubles do not accept the current optional `cancellation_event` keyword. The safe correction is to align those doubles with the current signature, or equivalently preserve compatibility without dropping cancellation propagation. The runtime cancellation contract must not be weakened.

## 4. Voice schema / auditions

The producer in `local_voice_worker.py` writes current audition metadata containing both:

- `schema = VOICE_CACHE_SCHEMA`
- `audio_file = <generated wav name>`

The consumer in `LocalVoiceBackend` correctly requires current schema metadata and resolves the referenced audio file under the audition root.

The failing audition test fixture is stale: it writes only the schema and then expects the audition to be current. Required test fix: include `audio_file` in the current-schema fixture. Do not relax production validation to accept missing `audio_file`.

## 5. Voice cancellation / Global Stop boundary

The W5-owned local voice worker path is cancellation-aware:

- cancel during an active request terminates the owned worker
- cancellation propagates through provider fallback
- timeout/cancel does not start an unbounded retry
- late worker completion is not used to restart a stopped request

Additional regression coverage should explicitly exercise pre-set cancellation and cancellation during fallback. Cross-component Global Stop orchestration remains an integration verification dependency: the integrator must prove that listening, TTS and fallback workers are all stopped and that no late callback reactivates the stopped session.

For Windows helpers, termination must continue to use ownership provenance; foreign processes must never be killed.

## 6. Migration backup preflight

A real runtime regression was confirmed in `src/openjarvis/migration/backup.py`.

Current defect:

1. `_preflight_target_paths()` returns immediately on non-Windows, so the Windows-safe target-path invariant is not deterministically testable on Linux.
2. `create_verified_backup()` creates `destination`, `data`, and `manifests` before source scan / target-path preflight.

Prepared correction:

- remove the host-OS early return from `_preflight_target_paths`
- scan the source first
- run target-path preflight against the future `data_root`
- only after successful preflight create the destination/data/manifests directories
- retain the existing cleanup path for failures after destination creation
- retain the existing `destination.exists()` fail-closed check, so an old valid backup is never destroyed

This satisfies the required ordering: validation happens before copy/mutation, a preflight failure leaves no partial destination, and recovery behavior is deterministic.

A migration-only tree containing exactly this change was created directly from the frozen base, but GitHub publication was blocked by the connected write-safety layer before a code commit/ref update occurred.

## 7. Git secure / module isolation

The existing `SecureGitService` policies themselves are strict:

- upstream push must be `DISABLED`
- integration-repo mutation is rejected
- dirty integration repo blocks worktree creation
- task branch prefix is mandatory
- foreign/unowned worktrees are rejected
- commit paths are explicit and bounded to the owned worktree

The four reported failures are consistent with `GitPolicyError` identity changing across `importlib.reload()` boundaries. A robust fix is to define the semantic policy exception in a stable helper module and import it from `git_secure.py`, so reloading `git_secure` does not create a new exception class identity.

The security-control file modification was blocked by the connected GitHub safety layer. No broader catch, exception swallowing, policy weakening, or identity trick was used as a workaround.

## 8. Operative agent `tool_id`

`tests/operators/test_operators.py::TestOperativeAgent::test_run_tool_loop` uses a legacy test double (`spec.name` / `run`) that no longer satisfies the canonical tool contract (`tool_id`, manifest, `execute()` returning `ToolResult`).

Classification: W5 test/tool-contract compatibility, not W1 Action-Service semantics. No Action-Service or manifest-security code should be duplicated or changed. The test double should be updated to expose the canonical `tool_id` and execution contract.

## 9. Validation evidence

Requested commands were not claimed as executed because the private repository is not available in the local execution container and connector writes never produced a runnable W5 code candidate:

- `uv run pytest -q tests/speech` — not executed on a W5 code candidate
- `uv run pytest -q tests/migration/test_backup.py` — not executed on a W5 code candidate
- `uv run pytest -q tests/tools/test_git_secure.py` — not executed on a W5 code candidate
- `ruff check <changed files>` — not executed on a W5 code candidate
- `ruff format --check <changed files>` — not executed on a W5 code candidate
- `git diff --check` — not executed on a W5 code candidate

Existing baseline evidence does prove both dedicated Windows launcher ownership jobs (3.12 and 3.13) pass before W5 changes.

## 10. Remaining integrator / worker dependencies

Before this worker can be marked ready, the following must be published and validated on `closure/w5-platform-voice-migration`:

1. non-Windows gating for only the seven live Windows ownership cases
2. migration preflight-before-mutation runtime fix
3. voice timeout test-double signature alignment
4. current audition fixture with `audio_file`
5. explicit voice cancellation/fallback regression tests
6. reload-stable `GitPolicyError` identity without weakening Git policy
7. operative test double aligned to canonical `tool_id` / `execute()`
8. requested W5 pytest suites + Ruff + `git diff --check`
9. cross-boundary Global Stop verification at integration

## Final disposition

`READY_FOR_INTEGRATION: NO`

Reason: required W5 code/test changes could not be published through the connected GitHub write boundary in this run. The remote branch contains only this blocker/handoff documentation on top of the frozen base. Draft-PR creation against `integration/jarvis-operator-final` was attempted and blocked by the same write-safety layer. No merge was performed.
