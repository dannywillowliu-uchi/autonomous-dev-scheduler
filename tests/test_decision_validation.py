"""Tests for DrivingPlanner._validate_decisions()."""

from __future__ import annotations

from unittest.mock import MagicMock

from autodev.config import SwarmConfig
from autodev.swarm.models import (
	AgentStatus,
	DecisionType,
	PlannerDecision,
	SwarmAgent,
	SwarmState,
	SwarmTask,
	TaskStatus,
)
from autodev.swarm.planner import DrivingPlanner


def _make_planner(**config_overrides: object) -> DrivingPlanner:
	ctrl = MagicMock()
	ctrl._config = MagicMock()
	ctrl._config.target.resolved_path = "/tmp/test"
	sc = SwarmConfig()
	for k, v in config_overrides.items():
		setattr(sc, k, v)
	return DrivingPlanner(ctrl, sc)


def _spawn(prompt: str = "Do the thing", files: list[str] | None = None, priority: int = 0) -> PlannerDecision:
	payload: dict = {"role": "implementer", "name": "agent", "prompt": prompt}
	if files is not None:
		payload["files_hint"] = files
	return PlannerDecision(type=DecisionType.SPAWN, payload=payload, priority=priority)


def _create_task(
	title: str = "Implement feature",
	files: list[str] | None = None,
	depends_on: list[str] | None = None,
	task_id: str | None = None,
	priority: int = 0,
) -> PlannerDecision:
	payload: dict = {"title": title, "description": "A task"}
	if files is not None:
		payload["files_hint"] = files
	if depends_on is not None:
		payload["depends_on"] = depends_on
	if task_id is not None:
		payload["task_id"] = task_id
	return PlannerDecision(type=DecisionType.CREATE_TASK, payload=payload, priority=priority)


def _state(
	agents: list[SwarmAgent] | None = None,
	tasks: list[SwarmTask] | None = None,
) -> SwarmState:
	return SwarmState(
		mission_objective="Test mission",
		agents=agents or [],
		tasks=tasks or [],
	)


class TestValidatePassesCleanDecisions:
	def test_valid_decisions_pass_through_unchanged(self) -> None:
		planner = _make_planner(max_agents=5)
		decisions = [
			_spawn("Implement the auth module", files=["src/auth.py"]),
			_spawn("Implement the db module", files=["src/db.py"]),
			_create_task("Write tests for auth"),
			PlannerDecision(type=DecisionType.WAIT, payload={"duration": 30}),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 4

	def test_empty_decisions_pass_through(self) -> None:
		planner = _make_planner()
		result = planner._validate_decisions([], _state())
		assert result == []

	def test_non_spawn_create_decisions_always_pass(self) -> None:
		planner = _make_planner()
		decisions = [
			PlannerDecision(type=DecisionType.WAIT, payload={}),
			PlannerDecision(type=DecisionType.KILL, payload={"agent_id": "abc"}),
			PlannerDecision(type=DecisionType.ESCALATE, payload={"reason": "stuck"}),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 3


class TestEmptyPromptCheck:
	def test_rejects_spawn_with_empty_prompt(self) -> None:
		planner = _make_planner()
		decisions = [_spawn(prompt="")]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 0

	def test_rejects_spawn_with_whitespace_prompt(self) -> None:
		planner = _make_planner()
		decisions = [_spawn(prompt="   \n\t  ")]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 0

	def test_accepts_spawn_with_valid_prompt(self) -> None:
		planner = _make_planner()
		decisions = [_spawn(prompt="Fix the login bug in auth.py")]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 1


class TestSpawnBudgetCheck:
	def test_enforces_spawn_budget(self) -> None:
		planner = _make_planner(max_agents=3)
		# 1 active agent, max 3, so only 2 spawns allowed
		agent = SwarmAgent(name="existing", status=AgentStatus.WORKING)
		decisions = [
			_spawn("Task A", priority=3),
			_spawn("Task B", priority=2),
			_spawn("Task C", priority=1),
			_spawn("Task D", priority=0),
			_spawn("Task E", priority=0),
		]
		result = planner._validate_decisions(decisions, _state(agents=[agent]))
		spawn_count = sum(1 for d in result if d.type == DecisionType.SPAWN)
		assert spawn_count == 2

	def test_unbounded_agents_allows_all_spawns(self) -> None:
		planner = _make_planner(max_agents=0)  # 0 = unbounded
		decisions = [_spawn(f"Task {i}") for i in range(10)]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 10

	def test_at_max_agents_rejects_all_spawns(self) -> None:
		planner = _make_planner(max_agents=2)
		agents = [
			SwarmAgent(name="a1", status=AgentStatus.WORKING),
			SwarmAgent(name="a2", status=AgentStatus.SPAWNING),
		]
		decisions = [_spawn("Extra agent")]
		result = planner._validate_decisions(decisions, _state(agents=agents))
		spawn_count = sum(1 for d in result if d.type == DecisionType.SPAWN)
		assert spawn_count == 0


class TestDuplicateTaskCheck:
	def test_rejects_duplicate_task_matching_existing(self) -> None:
		planner = _make_planner()
		existing_task = SwarmTask(
			title="Implement user authentication",
			status=TaskStatus.PENDING,
		)
		decisions = [_create_task("Implement user authentication")]
		result = planner._validate_decisions(decisions, _state(tasks=[existing_task]))
		assert len(result) == 0

	def test_rejects_near_duplicate_task(self) -> None:
		planner = _make_planner()
		existing_task = SwarmTask(
			title="Implement user authentication module",
			status=TaskStatus.PENDING,
		)
		# Very similar title should be caught
		decisions = [_create_task("Implement user authentication modules")]
		result = planner._validate_decisions(decisions, _state(tasks=[existing_task]))
		assert len(result) == 0

	def test_allows_sufficiently_different_task(self) -> None:
		planner = _make_planner()
		existing_task = SwarmTask(
			title="Implement user authentication",
			status=TaskStatus.PENDING,
		)
		decisions = [_create_task("Write database migration scripts")]
		result = planner._validate_decisions(decisions, _state(tasks=[existing_task]))
		assert len(result) == 1

	def test_ignores_completed_tasks_for_dedup(self) -> None:
		planner = _make_planner()
		existing_task = SwarmTask(
			title="Implement user authentication",
			status=TaskStatus.COMPLETED,
		)
		decisions = [_create_task("Implement user authentication")]
		result = planner._validate_decisions(decisions, _state(tasks=[existing_task]))
		assert len(result) == 1

	def test_dedup_within_batch(self) -> None:
		planner = _make_planner()
		decisions = [
			_create_task("Implement user auth"),
			_create_task("Implement user auth"),  # exact duplicate in same batch
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 1


class TestFileOverlapCheck:
	def test_rejects_overlapping_spawn_decisions(self) -> None:
		planner = _make_planner()
		decisions = [
			_spawn("Fix auth", files=["src/auth.py", "src/models.py"]),
			_spawn("Refactor auth", files=["src/auth.py", "src/views.py"]),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 1
		# First one should survive
		assert result[0].payload["prompt"] == "Fix auth"

	def test_allows_non_overlapping_files(self) -> None:
		planner = _make_planner()
		decisions = [
			_spawn("Fix auth", files=["src/auth.py"]),
			_spawn("Fix db", files=["src/db.py"]),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 2

	def test_no_files_hint_passes(self) -> None:
		planner = _make_planner()
		decisions = [
			_spawn("Research the codebase"),
			_spawn("Investigate the tests"),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 2

	def test_overlap_between_spawn_and_create_task(self) -> None:
		planner = _make_planner()
		decisions = [
			_spawn("Fix auth", files=["src/auth.py"]),
			_create_task("Refactor auth", files=["src/auth.py"]),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 1

	def test_files_hint_as_comma_string(self) -> None:
		d = PlannerDecision(
			type=DecisionType.SPAWN,
			payload={"prompt": "Work on stuff", "files_hint": "src/a.py, src/b.py"},
		)
		files = DrivingPlanner._extract_files_hint(d)
		assert files == {"src/a.py", "src/b.py"}


class TestCircularDependencyCheck:
	def test_detects_simple_cycle(self) -> None:
		planner = _make_planner()
		decisions = [
			_create_task("Implement authentication module", task_id="auth", depends_on=["database"]),
			_create_task("Set up database schema migration", task_id="database", depends_on=["auth"]),
		]
		result = planner._validate_decisions(decisions, _state())
		create_tasks = [d for d in result if d.type == DecisionType.CREATE_TASK]
		assert len(create_tasks) == 0

	def test_detects_transitive_cycle(self) -> None:
		planner = _make_planner()
		decisions = [
			_create_task("Implement authentication module", task_id="auth", depends_on=["deploy"]),
			_create_task("Set up database schema migration", task_id="db", depends_on=["auth"]),
			_create_task("Configure deployment pipeline", task_id="deploy", depends_on=["db"]),
		]
		result = planner._validate_decisions(decisions, _state())
		create_tasks = [d for d in result if d.type == DecisionType.CREATE_TASK]
		assert len(create_tasks) == 0

	def test_allows_valid_dependency_chain(self) -> None:
		planner = _make_planner()
		decisions = [
			_create_task("Implement authentication module", task_id="auth"),
			_create_task("Set up database schema migration", task_id="db", depends_on=["auth"]),
			_create_task("Configure deployment pipeline", task_id="deploy", depends_on=["db"]),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 3

	def test_cycle_does_not_affect_non_create_decisions(self) -> None:
		planner = _make_planner()
		decisions = [
			PlannerDecision(type=DecisionType.WAIT, payload={"duration": 10}),
			_create_task("Implement authentication module", task_id="auth", depends_on=["database"]),
			_create_task("Set up database schema migration", task_id="database", depends_on=["auth"]),
		]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 1
		assert result[0].type == DecisionType.WAIT

	def test_single_task_no_cycle(self) -> None:
		planner = _make_planner()
		decisions = [_create_task("Solo task", task_id="X")]
		result = planner._validate_decisions(decisions, _state())
		assert len(result) == 1


class TestCombinedValidation:
	def test_multiple_rules_applied_together(self) -> None:
		planner = _make_planner(max_agents=2)
		existing_task = SwarmTask(
			title="Write unit tests",
			status=TaskStatus.IN_PROGRESS,
		)
		decisions = [
			_spawn("Valid agent", files=["src/a.py"]),
			_spawn("", files=["src/b.py"]),  # empty prompt
			_spawn("Another valid", files=["src/c.py"]),
			_spawn("Over budget", files=["src/d.py"]),  # 3rd spawn exceeds budget
			_create_task("Write unit tests"),  # duplicate
		]
		result = planner._validate_decisions(
			decisions, _state(tasks=[existing_task])
		)
		# Valid agent + Another valid should pass; empty prompt, budget, duplicate rejected
		assert len(result) == 2
