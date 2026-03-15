"""Cycle evaluator for swarm planner decisions -- algorithmic grading, no LLM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from autodev.swarm.models import (
	AgentStatus,
	DecisionType,
	PlannerDecision,
	SwarmState,
	TaskStatus,
)

_TEST_KEYWORDS = frozenset({"test", "tests", "testing", "pytest", "unittest", "spec", "verify", "validation"})


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
	return max(lo, min(hi, value))


@dataclass
class CycleGrade:
	cycle_number: int
	task_quality_score: float
	agent_utilization_score: float
	convergence_score: float
	composite_score: float
	feedback: str


class CycleEvaluator:
	def __init__(self) -> None:
		self._history: list[CycleGrade] = []

	def grade_cycle(
		self,
		decisions: list[PlannerDecision],
		results: list[dict[str, Any]],
		state_before: SwarmState,
		state_after: SwarmState,
	) -> CycleGrade:
		tq = self._score_task_quality(decisions, state_before)
		au = self._score_agent_utilization(decisions, results, state_before, state_after)
		cs = self._score_convergence(state_before, state_after)
		composite = 0.3 * tq + 0.3 * au + 0.4 * cs
		feedback = self._build_feedback(tq, au, cs)
		grade = CycleGrade(
			cycle_number=state_after.cycle_number,
			task_quality_score=round(tq, 3),
			agent_utilization_score=round(au, 3),
			convergence_score=round(cs, 3),
			composite_score=round(composite, 3),
			feedback=feedback,
		)
		self._history.append(grade)
		return grade

	def _score_task_quality(
		self, decisions: list[PlannerDecision], state_before: SwarmState
	) -> float:
		create_decisions = [d for d in decisions if d.type == DecisionType.CREATE_TASK]
		if not create_decisions:
			return 0.5

		existing_files: set[str] = set()
		for task in state_before.tasks:
			if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.CLAIMED):
				existing_files.update(task.files_hint)

		scores: list[float] = []
		for d in create_decisions:
			score = 1.0
			payload = d.payload

			if not payload.get("files_hint"):
				score -= 0.2

			description = payload.get("description", "")
			if not description or len(description) < 100:
				score -= 0.3

			files_hint = payload.get("files_hint", [])
			if files_hint and existing_files.intersection(files_hint):
				score -= 0.2

			if payload.get("depends_on"):
				score += 0.1

			if description and any(kw in description.lower() for kw in _TEST_KEYWORDS):
				score += 0.1

			scores.append(_clamp(score))

		return sum(scores) / len(scores)

	def _score_agent_utilization(
		self,
		decisions: list[PlannerDecision],
		results: list[dict[str, Any]],
		state_before: SwarmState,
		state_after: SwarmState,
	) -> float:
		spawn_decisions = [d for d in decisions if d.type == DecisionType.SPAWN]
		total_spawns = len(spawn_decisions)

		if total_spawns == 0:
			agents = state_after.agents
			if not agents:
				return 0.5
			working = sum(
				1 for a in agents
				if a.status in (AgentStatus.WORKING, AgentStatus.IDLE)
			)
			score = working / len(agents)
		else:
			successful = sum(1 for r in results if r.get("success"))
			score = successful / total_spawns

		# Penalize agents killed before 5 minutes
		kill_decisions = [d for d in decisions if d.type == DecisionType.KILL]
		before_agents = {a.id: a for a in state_before.agents}
		for kd in kill_decisions:
			agent_id = kd.payload.get("agent_id", "")
			agent = before_agents.get(agent_id)
			if agent and agent.spawned_at:
				try:
					spawned = datetime.fromisoformat(agent.spawned_at)
					age_seconds = (datetime.now(timezone.utc) - spawned).total_seconds()
					if age_seconds < 300:
						score -= 0.3
				except (ValueError, TypeError):
					pass

		return _clamp(score)

	def _score_convergence(self, state_before: SwarmState, state_after: SwarmState) -> float:
		tests_before = state_before.core_test_results.get("pass", 0)
		tests_after = state_after.core_test_results.get("pass", 0)
		total_tests = max(
			10,
			state_after.core_test_results.get("pass", 0)
			+ state_after.core_test_results.get("fail", 0),
		)
		test_delta = tests_after - tests_before

		completed_before = sum(1 for t in state_before.tasks if t.status == TaskStatus.COMPLETED)
		completed_after = sum(1 for t in state_after.tasks if t.status == TaskStatus.COMPLETED)
		task_delta = completed_after - completed_before
		total_tasks = max(1, len(state_after.tasks))

		raw = (test_delta / total_tests + task_delta / total_tasks) / 2
		return _clamp(raw)

	def _build_feedback(self, tq: float, au: float, cs: float) -> str:
		scores = {
			"task_quality": tq,
			"agent_utilization": au,
			"convergence": cs,
		}
		weakest = min(scores, key=scores.get)  # type: ignore[arg-type]
		weakest_val = scores[weakest]
		reasons = {
			"task_quality": "tasks lack file hints and specific descriptions",
			"agent_utilization": "agents not completing work or killed prematurely",
			"convergence": "test count and task completions not improving",
		}
		return f"Weakest area: {weakest} ({weakest_val:.1f}) -- {reasons[weakest]}"
