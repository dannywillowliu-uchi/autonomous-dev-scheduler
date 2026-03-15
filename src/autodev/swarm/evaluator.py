"""Cycle evaluator for swarm planner decisions -- algorithmic grading, no LLM."""

from __future__ import annotations

from dataclasses import dataclass

from autodev.swarm.models import (
	AgentStatus,
	SwarmState,
	TaskPriority,
	TaskStatus,
)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
	return max(lo, min(hi, value))


_PRIORITY_WEIGHTS: dict[TaskPriority, float] = {
	TaskPriority.LOW: 0.5,
	TaskPriority.NORMAL: 1.0,
	TaskPriority.HIGH: 1.5,
	TaskPriority.CRITICAL: 2.0,
}


@dataclass
class CycleGrade:
	task_quality_score: float  # 0-1: ratio of completed tasks to total, weighted by priority
	agent_utilization_score: float  # 0-1: ratio of working agents to total, penalize idle/dead
	convergence_score: float  # 0-1: test count improvement vs previous cycle
	overall_grade: float  # 0-1: weighted average
	feedback: str


class CycleEvaluator:
	"""Grades planner cycle quality using algorithmic scoring (no LLM calls).

	Stores history of grades for trend analysis. Call grade_cycle() each cycle
	with the current SwarmState, then use get_feedback() to inject a summary
	into the planner prompt.
	"""

	def __init__(self) -> None:
		self._history: list[CycleGrade] = []
		self._prev_test_pass_count: int | None = None

	def grade_cycle(self, state: SwarmState) -> CycleGrade:
		"""Grade a single planner cycle. Purely algorithmic, no LLM calls."""
		tq = self._score_task_quality(state)
		au = self._score_agent_utilization(state)
		cs = self._score_convergence(state)
		overall = 0.3 * tq + 0.3 * au + 0.4 * cs
		feedback = self._build_feedback(tq, au, cs)

		grade = CycleGrade(
			task_quality_score=round(tq, 3),
			agent_utilization_score=round(au, 3),
			convergence_score=round(cs, 3),
			overall_grade=round(overall, 3),
			feedback=feedback,
		)
		self._history.append(grade)
		self._prev_test_pass_count = state.core_test_results.get("pass", 0)
		return grade

	def get_feedback(self) -> str:
		"""Return a string summary for injection into the planner prompt."""
		if not self._history:
			return ""
		return self._history[-1].feedback

	# -- Scoring methods --

	def _score_task_quality(self, state: SwarmState) -> float:
		"""Ratio of completed tasks to total, weighted by priority."""
		tasks = state.tasks
		if not tasks:
			return 0.5

		weighted_completed = 0.0
		weighted_total = 0.0
		for task in tasks:
			weight = _PRIORITY_WEIGHTS.get(task.priority, 1.0)
			weighted_total += weight
			if task.status == TaskStatus.COMPLETED:
				weighted_completed += weight

		if weighted_total == 0:
			return 0.5
		return _clamp(weighted_completed / weighted_total)

	def _score_agent_utilization(self, state: SwarmState) -> float:
		"""Ratio of working agents to total, penalizing idle and dead agents."""
		agents = state.agents
		if not agents:
			return 0.5

		total = len(agents)
		working = sum(1 for a in agents if a.status == AgentStatus.WORKING)
		idle = sum(1 for a in agents if a.status == AgentStatus.IDLE)
		dead = sum(1 for a in agents if a.status == AgentStatus.DEAD)

		score = working / total
		score -= 0.1 * (idle / total)
		score -= 0.2 * (dead / total)
		return _clamp(score)

	def _score_convergence(self, state: SwarmState) -> float:
		"""Compare current test count to previous cycle, reward improvement."""
		current_pass = state.core_test_results.get("pass", 0)

		if self._prev_test_pass_count is None:
			return 0.5

		total_tests = max(
			10,
			state.core_test_results.get("pass", 0)
			+ state.core_test_results.get("fail", 0),
		)
		delta = current_pass - self._prev_test_pass_count
		raw = 0.5 + (delta / total_tests)
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
			"task_quality": "low ratio of completed tasks -- check for blocked or failed tasks",
			"agent_utilization": "agents not actively working -- too many idle or dead agents",
			"convergence": "test count not improving -- swarm may be stagnating",
		}
		parts = [f"Weakest: {weakest} ({weakest_val:.2f}) -- {reasons[weakest]}"]

		if len(self._history) >= 1:
			recent = self._history[-3:]
			avg = sum(g.overall_grade for g in recent) / len(recent)
			trend = "improving" if recent[-1].overall_grade > recent[0].overall_grade else "flat/declining"
			parts.append(f"Trend ({len(recent)} cycles): {trend}, avg: {avg:.2f}")

		return " | ".join(parts)
