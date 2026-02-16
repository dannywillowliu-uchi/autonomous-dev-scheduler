"""Tests for backlog item ID tracing through ContinuousPlanner."""

from __future__ import annotations

from unittest.mock import AsyncMock

from mission_control.config import ContinuousConfig, MissionConfig, PlannerConfig, TargetConfig
from mission_control.continuous_planner import ContinuousPlanner
from mission_control.db import Database
from mission_control.models import Mission, Plan, PlanNode, WorkUnit


def _config() -> MissionConfig:
	mc = MissionConfig()
	mc.target = TargetConfig(name="test", path="/tmp/test", objective="Build API")
	mc.planner = PlannerConfig(max_depth=2)
	mc.continuous = ContinuousConfig(backlog_min_size=2)
	return mc


def _mission() -> Mission:
	return Mission(id="m1", objective="Build a production API")


def _mock_plan_round(unit_ids: list[str]) -> AsyncMock:
	"""Create a mock plan_round that returns units with the given IDs."""
	units = [WorkUnit(id=uid, plan_id="p1", title=f"Task {uid}") for uid in unit_ids]
	plan = Plan(id="p1", objective="test")
	root = PlanNode(id="root", plan_id="p1", strategy="leaves")
	root._child_leaves = [  # type: ignore[attr-defined]
		(PlanNode(id=f"leaf-{wu.id}", node_type="leaf"), wu)
		for wu in units
	]
	return AsyncMock(return_value=(plan, root))


class TestBacklogItemIdsInContext:
	async def test_backlog_ids_passed_to_planner_context(self) -> None:
		"""Backlog item IDs appear in the enriched context sent to plan_round."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._inner.plan_round = _mock_plan_round(["wu1"])

		await planner.get_next_units(
			_mission(), backlog_item_ids=["backlog-001", "backlog-002"],
		)

		call_kwargs = planner._inner.plan_round.call_args[1]
		ctx = call_kwargs["feedback_context"]
		assert "Backlog items being worked on:" in ctx
		assert "backlog-001" in ctx
		assert "backlog-002" in ctx

	async def test_backlog_ids_appended_to_existing_feedback(self) -> None:
		"""Backlog section is appended after existing feedback context."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._inner.plan_round = _mock_plan_round(["wu1"])

		await planner.get_next_units(
			_mission(),
			feedback_context="Previous round completed auth module.",
			backlog_item_ids=["backlog-099"],
		)

		call_kwargs = planner._inner.plan_round.call_args[1]
		ctx = call_kwargs["feedback_context"]
		assert ctx.startswith("Previous round completed auth module.")
		assert "backlog-099" in ctx

	async def test_no_backlog_ids_leaves_context_unchanged(self) -> None:
		"""Without backlog IDs, context has no backlog section."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._inner.plan_round = _mock_plan_round(["wu1"])

		await planner.get_next_units(
			_mission(), feedback_context="Some feedback.",
		)

		call_kwargs = planner._inner.plan_round.call_args[1]
		ctx = call_kwargs["feedback_context"]
		assert "Backlog items being worked on:" not in ctx


class TestUnitToBacklogMapping:
	async def test_single_backlog_id_mapped_to_units(self) -> None:
		"""Units produced by replan are mapped to the provided backlog ID."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._inner.plan_round = _mock_plan_round(["wu1", "wu2"])

		await planner.get_next_units(
			_mission(), backlog_item_ids=["backlog-001"],
		)

		mapping = planner.get_backlog_mapping()
		assert mapping["wu1"] == "backlog-001"
		assert mapping["wu2"] == "backlog-001"

	async def test_multiple_backlog_ids_joined(self) -> None:
		"""Multiple backlog IDs are joined with commas in the mapping."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._inner.plan_round = _mock_plan_round(["wu1"])

		await planner.get_next_units(
			_mission(), backlog_item_ids=["b1", "b2", "b3"],
		)

		mapping = planner.get_backlog_mapping()
		assert mapping["wu1"] == "b1,b2,b3"

	async def test_no_backlog_ids_no_mapping(self) -> None:
		"""Without backlog IDs, no unit-to-backlog mapping is created."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._inner.plan_round = _mock_plan_round(["wu1"])

		await planner.get_next_units(_mission())

		assert planner.get_backlog_mapping() == {}

	async def test_mapping_accumulates_across_replans(self) -> None:
		"""Mapping grows as multiple replan cycles produce new units."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))

		# First replan
		planner._inner.plan_round = _mock_plan_round(["wu1"])
		await planner.get_next_units(
			_mission(), backlog_item_ids=["backlog-A"],
		)

		# Second replan
		planner._inner.plan_round = _mock_plan_round(["wu2"])
		await planner.get_next_units(
			_mission(), backlog_item_ids=["backlog-B"],
		)

		mapping = planner.get_backlog_mapping()
		assert mapping["wu1"] == "backlog-A"
		assert mapping["wu2"] == "backlog-B"


class TestGetBacklogMapping:
	def test_returns_copy(self) -> None:
		"""get_backlog_mapping returns a copy, not the internal dict."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._unit_to_backlog["wu1"] = "backlog-001"

		mapping = planner.get_backlog_mapping()
		mapping["wu1"] = "tampered"

		assert planner._unit_to_backlog["wu1"] == "backlog-001"

	def test_empty_initially(self) -> None:
		"""Mapping is empty on a fresh planner."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		assert planner.get_backlog_mapping() == {}


class TestPlannerWithoutBacklogIds:
	async def test_normal_replan_works(self) -> None:
		"""Planner works normally when no backlog IDs are provided."""
		planner = ContinuousPlanner(_config(), Database(":memory:"))
		planner._inner.plan_round = _mock_plan_round(["wu1", "wu2"])

		plan, units, epoch = await planner.get_next_units(_mission())

		assert len(units) == 2
		assert epoch.number == 1
		assert planner.get_backlog_mapping() == {}

	async def test_backlog_serve_works_without_ids(self) -> None:
		"""Serving from existing backlog works without backlog IDs."""
		config = _config()
		config.continuous.backlog_min_size = 1
		planner = ContinuousPlanner(config, Database(":memory:"))

		# Pre-populate backlog above min_size
		planner._backlog = [
			WorkUnit(id="wu1", title="T1"),
			WorkUnit(id="wu2", title="T2"),
		]

		plan, units, epoch = await planner.get_next_units(_mission(), max_units=1)

		assert len(units) == 1
		assert units[0].id == "wu1"
		assert planner.get_backlog_mapping() == {}
