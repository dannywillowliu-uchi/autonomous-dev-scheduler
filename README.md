# autonomous-dev-scheduler

Autonomous dev daemon that continuously improves a codebase toward a "north star" objective. Spawns parallel Claude Code workers, manages state in SQLite, and learns from its own outcomes via an RL-style feedback loop.

Point it at a repo with an objective and a verification command. It plans, executes, merges, verifies, and pushes -- in a loop -- until the objective is met or it stalls.

## How it works

```
                    ┌─────────────────────────────────┐
                    │     Continuous Controller        │
                    │  (epoch loop, wall-time budget)  │
                    └──────────────┬──────────────────┘
                                   │
           ┌───────────────────────┼───────────────────────┐
           ▼                       ▼                       ▼
    ┌─────────────┐      ┌──────────────┐       ┌──────────────┐
    │   Planner   │      │   Workers    │       │  Green Branch │
    │  (adaptive, │─────▶│  (parallel,  │──────▶│  (merge queue │
    │  replan on  │      │  workspace   │       │  + verify +   │
    │  stall)     │      │  pool)       │       │  promote)     │
    └─────────────┘      └──────────────┘       └──────┬───────┘
           ▲                                           │
           │              ┌──────────────┐             │
           └──────────────│   Feedback   │◀────────────┘
                          │  (evaluate,  │
                          │  reflect,    │
                          │  strategize) │
                          └──────────────┘
```

Each epoch:
1. **Plan** -- Planner decomposes the objective into work units, replanning when progress stalls
2. **Execute** -- Parallel Claude workers run in isolated workspace clones, each on its own feature branch
3. **Merge** -- Workers' branches queue into the merge queue and merge into `mc/working` via the green branch manager
4. **Verify + Promote** -- Verification runs on `mc/working`; passing code promotes to `mc/green` with optional deploy
5. **Evaluate** -- Score progress (test improvement, lint reduction, completion rate)
6. **Feedback** -- Record reflections, compute rewards, feed experiences to next epoch
7. **Strategize** -- Strategist proposes follow-up objectives from backlog; auto-chain missions

## Quick start

```bash
# Clone and install
git clone https://github.com/dannywillowliu-uchi/autonomous-dev-scheduler.git
cd autonomous-dev-scheduler
uv venv && uv pip install -e .

# Configure (edit to point at your repo)
cp mission-control.toml.example mission-control.toml
# Edit: target.path, target.objective, target.verification.command

# Run
.venv/bin/python -m mission_control.cli mission --config mission-control.toml --workers 2
```

## Configuration

All config lives in `mission-control.toml`:

```toml
[target]
name = "my-project"
path = "/path/to/repo"
branch = "main"
objective = "Add comprehensive test coverage for the auth module"

[target.verification]
command = "pytest -q && ruff check src/"
timeout = 120

[scheduler]
model = "opus"           # Model for all Claude subprocesses
session_timeout = 900    # Max seconds per worker session

[scheduler.budget]
max_per_session_usd = 5.0
max_per_run_usd = 100.0

[scheduler.parallel]
num_workers = 2          # Parallel Claude workers
pool_dir = "/tmp/mc-pool"

[rounds]
max_rounds = 20          # Max rounds before stopping
stall_threshold = 5      # Rounds with no improvement before stopping

[green_branch]
auto_push = true         # Push mc/green to main after each round
push_branch = "main"
fixup_max_attempts = 3
```

## Architecture

```
src/mission_control/
├── cli.py                   # CLI entrypoint (mission, init, discover, summary)
├── config.py                # TOML config loader + validation
├── models.py                # Dataclasses (Mission, WorkUnit, Epoch, Snapshot, ...)
├── db.py                    # SQLite with WAL mode
├── continuous_controller.py # Main epoch loop with pause/resume signals
├── continuous_planner.py    # Adaptive planner with replan-on-stall
├── recursive_planner.py     # Tree decomposition of objectives
├── worker.py                # Worker prompt rendering + subprocess management
├── evaluator.py             # Objective scoring (test/lint/completion metrics)
├── feedback.py              # Reflections, rewards, experiences
├── green_branch.py          # Green branch pattern (mc/working -> mc/green) + deploy
├── merge_queue.py           # Ordered merge queue for worker branches
├── memory.py                # Context loading for workers
├── session.py               # Claude subprocess spawning + output parsing
├── strategist.py            # Follow-up objective proposal + mission chaining
├── auto_discovery.py        # Gap analysis -> research -> backlog pipeline
├── priority.py              # Backlog priority scoring
├── overlap.py               # Work unit overlap detection
├── heartbeat.py             # Liveness monitoring + stale worker recovery
├── notifier.py              # Telegram notifications with batching + retry
├── token_parser.py          # Token usage tracking + cost estimation
├── json_utils.py            # Robust JSON extraction from LLM output
├── state.py                 # Mission state formatting
├── dashboard/               # TUI + live web dashboard
├── backends/
│   ├── base.py              # WorkerBackend ABC
│   ├── local.py             # Local subprocess backend with workspace pool
│   └── ssh.py               # Remote SSH backend
└── workspace.py             # Git clone pool management
```

## Key concepts

**Green branch pattern**: Workers merge into `mc/working` via a merge queue. After merging, verification runs on `mc/working`. Passing code promotes (ff-merge) to `mc/green`. Only verified code reaches `mc/green`. Optional deploy + health check after promotion.

**Continuous controller**: Runs in epochs with wall-time budgets. Supports pause/resume signals, heartbeat monitoring, and automatic stale worker recovery. Missions chain automatically via the strategist.

**Adaptive planning**: The planner decomposes objectives into work units and replans when progress stalls. Supports research units (no-commit exploration) and experiment mode for safe prototyping.

**Feedback loop**: After each epoch, the system records:
- **Reflections** -- Objective metrics (test deltas, lint reduction, completion rate)
- **Rewards** -- Composite score from objective signals (no LLM self-evaluation)
- **Experiences** -- Per-unit outcomes indexed by keywords for retrieval in future epochs

**Auto-discovery**: Pipeline that analyzes the codebase for gaps, researches best practices, and populates the backlog with prioritized improvement items.

**Workspace pool**: Parallel workers each get an isolated git clone from a pre-warmed pool. Clones are recycled between epochs.

## Tests

```bash
uv run pytest -q                           # 800+ tests
uv run ruff check src/ tests/              # Lint
uv run mypy src/mission_control --ignore-missing-imports  # Types
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` command available)
- Claude Max or API key with sufficient budget
- Git
