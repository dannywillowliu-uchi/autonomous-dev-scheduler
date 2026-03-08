"""Integration tests for the full swarm lifecycle.

Tests the end-to-end flow: initialize -> create tasks -> spawn agents ->
agents complete/fail -> orphan recovery -> retry -> completion detection.

All subprocess spawning is mocked -- no real Claude processes are launched.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from autodev.config import SwarmConfig
from autodev.swarm.controller import SwarmController
from autodev.swarm.models import (
	AgentStatus,
	DecisionType,
	PlannerDecision,
	TaskStatus,
)

# -- Helpers --


def _make_config(tmp_path: Path) -> MagicMock:
	config = MagicMock()
	config.target.name = "test-project"
	config.target.objective = "Build a test compiler"
	config.target.resolved_path = str(tmp_path)
	config.notification = MagicMock()
	return config


def _make_swarm_config(**overrides: object) -> SwarmConfig:
	sc = SwarmConfig()
	for k, v in overrides.items():
		setattr(sc, k, v)
	return sc


def _make_db() -> MagicMock:
	db = MagicMock()
	db.get_knowledge_for_mission.return_value = []
	return db


def _fake_proc(*, returncode: int | None = None, stdout: bytes = b"") -> MagicMock:
	"""Create a mock asyncio.subprocess.Process."""
	proc = MagicMock()
	proc.returncode = returncode
	proc.stdout = AsyncMock()
	proc.stdout.read = AsyncMock(return_value=stdout)
	proc.terminate = MagicMock()
	proc.kill = MagicMock()
	proc.wait = AsyncMock()
	return proc


def _ad_result(status: str = "completed", summary: str = "Done", **extra: object) -> bytes:
	"""Build AD_RESULT output bytes."""
	import json
	data = {"status": status, "summary": summary, "commits": [], "files_changed": [], **extra}
	return f'AD_RESULT:{json.dumps(data)}\n'.encode()


# -- Integration Tests --


class TestSwarmLifecycleHappyPath:
	"""Full lifecycle: init -> tasks -> agents -> completion."""

	async def test_full_lifecycle(self, tmp_path: Path) -> None:
		"""End-to-end: create tasks, spawn agents, agents complete, all done."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(max_agents=3), _make_db(),
			stalled_task_timeout=0.0,
		)

		# 1. Initialize
		with patch.object(Path, "home", return_value=tmp_path):
			await ctrl.initialize()

		team_dir = tmp_path / ".claude" / "teams" / "autodev-test-project"
		assert team_dir.exists()
		assert (team_dir / "inboxes" / "team-lead.json").exists()

		# 2. Create tasks
		task_decisions = [
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Write lexer", "priority": 2}),
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Write parser", "priority": 1}),
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Write codegen", "priority": 0}),
		]
		await ctrl.execute_decisions(task_decisions)
		assert len(ctrl.tasks) == 3
		assert all(t.status == TaskStatus.PENDING for t in ctrl.tasks)

		# 3. Spawn agents with task assignments
		procs = {}
		for i, task in enumerate(ctrl.tasks):
			proc = _fake_proc()
			procs[task.id] = proc

			with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
				await ctrl.execute_decisions([
					PlannerDecision(
						type=DecisionType.SPAWN,
						payload={"name": f"worker-{i}", "prompt": f"Do {task.title}", "task_id": task.id},
					),
				])

		assert len(ctrl.agents) == 3
		assert all(a.status == AgentStatus.WORKING for a in ctrl.agents)
		assert all(t.status == TaskStatus.CLAIMED for t in ctrl.tasks)

		# 4. Agents complete work -- simulate process exit with AD_RESULT
		for task in ctrl.tasks:
			agent = next(a for a in ctrl.agents if a.current_task_id == task.id)
			proc = ctrl._processes[agent.id]
			proc.returncode = 0
			proc.stdout.read = AsyncMock(return_value=_ad_result(summary=f"Completed {task.title}"))

		events = await ctrl.monitor_agents()

		# 5. Verify all tasks completed
		assert all(t.status == TaskStatus.COMPLETED for t in ctrl.tasks)
		completion_events = [e for e in events if e["type"] == "agent_completed"]
		assert len(completion_events) == 3
		assert all(e["status"] == "completed" for e in completion_events)

		# 6. All agents should be dead (process exited)
		assert all(a.status == AgentStatus.DEAD for a in ctrl.agents)

		# 7. Verify completion detection: no active agents, all tasks done
		active = [a for a in ctrl.agents if a.status in (AgentStatus.WORKING, AgentStatus.SPAWNING)]
		pending = [t for t in ctrl.tasks if t.status == TaskStatus.PENDING]
		assert len(active) == 0
		assert len(pending) == 0

	async def test_task_state_transitions(self, tmp_path: Path) -> None:
		"""Track a single task through PENDING -> CLAIMED -> COMPLETED."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		# Create task
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Single task"}),
		])
		task = ctrl.tasks[0]
		assert task.status == TaskStatus.PENDING

		# Spawn agent for task
		proc = _fake_proc()
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "w1", "prompt": "do it", "task_id": task.id,
				}),
			])
		assert task.status == TaskStatus.CLAIMED
		assert task.claimed_by is not None

		# Agent completes
		agent = ctrl.agents[0]
		proc.returncode = 0
		proc.stdout.read = AsyncMock(return_value=_ad_result())

		await ctrl.monitor_agents()

		assert task.status == TaskStatus.COMPLETED
		assert task.completed_at is not None
		assert task.attempt_count == 1
		assert agent.tasks_completed == 1


class TestSwarmFailureRecovery:
	"""Agent crash -> task failure -> orphan recovery -> retry."""

	async def test_crash_and_retry_cycle(self, tmp_path: Path) -> None:
		"""Agent crashes, task fails, requeue succeeds, second agent completes."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		# Create a retriable task
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={
				"title": "Flaky task", "max_attempts": 3,
			}),
		])
		task = ctrl.tasks[0]

		# First agent crashes
		crash_proc = _fake_proc(returncode=1, stdout=b"Segfault\nTraceback...")
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=crash_proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "crash-worker", "prompt": "try it", "task_id": task.id,
				}),
			])

		await ctrl.monitor_agents()

		assert task.status == TaskStatus.FAILED
		assert task.attempt_count == 1
		assert task.claimed_by is None

		# Requeue
		requeued = ctrl.requeue_failed_tasks()
		assert task.id in requeued
		assert task.status == TaskStatus.PENDING

		# Second agent succeeds
		success_proc = _fake_proc(returncode=0, stdout=_ad_result(summary="Fixed it"))
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=success_proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "retry-worker", "prompt": "try again", "task_id": task.id,
				}),
			])

		await ctrl.monitor_agents()

		assert task.status == TaskStatus.COMPLETED
		assert task.attempt_count == 2
		assert task.result_summary == "Fixed it"

	async def test_max_retries_exhausted(self, tmp_path: Path) -> None:
		"""Task fails max_attempts times and stays FAILED after requeue attempt."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={
				"title": "Hopeless task", "max_attempts": 2,
			}),
		])
		task = ctrl.tasks[0]

		# Fail twice
		for attempt in range(2):
			crash_proc = _fake_proc(returncode=1, stdout=b"crash")
			with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=crash_proc)):
				await ctrl.execute_decisions([
					PlannerDecision(type=DecisionType.SPAWN, payload={
						"name": f"worker-{attempt}", "prompt": "try", "task_id": task.id,
					}),
				])
			await ctrl.monitor_agents()

			if attempt < 1:
				ctrl.requeue_failed_tasks()

		# Should not requeue beyond max_attempts
		requeued = ctrl.requeue_failed_tasks()
		assert task.id not in requeued
		assert task.status == TaskStatus.FAILED
		assert task.attempt_count == 2


class TestOrphanRecoveryIntegration:
	"""Orphaned task detection and recovery in realistic scenarios."""

	async def test_agent_dies_task_recovered(self, tmp_path: Path) -> None:
		"""Agent process dies unexpectedly, task eventually recovered."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		# Create task and spawn agent
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Orphan target"}),
		])
		task = ctrl.tasks[0]

		proc = _fake_proc()
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "doomed", "prompt": "work", "task_id": task.id,
				}),
			])

		agent = ctrl.agents[0]
		assert task.status == TaskStatus.CLAIMED

		# Simulate agent death without process exit (e.g., killed externally)
		# Mark agent as dead and backdate claim
		agent.status = AgentStatus.DEAD
		agent.death_time = time.monotonic()
		task.claimed_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

		# Remove from processes (simulating external cleanup)
		ctrl._processes.pop(agent.id, None)

		events = await ctrl.monitor_agents()

		# Task should be recovered to PENDING
		assert task.status == TaskStatus.PENDING
		assert task.claimed_by is None
		recovered = [e for e in events if e["type"] == "orphaned_task_recovered"]
		assert len(recovered) == 1

	async def test_missing_agent_orphan_recovery(self, tmp_path: Path) -> None:
		"""Task claimed by an agent that was already cleaned up from the pool."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Lost task"}),
		])
		task = ctrl.tasks[0]

		# Manually set task as claimed by a nonexistent agent
		task.status = TaskStatus.CLAIMED
		task.claimed_by = "ghost-agent-id"
		task.claimed_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

		await ctrl.monitor_agents()

		assert task.status == TaskStatus.PENDING
		assert task.claimed_by is None


class TestDeadAgentCleanupIntegration:
	"""Dead agent cleanup as part of the monitoring cycle."""

	async def test_dead_agents_cleaned_after_threshold(self, tmp_path: Path) -> None:
		"""Dead agents are removed from active pool after death threshold."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		# Spawn and immediately complete an agent
		proc = _fake_proc(returncode=0, stdout=_ad_result())
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Quick task"}),
		])
		task = ctrl.tasks[0]

		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "fast-worker", "prompt": "go", "task_id": task.id,
				}),
			])

		await ctrl.monitor_agents()

		# Agent is dead but still in the pool (within threshold)
		assert len(ctrl.agents) == 1
		assert ctrl.agents[0].status == AgentStatus.DEAD

		# Backdate death_time to exceed threshold
		ctrl.agents[0].death_time = time.monotonic() - 600

		await ctrl.monitor_agents()

		# Agent should be moved to history
		assert len(ctrl.agents) == 0
		assert len(ctrl.dead_agent_history) == 1
		assert ctrl.dead_agent_history[0].name == "fast-worker"


class TestCompletionDetection:
	"""Controller detects when all work is done."""

	async def test_all_tasks_done_no_active_agents(self, tmp_path: Path) -> None:
		"""When all tasks are completed/failed and no agents are active, swarm is done."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		# Create two tasks
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Task A"}),
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Task B", "max_attempts": 1}),
		])

		# Task A succeeds
		proc_a = _fake_proc(returncode=0, stdout=_ad_result(summary="A done"))
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc_a)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "worker-a", "prompt": "do A", "task_id": ctrl.tasks[0].id,
				}),
			])
		await ctrl.monitor_agents()

		# Task B fails (no retries)
		proc_b = _fake_proc(returncode=1, stdout=b"crash")
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc_b)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "worker-b", "prompt": "do B", "task_id": ctrl.tasks[1].id,
				}),
			])
		await ctrl.monitor_agents()

		# No requeue possible (max_attempts=1, attempt_count=1)
		requeued = ctrl.requeue_failed_tasks()
		assert len(requeued) == 0

		# Verify completion state
		active_agents = [a for a in ctrl.agents if a.status in (AgentStatus.WORKING, AgentStatus.SPAWNING)]
		terminal_tasks = [
			t for t in ctrl.tasks
			if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
		]
		requeueable = [
			t for t in ctrl.tasks
			if t.status == TaskStatus.FAILED and t.attempt_count < t.max_attempts
		]

		assert len(active_agents) == 0
		assert len(terminal_tasks) == len(ctrl.tasks)
		assert len(requeueable) == 0


class TestBuildStateIntegration:
	"""State building reflects the full lifecycle."""

	async def test_state_snapshot_accuracy(self, tmp_path: Path) -> None:
		"""SwarmState snapshot accurately reflects controller state."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		# Create tasks
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Active task"}),
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Pending task"}),
		])

		# Spawn agent for first task
		proc = _fake_proc()
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "state-worker", "prompt": "go", "task_id": ctrl.tasks[0].id,
				}),
			])

		state = ctrl.build_state(core_test_results={"pass": 50, "fail": 2})

		assert state.mission_objective == "Build a test compiler"
		assert len(state.agents) == 1
		assert len(state.tasks) == 2
		assert state.core_test_results["pass"] == 50

		# Render should produce readable output
		rendered = ctrl.render_state(state)
		assert "Active task" in rendered
		assert "Pending task" in rendered
		assert "state-worker" in rendered

	async def test_state_reflects_dead_history(self, tmp_path: Path) -> None:
		"""Dead agent history appears in state snapshot."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		# Spawn and complete an agent
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Done task"}),
		])
		proc = _fake_proc(returncode=0, stdout=_ad_result())
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "hist-worker", "prompt": "go", "task_id": ctrl.tasks[0].id,
				}),
			])
		await ctrl.monitor_agents()

		# Backdate and clean up
		ctrl.agents[0].death_time = time.monotonic() - 600
		await ctrl.monitor_agents()

		state = ctrl.build_state()
		assert len(state.dead_agent_history) == 1
		assert state.dead_agent_history[0].name == "hist-worker"


class TestCleanupIntegration:
	"""Graceful shutdown of the entire swarm."""

	async def test_cleanup_kills_all_agents(self, tmp_path: Path) -> None:
		"""cleanup() terminates all running agent processes."""
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
			stalled_task_timeout=0.0,
		)

		with patch.object(Path, "home", return_value=tmp_path):
			await ctrl.initialize()

		# Spawn two agents
		procs = []
		for i in range(2):
			proc = _fake_proc()
			procs.append(proc)
			with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
				await ctrl.execute_decisions([
					PlannerDecision(type=DecisionType.SPAWN, payload={
						"name": f"cleanup-worker-{i}", "prompt": "work",
					}),
				])

		assert len([a for a in ctrl.agents if a.status == AgentStatus.WORKING]) == 2

		await ctrl.cleanup()

		assert all(a.status == AgentStatus.DEAD for a in ctrl.agents)
		for proc in procs:
			proc.terminate.assert_called()


class TestScalingRecommendation:
	"""Scaling advice based on task pool vs active agents."""

	async def test_scale_up_when_many_pending(self, tmp_path: Path) -> None:
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
		)

		# Create many pending tasks, no agents
		for i in range(6):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": f"Task {i}"}),
			])

		rec = ctrl.get_scaling_recommendation()
		assert rec["scale_up"] > 0

	async def test_no_scale_when_balanced(self, tmp_path: Path) -> None:
		ctrl = SwarmController(
			_make_config(tmp_path), _make_swarm_config(), _make_db(),
		)

		# One pending task, one active agent
		await ctrl.execute_decisions([
			PlannerDecision(type=DecisionType.CREATE_TASK, payload={"title": "Task"}),
		])
		proc = _fake_proc()
		with patch.object(ctrl, "_spawn_claude_session", new=AsyncMock(return_value=proc)):
			await ctrl.execute_decisions([
				PlannerDecision(type=DecisionType.SPAWN, payload={
					"name": "w1", "prompt": "go", "task_id": ctrl.tasks[0].id,
				}),
			])

		rec = ctrl.get_scaling_recommendation()
		assert rec["scale_up"] == 0
