# OpenJarvis Closure Coordination Protocol

Repository: `JokerON165Hz/OpenJarvis`
Coordination branch: `coord/openjarvis-closure`
Frozen functional base for Wave A: `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`

## Mandatory startup protocol for every worker

Before editing anything:

1. Fetch GitHub remotes and inspect current `main`, PR #7, and the assigned worker branch.
2. Read `docs/closure/README.md` from `coord/openjarvis-closure`.
3. Read `docs/closure/OPENJARVIS_CLOSURE_00_BASELINE_AND_CI_MAP.md` from `coord/openjarvis-closure` completely.
4. Read every handoff explicitly listed as a dependency in the worker assignment from the exact remote branch where that handoff lives.
5. Verify that the worker starts from the exact instructed base SHA. If the SHA differs, stop mutation work and classify the mismatch in the handoff instead of silently rebasing onto a moving target.
6. Inspect current GitHub Actions/PR state freshly. Handoff status is evidence, not a substitute for current verification.

## Mandatory isolation rules

- Each worker uses its own dedicated branch.
- Do not commit directly to `main`.
- Do not commit directly to `integration/jarvis-operator-final` unless the Final Integrator assignment explicitly authorizes it.
- Stay inside the assignment's write boundary.
- If a necessary fix belongs to another worker's owned area, document the required cross-area change instead of silently taking ownership.
- Never weaken security checks, remove tests, skip tests, broaden allowances, or change expected behavior merely to make CI green.
- Never run repository-wide Ruff formatting while parallel functional workers are active.

## Mandatory completion protocol for every worker

Before declaring completion:

1. Run the assignment-specific focused tests.
2. Run all reasonable regression tests for changed areas.
3. Run `git diff --check`.
4. Run relevant lint/format checks on files owned/changed by that worker.
5. Create the required Markdown handoff under `docs/closure/`.
6. The handoff must record at minimum:
   - assignment name
   - authoritative base SHA
   - branch name
   - final local/remote head SHA
   - changed files
   - root causes
   - fixes
   - exact tests/commands and results
   - skipped/unavailable validations
   - remaining blockers
   - cross-worker dependencies/conflicts
   - whether the work is ready for integration
7. Commit the code, tests, and handoff intentionally.
8. Push the worker branch to GitHub.
9. Verify the remote branch head after the push.
10. If connector/tooling allows it, open a Draft PR targeting the instructed integration branch and include the handoff path in its description. Do not merge it.
11. Return the branch, remote SHA, Draft PR number if created, and handoff path.

A worker is NOT complete if its work exists only locally or only in chat. The remote GitHub branch and handoff are mandatory deliverables.

## Handoff consumption rule

Later workers and integrators must read handoffs from GitHub, not rely on pasted summaries. When a dependency handoff exists on a remote worker branch, fetch that exact branch/path and verify its recorded head against GitHub before using it.

## Integration rule

Only an explicitly assigned Integrator may combine worker branches. Integration must be semantic, not blind `ours/theirs`. After integration, all relevant handoff files must be retained under `docs/closure/` on the integration branch so subsequent validation workers can read one coherent evidence set.

## Release rule

PR #7 must remain Draft and unmerged until the Final Release Gate concludes `PRODUCTION RELEASE: READY` and all required CI/live-validation conditions in the final assignment are satisfied. Post-merge `main` validation is a separate mandatory gate.
