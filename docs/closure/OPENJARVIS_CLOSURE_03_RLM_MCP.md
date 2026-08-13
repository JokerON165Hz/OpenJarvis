# OPENJARVIS CLOSURE 03 — RLM / MCP

Worker: W3 / K2  
Branch: `closure/w3-rlm-mcp`  
Frozen base: `26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e`  
Implementation head before this handoff commit: `cd7d30ae34eb64dd4bd2db9c583873eaed873868`

> Git commit IDs are hashes of the commit tree, including this file, so a handoff file cannot contain the SHA of the commit that contains itself. The final remote branch head must therefore be read from `closure/w3-rlm-mcp` after this handoff commit; it is reported in the worker result.

## Scope and ownership

W3 changes are limited to RLM bridge behavior and MCP discovery/cache behavior. W1-owned `src/openjarvis/tools/action_service.py` and `src/openjarvis/tools/manifest.py` were read but not changed. `.github/workflows/ci.yml` and the server router were not changed.

The current W1 Action Service contract was re-read before mutation. W3 relies on its canonical `register_runtime`, `refresh_runtime`, `replace_runtime_policy`, and `unregister_runtime` authority. In particular, live policy replacement is allowed only for mutable policy fields; schema, identity, capability, timeout, network/secret policy and other immutable manifest fields remain protected by W1.

## Original failure contracts

### 1. RLM bounded read

Original regression: `tests/agents/test_rlm.py::TestRLMDirectToolBridge::test_root_repl_can_use_bounded_read_helper`.

Root cause: the RLM helper could omit `max_lines` for a non-positive value, which creates an unbounded fallback. The baseline test double also represented a legacy `file_read` runtime whose implementation accepted `max_lines` while its declared ToolSpec omitted the argument; the strict ToolExecutor correctly rejects undeclared arguments.

Fix:

- `src/openjarvis/agents/_rlm_bounds.py` installs fail-closed helper methods.
- `max_lines` must parse as an integer and be strictly positive.
- the helper requires the effective `file_read` ToolSpec to expose `max_lines` before invoking the canonical executor.
- `src/openjarvis/agents/react.py`, which is the already-expected backward-compat import in `agents/__init__.py`, normalizes a legacy runtime with `tool_id == file_read` onto the code-owned canonical `FileReadTool` spec/manifest before ToolExecutor construction.
- the compatibility proxy delegates execution through ToolExecutor and bounds the returned content again as defense in depth.
- no direct filesystem API is introduced.
- no manifest validation is relaxed.

### 2. RLM file chunk

Original regression: `tests/agents/test_rlm.py::TestRLMDirectToolBridge::test_root_repl_can_use_file_chunk_helper`.

Fix:

- chunk bounds must be integers with `1 <= start_line <= end_line`; invalid ranges fail closed.
- the helper requests only the prefix required to reach `end_line` through `file_read(max_lines=end_line)` and slices that bounded result using stable 1-based inclusive semantics.
- EOF is clean: slicing a shorter bounded result simply returns the available tail.
- there is no path-only or unlimited fallback.

### 3. MCP live policy change

Original regression: `tests/mcp/test_action_bridge.py::test_live_policy_change_replaces_policy_but_not_schema`.

The Frozen Base Action Bridge already routes discovered MCP tools through the canonical ToolActionService. W3 deliberately does not duplicate or weaken that authority. The overlay re-exports the base `_manifest` and `_runtime`, so `replace_runtime_policy` remains the single policy-replacement gate. Schema/identity/capability changes therefore continue to fail closed under the W1 contract while policy-only changes can replace the current policy.

### 4. MCP tool discovery/cache

Original regressions:

- `tests/server/test_mcp_tools_cache.py::test_returns_tools_from_mcp_server`
- `tests/server/test_mcp_tools_cache.py::test_caches_successful_discovery`
- `tests/server/test_mcp_tools_cache.py::test_does_not_cache_empty_results`

Fix in `src/openjarvis/mcp/action_bridge/__init__.py`:

- cache reuse is bound to a SHA-256 fingerprint of effective server configuration, transport-auth presence/fingerprint, registry identity, and ToolActionService catalog identity.
- a cached value with no matching identity is discarded before discovery.
- a successful non-empty MCP discovery may retain the base cache and receives the current identity.
- an empty MCP adapter set clears the cache, even if the lower layer attempted to preserve a result.
- policy/server configuration changes naturally change the context fingerprint and therefore cannot reuse a stale schema cache.
- credentials themselves are never placed in the cache key; only a one-way hash is used.

### 5. Canonical MCP Action Bridge

MCP tool execution remains on the Frozen Base canonical bridge and ToolActionService. W3 does not add direct external side effects, an MCP-local approval mechanism, an MCP-local security level, or an alternate Action ID path. The existing Action Service continues to own manifest validation, policy replacement, runtime registration, action proposal/execution, task/correlation identity and idempotency semantics.

W1 dependency: integration must preserve the current W1 immutable-vs-mutable runtime-policy contract. W3 requires no W1 source change.

### 6. Cache / idempotency / reconnect

The overlay invalidates cached tool discovery when server/registry/catalog context changes. After forced rediscovery, or when discovery status reports an unavailable server, MCP runtimes for configured servers that are no longer present in the current adapter set are unregistered. This prevents a prior transient runtime handle from remaining executable authority after reconnect/schema failure. Empty discovery is not retained as a final tool set.

No retry path in W3 directly executes the external effect; execution still goes through the canonical Action Service, so discovery/cache retries do not themselves duplicate an Action effect.

## Files changed

- `src/openjarvis/agents/_rlm_bounds.py` — added bounded, fail-closed RLM helpers.
- `src/openjarvis/agents/react.py` — added the previously expected ReAct compatibility shim and canonical legacy `file_read` adapter; installs the RLM bounds bridge before normal RLM use.
- `src/openjarvis/mcp/action_bridge/__init__.py` — added context-bound overlay around the unchanged Frozen Base Action Bridge.
- `docs/closure/OPENJARVIS_CLOSURE_03_RLM_MCP.md` — this handoff.

No W1-owned file, manifest file, server router, or CI workflow is changed.

## Validation evidence

### Remote branch/diff

Before this handoff, GitHub compare against the frozen base reported the branch `ahead_by=3`, `behind_by=0`, with exactly the three W3 Python files above and no foreign-worker changes.

### Executed in the available worker environment

The execution container cannot resolve `github.com`, so an exact repository checkout cannot be cloned. The installed environment also does not contain OpenJarvis or Ruff. Because the repository bytes are only available through the connected GitHub API, the requested `uv` commands cannot be truthfully reported as executed here.

Checks that were executed against byte-for-byte copies of the three published Python files:

- Python `py_compile`: PASS for all three files.
- line-length audit against project Ruff limit 120: PASS; no line exceeds 120.
- isolated RLM behavioral simulation: PASS.
  - legacy `file_read` receives canonical `max_lines`.
  - bounded head returns only requested lines.
  - chunk uses stable 1-based inclusive semantics.
  - EOF returns available tail cleanly.
  - zero/non-positive bounds fail closed.
  - invalid reversed chunk fails closed.
- isolated MCP behavioral simulation: PASS.
  - first non-empty discovery is cacheable.
  - same context reuses cache without rediscovery.
  - server/policy context change invalidates cache and rediscovers.
  - empty discovery clears cache.
  - failed forced rediscovery unregisters stale MCP runtime authority.

### Required commands not executable in this worker environment

Not executed because an exact checkout cannot be obtained and no PR CI run could be triggered:

- `uv run pytest -q tests/agents/test_rlm.py`
- `uv run pytest -q tests/mcp`
- `uv run pytest -q tests/server/test_mcp_tools_cache.py`
- `uv run pytest -q tests/server/test_mcp_routes.py`
- canonical Action Bridge test suite
- `uv run ruff check <changed Python files>`
- `uv run ruff format --check <changed Python files>`
- `git diff --check 26ed6fcf30fd1f2f2473c29b7fcd5e736a91931e..HEAD`

GitHub Actions query for implementation head `cd7d30ae34eb64dd4bd2db9c583873eaed873868` returned no workflow runs. Two authorized Draft-PR creation attempts against `integration/jarvis-operator-final` were rejected by the connector policy gate before GitHub mutation, so PR-triggered CI is unavailable from this worker session.

## Commits before handoff

- `606e60f3f5d2000143f2d3682b233f4b18dd7f03` — `fix(rlm): require bounded file-read contract`
- `322f635fd1e7c8d8e9299d9440775b39214b008a` — `fix(rlm): wire canonical bounded read bridge`
- `cd7d30ae34eb64dd4bd2db9c583873eaed873868` — `fix(mcp): bind discovery cache to runtime context`

## Integrator notes

1. Re-run the exact required pytest and targeted Ruff commands on a normal checkout.
2. Verify the package-vs-module resolution for `openjarvis.mcp.action_bridge`: the package overlay is intentionally selected and loads the sibling Frozen Base module as its canonical implementation.
3. Preserve W1 Action Service immutable manifest enforcement when integrating W1 before W3.
4. Create the Draft PR from `closure/w3-rlm-mcp` to `integration/jarvis-operator-final` if the connector/UI permits it; do not merge directly.

## Readiness

`READY_FOR_INTEGRATION: NO`

Reason: implementation is pushed and isolated behavioral checks are green, but the mandatory repository test/Ruff suite and PR CI could not be executed in this worker environment, and Draft-PR creation is blocked by the connector policy gate. Integration should only flip readiness after those exact checks are green.
