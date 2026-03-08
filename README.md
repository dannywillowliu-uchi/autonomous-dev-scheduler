# autodev

Autonomous development framework. Point it at a repo with an objective and a verification command. It spawns a driving planner that continuously dispatches parallel Claude Code agents, learns from outcomes, and loops until the objective is met.

```
    +-------------------+
    |  Driving Planner  |  (async LLM loop, cycles every 60s+)
    |                   |
    |  1. Monitor agents|
    |  2. Record learnings
    |  3. Detect stagnation
    |  4. Build state   |
    |  5. Call LLM      |  -> structured decisions
    |  6. Execute       |  -> spawn/kill/create_task/adjust/wait/escalate
    +--------+----------+
             |
    +--------+--------+--------+
    |        |        |        |
 [Agent]  [Agent]  [Agent]  [Agent]   (Claude Code subprocesses)
    |        |        |        |
    +--- team inbox messages ---+
    |                           |
    +---> planner reads <-------+
    |                           |
    +-> .autodev-swarm-learnings.md (persists across runs)
```

Each cycle:
1. **Monitor** -- Check agent processes for completion/failure, parse `AD_RESULT` handoffs
2. **Learn** -- Record successful approaches, failed approaches, and discoveries to persistent learnings file
3. **Re-queue** -- Failed tasks with retry budget get re-queued as pending
4. **Stagnation check** -- Detect flat test counts, rising cost with flat progress, high failure rates. Suggest pivots
5. **Plan** -- LLM receives full state snapshot (agents, tasks, test results, stagnation signals, learnings) and emits structured decisions
6. **Execute** -- Controller spawns/kills agents, creates tasks, adjusts parameters
7. **Loop** -- Stops when all tasks complete and no agents are active

Agents communicate with the planner via file-based team inboxes (`~/.claude/teams/{team}/inboxes/`). The planner reads these each cycle for visibility into in-progress work.

## Installation

```bash
git clone git@github.com:dannywillowliu-uchi/autonomous-development.git
cd autonomous-development
uv sync --extra dev
```

Or via pip:

```bash
pip install autonomous-dev
pip install autonomous-dev[mcp,dashboard,tracing]  # with extras
```

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` in PATH)
- Claude Max subscription or API key
- Git

## Quickstart

```bash
# Copy and edit the example config
cp autodev.toml.example autodev.toml
# Edit: target.path, target.objective, target.verification.command

# Launch
uv run autodev swarm --config autodev.toml

# With live TUI dashboard
uv run autodev swarm-tui --config autodev.toml
```

## Configuration

The three fields you must set:

```toml
[target]
name = "my-project"
path = "/absolute/path/to/your/repo"
objective = """What you want built or improved.
Be specific about the end state and success criteria."""

[target.verification]
command = "pytest -q && ruff check src/"   # must exit 0 when healthy
```

See [`autodev.toml.example`](autodev.toml.example) for the full annotated config. Key sections:

| Section | What it controls |
|---------|-----------------|
| `[target]` | Repo path, branch, objective, verification command |
| `[swarm]` | Max/min agents, planner model, cooldown, stagnation threshold |
| `[core_tests]` | Correctness test suite (opt-in, project-defined runner) |
| `[mcp]` | MCP server config passed to all Claude subprocesses |
| `[scheduler]` | Model choice, budget limits, session timeout |
| `[backend]` | Execution backend (local, SSH, or container) |
| `[heartbeat]` | Progress monitoring interval, idle alerts |

## CLI

```bash
# Swarm mode (default)
autodev swarm --config autodev.toml [--max-agents N]
autodev swarm-tui --config autodev.toml    # with live TUI dashboard

# Mission mode (epoch-based, legacy)
autodev mission --config autodev.toml [--workers N] [--chain] [--approve-all]

# Status and history
autodev status --config autodev.toml
autodev summary --config autodev.toml
autodev history --config autodev.toml

# Dashboards
autodev live --config autodev.toml [--port 8080]    # Web (FastAPI + HTMX)
autodev dashboard --config autodev.toml              # TUI (mission mode)

# Setup and diagnostics
autodev init --config autodev.toml
autodev validate-config --config autodev.toml
autodev diagnose --config autodev.toml

# Multi-project registry
autodev register --config autodev.toml
autodev unregister --config autodev.toml
autodev projects

# External interfaces
autodev mcp --config autodev.toml     # MCP server (stdio)
autodev a2a --config autodev.toml     # Agent-to-Agent protocol

# Intelligence and tracing
autodev intel                                  # Scan external AI/agent ecosystem sources
autodev trace --file trace.jsonl               # Read trace file as human-readable timeline
```

> When running from source, prefix with `uv run`: `uv run autodev swarm --config autodev.toml`

## Key concepts

**Driving planner**: Stateless LLM loop. Each cycle: build state snapshot, call LLM, parse structured decisions (spawn, kill, create_task, adjust, wait, escalate), execute via controller. Intelligence comes from context, not persistent session state.

**Team inbox messaging**: File-based JSON inboxes at `~/.claude/teams/{team}/inboxes/`. Agents report progress to the planner; planner sends directives to agents. Enables visibility into in-progress work without waiting for process exit.

**Stagnation detection**: Tracks test pass rates, completion counts, failure counts, and cost over sliding windows. Three heuristics: flat test count (switch to research), rising cost with flat progress (reduce agents), high failure rate (spawn diagnostic agent).

**Persistent learnings**: Accumulates discoveries, successful approaches, failed approaches, and stagnation pivots in `.autodev-swarm-learnings.md`. The planner reads this each cycle. Entries are deduplicated and bounded to ~200 lines. Persists across runs.

**Agent lifecycle guards**: Minimum 5-minute age before an agent can be killed. Task retry budgets (default 3 attempts). Scaling recommendations based on pending/active ratios.

**Core test feedback loop**: Optional correctness signal. The controller runs a project-defined test command and feeds pass/fail/regression diagnostics back to the planner. Results include failure details and skip analysis, so the planner can trace failures to root causes and prioritize high-leverage fixes.

**AD_RESULT protocol**: Workers emit structured `AD_RESULT:{json}` handoffs on stdout with status, commits, files changed, discoveries, and concerns. The controller parses these on process exit.

## Architecture

```
src/autodev/
+-- cli.py                    # CLI entry point (argparse subcommands)
+-- config.py                 # TOML config loader + dataclasses (30+ sections)
+-- models.py                 # Domain models (Mission, WorkUnit, Epoch, Experience, ...)
+-- db.py                     # SQLite with WAL mode + schema migrations
+-- # Swarm mode (default)
+-- swarm/
|   +-- controller.py         # Agent lifecycle, task pool, team messaging
|   +-- planner.py            # Driving planner: observe -> reason -> decide loop
|   +-- models.py             # Swarm data models (agents, tasks, decisions)
|   +-- prompts.py            # Planner system/cycle prompts
|   +-- worker_prompt.py      # Worker prompt builder with inbox instructions
|   +-- context.py            # Context synthesizer: state rendering for planner
|   +-- stagnation.py         # Stagnation detection and pivot suggestions
|   +-- learnings.py          # Persistent cross-run learnings
|   +-- tui.py                # Swarm TUI dashboard with activity feed
+-- # Mission mode (legacy)
+-- continuous_controller.py  # Epoch-based orchestration loop
+-- continuous_planner.py     # Adaptive planner wrapper around RecursivePlanner
+-- recursive_planner.py      # LLM-based tree decomposition with PLAN_RESULT marker
+-- deliberative_planner.py   # Planner + critic deliberation rounds
+-- critic_agent.py           # LLM critic for plan review
+-- planner_context.py        # Planner context builder + MISSION_STATE.md writer
+-- context_gathering.py      # Pre-planning codebase + backlog context
+-- batch_analyzer.py         # Heuristic pattern detection (hotspots, failures, stalls)
+-- core_tests.py             # Core test runner integration + experience storage
+-- green_branch.py           # autodev/green branch lifecycle, merge, fixup agents
+-- # Workers (shared)
+-- worker.py                 # Worker prompt rendering + AD_RESULT handoff parsing
+-- session.py                # Claude subprocess spawning + output parsing
+-- feedback.py               # Worker context from past experiences
+-- overlap.py                # File overlap detection + dependency injection
+-- workspace.py              # Worker workspace management
+-- # Quality and safety
+-- diff_reviewer.py          # Fire-and-forget LLM diff review
+-- grading.py                # Deterministic decomposition grading
+-- criteria_validator.py     # Acceptance criteria validation
+-- circuit_breaker.py        # Circuit breaker state machine
+-- degradation.py            # Graceful degradation strategies
+-- adaptive_concurrency.py   # Dynamic worker count adjustment
+-- path_security.py          # File path validation + sanitization
+-- # Infrastructure
+-- launcher.py               # Mission launch orchestration
+-- heartbeat.py              # Time-based progress monitor + alerts
+-- notifier.py               # Telegram notifications
+-- hitl.py                   # Human-in-the-loop approval gates
+-- memory.py                 # Episodic/semantic context store
+-- state.py                  # Mission state management
+-- snapshot.py               # State snapshots for recovery
+-- checkpoint.py             # Checkpoint/restore support
+-- causal.py                 # Causal analysis of failures
+-- prompt_evolution.py       # Prompt A/B testing with UCB1 bandit
+-- tool_synthesis.py         # Dynamic tool creation for workers
+-- # External interfaces
+-- mcp_server.py             # MCP server for Claude Code integration
+-- a2a.py                    # Agent-to-Agent protocol server
+-- registry.py               # Multi-project registry
+-- mcp_registry.py           # MCP tool registry
+-- event_stream.py           # Server-Sent Events for dashboard
+-- trace_log.py              # Structured trace logging
+-- tracing.py                # OpenTelemetry integration
+-- diagnose.py               # Operational health checks
+-- mission_report.py         # Post-mission report generation
+-- intelligence/
|   +-- evaluator.py          # Intelligence evaluation
|   +-- scanner.py            # External source scanning
|   +-- sources.py            # HN, GitHub, arXiv feeds
+-- dashboard/
|   +-- live.py               # FastAPI + HTMX web dashboard
|   +-- tui.py                # Terminal UI (mission mode)
|   +-- provider.py           # Dashboard data provider
+-- backends/
|   +-- local.py              # Local subprocess backend with workspace pool
|   +-- ssh.py                # Remote SSH backend
|   +-- container.py          # Container backend
```

## Setting up a new project

1. **Create a config file**:
   ```bash
   cp autodev.toml.example my-project/autodev.toml
   ```

2. **Edit the required fields**:
   ```toml
   [target]
   name = "my-project"
   path = "/absolute/path/to/my-project"
   objective = """Build a REST API with user auth, CRUD endpoints, and tests."""

   [target.verification]
   command = "pytest -q && ruff check src/"
   setup_command = "uv sync --extra dev"
   ```

3. **Optionally add a core test suite** for correctness feedback:
   ```toml
   [core_tests]
   enabled = true
   runner_command = "python tests/core/runner.py"
   baseline_path = "tests/core/baseline.json"
   ```
   The runner must produce a `results.json` with this schema:
   ```json
   {
     "summary": {"total": 100, "passed": 70, "failed": 10, "skipped": 20},
     "tests": {"test_name": {"status": "PASS|FAIL|SKIP", "category": "...", "error_msg": "...", "diagnostic": "..."}},
     "deltas": {"newly_passing": [], "newly_failing": [], "newly_compiling": []},
     "skip_analysis": {"error pattern": {"count": 10, "examples": ["test1", "test2"]}}
   }
   ```

4. **Launch**:
   ```bash
   uv run autodev swarm --config my-project/autodev.toml
   ```

### Tips for writing objectives

- Be specific about the end state, not the steps
- Include language/framework constraints: "in Python using FastAPI"
- Include success criteria: "all tests pass, ruff clean"
- Broad objectives work well -- the planner handles decomposition

## Mission mode (legacy)

Mission mode (`autodev mission`) is the original epoch-based execution mode. It uses deliberative planning with a critic agent, green branch merging, and batch analysis. Still fully supported but swarm mode is recommended for most use cases.

```
    [Research Phase] -> MISSION_STRATEGY.md
              |
    +-------------------+
    |  Orchestration    |
    |  Loop (per epoch) |
    |  plan -> ambition gate -> dispatch -> merge -> reflect
    +--------+----------+
             |
    Layer 0: [W1] [W2] [W3]  (parallel)
             barrier
    Layer 1: [W4]             (sequential deps)
             |
    Green Branch Merge -> verify -> fixup if failed
             |
    Reflection -> MISSION_STATE.md -> next epoch
```

Key concepts specific to mission mode:

**Recursive planner**: Decomposes objectives into a tree of work units with acceptance criteria and dependencies. A critic agent reviews plans before execution.

**Green branch pattern**: Workers commit to isolated unit branches. Completed units merge to `autodev/green`. Pre-merge verification gates the merge; failures trigger fixup agents.

**Ambition gate**: Plans scored on ambition (1-10). Below threshold, forced to replan.

**Mission chaining**: With `--chain`, after completion, a new objective is proposed and a new mission starts automatically.

## MCP Server

The MCP server lets Claude Code (or any MCP client) control missions from chat.

Add to your Claude Code MCP config (`~/.claude.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "autodev": {
      "command": "uv",
      "args": ["run", "autodev", "mcp", "--config", "/absolute/path/to/autodev.toml"]
    }
  }
}
```

Available tools: `list_projects`, `get_project_status`, `launch_mission`, `stop_mission`, `retry_unit`, `adjust_mission`, `register_project`, `get_round_details`, `web_research`.

## Tests

```bash
uv run pytest -q                                  # 2,200+ tests
uv run ruff check src/ tests/                     # Lint
uv run mypy src/autodev --ignore-missing-imports   # Types
```

## Example: C compiler

We used autodev to build a [C compiler](https://github.com/dannywillowliu-uchi/C_compiler_orchestrated) from scratch. Zero human-written compiler code. The core test feedback loop ran against GCC torture tests, and the planner autonomously identified the highest-leverage compiler bugs each epoch.

| Metric | Value |
|--------|-------|
| Wall time | ~5 hours |
| API cost | ~$55 |
| Workers | 4 parallel |
| Units merged | ~35 |
| Source code | 8,106 lines (16 modules) |
| Test code | 22,309 lines (42 test files) |
| Tests passing | 1,788 |
| GCC torture tests | 221/221 (100%) |
| Human-written compiler code | 0 lines |

The final push from 216/221 to 221/221 was done using swarm mode, which autonomously diagnosed and fixed the remaining 5 torture test failures in a single run.

## License

MIT
