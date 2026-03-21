# OpenCode Backend Integration Spec

**Date**: 2026-03-21
**Status**: Draft
**Author**: spec-writer-v2 (autodev swarm agent)
**Sources**: `.claude-project/research/opencode-architecture-analysis.md`, `docs/backend-abstraction-analysis.md`

---

## 1. Problem Statement

autodev is tightly coupled to Claude Code as its sole AI agent backend. Every spawn path -- both mission-mode workers (`build_claude_cmd` in `config.py:1276`) and swarm agents (`_spawn_claude_session` in `swarm/controller.py:720`) -- hardcodes Claude CLI flags, output format assumptions, and permission model semantics.

This creates three concrete problems:

1. **No model diversity**: Claude Code locks workers to Anthropic models. Tasks like simple linting fixes or research lookups could run on cheaper providers (OpenAI, Gemini, Groq) via OpenCode's multi-provider support, reducing cost per swarm run.

2. **No resilience**: If Claude Code is unavailable (API outage, rate limiting, binary corruption), the entire swarm halts. A second backend provides failover capability.

3. **Missing capabilities**: OpenCode offers session resumption (`--continue`/`--session <id>`), auto-compaction (context summarization at 95% capacity), and per-session cost tracking -- features autodev lacks today. Integrating these as a backend unlocks them without reimplementing in Python.

**Goal**: Add `OpenCodeBackend` as a second worker backend, routable per-task via config, without breaking existing Claude Code functionality.

---

## 2. Phase 1: Research Findings Summary

### 2.1 OpenCode Tool Execution vs autodev Workers

OpenCode implements a Go-native tool loop (`agent.go:processGeneration`) that streams LLM responses, collects `tool_call` events, executes tools sequentially via in-process `BaseTool` interfaces, and loops until the LLM returns a final response. Tools are compiled into the binary (13 built-in: `edit`, `write`, `view`, `bash`, `glob`, `grep`, `ls`, `fetch`, `sourcegraph`, `diagnostics`, `agent`, `patch`, plus MCP tools via `mcp-tools.go`).

autodev's workers (`worker.py`, `session.py`) are fundamentally different: they delegate the entire tool loop to Claude Code as a subprocess. autodev never sees individual tool calls -- it only parses the final `AD_RESULT:{}` JSON marker from stdout. This means:

| Dimension | OpenCode | autodev |
|-----------|----------|---------|
| Tool dispatch | In-process Go loop | Delegated to Claude subprocess |
| Result capture | Structured `ToolResponse` then `AgentEvent` | `AD_RESULT:{}` marker from stdout |
| Tool set | Fixed at compile time + MCP | Claude's built-in tools + MCP via `--mcp-config` |
| Parallelism | Sequential within agent | Multiple parallel subprocesses |
| Permission model | pubsub `PermissionRequest` with auto-approve | `--dangerously-skip-permissions` CLI flag |

**Key gap**: OpenCode does not emit `AD_RESULT` markers. Its output is either raw text or `--format json` structured output containing the final LLM response. An adapter layer is required to translate OpenCode's output into autodev's `MCResultSchema` (`{status, commits, summary, files_changed, discoveries, concerns}`).

### 2.2 Context Management

OpenCode loads project context once via `sync.Once` from config-file paths (`CLAUDE.md`, `opencode.md`, `.cursorrules`, etc.) and appends it to the system prompt. Conversation history is stored in SQLite and auto-summarized at 95% context capacity via a dedicated `AgentSummarizer` session.

autodev's `swarm/context.py` takes a completely different approach: it rebuilds context from scratch each planner cycle by aggregating git state, test output, swarm state, team inbox messages, learnings, and stagnation signals. Workers receive their full context as a one-shot prompt -- there is no per-worker history management.

| Dimension | OpenCode | autodev |
|-----------|----------|---------|
| Context source | Config file paths + conversation DB | Live git/test/swarm state per cycle |
| History management | SQLite messages, auto-summarize | No per-worker history; fresh prompt each time |
| Cross-agent context | None (single-agent) | Team inboxes, shared learnings, swarm state |
| Context loading | Read-once (`sync.Once`) | Rebuilt each planner cycle |

**Integration opportunity**: OpenCode's auto-compaction is valuable for long-running autodev workers that currently have no defense against context overflow. However, autodev's cross-agent context (inboxes, learnings) has no OpenCode equivalent and must be injected via the prompt.

### 2.3 Session Lifecycle

OpenCode's `Session` struct (`session.go`) provides persistent, SQLite-backed conversations with `ParentSessionID` for sub-agents, `SummaryMessageID` for compacted history, and per-session token/cost accounting. Sessions support `--continue` (resume last), `--session <id>` (resume specific), and `--fork` (branch from existing).

autodev workers (`session.py`) are ephemeral: each subprocess starts fresh, runs to completion or timeout, and its only persistent artifact is the `AD_RESULT` JSON parsed from stdout. There is no session resumption, no cost tracking per worker, and no parent/child relationship.

| Dimension | OpenCode | autodev |
|-----------|----------|---------|
| Persistence | Full SQLite message history | Ephemeral subprocess, no session state |
| Resumption | `--continue` / `--session <id>` / `--fork` | None; workers restart from scratch |
| Cost tracking | Per-session `PromptTokens`, `CompletionTokens`, `Cost` | Not tracked per worker |
| Parent/child | `ParentSessionID` for sub-agent delegation | No worker hierarchy |

**Integration opportunity**: Session resumption enables a "retry with context" pattern -- when a worker fails, re-invoke with `--continue` instead of starting fresh. This could significantly improve success rates on complex tasks. Per-session cost tracking fills a gap in autodev's observability.

### 2.4 Process Model

OpenCode's non-interactive mode (`opencode run --command "..." --format json`) is production-ready for headless execution:

```
opencode run --command "prompt text"
             --format json              # Structured JSON output
             --model provider/model     # Multi-provider model selection
             --agent coder|task         # Agent type (full vs read-only)
             --session <id>             # Resume specific session
             --continue                 # Resume last session
             --cwd <path>              # Working directory
```

The server mode (`opencode serve --port <N>`) exposes an HTTP API but is undocumented and adds lifecycle complexity. The subprocess approach (`opencode run`) is recommended as the primary integration path because:

1. It mirrors autodev's existing subprocess model (`asyncio.create_subprocess_exec`)
2. JSON output eliminates fragile `AD_RESULT` marker parsing (though an adapter is still needed for schema translation)
3. Workspace isolation can reuse autodev's existing `WorkspacePool`
4. No server lifecycle management overhead

### 2.5 Two Spawn Paths Problem

A critical finding from the backend analysis: autodev has **two separate spawn paths** that both need OpenCode support:

1. **Mission mode**: Workers go through `WorkerBackend` ABC (`backends/base.py`) via `LocalBackend.spawn()`. Adding OpenCode here follows the established pattern.

2. **Swarm mode**: Agents are spawned directly in `swarm/controller.py:_spawn_claude_session()` via `build_claude_cmd()` + `asyncio.create_subprocess_exec`, **bypassing the backend abstraction entirely**.

Both paths must be addressed for full integration.

---

## 3. Phase 2: Backend Integration Design

### 3.1 OpenCodeBackend Class

**New file**: `src/autodev/backends/opencode.py`

```python
class OpenCodeBackend(WorkerBackend):
	"""Execute workers via OpenCode's non-interactive mode."""

	def __init__(
		self,
		source_repo: str,
		pool_dir: str,
		max_clones: int = 10,
		base_branch: str = "main",
		max_output_mb: int = 50,
		config: MissionConfig | None = None,
		opencode_config: OpenCodeConfig | None = None,
	) -> None: ...

	async def initialize(self, warm_count: int = 0) -> None:
		"""Pre-create workspace clones (reuses WorkspacePool)."""

	async def provision_workspace(
		self, worker_id: str, source_repo: str, base_branch: str
	) -> str:
		"""Provision workspace. Identical to LocalBackend except:
		- Writes opencode.md instead of CLAUDE.md
		- Skips .venv symlink if opencode_config.skip_venv is True
		"""

	async def spawn(
		self, worker_id: str, workspace_path: str, command: list[str], timeout: int
	) -> WorkerHandle:
		"""Spawn opencode subprocess. command is pre-built by build_opencode_cmd()."""

	async def check_status(self, handle: WorkerHandle) -> str:
		"""Check process status via returncode (same as LocalBackend)."""

	async def get_output(self, handle: WorkerHandle) -> str:
		"""Read stdout with output size limits. If --format json,
		wraps output in AD_RESULT adapter before returning."""

	async def kill(self, handle: WorkerHandle) -> None:
		"""Kill subprocess (same as LocalBackend)."""

	async def release_workspace(self, workspace_path: str) -> None:
		"""Release workspace back to pool (same as LocalBackend)."""

	async def cleanup(self) -> None:
		"""Kill all processes, clear buffers, cleanup pool."""

	# -- OpenCode-specific --

	async def _write_worker_opencode_md(self, workspace: Path) -> None:
		"""Write worker-specific opencode.md into the workspace.
		Equivalent of LocalBackend._write_worker_claude_md().
		Contains verification command, git rules, AD_RESULT protocol."""

	def _adapt_output(self, raw_json: str) -> str:
		"""Translate OpenCode JSON output to AD_RESULT format.
		Extracts content from OpenCode's response structure,
		wraps it in AD_RESULT:{...} marker for session.py compatibility."""
```

**Design decisions**:
- Reuses `WorkspacePool` from `workspace.py` -- no need to reimplement workspace management
- Reuses same output size tracking (`_stdout_bufs`, thresholds) as `LocalBackend`
- The `_adapt_output` method is the key translation point: it converts OpenCode's `--format json` output into an `AD_RESULT:{}` string that `parse_mc_result()` in `session.py` can parse unchanged
- Worker instructions go in `opencode.md` (one of OpenCode's default context paths) rather than `CLAUDE.md`

### 3.2 Output Adapter

OpenCode's `--format json` output returns the final LLM response as structured JSON. The adapter must translate this into autodev's `MCResultSchema`:

```python
def adapt_opencode_output(raw_output: str) -> str:
	"""Convert OpenCode JSON output to AD_RESULT marker format.

	OpenCode --format json returns:
	{
		"content": "...",        # Final LLM text response
		"model": "...",          # Model used
		"tokens": {...},         # Token usage
		"cost": 0.0             # Session cost
	}

	This function:
	1. Parses the JSON output
	2. Checks if the LLM response already contains AD_RESULT (prompt instructed it)
	3. If not, constructs a synthetic AD_RESULT from available data
	4. Returns the output with AD_RESULT appended
	"""
```

The preferred approach is **prompt-driven**: the worker prompt (in `opencode.md`) instructs the LLM to emit `AD_RESULT:{}` as its final output, just as Claude workers do. The adapter serves as a fallback when the LLM doesn't comply, constructing a best-effort result from the JSON output and git state in the workspace.

### 3.3 OpenCodeConfig Dataclass

**Modified file**: `src/autodev/config.py`

```python
@dataclass
class OpenCodeConfig:
	"""OpenCode backend settings."""

	binary_path: str = ""              # Path to opencode binary; empty = PATH lookup
	default_model: str = ""            # Default model (e.g. "anthropic/claude-sonnet-4.6")
	default_agent: str = "coder"       # Agent type: "coder" or "task"
	output_format: str = "json"        # Output format flag
	config_dir: str = ""               # OpenCode config directory override
	skip_venv: bool = False            # Skip .venv symlink (not needed for all projects)
	session_resume: bool = False       # Enable --continue for retries
	extra_args: list[str] = field(default_factory=list)  # Additional CLI flags
	env_passthrough: list[str] = field(default_factory=list)  # Extra env vars to allow
```

Added to `BackendConfig`:

```python
@dataclass
class BackendConfig:
	type: str = "local"  # local/ssh/container/opencode
	max_output_mb: int = 50
	ssh_hosts: list[SSHHostConfig] = field(default_factory=list)
	container: ContainerConfig = field(default_factory=ContainerConfig)
	opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)  # NEW
```

TOML configuration example:

```toml
[backend]
type = "opencode"

[backend.opencode]
binary_path = ""
default_model = "anthropic/claude-sonnet-4.6"
default_agent = "coder"
output_format = "json"
session_resume = true
```

### 3.4 Command Builder

**Modified file**: `src/autodev/config.py`

```python
def build_opencode_cmd(
	config: MissionConfig,
	*,
	model: str = "",
	prompt: str | None = None,
	output_format: str = "json",
	agent_type: str = "coder",
	session_id: str | None = None,
	continue_session: bool = False,
	cwd: str | None = None,
	extra_args: list[str] | None = None,
) -> list[str]:
	"""Build the opencode subprocess command list.

	Equivalent of build_claude_cmd() for OpenCode backend.
	"""
	oc = config.backend.opencode
	binary = oc.binary_path or _find_opencode_binary()

	cmd = [binary, "run"]
	if prompt:
		cmd.extend(["--command", prompt])
	cmd.extend(["--format", output_format or oc.output_format])

	effective_model = model or oc.default_model
	if effective_model:
		cmd.extend(["--model", effective_model])

	effective_agent = agent_type or oc.default_agent
	if effective_agent:
		cmd.extend(["--agent", effective_agent])

	if session_id:
		cmd.extend(["--session", session_id])
	elif continue_session:
		cmd.append("--continue")

	if oc.extra_args:
		cmd.extend(oc.extra_args)
	if extra_args:
		cmd.extend(extra_args)

	return cmd


def _find_opencode_binary() -> str:
	"""Locate the opencode binary on the system."""
	import shutil
	candidates = [
		shutil.which("opencode"),
		os.path.expanduser("~/.local/bin/opencode"),
		"/usr/local/bin/opencode",
		os.path.expanduser("~/go/bin/opencode"),
	]
	for candidate in candidates:
		if candidate and os.path.isfile(candidate):
			return candidate
	raise FileNotFoundError(
		"opencode binary not found. Install from https://github.com/opencode-ai/opencode"
	)


def opencode_subprocess_env(config: MissionConfig | None = None) -> dict[str, str]:
	"""Build a restricted environment for opencode subprocess calls.

	Reuses the same allowlist/denylist logic as claude_subprocess_env(),
	with additional keys from opencode config's env_passthrough.
	"""
	env = claude_subprocess_env(config)
	if config and config.backend.opencode.env_passthrough:
		for key in config.backend.opencode.env_passthrough:
			if key in os.environ:
				env[key] = os.environ[key]
	return env
```

### 3.5 Swarm Controller Spawn Routing

**Modified file**: `src/autodev/swarm/controller.py`

The swarm controller currently hardcodes `_spawn_claude_session`. The integration adds a routing layer:

```python
async def _spawn_agent_session(
	self, agent: SwarmAgent, prompt: str
) -> asyncio.subprocess.Process | None:
	"""Route agent spawning to the appropriate backend.

	Checks task-level backend override first, then falls back to config.
	"""
	backend_type = self._resolve_backend(agent)

	if backend_type == "opencode":
		return await self._spawn_opencode_session(agent, prompt)
	else:
		return await self._spawn_claude_session(agent, prompt)


def _resolve_backend(self, agent: SwarmAgent) -> str:
	"""Determine which backend to use for this agent.

	Priority:
	1. Task-level metadata (task.metadata.get("backend"))
	2. Agent role mapping (e.g. research agents use cheaper model via opencode)
	3. Config default (backend.type)
	"""
	if agent.task_id:
		task = next((t for t in self._tasks if t.id == agent.task_id), None)
		if task and task.metadata.get("backend"):
			return task.metadata["backend"]
	return self._config.backend.type


async def _spawn_opencode_session(
	self, agent: SwarmAgent, prompt: str
) -> asyncio.subprocess.Process | None:
	"""Spawn an OpenCode subprocess for an agent.

	Parallel to _spawn_claude_session but uses build_opencode_cmd()
	and opencode_subprocess_env().
	"""
	from autodev.config import build_opencode_cmd, opencode_subprocess_env

	cmd = build_opencode_cmd(
		self._config,
		model=self._config.backend.opencode.default_model,
		prompt=prompt,
		output_format="json",
		agent_type=self._config.backend.opencode.default_agent,
	)
	env = opencode_subprocess_env(self._config)
	env["AUTODEV_TEAM_NAME"] = self._team_name
	env["AUTODEV_AGENT_ID"] = agent.id
	env["AUTODEV_AGENT_NAME"] = agent.name
	env["AUTODEV_AGENT_ROLE"] = agent.role.value

	try:
		proc = await asyncio.create_subprocess_exec(
			*cmd,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			cwd=str(self._config.target.resolved_path),
			env=env,
		)
		self._agent_spawn_times[agent.id] = _now_iso()
		self._agent_outputs[agent.id] = ""
		# Note: OpenCode JSON output doesn't use stream-json format,
		# so trace parsing is simplified (no per-tool-call events)
		trace_task = asyncio.create_task(
			self._stream_agent_output(agent.id, agent.name, proc)
		)
		self._trace_tasks[agent.id] = trace_task
		return proc
	except Exception as e:
		logger.error("Failed to spawn OpenCode session for %s: %s", agent.name, e)
		return None
```

### 3.6 Prompt Adaptation

Worker prompts are AI-model-agnostic text. The `AD_RESULT` protocol, git rules, inbox communication, and verification instructions work identically regardless of backend. The only changes needed:

1. **Worker instructions file**: Write `opencode.md` instead of `CLAUDE.md` in the workspace (OpenCode reads `opencode.md` from its default context paths)
2. **Tool references**: Replace Claude-specific tool names (e.g., `WebSearch`, `WebFetch`, `Read`, `Edit`) with OpenCode equivalents (`fetch`, `view`, `edit`, `grep`, `glob`) in the MCP section of the prompt
3. **Permission instructions**: Remove references to `--dangerously-skip-permissions` (OpenCode uses `Permissions.AutoApproveSession()` automatically in non-interactive mode)

The swarm worker prompt builder (`swarm/worker_prompt.py:build_worker_prompt`) already assembles sections generically. The `_mcp_section()` function needs a backend-aware branch:

```python
def _mcp_section(swarm_config: SwarmConfig, backend: str = "claude") -> str:
	if backend == "opencode":
		return """## MCP Tools
- OpenCode provides built-in MCP support via its native client
- Available tools: edit, write, view, bash, glob, grep, ls, fetch, diagnostics
- MCP servers configured in opencode config are available automatically"""
	else:
		return _existing_mcp_section(swarm_config)
```

### 3.7 Data Flow

```
Planner Decision (spawn agent)
    |
    +-- backend == "claude" --> build_claude_cmd() --> claude -p ... --> stdout --> parse AD_RESULT
    |
    +-- backend == "opencode" --> build_opencode_cmd() --> opencode run ... --> stdout (JSON) --> adapt_opencode_output() --> parse AD_RESULT
```

Both paths converge at `parse_mc_result()` in `session.py`, which remains unchanged.

---

## 4. Phase 3: Conditional Enhancements

These enhancements depend on Phase 1/2 success. Each has explicit go/no-go criteria.

### 4.1 LSP-Driven Context

OpenCode integrates LSP clients (`App.LSPClients`) to provide `diagnostics` tool output (compiler errors, warnings) directly to the LLM. autodev workers currently have no LSP integration.

**Enhancement**: When spawning an OpenCode worker in a workspace, configure LSP clients for the project's languages so the LLM receives typed diagnostics alongside its tool calls.

**Go/no-go criteria**:
- Go: OpenCode backend produces measurably better results on type-error-heavy tasks (>20% improvement in first-attempt success rate on tasks involving type errors)
- No-go: LSP startup time exceeds 10s per worker (too slow for swarm's rapid spawn/kill cycle)

**Implementation**: Pass `--log-level DEBUG` during Phase 2 testing to observe whether LSP clients activate. If they do, no additional work is needed -- OpenCode handles LSP automatically based on project file types.

### 4.2 Session Persistence and Resumption

OpenCode's `--continue` and `--session <id>` flags enable resuming a failed worker with its full conversation history intact.

**Enhancement**: When a worker fails, store its OpenCode session ID in `WorkerHandle.backend_metadata`. On retry, invoke with `--session <id>` instead of starting fresh.

**Go/no-go criteria**:
- Go: Retry-with-context produces >30% higher success rate than fresh-start retries on the same task set
- No-go: Session DB grows unbounded without cleanup (OpenCode has no built-in session pruning)

**Implementation**:
```python
# In OpenCodeBackend.spawn(), if retrying:
if retry_session_id:
	cmd = build_opencode_cmd(config, session_id=retry_session_id, ...)
# Store session ID in handle:
handle.backend_metadata = json.dumps({"session_id": session_id})
```

Requires adding a `last_session_id` field to `SwarmAgent` or `SwarmTask` metadata to track across retries.

### 4.3 Tool Execution Observability

OpenCode executes tools in-process, meaning autodev could potentially observe individual tool calls (file edits, bash commands) rather than just the final result.

**Enhancement**: If OpenCode's `--format json` includes tool-call-level detail, parse it to populate swarm trace logs with per-tool timing and status.

**Go/no-go criteria**:
- Go: `--format json` output includes a `tool_calls` array or similar structured field
- No-go: JSON output only contains the final LLM response text (current evidence suggests this is the case)

**Investigation needed**: Run `opencode run --command "list files in current dir" --format json` and inspect the output structure.

### 4.4 Cost-Based Routing

OpenCode tracks per-session cost via `Session.Cost`. Combined with multi-provider support, this enables cost-aware task routing.

**Enhancement**: Planner selects backend per-task based on estimated complexity:
- Simple tasks (research, linting, small fixes) -> OpenCode with cheaper model
- Complex tasks (architecture, multi-file refactors) -> Claude Code with Opus

**Go/no-go criteria**:
- Go: OpenCode session cost data is accessible from the JSON output
- No-go: Cost data requires querying OpenCode's SQLite DB directly (too fragile)

---

## 5. Files Changed

### New Files (3)

| File | Purpose |
|------|---------|
| `src/autodev/backends/opencode.py` | `OpenCodeBackend` class implementing `WorkerBackend` ABC. ~250 lines. Contains `_adapt_output()`, `_write_worker_opencode_md()`, workspace provisioning, subprocess management. |
| `src/autodev/worker_opencode_md.md` | Worker instructions template for OpenCode workers. Equivalent of `worker_claude_md.md`. Contains verification command, git rules, AD_RESULT protocol, but with OpenCode-specific tool references. |
| `tests/test_opencode_backend.py` | Unit tests for `OpenCodeBackend`, output adapter, command builder, config parsing. |

### Modified Files (7)

| File | Changes |
|------|---------|
| `src/autodev/config.py` | Add `OpenCodeConfig` dataclass (~15 lines). Add `opencode` field to `BackendConfig`. Add `build_opencode_cmd()` function (~40 lines). Add `_find_opencode_binary()` (~15 lines). Add `opencode_subprocess_env()` (~10 lines). Update `_build_backend()` parser to handle `[backend.opencode]` TOML section. |
| `src/autodev/swarm/controller.py` | Add `_spawn_agent_session()` routing method (~15 lines). Add `_resolve_backend()` method (~12 lines). Add `_spawn_opencode_session()` method (~35 lines, parallel to `_spawn_claude_session`). Update `_handle_spawn()` to call `_spawn_agent_session()` instead of `_spawn_claude_session()`. |
| `src/autodev/swarm/worker_prompt.py` | Update `_mcp_section()` to accept `backend` parameter and return backend-appropriate tool references (~10 lines). Update `build_worker_prompt()` signature to accept `backend: str = "claude"`. |
| `src/autodev/worker.py` | Add `build_opencode_worker_cmd()` function that wraps `build_opencode_cmd()` with worker-specific defaults (~20 lines). Update `WorkerAgent._spawn_and_wait()` to route between `build_claude_cmd` and `build_opencode_cmd` based on backend config. |
| `src/autodev/session.py` | No functional changes. `parse_mc_result()` and `extract_fallback_handoff()` remain unchanged because the output adapter in `opencode.py` ensures AD_RESULT format compatibility. Add a comment documenting the adapter expectation. |
| `src/autodev/swarm/models.py` | Add optional `backend` field to `SwarmTask.metadata` dict (documentation only -- metadata is already `dict[str, Any]`). |
| `src/autodev/intelligence/utils.py` | Add `find_opencode_binary()` function (~12 lines) alongside existing `find_claude_binary()`. |

---

## 6. Testing Plan

### Unit Tests (10)

| # | Test | What It Validates |
|---|------|-------------------|
| 1 | `test_build_opencode_cmd_defaults` | `build_opencode_cmd()` with no overrides produces `["opencode", "run", "--format", "json"]` plus prompt |
| 2 | `test_build_opencode_cmd_full` | All flags: `--model`, `--agent`, `--session`, `--format`, `--command`, extra_args are placed correctly |
| 3 | `test_build_opencode_cmd_continue` | `continue_session=True` produces `--continue` flag; mutually exclusive with `--session` |
| 4 | `test_opencode_config_from_toml` | TOML `[backend.opencode]` section parses into `OpenCodeConfig` with correct field values |
| 5 | `test_adapt_output_with_ad_result` | When OpenCode JSON output contains an `AD_RESULT:{}` in the content field, adapter extracts it unchanged |
| 6 | `test_adapt_output_fallback` | When OpenCode JSON output has no `AD_RESULT`, adapter constructs a synthetic one from the JSON content + workspace git state |
| 7 | `test_adapt_output_malformed_json` | When OpenCode returns non-JSON (e.g. error text), adapter returns a failed AD_RESULT with the raw output as summary |
| 8 | `test_opencode_subprocess_env` | `opencode_subprocess_env()` returns base allowlist keys plus `env_passthrough` keys from config |
| 9 | `test_resolve_backend_task_override` | `_resolve_backend()` returns task-level `metadata["backend"]` when set, ignoring config default |
| 10 | `test_resolve_backend_config_default` | `_resolve_backend()` falls back to `config.backend.type` when task has no backend metadata |

### Integration Tests (3, manual)

| # | Test | Procedure |
|---|------|-----------|
| 1 | **End-to-end OpenCode worker** | Install OpenCode binary. Set `[backend] type = "opencode"` in config. Run `autodev mission` with a simple objective ("add a docstring to function X"). Verify: worker spawns, completes, AD_RESULT is parsed, commit appears. |
| 2 | **Mixed-backend swarm** | Set `[backend] type = "local"` (Claude default). Create a task with `metadata: {backend: "opencode"}`. Run `autodev swarm`. Verify: the specific task routes to OpenCode while others use Claude. Both complete and report results. |
| 3 | **Fallback on missing binary** | Set `[backend] type = "opencode"` with `binary_path = "/nonexistent"`. Run `autodev swarm`. Verify: `FileNotFoundError` is caught, logged, and planner receives a failed task result (not a crash). |

---

## 7. Risk Assessment

| # | Risk | Severity | Likelihood | Mitigation |
|---|------|----------|------------|------------|
| 1 | **Archived repository**: OpenCode was archived Sep 2025, continued as "Crush". Long-term maintenance is uncertain. | High | Medium | Phase 1 produces go/no-go gate. If OpenCode integration works but the binary becomes unmaintainable, the same `WorkerBackend` pattern supports adding other backends (Aider, Codex CLI, etc.) with minimal code. The abstraction is the value, not the specific backend. |
| 2 | **AD_RESULT compliance**: OpenCode's LLM may not reliably emit `AD_RESULT` markers despite prompt instructions, especially with non-Anthropic models. | Medium | High | Three-layer defense: (1) prompt instructs AD_RESULT emission, (2) `_adapt_output()` constructs synthetic AD_RESULT from JSON output, (3) `extract_fallback_handoff()` recovers from raw output. All three layers already exist or are added by this spec. |
| 3 | **stream-json incompatibility**: Swarm trace parsing (`_stream_agent_output`) expects Claude's `stream-json` format. OpenCode has no equivalent. | Medium | High | OpenCode workers use `--format json` (non-streaming). Trace logging degrades gracefully: tool-call-level events are lost, but final output and AD_RESULT are captured. Add a `backend_type` check in `_stream_agent_output` to skip stream-json parsing for OpenCode agents. |
| 4 | **Go binary dependency**: Adds a Go binary to a Python project's runtime requirements. Build, install, and version management adds operational complexity. | Low | High | Binary is optional -- only required when `backend.type = "opencode"`. Document installation in the worker instructions template. `_find_opencode_binary()` provides clear error messaging when missing. |
| 5 | **Workspace isolation mismatch**: OpenCode has no built-in workspace isolation (no equivalent of `WorkspacePool`). Workers sharing a workspace could clobber each other's files. | High | Low | Mitigated by design: `OpenCodeBackend` reuses autodev's existing `WorkspacePool` for isolation. Each OpenCode worker gets its own cloned workspace, same as Claude workers. The `--cwd` flag is not needed because the subprocess is launched with `cwd=workspace_path`. |
| 6 | **Permission model mismatch**: If OpenCode's auto-approve doesn't cover all tools in non-interactive mode, workers hang waiting for permission on `stdin=DEVNULL`. | High | Low | OpenCode's `RunNonInteractive()` calls `Permissions.AutoApproveSession(sess.ID)` which auto-approves all tools. Verified in the architecture analysis. Add a startup test in integration testing that confirms no permission prompts appear. |

---

## 8. Migration & Rollback

### Enabling OpenCode Backend

The integration is fully opt-in via configuration:

**Step 1**: Install the OpenCode binary:
```bash
go install github.com/opencode-ai/opencode@latest
# or download from releases
```

**Step 2**: Update project config TOML:
```toml
# For full OpenCode mode:
[backend]
type = "opencode"

[backend.opencode]
default_model = "anthropic/claude-sonnet-4.6"

# For mixed mode (default Claude, per-task OpenCode):
[backend]
type = "local"

[backend.opencode]
default_model = "openai/gpt-4o"
```

**Step 3**: For mixed-mode, planner can assign backend per-task via task metadata:
```json
{"type": "spawn", "agent_name": "researcher", "task_id": "abc",
 "task_metadata": {"backend": "opencode"}}
```

### Disabling / Rolling Back

**Immediate rollback**: Change `backend.type` back to `"local"` in TOML. No code changes, no data migration, no cleanup needed. All OpenCode-specific code paths are gated behind the config check.

**Partial rollback**: Remove `[backend.opencode]` section from TOML. The `OpenCodeConfig` dataclass has safe defaults (`binary_path = ""`, `default_model = ""`), so the code is inert.

**Code removal** (if permanently abandoning): Delete `src/autodev/backends/opencode.py`, `src/autodev/worker_opencode_md.md`, and `tests/test_opencode_backend.py`. Remove `OpenCodeConfig` from `config.py` and `opencode` field from `BackendConfig`. Remove `_spawn_opencode_session` from `controller.py`. Total: ~3 files deleted, ~4 files edited, all changes are self-contained.

### Safety Guarantees

1. **No existing tests break**: All new code is additive; no existing function signatures change. `parse_mc_result()` and `extract_fallback_handoff()` are untouched.
2. **No config migration**: `BackendConfig.type` defaults to `"local"`, so existing configs without `[backend.opencode]` continue to work identically.
3. **No database changes**: No schema migrations. OpenCode session IDs are stored in `WorkerHandle.backend_metadata` (already an opaque string field).
4. **Feature flag granularity**: Backend selection can be set globally (`backend.type`), per-task (`task.metadata.backend`), or per-agent-role (via planner logic). Rolling back any layer doesn't affect the others.

---

## Appendix: Key Interface Mapping

| autodev Concept | Claude Code Equivalent | OpenCode Equivalent |
|----------------|----------------------|---------------------|
| `build_claude_cmd()` | `claude -p --output-format text --model ... prompt` | `opencode run --command prompt --format json --model ...` |
| `--dangerously-skip-permissions` | Auto-approve all tools | `Permissions.AutoApproveSession()` (automatic in non-interactive) |
| `--max-turns 200` | Limit tool call iterations | No direct equivalent; agent loops until LLM stops calling tools |
| `--mcp-config path` | Load MCP servers from config | Built-in MCP client reads from `opencode.toml` / environment |
| `--output-format stream-json` | Streaming events for trace parsing | No streaming equivalent; `--format json` returns final result |
| `CLAUDE.md` | Project context file | `opencode.md` (in default context paths) |
| `--session-id <id>` | Track session for logging | `--session <id>` (resume specific session) |
| `--resume <id>` | Resume previous session | `--continue` (resume last) or `--session <id>` |
| `claude_subprocess_env()` | Restricted env allowlist | `opencode_subprocess_env()` (same base + passthrough) |
| `AD_RESULT:{}` marker | Worker output protocol | Not native; must be prompt-driven or adapter-generated |
