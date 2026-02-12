# mission-control - Claude Code Project Instructions

Long-running autonomous development framework. Spawns Claude Code sessions as subprocesses, manages state in SQLite, discovers work by running code, reviews output algorithmically.

## Verification

Before ANY commit, run:
```
.venv/bin/python -m pytest -q && .venv/bin/ruff check src/ tests/ && .venv/bin/python -m mypy src/mission_control --ignore-missing-imports
```

## Architecture

- `config.py` -- TOML config loader
- `models.py` -- Dataclasses (Session, Snapshot, TaskRecord, Decision, SnapshotDelta)
- `db.py` -- SQLite ops (WAL mode, all CRUD)
- `state.py` -- Project health snapshots (run verification, parse output)
- `discovery.py` -- Code-based task discovery (priority 1-7)
- `session.py` -- Claude Code subprocess spawning
- `reviewer.py` -- Algorithmic post-session review
- `memory.py` -- Context loading for sessions
- `scheduler.py` -- Main async loop
- `cli.py` -- argparse CLI

## Conventions

- Tabs for indentation
- 120 char line length
- Double quotes
- Python 3.11+
- Type hints on public functions
- Minimal comments
