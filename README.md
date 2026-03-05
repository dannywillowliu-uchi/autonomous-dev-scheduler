# autonomous-development

Autonomous dev daemon that continuously improves a codebase toward a defined objective. Spawns parallel Claude Code workers, manages state in SQLite, and learns from its own outcomes across missions.

Point it at a repo with an objective and a verification command. It plans, executes, merges, verifies, and pushes -- in a loop -- until the objective is met or it stalls.

## How it works

```
                        Mission Start
                             |
                     [Research Phase]
                 3 parallel agents investigate
                 codebase, domain, prior art
                             |
                   Synthesis -> MISSION_STRATEGY.md
                             |
              +--------------+--------------+
              |                             |
              v                             |
    +-------------------+                   |
    |  Orchestration    |                   |
    |  Loop (per epoch) |                   |
    |                   |                   |
    |  1. Stop check    |                   |
    |  2. Reflect       |--- stop -----> [Final Verify]
    |  3. Plan          |                   |
    |  4. Ambition gate |                [Evaluator]
    |  5. Dispatch      |                   |
    +--------+----------+             pass / fail
             |                         /        \
             v                   [Chain] --> [Done]
    +-------------------+            \
    |  Layered          |         [Replan]
    |  Execution        |
    |                   |
    |  Layer 0: [W1] [W2] [W3]  (parallel)
    |           barrier
    |  Layer 1: [W4]             (sequential deps)
    |           barrier
    +--------+----------+
             |
             v
    +-------------------+
    |  Green Branch     |
    |  Merge            |
    |                   |
    |  merge to mc/green
    |  verify (pytest)  |
    |  fail? -> fixup   |
    +--------+----------+
             |
             v
    +-------------------+
    |  Reflection       |
    |                   |
    |  Batch analysis   |  (hotspots, failures, stalls)
    |  Strategic review |  (LLM synthesis)
    |  Update state     |  -> MISSION_STATE.md
    +--------+----------+
             |
             +-----> back to Orchestration Loop
```

Each epoch:
1. **Plan** -- Recursive planner reads `MISSION_STATE.md` from disk, decomposes the objective into work units with acceptance criteria and dependency ordering
2. **Ambition gate** -- Reject trivially scoped plans (configurable threshold) and force replanning
3. **Layered execution** -- Work units dispatch in topological layers (parallel within layers, sequential across). Workers run as Claude Code subprocesses
4. **Green branch merge** -- Completed units merge to `mc/green`. Pre-merge verification gates the merge; failures trigger fixup agents
5. **Handoff ingestion** -- Workers emit structured `MC_RESULT` handoffs with files changed, concerns, discoveries
6. **Batch analysis** -- Pattern detection: file hotspots, failure clusters, stalled areas, effort distribution
7. **State update** -- Fixed-size `MISSION_STATE.md` with progress counts, active issues, patterns, and files modified
8. **Core test feedback** (optional) -- Run a project-defined test suite and feed pass/fail/regression data back to the planner
9. **Loop** -- Back to planning with updated state. Stops on wall time limit, stall detection, or empty plan

## Installation

Install from source:

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

## Quickstart

```bash
# Copy and edit the example config
cp mission-control.toml.example mission-control.toml
# Edit: target.path, target.objective, target.verification.command

# Launch
uv run mc mission --config mission-control.toml
```

That's it. It will plan, dispatch parallel workers, merge results, and loop until the objective is met or it stalls.

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` in PATH)
- Claude Max subscription or API key
- Git

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

See [`mission-control.toml.example`](mission-control.toml.example) for the full annotated config. Key sections:

| Section | What it controls |
|---------|-----------------|
| `[target]` | Repo path, branch, objective, verification command |
| `[scheduler]` | Model choice, worker count, budget limits, session timeout |
| `[planner]` | Decomposition depth, max units per round, deliberation with critic |
| `[continuous]` | Wall time limit, ambition gate, pre-merge verification, reconcile interval |
| `[green_branch]` | Working/green branch names, auto-push target, fixup retries |
| `[rounds]` | Max planning rounds, stall detection threshold |
| `[heartbeat]` | Progress monitoring interval, idle alerts |
| `[discovery]` | Auto-discover next objective after completion |
| `[backend]` | Execution backend (local or SSH) |
| `[core_tests]` | Per-epoch correctness test suite (opt-in, project-defined runner) |

## CLI

> If installed via pip, use `mc` directly. If running from source, use `uv run mc`.

```bash
# Run a mission
mc mission --config mission-control.toml [--workers N] [--chain] [--approve-all]

# Status and history
mc status --config mission-control.toml
mc summary --config mission-control.toml
mc history --config mission-control.toml

# Dashboards
mc live --config mission-control.toml [--port 8080]    # Web (FastAPI + HTMX)
mc dashboard --config mission-control.toml              # TUI

# Setup and diagnostics
mc init --config mission-control.toml
mc validate-config --config mission-control.toml
mc diagnose --config mission-control.toml

# Multi-project registry
mc register --config mission-control.toml
mc unregister --config mission-control.toml
mc projects

# External interfaces
mc mcp --config mission-control.toml     # MCP server (stdio)
mc a2a --config mission-control.toml     # Agent-to-Agent protocol

# Intelligence
mc intel                                  # Scan external AI/agent ecosystem sources
mc trace --file trace.jsonl               # Read trace file as human-readable timeline
```

### Make targets

| Target | Description |
|--------|-------------|
| `make setup` | Create venv, install all deps |
| `make test` | Run pytest and ruff |
| `make traces` | Start Jaeger (OTLP on :4317/:4318, UI on :16686) |
| `make dashboard` | Start live web dashboard on :8080 |
| `make run` | Run a mission with default config |
| `make clean` | Stop Docker containers |

## Architecture

```
src/mission_control/
+-- cli.py                    # CLI entry point (argparse subcommands)
+-- config.py                 # TOML config loader + dataclasses
+-- models.py                 # Domain models (Mission, WorkUnit, Epoch, Experience, ...)
+-- db.py                     # SQLite with WAL mode + schema migrations
+-- # Core loop
+-- continuous_controller.py  # Main orchestration loop: plan -> execute -> merge -> reflect
+-- continuous_planner.py     # Adaptive planner wrapper around RecursivePlanner
+-- recursive_planner.py      # LLM-based tree decomposition with PLAN_RESULT marker
+-- deliberative_planner.py   # Planner + critic deliberation rounds
+-- critic_agent.py           # LLM critic for plan review
+-- planner_context.py        # Planner context builder + MISSION_STATE.md writer
+-- context_gathering.py      # Pre-planning codebase + backlog context
+-- batch_analyzer.py         # Heuristic pattern detection (hotspots, failures, stalls)
+-- core_tests.py             # Per-epoch core test runner integration + experience storage
+-- # Workers
+-- worker.py                 # Worker prompt rendering + MC_RESULT handoff parsing
+-- feedback.py               # Worker context from past experiences
+-- overlap.py                # File overlap detection + dependency injection
+-- workspace.py              # Worker workspace management
+-- # Merge pipeline
+-- green_branch.py           # mc/green branch lifecycle, merge, fixup agents
+-- # Quality
+-- diff_reviewer.py          # Fire-and-forget LLM diff review
+-- grading.py                # Deterministic decomposition grading
+-- criteria_validator.py     # Acceptance criteria validation
+-- # Infrastructure
+-- session.py                # Claude subprocess spawning + output parsing
+-- launcher.py               # Mission launch orchestration
+-- heartbeat.py              # Time-based progress monitor + alerts
+-- notifier.py               # Telegram notifications
+-- hitl.py                   # Human-in-the-loop approval gates
+-- degradation.py            # Graceful degradation strategies
+-- circuit_breaker.py        # Circuit breaker state machine
+-- adaptive_concurrency.py   # Dynamic worker count adjustment
+-- ema.py                    # Exponential moving average budget tracking
+-- memory.py                 # Typed context store for workers
+-- state.py                  # Mission state management
+-- snapshot.py               # State snapshots for recovery
+-- checkpoint.py             # Checkpoint/restore support
+-- causal.py                 # Causal analysis of failures
+-- prompt_evolution.py       # Worker prompt adaptation over time
+-- tool_synthesis.py         # Dynamic tool creation for workers
+-- token_parser.py           # Structured output parsing
+-- path_security.py          # File path validation + sanitization
+-- json_utils.py             # Safe JSON parsing utilities
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
|   +-- tui.py                # Terminal UI
|   +-- provider.py           # Dashboard data provider
+-- backends/
|   +-- local.py              # Local subprocess backend with workspace pool
|   +-- ssh.py                # Remote SSH backend
|   +-- container.py          # Container backend
```

## Key concepts

**Recursive planner**: Decomposes objectives into a tree of work units with acceptance criteria and dependencies. File overlap detection automatically adds dependency edges between units that touch the same files. A critic agent reviews plans before execution.

**Green branch pattern**: Workers commit to isolated unit branches. Completed units merge to `mc/green`. Pre-merge verification (pytest/ruff/etc.) gates the merge; failures trigger fixup agents that attempt automated repairs. Once green, auto-push to main.

**Fixed-size MISSION_STATE.md**: Progress summary that stays constant size regardless of mission length. Contains progress counts, active issues, strategy summary, and files modified. The planner reads this from disk each epoch rather than receiving growing context.

**Core test feedback loop**: Optional per-epoch correctness signal. When `[core_tests]` is enabled, the controller runs a project-defined test command after each epoch and feeds pass/fail/regression diagnostics back to the planner. Results include full failure details and skip analysis (what missing features block the most tests), so the planner can trace failures to root causes and prioritize high-leverage fixes. Results persist as experiences for cross-mission learning. Any project can plug in its own runner -- the framework is agnostic to what the tests do.

**Ambition gate**: Plans are scored on ambition (1-10). Below the threshold, the planner is forced to replan, preventing trivially scoped busywork.

**Batch analysis**: After each epoch, heuristic pattern detection runs on the DB: file hotspots (files touched by 3+ units), failure clusters, stalled areas, effort distribution. This feeds back into the next planning round.

**Graceful degradation**: Circuit breakers track failure rates per component. When tripped, the system falls back to simpler strategies instead of failing outright. Adaptive concurrency adjusts worker count based on success rates.

**Mission chaining**: With `--chain`, after a mission completes, a new objective is proposed and a new mission starts automatically.

## Setting up a new project

1. **Create a config file**:
   ```bash
   cp mission-control.toml.example my-project/mission-control.toml
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

3. **Optionally add a core test suite** for per-epoch correctness feedback:
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
   uv run mc mission --config my-project/mission-control.toml
   ```

### Tips for writing objectives

- Be specific about the end state, not the steps
- Include language/framework constraints: "in Python using FastAPI"
- Include success criteria: "all tests pass, ruff clean"
- Broad objectives work well -- the planner handles decomposition

## MCP Server

The MCP server lets Claude Code (or any MCP client) control missions from chat.

Add to your Claude Code MCP config (`~/.claude.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "mission-control": {
      "command": "uv",
      "args": ["run", "mc", "mcp", "--config", "/absolute/path/to/mission-control.toml"]
    }
  }
}
```

Available tools: `list_projects`, `get_project_status`, `launch_mission`, `stop_mission`, `retry_unit`, `adjust_mission`, `register_project`, `get_round_details`, `web_research`.

## Tests

```bash
uv run pytest -q                                          # 2,200+ tests
uv run ruff check src/ tests/                             # Lint
uv run mypy src/mission_control --ignore-missing-imports  # Types
```

## Example: C compiler (ongoing)

We're using mission-control to build a [C compiler](https://github.com/dannywillowliu-uchi/C_compiler_orchestrated) from scratch -- zero human-written code. With the core test feedback loop running against 225 real GCC torture tests, the planner autonomously identifies the highest-leverage compiler bugs each epoch and plans targeted fixes. Current status: 70/225 GCC torture tests passing, 2,800+ unit tests, growing each mission run.
