# Backend Abstraction Layer Analysis

Analysis of autodev's backend extension points for implementing a new `OpenCodeBackend`.

---

## 1. WorkerBackend ABC (`src/autodev/backends/base.py`)

The abstract base class defines **7 abstract methods** and **1 dataclass**:

### WorkerHandle (dataclass)

```python
@dataclass
class WorkerHandle:
	worker_id: str
	pid: int | None = None
	workspace_path: str = ""
	backend_metadata: str = ""  # opaque string for backend-specific data (e.g. SSH host info)
```

### Abstract Methods

| Method | Signature | Return | Semantics |
|--------|-----------|--------|-----------|
| `provision_workspace` | `(worker_id: str, source_repo: str, base_branch: str)` | `str` (workspace path) | Clone/prepare a workspace. Must return a path usable by git operations. |
| `spawn` | `(worker_id: str, workspace_path: str, command: list[str], timeout: int)` | `WorkerHandle` | Start the AI agent subprocess. Command is pre-built by caller. |
| `check_status` | `(handle: WorkerHandle)` | `str` (`"running"`, `"completed"`, `"failed"`) | Non-blocking poll of process state. |
| `get_output` | `(handle: WorkerHandle)` | `str` (decoded stdout) | Incremental or final stdout. Must handle still-running (partial read) and finished (drain) cases. |
| `kill` | `(handle: WorkerHandle)` | `None` | Force-stop a worker. Must clean up remote resources if applicable. |
| `release_workspace` | `(workspace_path: str)` | `None` | Return workspace to pool or delete it. |
| `cleanup` | `()` | `None` | Tear down all processes and resources (called on shutdown). |

### Non-abstract Methods in LocalBackend (Optional to Implement)

| Method | Purpose | Required for OpenCode? |
|--------|---------|----------------------|
| `initialize(warm_count)` | Pre-create workspace clones | Recommended but not in ABC |
| `_verify_workspace_health` | Pre-flight git health check | Recommended |
| `_repair_editable_install` | Fix .pth file corruption | Claude-specific (symlinked venv) |
| `_write_worker_claude_md` | Write worker-specific CLAUDE.md | Claude-specific; OpenCode needs equivalent |

---

## 2. LocalBackend Implementation (`src/autodev/backends/local.py`)

### Workspace Management
- Uses `WorkspacePool` for shared git clones (`git clone --shared`)
- `provision_workspace` flow:
  1. Acquire clone from pool
  2. Health check (.git/HEAD, `git status`, .venv symlink)
  3. `git fetch origin`
  4. `git checkout -B <base_branch> origin/<base_branch>`
  5. `rsync -a --delete` source `src/` to workspace `src/` (sync uncommitted changes)
  6. `git checkout -B mc/unit-<worker_id>` (feature branch)
  7. Write worker-specific `CLAUDE.md`
  8. Block pushes: `git config remote.origin.pushUrl no_push_allowed`
  9. Symlink source `.venv` into workspace

### Subprocess Spawning
- `spawn()` uses `asyncio.create_subprocess_exec(*command, ...)`
- `stdin=DEVNULL`, `stdout=PIPE`, `stderr=STDOUT` (merged)
- Environment from `claude_subprocess_env(config)` -- allowlisted env vars
- Tracks stdout in `_stdout_bufs` dict with incremental reads (64KB chunks)
- Output size limit: `max_output_mb` (default 50MB), kills worker on exceed
- Warning thresholds at 10/25/50 MB

### Claude-Specific Assumptions
1. `.venv` symlink to source repo's venv (workers cannot `pip install`)
2. `CLAUDE.md` written to workspace with verification command
3. `remote.origin.pushUrl = no_push_allowed` to prevent direct pushes
4. Editable install repair (.pth files pointing to stale clones)
5. `rsync` of `src/` directory (hardcoded path assumption)

### What Changes for OpenCode
- `.venv` symlink may not be needed if OpenCode uses different isolation
- Worker instructions file format changes (CLAUDE.md -> OpenCode equivalent)
- `rsync` of `src/` is project-specific, could be generalized
- Branch naming convention `mc/unit-<id>` is arbitrary, could be kept

---

## 3. Existing Backend Implementations (Precedents)

### ContainerBackend (`src/autodev/backends/container.py`)
- Wraps `docker run` around the workspace
- Host workspace is volume-mounted at `container_config.workspace_mount`
- Environment vars passed via `-e` flags
- Health check includes stale container detection (`docker inspect`)
- Kill includes `docker stop` fallback
- **Proves the pattern**: wraps arbitrary execution around the same `command: list[str]`

### SSHBackend (`src/autodev/backends/ssh.py`)
- Runs workers on remote hosts via SSH
- `provision_workspace` does `git clone --depth=1` via SSH
- `spawn` runs `ssh <target> "cd <path> && <command>"`
- Workspace path encodes host metadata: `"<path>::<json_metadata>"`
- Retry with exponential backoff on connection failures
- **Proves the pattern**: remote execution, different workspace semantics

---

## 4. Config Layer (`src/autodev/config.py`)

### Relevant Config Dataclasses

```python
@dataclass
class BackendConfig:
	type: str = "local"  # local/ssh/container
	max_output_mb: int = 50
	ssh_hosts: list[SSHHostConfig] = field(default_factory=list)
	container: ContainerConfig = field(default_factory=ContainerConfig)
```

**Extension point**: Add `opencode: OpenCodeConfig` field to `BackendConfig` and add `"opencode"` to the `type` discriminator.

### OpenCodeConfig Would Need

```python
@dataclass
class OpenCodeConfig:
	binary_path: str = ""          # path to opencode binary, or empty for PATH lookup
	output_format: str = "text"    # output format flag equivalent
	default_model: str = ""        # model override
	config_dir: str = ""           # equivalent of CLAUDE_CONFIG_DIR
	extra_args: list[str] = field(default_factory=list)  # additional CLI flags
```

### build_claude_cmd() (`config.py:1276`)

Central function that builds ALL `claude` subprocess commands. Signature:

```python
def build_claude_cmd(
	config: MissionConfig,
	*,
	model: str,
	output_format: str = "text",
	budget: float | None = None,
	max_turns: int | None = None,
	permission_mode: str | None = None,
	session_id: str | None = None,
	prompt: str | None = None,
	resume_session: str | None = None,
	allowed_tools: list[str] | None = None,
	setting_sources: str | None = None,
	json_schema: str | None = None,
) -> list[str]:
```

**Claude-specific CLI flags used:**
| Flag | Purpose | OpenCode Equivalent Needed |
|------|---------|--------------------------|
| `-p` | Non-interactive/pipe mode | Yes |
| `--output-format text\|stream-json` | Output format | Yes |
| `--model <model>` | Model selection | Yes |
| `--max-budget-usd <n>` | Cost limit | If supported |
| `--max-turns <n>` | Iteration limit | If supported |
| `--dangerously-skip-permissions` | Auto mode (when permission_mode=auto) | Yes (equivalent) |
| `--permission-mode <mode>` | Permission control | If supported |
| `--session-id <id>` | Session tracking | If supported |
| `--resume <id>` | Resume session | If supported |
| `--mcp-config <path>` | MCP server config | If supported |
| `--strict-mcp-config` | Strict MCP mode | If supported |
| `--allowedTools <tool>` | Tool allowlisting (repeated) | If supported |
| `--setting-sources project` | Restrict to project settings | If supported |
| `--json-schema <schema>` | Structured output schema | If supported |

**Key insight**: `build_claude_cmd` is the single function to replace/extend. An `OpenCodeBackend` needs a `build_opencode_cmd()` equivalent.

### claude_subprocess_env() (`config.py:1232`)

Builds a restricted environment dict:
- **Allowlist**: HOME, USER, PATH, SHELL, LANG, VIRTUAL_ENV, PYTHONPATH, GIT_*, etc.
- **Denylist**: ANTHROPIC_API_KEY, AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, DATABASE_URL, etc.
- **Extra keys**: from `config.security.extra_env_keys`

OpenCode would need the same env filtering, possibly with different allowlist entries.

### find_claude_binary() (`intelligence/utils.py:9`)

Searches for `claude` in: `shutil.which("claude")`, `~/.local/bin/claude`, `/usr/local/bin/claude`, `/opt/homebrew/bin/claude`.

OpenCode needs equivalent: `find_opencode_binary()`.

---

## 5. Swarm Controller Integration (`src/autodev/swarm/controller.py`)

### Agent Spawn Flow (`_handle_spawn` -> `_spawn_claude_session`)

1. **Decision handler** (`_handle_spawn`, line 281):
   - Validates max_agents limit
   - Creates `SwarmAgent` with role, task assignment
   - Detects stale files via `_recent_changes`
   - Calls `_build_worker_prompt()` -> `_spawn_claude_session()`

2. **Prompt builder** (`_build_worker_prompt`, line 656):
   - Delegates to `swarm/worker_prompt.py::build_worker_prompt()`
   - Injects: identity, peers, inbox instructions, skills, verification, AD_RESULT protocol

3. **Session spawner** (`_spawn_claude_session`, line 720):
   ```python
   cmd = build_claude_cmd(
       self._config,
       model=self._config.scheduler.model,
       prompt=prompt,
       setting_sources=setting_sources,
       permission_mode="auto",
       max_turns=200,
       output_format="stream-json",
   )
   env = claude_subprocess_env(self._config)
   env["AUTODEV_TEAM_NAME"] = self._team_name
   env["AUTODEV_AGENT_ID"] = agent.id
   env["AUTODEV_AGENT_NAME"] = agent.name
   env["AUTODEV_AGENT_ROLE"] = agent.role.value
   ```
   - `cwd` = target project path (not a clone -- swarm mode runs in-place)
   - `stdout=PIPE`, `stderr=PIPE` (separate, unlike LocalBackend's merged)
   - Starts background `_stream_agent_output` task for trace logging

### Claude-Specific Coupling Points in Controller
1. `build_claude_cmd()` call directly (line 737)
2. `claude_subprocess_env()` call (line 746)
3. `output_format="stream-json"` for trace parsing
4. `permission_mode="auto"` -> `--dangerously-skip-permissions`
5. `max_turns=200` -- Claude-specific iteration limit
6. Trace parsing expects `stream-json` events with `type`, `tool`, `result` fields

### What the Swarm Controller Does NOT Use
- The controller does **not** use `WorkerBackend` ABC for swarm agents
- Swarm agents are spawned directly via `asyncio.create_subprocess_exec`
- Only mission-mode workers go through the backend abstraction
- This is a significant finding: **two separate spawn paths exist**

---

## 6. Worker Prompt and AD_RESULT Protocol (`src/autodev/worker.py`, `src/autodev/session.py`)

### Prompt Templates (Mission Mode)

`WORKER_PROMPT_TEMPLATE` includes these placeholders:
- `{target_name}`, `{workspace_path}`, `{title}`, `{description}`
- `{files_hint}`, `{test_passed}`, `{test_total}`, `{lint_errors}`, `{type_errors}`
- `{branch_name}`, `{verification_hint}`, `{verification_command}`, `{context_block}`

Additional templates: `RETRY_WORKER_PROMPT_TEMPLATE`, `RESEARCH_WORKER_PROMPT_TEMPLATE`, `EXPERIMENT_WORKER_PROMPT_TEMPLATE`, `CONFLICT_RESOLUTION_PROMPT`.

### Swarm Worker Prompt (`swarm/worker_prompt.py`)

Assembles 13 sections:
1. Task prompt (from planner)
2. Identity (agent name, role, team)
3. Peers (active agents list)
4. Task pool (unclaimed tasks)
5. Inbox communication (JSON inbox protocol)
6. File conflict avoidance
7. Available skills
8. Goal fitness context
9. Verification command
10. Capabilities manifest
11. Auth request protocol
12. Skill creation instructions
13. **AD_RESULT protocol** (critical -- this is the output contract)

### AD_RESULT Format (`session.py`)

```json
{
  "status": "completed|failed|blocked",
  "commits": ["hash1", "hash2"],
  "summary": "what the worker did",
  "files_changed": ["src/foo.py"],
  "discoveries": ["key findings"],
  "concerns": ["potential issues"]
}
```

**Parsing** (`parse_mc_result`, session.py:16):
- Finds last `AD_RESULT:` marker in output
- Extracts JSON via balanced brace extraction, fallback to single-line regex
- Validates against `MCResultSchema` (Pydantic)
- Normalizes aliases: `success`->`completed`, `failure`/`error`->`failed`, `files_modified`->`files_changed`

**Fallback** (`extract_fallback_handoff`, session.py:100):
- When `AD_RESULT` is missing, recovers from raw output:
  - Commits from `git log` / `git commit` patterns
  - Files from `git diff --stat` patterns
  - Status from exit code + error pattern matching

### What's Backend-Agnostic vs Claude-Specific

**Fully agnostic** (keep as-is):
- AD_RESULT protocol and parsing
- WorkerHandle dataclass
- WorkerBackend ABC
- MCResultSchema validation
- Fallback handoff extraction
- Worker prompt templates (they're AI-model-agnostic text)
- Inbox communication system

**Claude-specific** (must be abstracted):
- `build_claude_cmd()` -- CLI flag construction
- `find_claude_binary()` -- binary discovery
- `claude_subprocess_env()` -- env allowlist (name is Claude-specific, logic is generic)
- `output_format="stream-json"` in swarm controller -- Claude's streaming format
- `--dangerously-skip-permissions` -- Claude's auto-mode flag
- `--max-turns` -- Claude-specific iteration control
- `--mcp-config` / `--strict-mcp-config` -- Claude's MCP support
- `--allowedTools` -- Claude's tool restriction
- `CLAUDE.md` worker instructions file
- `CLAUDE_CONFIG_DIR` env var
- Swarm trace parsing of stream-json events

---

## 7. Extension Points Summary

### To Add OpenCodeBackend (Mission Mode)

1. **Create `src/autodev/backends/opencode.py`** implementing `WorkerBackend` ABC
   - All 7 abstract methods
   - `initialize()` for warm pool (optional)
   - Workspace health checks

2. **Add config** to `BackendConfig`:
   ```python
   opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
   ```

3. **Add `build_opencode_cmd()`** to `config.py` (or new module):
   - Map equivalent CLI flags for OpenCode
   - Handle model, budget, permission mode, prompt passing

4. **Add `find_opencode_binary()`** to `intelligence/utils.py`

5. **Update backend factory** -- wherever `BackendConfig.type` is checked to instantiate backends

6. **Worker instructions**: Create `worker_opencode_instructions.md` template (equivalent of `worker_claude_md.md`)

### To Add OpenCode to Swarm Mode

This is harder because swarm mode **bypasses the backend abstraction** and calls `build_claude_cmd` directly in `controller.py:737`.

Options:
- A. Refactor `_spawn_claude_session` to use an abstraction (e.g., `_spawn_agent_session` that delegates to a backend)
- B. Add a parallel `_spawn_opencode_session` method and a config switch
- C. Abstract `build_claude_cmd` into a `build_agent_cmd(backend_type, ...)` dispatcher

### Key Risks

1. **stream-json format**: The swarm controller parses Claude's `stream-json` output format for trace logging and tool call tracking. OpenCode may have a different streaming format (or none).

2. **MCP config**: Claude's `--mcp-config` / `--strict-mcp-config` flags enable MCP server access. OpenCode needs equivalent or the MCP tools section of worker prompts becomes dead code.

3. **Permission auto-mode**: Claude's `--dangerously-skip-permissions` is critical for unattended operation. OpenCode needs an equivalent or agents will hang on permission prompts (stdin is DEVNULL).

4. **Tool allowlisting**: Claude's `--allowedTools` restricts what the agent can do. Without this, read-only research agents could accidentally modify files.

5. **Two spawn paths**: Mission mode uses `WorkerBackend.spawn()`, swarm mode uses direct subprocess spawning. Both need to support OpenCode for full integration.
