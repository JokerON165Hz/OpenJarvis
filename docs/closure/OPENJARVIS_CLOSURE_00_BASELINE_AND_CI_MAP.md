# OPENJARVIS CLOSURE 00 – BASELINE FREEZE / CI FAILURE MAP

Generated: 2026-08-12 (Europe/Vienna)
Repository: `JokerON165Hz/OpenJarvis`
Scope: READ-ONLY baseline freeze. This GitHub copy is the coordination handoff for the Closure Wave.

## 1. Authoritative freeze

- `MAIN_SHA`: `100595f8aa2df86a74b6bd6b676c0036f25dc028`
- `PR7_HEAD_SHA`: `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`
- PR: `#7`
- Head branch: `integration/jarvis-operator-final`
- Base branch: `main`
- State: OPEN
- Draft: YES
- Mergeable: YES
- Behind current `main`: 0
- Current `main` equals the PR base SHA: YES
- Compare base -> head: ahead by 196, behind by 0

Workers MUST branch from exact frozen PR head `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`, not from a moving branch.

## 2. Current PR-head workflow state

Exactly five Actions runs were associated with current PR head at baseline inspection. All were completed; none were pending.

- Central CI: FAILURE
  - rust: SUCCESS
  - test-windows (3.13): SUCCESS
  - test-windows (3.12): SUCCESS
  - lint: FAILURE because `ruff format --check` is red while `ruff check` is green
  - test: FAILURE, `50 failed, 8215 passed, 61 skipped`
- Frontend CI: SUCCESS
- Deploy Documentation: SUCCESS, deploy skipped by expected PR gating
- Installer integration: SUCCESS
- Desktop Build & Release validation: SUCCESS, release-only jobs skipped by expected event gating

Coverage gate passed: 65.79% >= 60%.

## 3. Failure decomposition

### Codex SDK: 15 failures

Root cause: `openai_codex` is not installed in the normal Linux CI environment. PR #7 defines `codex = ["openai-codex==0.144.4"]`, while the normal CI install command omits `--extra codex`.

Classification: PR-caused CI/dependency integration regression.

### Windows launcher ownership tests collected on Ubuntu: 7 failures

Root cause: PowerShell-dependent Windows tests are collected in the normal Ubuntu test lane. Dedicated Windows 3.12 and 3.13 jobs are green.

Classification: PR-caused platform-gating regression, not evidence of a Windows product failure.

### RLM direct-tool bridge: 2 failures

- bounded-read helper passes `max_lines` but active manifest rejects it
- file-chunk helper no longer returns expected bounded chunk

Classification: real PR integration/product contract regressions.

### Remaining behavioral/integration failures

Confirmed failing contract areas include:

- migration backup preflight/cleanup ordering
- security confirmation callback
- legacy action gateway approval/policy compatibility
- MCP discovery/cache behavior
- local voice cancellation/signature and audition-schema compatibility
- operative-agent `tool_id` contract
- ActionStatus/GitPolicyError module identity/isolation behavior
- operator/action service recovery, retry, claim, Global Stop and canonical side-effect/manifest snapshots
- manifest safety: level-four execution must remain rejected
- task-route fresh-turn follow-up/memory-candidate continuation
- tool-browser action creation/execution, sharing the action-service canonicalization defect

The central functional closure must fix the actual product contracts or explicitly justify a deliberate contract change. Tests must not be weakened merely to obtain green CI.

## 4. Ruff format gate

At baseline:

- `ruff check src/ tests/`: PASS
- `ruff format --check src/ tests/`: FAIL
- Ruff 0.15.1 reported 834 files would be reformatted and 694 already formatted.

Interpretation: broad baseline-dominant formatter debt plus PR overlap. Do NOT let a parallel worker globally rewrite all 834 files while functional workers are active.

Critical rule: defer global Ruff normalization until functional worker changes have been integrated, or restrict formatting to a worker's own disjoint changed files.

## 5. Worker allocation

### Worker 1 – Operator/action safety core

Own primarily:
- `src/openjarvis/tools/action_service.py`
- `src/openjarvis/tools/action_store.py` as required
- `src/openjarvis/tools/manifest.py`
- operator action/recovery/global-stop core
- corresponding action/manifest tests

Target: action-state-machine regressions, canonicalization, retry/recovery/global-stop, manifest level-four rejection, enum isolation as applicable.

### Worker 2 – Codex CI/dependency contract

Own primarily:
- `pyproject.toml`
- `uv.lock` only if required
- `.github/workflows/ci.yml` as sole functional owner
- Codex CI tests only where the test itself is genuinely wrong

Target: install/gate Codex dependency correctly; preserve lifecycle assertions.

### Worker 3 – RLM/tool bridge + MCP discovery

Own primarily:
- `src/openjarvis/agents/rlm.py`
- `src/openjarvis/agents/rlm_repl.py`
- bounded read/chunk bridge
- MCP discovery/cache implementation and related tests

Target: 2 RLM + 3 MCP cache failures.

### Worker 4 – Server policy/follow-up integration

Own primarily:
- legacy server action gateway path
- `src/openjarvis/server/task_routes.py`
- `src/openjarvis/server/tool_browser_routes.py`
- confirmation callback path as required
- related server/security tests

Target: legacy approval contract, confirmation callback, task fresh-turn follow-up, tool-browser failures.

Worker 4 must coordinate with Worker 1 and must not independently redefine Worker-1-owned action semantics.

### Worker 5 – Platform/voice/migration/isolation cleanup

Own primarily:
- Windows launcher test platform gating/utilities
- speech/local voice implementation/tests
- migration backup implementation/tests
- Git secure/module-reload isolation cause
- operative-agent `tool_id` contract path

Target: 7 Linux-collected Windows tests, 3 speech failures, migration regression, Git identity/isolation failures, operative contract.

### Worker 6 – Verification + deferred Ruff normalization

Parallel phase: READ-ONLY verifier/conflict coordinator.

After Workers 1–5 are integrated:
- run exact CI commands
- perform global Ruff formatting once on the integrated tree
- verify Ruff, full Python tests, Windows lanes, Rust, Frontend, Docs, Installer, Desktop validation
- resolve formatting-only conflicts centrally

## 6. High-risk conflict rules

- `.github/workflows/ci.yml`: Worker 2 sole functional owner.
- `pyproject.toml` / `uv.lock`: Worker 2 owns dependency intent.
- `src/openjarvis/tools/action_service.py`: Worker 1 owns; Worker 4 adapts call sites only.
- `src/openjarvis/tools/manifest.py`: Worker 1 owns; Worker 3 reports required RLM schema changes instead of independently changing it unless explicitly coordinated.
- `src/openjarvis/server/tool_browser_routes.py`: Worker 4 owns route; Worker 1 owns underlying action semantics.
- broad Ruff set: no global formatting during parallel functional work.

## 7. Closure-wave readiness

The baseline inspection found:

- stable known current main and PR head at inspection time
- PR mergeable and not behind main
- no pending PR-head Actions jobs
- Frontend, Rust, Windows 3.12/3.13, Docs build, Installer and Desktop validation green
- only normal Python tests and Ruff format red
- Python failure surface decomposed into separable workstreams
- global formatter explicitly deferred to avoid cross-worker conflicts

Final baseline status:

`READY_FOR_PARALLEL_CLOSURE`

## 8. Coordination rule for all workers

Every Closure worker must:

1. Read this file from GitHub before editing.
2. Verify current GitHub state freshly before acting; do not assume SHAs/statuses remain current.
3. Branch from the exact frozen PR7 head unless an Integrator explicitly provides a newer integration SHA.
4. Stay within its write boundary.
5. Commit all intended code/test/doc changes.
6. Write its own handoff to `docs/closure/OPENJARVIS_CLOSURE_0X_*.md` on its own worker branch.
7. Push the branch to GitHub.
8. Verify the remote branch head after push.
9. Report branch name, base SHA, final head SHA, changed files, tests, blockers and handoff path.
10. Never merge into `main` or PR #7 unless acting under the later Final Integrator/Release Gate instruction.
