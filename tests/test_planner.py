"""Tests for the planner module."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from mission_control.config import (
	MissionConfig,
	SchedulerConfig,
	TargetConfig,
	VerificationConfig,
)
from mission_control.db import Database
from mission_control.models import Snapshot
from mission_control.planner import _get_file_tree, _parse_plan_output, create_plan


def _raw_units(units: list[dict[str, object]]) -> str:
	"""Serialize units to JSON string."""
	return json.dumps(units)


def _sample_units() -> list[dict[str, object]]:
	return [
		{
			"title": "Add user model",
			"description": "Create the User dataclass",
			"files_hint": "src/models.py",
			"verification_hint": "pytest passes",
			"priority": 1,
			"depends_on_indices": [],
		},
		{
			"title": "Add user API",
			"description": "Create REST endpoints for users",
			"files_hint": "src/api.py,src/routes.py",
			"verification_hint": "API tests pass",
			"priority": 2,
			"depends_on_indices": [0],
		},
	]


class TestParsePlanOutput:
	def test_parse_valid_json_plan(self) -> None:
		raw = _raw_units(_sample_units())
		units = _parse_plan_output(raw, "plan-abc")
		assert len(units) == 2
		assert units[0].title == "Add user model"
		assert units[0].plan_id == "plan-abc"
		assert units[0].files_hint == "src/models.py"
		assert units[0].priority == 1
		assert units[1].title == "Add user API"
		assert units[1].files_hint == "src/api.py,src/routes.py"
		assert units[1].priority == 2

	def test_parse_plan_with_dependencies(self) -> None:
		raw = _raw_units(_sample_units())
		units = _parse_plan_output(raw, "plan-abc")
		# Unit 0 has no deps
		assert units[0].depends_on == ""
		# Unit 1 depends on unit 0 -- resolved to unit 0's ID
		assert units[1].depends_on == units[0].id

	def test_parse_malformed_json(self) -> None:
		units = _parse_plan_output("This is not JSON at all {{{", "plan-x")
		assert units == []

	def test_parse_empty_output(self) -> None:
		units = _parse_plan_output("", "plan-x")
		assert units == []
		units2 = _parse_plan_output("   ", "plan-x")
		assert units2 == []

	def test_parse_json_in_markdown_fences(self) -> None:
		sample = _sample_units()
		fenced = (
			"Here is the plan:\n\n"
			"```json\n"
			+ json.dumps(sample, indent=2)
			+ "\n```\n\n"
			"Let me know if you need changes."
		)
		units = _parse_plan_output(fenced, "plan-md")
		assert len(units) == 2
		assert units[0].title == "Add user model"
		assert units[1].depends_on == units[0].id

	def test_parse_skips_non_dict_entries(self) -> None:
		raw = json.dumps([
			{"title": "Valid", "description": "ok", "priority": 1, "depends_on_indices": []},
			"not a dict",
			42,
		])
		units = _parse_plan_output(raw, "plan-skip")
		assert len(units) == 1
		assert units[0].title == "Valid"

	def test_parse_self_dependency_ignored(self) -> None:
		raw = json.dumps([
			{"title": "Self-ref", "description": "x", "priority": 1, "depends_on_indices": [0]},
		])
		units = _parse_plan_output(raw, "plan-self")
		assert len(units) == 1
		assert units[0].depends_on == ""

	def test_parse_out_of_range_dependency_ignored(self) -> None:
		raw = json.dumps([
			{"title": "Bad dep", "description": "x", "priority": 1, "depends_on_indices": [99]},
		])
		units = _parse_plan_output(raw, "plan-oor")
		assert len(units) == 1
		assert units[0].depends_on == ""

	def test_parse_multiple_dependencies(self) -> None:
		raw = json.dumps([
			{"title": "A", "description": "a", "priority": 1, "depends_on_indices": []},
			{"title": "B", "description": "b", "priority": 1, "depends_on_indices": []},
			{"title": "C", "description": "c", "priority": 2, "depends_on_indices": [0, 1]},
		])
		units = _parse_plan_output(raw, "plan-multi")
		assert len(units) == 3
		dep_ids = units[2].depends_on.split(",")
		assert units[0].id in dep_ids
		assert units[1].id in dep_ids


class TestGetFileTree:
	async def test_get_file_tree_truncation(self) -> None:
		# Create a mock that returns a very long output
		long_output = ("x" * 3000).encode("utf-8")
		mock_proc = AsyncMock()
		mock_proc.communicate.return_value = (long_output, None)

		with patch("mission_control.planner.asyncio.create_subprocess_exec", return_value=mock_proc):
			result = await _get_file_tree("/tmp/fake", max_depth=3)

		# Should be truncated to ~2000 chars + truncation message
		assert len(result) < 3000
		assert "... (truncated)" in result

	async def test_get_file_tree_short_output_not_truncated(self) -> None:
		short_output = ".\n./src\n./src/main.py\n".encode("utf-8")
		mock_proc = AsyncMock()
		mock_proc.communicate.return_value = (short_output, None)

		with patch("mission_control.planner.asyncio.create_subprocess_exec", return_value=mock_proc):
			result = await _get_file_tree("/tmp/fake")

		assert "... (truncated)" not in result
		assert "./src/main.py" in result

	async def test_get_file_tree_subprocess_failure(self) -> None:
		with patch("mission_control.planner.asyncio.create_subprocess_exec", side_effect=OSError("nope")):
			result = await _get_file_tree("/tmp/fake")

		assert result == "(file tree unavailable)"


class TestCreatePlanTimeout:
	async def test_timeout_kills_subprocess(self) -> None:
		config = MissionConfig(
			target=TargetConfig(
				name="test",
				path="/tmp/test",
				branch="main",
				objective="Build stuff",
				verification=VerificationConfig(command="pytest -q", timeout=10),
			),
			scheduler=SchedulerConfig(),
		)
		snapshot = Snapshot()
		db = Database(":memory:")

		# Mock the claude subprocess to timeout
		mock_proc = AsyncMock()
		mock_proc.communicate.side_effect = asyncio.TimeoutError()

		# Mock file tree subprocess (called first)
		mock_tree_proc = AsyncMock()
		mock_tree_proc.communicate.return_value = (b".\n./src\n", None)

		call_count = 0

		async def mock_exec(*args, **kwargs):
			nonlocal call_count
			call_count += 1
			if call_count == 1:
				return mock_tree_proc  # file tree call
			return mock_proc  # planner call

		with patch("mission_control.planner.asyncio.create_subprocess_exec", side_effect=mock_exec):
			plan = await create_plan(config, snapshot, db)

		# Plan should still be created (with empty output)
		assert plan.raw_planner_output == ""
		assert plan.total_units == 0
		# Subprocess should have been killed
		mock_proc.kill.assert_called_once()
		mock_proc.wait.assert_called()
