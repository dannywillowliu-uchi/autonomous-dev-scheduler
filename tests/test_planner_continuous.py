"""Tests for ContinuousPlanner and planner_context module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from mission_control.config import ContinuousConfig, MissionConfig, PlannerConfig, TargetConfig
from mission_control.continuous_planner import ContinuousPlanner
from mission_control.db import Database
from mission_control.models import Epoch, Handoff, Mission, Plan, PlanNode, WorkUnit
from mission_control.planner_context import build_planner_context, update_mission_state

# --- Helpers for ContinuousPlanner tests ---

def _config() -> MissionConfig:
	mc = MissionConfig()
	mc.target = TargetConfig(name="test", path="/tmp/test", objective="Build API")
	mc.planner = PlannerConfig(max_depth=2)
	mc.continuous = ContinuousConfig()
	return mc


def _mission() -> Mission:
	return Mission(id="m1", objective="Build a production API")


# === ContinuousPlanner tests ===


class TestCausalContextAndSnapshotDelegation:
	def test_set_causal_context_delegates(self) -> None:
		"""set_causal_context delegates to inner planner."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)
		planner.set_causal_context("model=opus: 9% failure")
		assert planner._inner._causal_risks == "model=opus: 9% failure"

	def test_set_project_snapshot_delegates(self) -> None:
		"""set_project_snapshot delegates to inner planner."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)
		planner.set_project_snapshot("src/ has 20 files")
		assert planner._inner._project_snapshot == "src/ has 20 files"

	def test_set_strategy(self) -> None:
		"""set_strategy stores the research phase strategy."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)
		planner.set_strategy("Use JWT auth with refresh tokens")
		assert planner._strategy == "Use JWT auth with refresh tokens"


class TestGetNextUnits:
	async def test_invokes_planner_every_time(self) -> None:
		"""Every call invokes the LLM (no backlog)."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)

		mock_wu = WorkUnit(id="wu1", plan_id="p1", title="Task 1")
		mock_plan = Plan(id="p1", objective="test")
		mock_root = PlanNode(id="root", plan_id="p1", node_type="branch", strategy="leaves")
		mock_root._child_leaves = [  # type: ignore[attr-defined]
			(PlanNode(id="leaf1", node_type="leaf"), mock_wu),
		]
		planner._inner.plan_round = AsyncMock(return_value=(mock_plan, mock_root))

		mission = _mission()
		plan, units, epoch = await planner.get_next_units(mission, max_units=3)

		assert len(units) == 1
		assert units[0].title == "Task 1"
		assert epoch.number == 1
		planner._inner.plan_round.assert_called_once()

	async def test_epoch_increments(self) -> None:
		"""Each call creates a new epoch."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)

		call_count = 0

		async def mock_plan_round(**kwargs):
			nonlocal call_count
			call_count += 1
			plan = Plan(id=f"p{call_count}", objective="test")
			root = PlanNode(id=f"root{call_count}", plan_id=plan.id, strategy="leaves")
			wu = WorkUnit(id=f"wu{call_count}", title=f"Task {call_count}")
			root._child_leaves = [(PlanNode(id=f"l{call_count}", node_type="leaf"), wu)]  # type: ignore[attr-defined]
			return plan, root

		planner._inner.plan_round = AsyncMock(side_effect=mock_plan_round)
		mission = _mission()

		_, _, epoch1 = await planner.get_next_units(mission)
		assert epoch1.number == 1
		_, _, epoch2 = await planner.get_next_units(mission)
		assert epoch2.number == 2

	async def test_empty_plan_returns_empty(self) -> None:
		"""Empty plan from LLM returns empty units."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)

		mock_plan = Plan(id="p1", objective="test")
		mock_root = PlanNode(id="root", plan_id="p1", strategy="leaves")
		planner._inner.plan_round = AsyncMock(return_value=(mock_plan, mock_root))

		mission = _mission()
		plan, units, epoch = await planner.get_next_units(mission, max_units=3)
		assert len(units) == 0

	async def test_limits_to_max_units(self) -> None:
		"""Only returns max_units even if planner generates more."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)

		mock_units = [WorkUnit(id=f"wu{i}", title=f"Task {i}") for i in range(5)]
		mock_plan = Plan(id="p1", objective="test")
		mock_root = PlanNode(id="root", plan_id="p1", strategy="leaves")
		mock_root._child_leaves = [  # type: ignore[attr-defined]
			(PlanNode(id=f"l{i}", node_type="leaf"), wu)
			for i, wu in enumerate(mock_units)
		]
		planner._inner.plan_round = AsyncMock(return_value=(mock_plan, mock_root))

		mission = _mission()
		plan, units, epoch = await planner.get_next_units(mission, max_units=2)
		assert len(units) == 2

	async def test_knowledge_context_passed_to_planner(self) -> None:
		"""Knowledge context is included in the feedback."""
		config = _config()
		db = Database(":memory:")
		planner = ContinuousPlanner(config, db)

		mock_plan = Plan(id="p1", objective="test")
		mock_root = PlanNode(id="root", plan_id="p1", strategy="leaves")
		wu = WorkUnit(id="wu1", title="Task")
		mock_root._child_leaves = [(PlanNode(id="l1", node_type="leaf"), wu)]  # type: ignore[attr-defined]
		planner._inner.plan_round = AsyncMock(return_value=(mock_plan, mock_root))

		mission = _mission()
		await planner.get_next_units(
			mission,
			knowledge_context="JWT auth is used, No refresh tokens",
		)

		call_kwargs = planner._inner.plan_round.call_args[1]
		feedback = call_kwargs.get("feedback_context", "")
		assert "JWT auth is used" in feedback
		assert "Accumulated Knowledge" in feedback


# === planner_context tests ===


class TestBuildPlannerContext:
	def test_no_handoffs_returns_empty(self, config: MissionConfig, db: Database) -> None:
		db.insert_mission(Mission(id="m1", objective="test"))
		result = build_planner_context(db, "m1")
		assert result == ""

	def test_failed_handoff_appears_in_recent_failures(self, config: MissionConfig, db: Database) -> None:
		db.insert_mission(Mission(id="m1", objective="test"))
		epoch = Epoch(id="ep1", mission_id="m1", number=1)
		db.insert_epoch(epoch)
		plan = Plan(id="p1", objective="test")
		db.insert_plan(plan)
		unit = WorkUnit(id="wu1", plan_id="p1", title="Task")
		db.insert_work_unit(unit)
		handoff = Handoff(
			id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
			status="failed", summary="Broke",
			concerns=["Something went wrong"],
		)
		db.insert_handoff(handoff)

		result = build_planner_context(db, "m1")
		assert "## Recent Failures" in result
		assert "Something went wrong" in result

	def test_completed_only_no_failures(self, config: MissionConfig, db: Database) -> None:
		db.insert_mission(Mission(id="m1", objective="test"))
		epoch = Epoch(id="ep1", mission_id="m1", number=1)
		db.insert_epoch(epoch)
		plan = Plan(id="p1", objective="test")
		db.insert_plan(plan)
		unit = WorkUnit(id="wu1", plan_id="p1", title="Task")
		db.insert_work_unit(unit)
		handoff = Handoff(
			id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
			status="completed", summary="Did the thing",
		)
		db.insert_handoff(handoff)

		result = build_planner_context(db, "m1")
		# No failures, no semantic memories -> empty
		assert result == ""

	def test_nonexistent_mission_returns_empty(self, db: Database) -> None:
		result = build_planner_context(db, "nonexistent")
		assert result == ""

	def test_db_error_returns_empty(self, config: MissionConfig) -> None:
		"""If db.get_recent_handoffs raises, returns empty string."""
		mock_db = MagicMock()
		mock_db.get_recent_handoffs.side_effect = RuntimeError("DB down")
		mock_db.get_top_semantic_memories.return_value = []
		result = build_planner_context(mock_db, "m1")
		assert result == ""


class TestUpdateMissionState:
	def test_writes_mission_state_file(self, config: MissionConfig, db: Database, tmp_path: Path) -> None:
		config.target.path = str(tmp_path)
		db.insert_mission(Mission(id="m1", objective="Build the thing"))

		mission = Mission(id="m1", objective="Build the thing")
		update_mission_state(db, mission, config)

		state_path = tmp_path / "MISSION_STATE.md"
		assert state_path.exists()
		content = state_path.read_text()
		assert "# Mission State" in content
		assert "Build the thing" in content
		assert "## Progress" in content

	def test_includes_progress_counts(self, config: MissionConfig, db: Database, tmp_path: Path) -> None:
		config.target.path = str(tmp_path)
		db.insert_mission(Mission(id="m1", objective="test"))
		epoch = Epoch(id="ep1", mission_id="m1", number=1)
		db.insert_epoch(epoch)
		plan = Plan(id="p1", objective="test")
		db.insert_plan(plan)
		wu1 = WorkUnit(
			id="wu1", plan_id="p1", title="Task 1",
			status="completed", finished_at="2025-01-01T12:00:00", epoch_id="ep1",
		)
		wu2 = WorkUnit(id="wu2", plan_id="p1", title="Task 2", status="failed", epoch_id="ep1")
		db.insert_work_unit(wu1)
		db.insert_work_unit(wu2)

		mission = Mission(id="m1", objective="test")
		update_mission_state(db, mission, config)

		content = (tmp_path / "MISSION_STATE.md").read_text()
		assert "1 tasks complete, 1 failed" in content
		assert "Epoch 1" in content

	def test_includes_active_issues(self, config: MissionConfig, db: Database, tmp_path: Path) -> None:
		config.target.path = str(tmp_path)
		db.insert_mission(Mission(id="m1", objective="test"))
		epoch = Epoch(id="ep1", mission_id="m1", number=1)
		db.insert_epoch(epoch)
		plan = Plan(id="p1", objective="test")
		db.insert_plan(plan)
		unit = WorkUnit(id="wu1", plan_id="p1", title="Task")
		db.insert_work_unit(unit)
		handoff = Handoff(
			id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
			status="failed", summary="Broke",
			concerns=["Something went wrong"],
		)
		db.insert_handoff(handoff)

		mission = Mission(id="m1", objective="test")
		update_mission_state(db, mission, config)

		content = (tmp_path / "MISSION_STATE.md").read_text()
		assert "## Active Issues" in content
		assert "Something went wrong" in content

	def test_includes_strategy(self, config: MissionConfig, db: Database, tmp_path: Path) -> None:
		config.target.path = str(tmp_path)
		db.insert_mission(Mission(id="m1", objective="test"))

		mission = Mission(id="m1", objective="test")
		update_mission_state(db, mission, config, strategy="Use JWT auth with refresh tokens")

		content = (tmp_path / "MISSION_STATE.md").read_text()
		assert "## Strategy" in content
		assert "JWT auth" in content

	def test_files_modified_grouped(self, config: MissionConfig, db: Database, tmp_path: Path) -> None:
		config.target.path = str(tmp_path)
		db.insert_mission(Mission(id="m1", objective="test"))
		epoch = Epoch(id="ep1", mission_id="m1", number=1)
		db.insert_epoch(epoch)
		plan = Plan(id="p1", objective="test")
		db.insert_plan(plan)
		unit = WorkUnit(id="wu1", plan_id="p1", title="Task")
		db.insert_work_unit(unit)
		handoff = Handoff(
			id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
			status="completed", summary="Done",
			files_changed=["src/a.py", "src/b.py"],
		)
		db.insert_handoff(handoff)

		mission = Mission(id="m1", objective="test")
		update_mission_state(db, mission, config)

		content = (tmp_path / "MISSION_STATE.md").read_text()
		assert "## Files Modified" in content
		assert "a.py" in content
		assert "b.py" in content

	def test_no_changelog_when_empty(self, config: MissionConfig, db: Database, tmp_path: Path) -> None:
		config.target.path = str(tmp_path)
		db.insert_mission(Mission(id="m1", objective="test"))

		mission = Mission(id="m1", objective="test")
		update_mission_state(db, mission, config, state_changelog=[])

		content = (tmp_path / "MISSION_STATE.md").read_text()
		assert "## Changelog" not in content
