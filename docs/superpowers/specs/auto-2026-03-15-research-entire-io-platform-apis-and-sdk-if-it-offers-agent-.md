I now have everything I need. Here's the implementation spec:

---

# Implementation Spec: Entire.io Integration

## Problem Statement

Autodev currently supports three worker backends (`local`, `ssh`, `container`) and passes MCP server configs to subprocesses. Entire.io is a cloud development platform by Nat Friedman (ex-GitHub CEO) and Daniel Gross, designed to provide persistent cloud environments purpose-built for AI agents. Integrating it would give autodev agents access to cloud-hosted workspaces without requiring users to manage their own SSH hosts or Docker infrastructure.

However, **Entire.io launched recently (circa March 2026) and has limited public API documentation**. The platform may still be invite-only or in early access. This spec takes a two-phase approach: (1) a research/adapter skeleton that can be implemented now with feature-flagged config, and (2) concrete API integration once documentation stabilizes.

## Changes Needed

### Phase 1: Config + Backend Skeleton

#### 1.1 `src/autodev/config.py` -- Add `EntireConfig` dataclass

Add after `ContainerConfig` (line ~206):

```python
@dataclass
class EntireConfig:
	"""Entire.io cloud backend settings."""

	api_key: str = ""  # ENTIRE_API_KEY env var fallback
	api_base_url: str = "https://api.entire.io"
	org_id: str = ""
	environment_template: str = ""  # pre-configured env template ID
	startup_timeout: int = 120
	machine_type: str = ""  # e.g. "small", "medium", "large"
	region: str = ""  # e.g. "us-east-1"
	persist_environments: bool = False  # keep envs alive between tasks
```

Update `BackendConfig` (line ~209):

```python
@dataclass
class BackendConfig:
	"""Worker backend settings."""

	type: str = "local"  # local/ssh/container/entire
	max_output_mb: int = 50
	ssh_hosts: list[SSHHostConfig] = field(default_factory=list)
	container: ContainerConfig = field(default_factory=ContainerConfig)
	entire: EntireConfig = field(default_factory=EntireConfig)
```

Update `_build_backend()` (line ~764) to parse `[backend.entire]` TOML section:

```python
if "entire" in data:
	bc.entire = _build_entire(data["entire"])
```

Add `_build_entire()` builder following the existing pattern (e.g., `_build_container()`).

Update `validate_config()` (~line 1412) to add validation for `backend.type == "entire"`:
- Error if `entire.api_key` is empty and `ENTIRE_API_KEY` env var is unset
- Error if `entire.api_base_url` is empty
- Warning if `entire.environment_template` is empty (will use platform defaults)

#### 1.2 `src/autodev/backends/entire.py` -- New backend (skeleton)

New file implementing `WorkerBackend` (following the pattern in `backends/base.py:17`):

```python
class EntireBackend(WorkerBackend):
```

Methods to implement, matching the abstract interface in `backends/base.py`:

| Method | Behavior |
|--------|----------|
| `__init__(config: EntireConfig, mission_config: MissionConfig)` | Store config, init HTTP client, workspace tracking dict |
| `async initialize(warm_count: int = 0)` | Validate API connectivity (health check endpoint), optionally pre-provision environments |
| `async provision_workspace(worker_id, source_repo, base_branch) -> str` | Call Entire.io API to create a cloud environment, clone the repo into it, checkout branch. Return environment ID as workspace identifier |
| `async spawn(worker_id, workspace_path, command, timeout) -> WorkerHandle` | Execute command in the Entire.io environment (via API exec endpoint). Store process reference. Set `backend_metadata` on `WorkerHandle` to environment ID |
| `async check_status(handle) -> str` | Poll Entire.io API for execution status. Map to `running/completed/failed` |
| `async get_output(handle) -> str` | Fetch stdout from Entire.io execution logs API. Respect `max_output_mb` truncation (same pattern as `LocalBackend`, line ~333) |
| `async kill(handle)` | Call Entire.io API to terminate the execution |
| `async release_workspace(workspace_path)` | Destroy or stop the cloud environment (conditional on `persist_environments` config) |
| `async cleanup()` | Destroy all tracked environments, clear internal state |

Internal helpers:
- `_api_request(method, path, body=None) -> dict` -- HTTP wrapper with auth header, retries, timeout
- `_resolve_api_key() -> str` -- Check `config.api_key`, then `os.environ.get("ENTIRE_API_KEY")`, raise if neither

The skeleton should raise `NotImplementedError("Entire.io API not yet available")` in each method body until the API is documented, but the class structure and config wiring should be complete.

#### 1.3 `src/autodev/continuous_controller.py` -- Wire backend selection (~line 724)

Add `elif` branch in `_init_components()`:

```python
elif self.config.backend.type == "entire":
	from autodev.backends.entire import EntireBackend
	backend = EntireBackend(
		config=self.config.backend.entire,
		mission_config=self.config,
		max_output_mb=self.config.backend.max_output_mb,
	)
	await backend.initialize()
	self._backend = backend
```

This follows the existing pattern where `ssh`/`container`/`local` are selected via if/elif/else.

#### 1.4 `src/autodev/swarm/controller.py` -- Wire for swarm mode

Find the equivalent backend selection logic in the swarm controller and add the same `entire` branch. The swarm controller spawns agents differently (via `build_claude_cmd()` + subprocess), so the Entire backend needs to handle command execution in cloud environments rather than local subprocesses.

### Phase 2: MCP Server Option (depends on Entire.io API availability)

#### 2.1 `src/autodev/mcp_servers/entire_mcp.py` -- New MCP server

If Entire.io exposes APIs for environment management (create, list, exec, destroy), expose them as MCP tools so that planner/worker agents can interact with Entire.io environments directly:

| Tool | Description |
|------|-------------|
| `entire_create_env` | Create a new cloud environment with specified template |
| `entire_exec` | Run a command in an existing environment |
| `entire_list_envs` | List active environments |
| `entire_destroy_env` | Tear down an environment |
| `entire_env_status` | Get environment health/resource status |

Follow the pattern in `src/autodev/mcp_server.py` (line ~1): use `mcp.server.Server`, define `Tool` objects with `inputSchema`, implement `@server.call_tool()` handler.

#### 2.2 `src/autodev/mcp_registry.py` -- Register Entire.io tools

No changes to `MCPToolRegistry` itself -- the registry handles synthesized tools generically. But if Entire.io tools are pre-registered (not synthesized), add them as built-in entries during initialization when the `entire` backend is configured.

### Phase 3: API Client (once docs are available)

#### 3.1 `src/autodev/backends/entire_client.py` -- HTTP client

Thin `httpx`/`aiohttp`-based client wrapping the Entire.io REST API:

```python
class EntireClient:
	async def create_environment(self, template_id: str, repo_url: str, branch: str) -> str: ...
	async def exec_command(self, env_id: str, command: list[str], timeout: int) -> str: ...
	async def get_execution_status(self, exec_id: str) -> dict: ...
	async def get_execution_output(self, exec_id: str) -> str: ...
	async def stop_execution(self, exec_id: str) -> None: ...
	async def destroy_environment(self, env_id: str) -> None: ...
	async def health_check(self) -> bool: ...
```

This client would be used by both `EntireBackend` and `entire_mcp.py`.

## Files Summary

| File | Action | Description |
|------|--------|-------------|
| `src/autodev/config.py` | Modify | Add `EntireConfig`, update `BackendConfig`, `_build_backend()`, `_build_entire()`, `validate_config()` |
| `src/autodev/backends/entire.py` | Create | `EntireBackend(WorkerBackend)` skeleton |
| `src/autodev/backends/entire_client.py` | Create | HTTP client for Entire.io API (Phase 3) |
| `src/autodev/continuous_controller.py` | Modify | Add `entire` branch in `_init_components()` (~line 724) |
| `src/autodev/swarm/controller.py` | Modify | Add `entire` backend wiring in agent spawn logic |
| `src/autodev/mcp_servers/entire_mcp.py` | Create | MCP server exposing Entire.io tools (Phase 2) |
| `tests/test_entire_backend.py` | Create | Unit tests |
| `tests/test_entire_config.py` | Create | Config parsing tests |

## Testing Requirements

### Unit Tests (`tests/test_entire_backend.py`)

1. **Config parsing**: TOML with `[backend] type = "entire"` and `[backend.entire]` section correctly produces `EntireConfig` with all fields
2. **API key resolution**: Test `_resolve_api_key()` precedence -- explicit config > env var > error
3. **Config validation**: `validate_config()` produces errors when `entire` backend is selected but `api_key` is missing
4. **Backend selection**: `_init_components()` instantiates `EntireBackend` when `backend.type == "entire"`
5. **WorkerHandle metadata**: `spawn()` sets `backend_metadata` to environment ID
6. **Output truncation**: `get_output()` respects `max_output_mb` (same as `LocalBackend` pattern)
7. **Cleanup**: `cleanup()` destroys all tracked environments
8. **Persist flag**: `release_workspace()` skips destruction when `persist_environments=True`

### Integration Tests (Phase 3, requires API access)

1. End-to-end: create env -> clone repo -> run command -> get output -> destroy env
2. Error handling: invalid API key returns clear error, network timeout retries
3. Concurrent environments: multiple workers provision simultaneously

### Config Tests (`tests/test_entire_config.py`)

1. Default values match dataclass defaults
2. TOML round-trip: write config -> read config -> assert equality
3. `BackendConfig.type` accepts `"entire"` without validation error (when API key is set)
4. Env var fallback for `api_key` works via `os.environ`

### What to Verify

- Existing backend tests still pass (no regressions from config changes)
- `_build_backend()` ignores `[backend.entire]` when `type != "entire"` (no side effects)
- Import of `EntireBackend` is lazy (in the `elif` branch) to avoid import errors when Entire.io deps aren't installed

## Risk Assessment

### Risk 1: API Instability (Medium)
**What**: Entire.io is a new platform; APIs may change frequently or lack stability guarantees.
**Mitigation**: Phase the implementation. Phase 1 (config + skeleton) has zero external dependency. Use `NotImplementedError` stubs. The backend only becomes active when explicitly configured with `type = "entire"`.

### Risk 2: No Public API Yet (High)
**What**: As of March 2026, Entire.io may still be invite-only with no documented public API.
**Mitigation**: Phase 1 is implementable without any API access -- it's purely config plumbing and interface conformance. Gate Phase 2/3 on API documentation availability. Monitor `https://entire.io/docs` and `https://github.com/entireio` for SDK releases.

### Risk 3: Auth Credential Leakage (Low)
**What**: API keys in TOML config files could be committed to git.
**Mitigation**: Support env var fallback (`ENTIRE_API_KEY`) as the primary auth mechanism. Add `entire.api_key` to the same validation path that checks Telegram tokens (config.py ~line 1408). Document that `.env` files must not be committed (already in project `.gitignore`).

### Risk 4: Cloud Environment Lifecycle (Medium)
**What**: Orphaned cloud environments (agent crashes before cleanup) could accumulate costs.
**Mitigation**: 
- `cleanup()` must be called in `finally` blocks (same pattern as `LocalBackend`)
- `persist_environments=False` by default -- environments are destroyed on release
- Add environment TTL/auto-destroy in the API call if Entire.io supports it
- Log environment IDs at creation for manual cleanup if needed

### Risk 5: Workspace Git Workflow Mismatch (Medium)
**What**: `LocalBackend` uses `WorkspacePool` with local git clones. Entire.io environments may manage git differently (pre-cloned repos, different filesystem layout).
**Mitigation**: `EntireBackend` does NOT use `WorkspacePool`. It manages workspaces entirely through the Entire.io API. The `provision_workspace()` return value is an opaque environment ID string rather than a filesystem path. This means `GreenBranchManager` won't work with Entire.io unless we add remote-aware merge support (defer to Phase 3).

### Risk 6: Green Branch Incompatibility (Medium)
**What**: `GreenBranchManager` adds the workspace as a git remote (line ~764-769 in `continuous_controller.py`). This won't work with cloud environments.
**Mitigation**: For Phase 1, skip green branch setup when `backend.type == "entire"`. Workers would need to push to a remote branch that the controller then merges. This is a design decision to resolve in Phase 3 based on what Entire.io's git integration looks like.

### Recommended Approach

Implement **Phase 1 only** now. This adds ~150 lines of config plumbing and a ~100 line backend skeleton with no external dependencies, no risk of breaking existing functionality, and zero cost if Entire.io never materializes. Gate Phases 2-3 on:

1. Public API documentation at `https://entire.io/docs` or `https://docs.entire.io`
2. A published SDK (check `https://github.com/entireio` or PyPI `entire-sdk`)
3. Confirmed support for programmatic environment creation and command execution