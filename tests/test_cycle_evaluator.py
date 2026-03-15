"""Tests for CycleEvaluator and CycleGrade."""

from __future__ import annotations

from autodev.swarm.evaluator import CycleEvaluator, CycleGrade
from autodev.swarm.models import (
	AgentStatus,
	SwarmAgent,
	SwarmState,
	SwarmTask,
	TaskPriority,
	TaskStatus,
)


def _make_task(
	status: TaskStatus = TaskStatus.PENDING,
	priority: TaskPriority = TaskPriority.NORMAL,
	**kwargs,
) -> SwarmTask:
	return SwarmTask(status=status, priority=priority, **kwargs)


def _make_agent(status: AgentStatus = AgentStatus.WORKING, **kwargs) -> SwarmAgent:
	return SwarmAgent(status=status, **kwargs)


def _make_state(**kwargs) -> SwarmState:
	return SwarmState(**kwargs)


class TestCycleGradeFields:
	def test_dataclass_fields(self):
		grade = CycleGrade(
			task_quality_score=0.8,
			agent_utilization_score=0.7,
			convergence_score=0.6,
			overall_grade=0.68,
			feedback="test feedback",
		)
		assert grade.task_quality_score == 0.8
		assert grade.agent_utilization_score == 0.7
		assert grade.convergence_score == 0.6
		assert grade.overall_grade == 0.68
		assert grade.feedback == "test feedback"


class TestTaskQualityScore:
	def test_all_completed(self):
		ev = CycleEvaluator()
		state = _make_state(tasks=[
			_make_task(status=TaskStatus.COMPLETED),
			_make_task(status=TaskStatus.COMPLETED),
		])
		grade = ev.grade_cycle(state)
		assert grade.task_quality_score == 1.0

	def test_none_completed(self):
		ev = CycleEvaluator()
		state = _make_state(tasks=[
			_make_task(status=TaskStatus.PENDING),
			_make_task(status=TaskStatus.FAILED),
		])
		grade = ev.grade_cycle(state)
		assert grade.task_quality_score == 0.0

	def test_weighted_by_priority(self):
		ev = CycleEvaluator()
		# Complete a CRITICAL task, leave a LOW task pending
		state = _make_state(tasks=[
			_make_task(status=TaskStatus.COMPLETED, priority=TaskPriority.CRITICAL),
			_make_task(status=TaskStatus.PENDING, priority=TaskPriority.LOW),
		])
		grade = ev.grade_cycle(state)
		# CRITICAL weight=2.0, LOW weight=0.5, completed=2.0/2.5=0.8
		assert grade.task_quality_score == 0.8

	def test_no_tasks_returns_half(self):
		ev = CycleEvaluator()
		state = _make_state(tasks=[])
		grade = ev.grade_cycle(state)
		assert grade.task_quality_score == 0.5


class TestAgentUtilizationScore:
	def test_all_working(self):
		ev = CycleEvaluator()
		state = _make_state(agents=[
			_make_agent(status=AgentStatus.WORKING),
			_make_agent(status=AgentStatus.WORKING),
		])
		grade = ev.grade_cycle(state)
		assert grade.agent_utilization_score == 1.0

	def test_all_idle_penalized(self):
		ev = CycleEvaluator()
		state = _make_state(agents=[
			_make_agent(status=AgentStatus.IDLE),
			_make_agent(status=AgentStatus.IDLE),
		])
		grade = ev.grade_cycle(state)
		# working/total = 0, idle penalty = 0.1 * 1.0 = 0.1 -> clamped to 0
		assert grade.agent_utilization_score == 0.0

	def test_dead_agents_penalized_more(self):
		ev = CycleEvaluator()
		state = _make_state(agents=[
			_make_agent(status=AgentStatus.WORKING),
			_make_agent(status=AgentStatus.DEAD),
		])
		grade = ev.grade_cycle(state)
		# working/total=0.5, dead penalty=0.2*0.5=0.1 -> 0.4
		assert grade.agent_utilization_score == 0.4

	def test_no_agents_returns_half(self):
		ev = CycleEvaluator()
		state = _make_state(agents=[])
		grade = ev.grade_cycle(state)
		assert grade.agent_utilization_score == 0.5

	def test_mixed_statuses(self):
		ev = CycleEvaluator()
		state = _make_state(agents=[
			_make_agent(status=AgentStatus.WORKING),
			_make_agent(status=AgentStatus.WORKING),
			_make_agent(status=AgentStatus.IDLE),
			_make_agent(status=AgentStatus.DEAD),
		])
		grade = ev.grade_cycle(state)
		# working/total=0.5, idle_penalty=0.1*0.25=0.025, dead_penalty=0.2*0.25=0.05
		# 0.5 - 0.025 - 0.05 = 0.425
		assert grade.agent_utilization_score == 0.425


class TestConvergenceScore:
	def test_first_cycle_returns_half(self):
		ev = CycleEvaluator()
		state = _make_state(core_test_results={"pass": 10, "fail": 2})
		grade = ev.grade_cycle(state)
		assert grade.convergence_score == 0.5

	def test_improvement_increases_score(self):
		ev = CycleEvaluator()
		state1 = _make_state(core_test_results={"pass": 10, "fail": 5})
		ev.grade_cycle(state1)

		state2 = _make_state(core_test_results={"pass": 15, "fail": 5})
		grade = ev.grade_cycle(state2)
		# delta=5, total=max(10, 20)=20, raw=0.5+5/20=0.75
		assert grade.convergence_score == 0.75

	def test_regression_decreases_score(self):
		ev = CycleEvaluator()
		state1 = _make_state(core_test_results={"pass": 15, "fail": 5})
		ev.grade_cycle(state1)

		state2 = _make_state(core_test_results={"pass": 10, "fail": 5})
		grade = ev.grade_cycle(state2)
		# delta=-5, total=max(10, 15)=15, raw=0.5+(-5/15)=0.167
		assert round(grade.convergence_score, 3) == 0.167

	def test_no_change_returns_half(self):
		ev = CycleEvaluator()
		state1 = _make_state(core_test_results={"pass": 10, "fail": 2})
		ev.grade_cycle(state1)

		state2 = _make_state(core_test_results={"pass": 10, "fail": 2})
		grade = ev.grade_cycle(state2)
		assert grade.convergence_score == 0.5

	def test_uses_minimum_total_of_10(self):
		ev = CycleEvaluator()
		state1 = _make_state(core_test_results={"pass": 2, "fail": 0})
		ev.grade_cycle(state1)

		state2 = _make_state(core_test_results={"pass": 3, "fail": 0})
		grade = ev.grade_cycle(state2)
		# delta=1, total=max(10, 3)=10, raw=0.5+1/10=0.6
		assert grade.convergence_score == 0.6


class TestOverallGrade:
	def test_weighted_average(self):
		ev = CycleEvaluator()
		# All tasks completed, all agents working, first cycle
		state = _make_state(
			tasks=[_make_task(status=TaskStatus.COMPLETED)],
			agents=[_make_agent(status=AgentStatus.WORKING)],
			core_test_results={"pass": 10, "fail": 0},
		)
		grade = ev.grade_cycle(state)
		# tq=1.0, au=1.0, cs=0.5 (first cycle)
		# overall = 0.3*1.0 + 0.3*1.0 + 0.4*0.5 = 0.8
		assert grade.overall_grade == 0.8


class TestFeedback:
	def test_identifies_weakest_area(self):
		ev = CycleEvaluator()
		# No completed tasks -> low task_quality, working agents -> high utilization
		state = _make_state(
			tasks=[_make_task(status=TaskStatus.PENDING)],
			agents=[_make_agent(status=AgentStatus.WORKING)],
		)
		grade = ev.grade_cycle(state)
		assert "task_quality" in grade.feedback

	def test_get_feedback_empty_when_no_history(self):
		ev = CycleEvaluator()
		assert ev.get_feedback() == ""

	def test_get_feedback_returns_latest(self):
		ev = CycleEvaluator()
		state = _make_state(tasks=[_make_task(status=TaskStatus.COMPLETED)])
		grade = ev.grade_cycle(state)
		assert ev.get_feedback() == grade.feedback

	def test_trend_shown_after_two_cycles(self):
		ev = CycleEvaluator()
		state = _make_state(tasks=[_make_task(status=TaskStatus.COMPLETED)])
		ev.grade_cycle(state)
		ev.grade_cycle(state)
		feedback = ev.get_feedback()
		assert "Trend" in feedback


class TestHistory:
	def test_history_accumulates(self):
		ev = CycleEvaluator()
		state = _make_state(tasks=[_make_task(status=TaskStatus.COMPLETED)])
		ev.grade_cycle(state)
		ev.grade_cycle(state)
		ev.grade_cycle(state)
		assert len(ev._history) == 3

	def test_grades_are_clamped(self):
		ev = CycleEvaluator()
		state = _make_state(
			tasks=[_make_task(status=TaskStatus.COMPLETED)],
			agents=[_make_agent(status=AgentStatus.WORKING)],
			core_test_results={"pass": 50, "fail": 0},
		)
		grade = ev.grade_cycle(state)
		assert 0.0 <= grade.task_quality_score <= 1.0
		assert 0.0 <= grade.agent_utilization_score <= 1.0
		assert 0.0 <= grade.convergence_score <= 1.0
		assert 0.0 <= grade.overall_grade <= 1.0
