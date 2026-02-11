"""Planner -- decompose an objective into parallel WorkUnits via Claude."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from mission_control.config import MissionConfig
from mission_control.db import Database
from mission_control.models import Plan, Snapshot, WorkUnit, _new_id

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """You are a task planner for an autonomous development system.

## Objective
{objective}

## Project Health
- Tests: {test_passed}/{test_total} passing ({test_failed} failed)
- Lint errors: {lint_errors}
- Type errors: {type_errors}

## Discovered Issues
{discovered_issues}

## File Tree
{file_tree}

## Instructions
Decompose the objective into independent work units that can be executed in parallel
by separate Claude Code agents. Each work unit should be self-contained and modify
a small set of files.

Output a JSON array of work units:
```json
[
  {{
    "title": "Short descriptive title",
    "description": "Detailed task description with acceptance criteria",
    "files_hint": "comma,separated,file,paths",
    "verification_hint": "What to verify after this unit",
    "priority": 1,
    "depends_on_indices": []
  }}
]
```

Rules:
- Each unit should touch as few files as possible
- Use depends_on_indices to reference other units by their array index (0-based)
- Priority 1 = most important, higher = less important
- Be specific about which files to modify
- Include verification criteria for each unit
"""

_MAX_FILE_TREE_CHARS = 2000


async def create_plan(config: MissionConfig, snapshot: Snapshot, db: Database) -> Plan:
	"""Run planner agent and create a Plan with WorkUnits."""
	cwd = str(config.target.resolved_path)

	file_tree = await _get_file_tree(cwd)

	# Build discovered issues summary from snapshot
	issues: list[str] = []
	if snapshot.test_failed > 0:
		issues.append(f"- {snapshot.test_failed} failing test(s)")
	if snapshot.lint_errors > 0:
		issues.append(f"- {snapshot.lint_errors} lint error(s)")
	if snapshot.type_errors > 0:
		issues.append(f"- {snapshot.type_errors} type error(s)")
	if snapshot.security_findings > 0:
		issues.append(f"- {snapshot.security_findings} security finding(s)")
	discovered_issues = "\n".join(issues) if issues else "None"

	prompt = PLANNER_PROMPT.format(
		objective=config.target.objective,
		test_passed=snapshot.test_passed,
		test_total=snapshot.test_total,
		test_failed=snapshot.test_failed,
		lint_errors=snapshot.lint_errors,
		type_errors=snapshot.type_errors,
		discovered_issues=discovered_issues,
		file_tree=file_tree,
	)

	cmd = [
		"claude",
		"-p",
		"--output-format", "text",
		"--max-budget-usd", "2.0",
		prompt,
	]

	try:
		proc = await asyncio.create_subprocess_exec(
			*cmd,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.STDOUT,
			cwd=cwd,
		)
		stdout_bytes, _ = await proc.communicate()
		output = stdout_bytes.decode("utf-8", errors="replace")
	except (OSError, asyncio.TimeoutError) as exc:
		logger.error("Planner subprocess failed: %s", exc)
		output = ""

	plan = Plan(
		objective=config.target.objective,
		status="active",
		raw_planner_output=output,
	)

	units = _parse_plan_output(output, plan.id)
	plan.total_units = len(units)

	db.insert_plan(plan)
	for unit in units:
		db.insert_work_unit(unit)

	return plan


def _parse_plan_output(output: str, plan_id: str) -> list[WorkUnit]:
	"""Parse planner JSON output into WorkUnit objects.

	Handles:
	- JSON array in output (may be surrounded by markdown fences)
	- depends_on_indices -> depends_on (comma-separated IDs)
	- Graceful fallback on malformed JSON
	"""
	if not output.strip():
		return []

	# Try to extract JSON array -- look for ```json ... ``` fences first
	json_str: str | None = None
	fence_match = re.search(r"```(?:json)?\s*\n(\[[\s\S]*?\])\s*\n```", output)
	if fence_match:
		json_str = fence_match.group(1)
	else:
		# Fall back to finding a bare JSON array
		bracket_match = re.search(r"(\[[\s\S]*\])", output)
		if bracket_match:
			json_str = bracket_match.group(1)

	if json_str is None:
		return []

	try:
		raw_units = json.loads(json_str)
	except (json.JSONDecodeError, ValueError):
		return []

	if not isinstance(raw_units, list):
		return []

	# Generate IDs upfront so we can resolve dependency indices
	unit_ids = [_new_id() for _ in raw_units]

	units: list[WorkUnit] = []
	for i, raw in enumerate(raw_units):
		if not isinstance(raw, dict):
			continue

		# Resolve depends_on_indices to comma-separated IDs
		dep_indices = raw.get("depends_on_indices", [])
		dep_ids: list[str] = []
		if isinstance(dep_indices, list):
			for idx in dep_indices:
				if isinstance(idx, int) and 0 <= idx < len(unit_ids) and idx != i:
					dep_ids.append(unit_ids[idx])

		units.append(WorkUnit(
			id=unit_ids[i],
			plan_id=plan_id,
			title=str(raw.get("title", "")),
			description=str(raw.get("description", "")),
			files_hint=str(raw.get("files_hint", "")),
			verification_hint=str(raw.get("verification_hint", "")),
			priority=int(raw.get("priority", 1)),
			depends_on=",".join(dep_ids),
		))

	return units


async def _get_file_tree(cwd: str | Path, max_depth: int = 3) -> str:
	"""Get a truncated file tree of the project."""
	try:
		proc = await asyncio.create_subprocess_exec(
			"find", ".", "-maxdepth", str(max_depth),
			"-not", "-path", "./.git/*",
			"-not", "-path", "./.git",
			"-not", "-path", "./__pycache__/*",
			"-not", "-path", "*/__pycache__/*",
			"-not", "-path", "./.venv/*",
			"-not", "-path", "./node_modules/*",
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.DEVNULL,
			cwd=str(cwd),
		)
		stdout_bytes, _ = await proc.communicate()
		tree = stdout_bytes.decode("utf-8", errors="replace")
	except OSError:
		return "(file tree unavailable)"

	if len(tree) > _MAX_FILE_TREE_CHARS:
		tree = tree[:_MAX_FILE_TREE_CHARS] + "\n... (truncated)"
	return tree
