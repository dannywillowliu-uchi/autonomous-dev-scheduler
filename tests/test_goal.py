"""Tests for the GOAL.md fitness function engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autodev.goal import (
	FitnessResult,
	GoalAction,
	GoalComponent,
	GoalSpec,
	IterationLog,
	check_stopping,
	parse_goal_file,
	rank_actions,
	render_score_summary,
	run_fitness,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FULL_GOAL_MD = """\
# Goal: test coverage

Maximize test coverage across the project.

## Fitness

pytest --cov --cov-report=term | tail -1

## Components

- unit tests (weight: 0.6): pytest tests/unit --cov -q | tail -1
- integration tests (weight: 0.4): pytest tests/integration --cov -q | tail -1

## Target

0.95

## Constraints

- No mocking of database connections
- Must run in under 5 minutes

## Actions

- Add missing unit tests for auth module [files: src/auth.py, tests/test_auth.py] [impact: high]
- Refactor integration test fixtures [files: tests/conftest.py] [impact: medium]
- Remove dead code to improve coverage ratio [impact: low]
"""

MINIMAL_GOAL_MD = """\
# Goal: basic check

## Fitness

echo 0.75
"""


@pytest.fixture
def full_goal_path(tmp_path: Path) -> Path:
	p = tmp_path / "GOAL.md"
	p.write_text(FULL_GOAL_MD)
	return p


@pytest.fixture
def minimal_goal_path(tmp_path: Path) -> Path:
	p = tmp_path / "GOAL.md"
	p.write_text(MINIMAL_GOAL_MD)
	return p


# ---------------------------------------------------------------------------
# parse_goal_file
# ---------------------------------------------------------------------------

class TestParseGoalFile:

	def test_parse_goal_file_complete(self, full_goal_path: Path) -> None:
		spec = parse_goal_file(full_goal_path)
		assert spec.name == "test coverage"
		assert "Maximize test coverage" in spec.description
		assert spec.fitness_command == "pytest --cov --cov-report=term | tail -1"
		assert len(spec.components) == 2
		assert spec.components[0].name == "unit tests"
		assert spec.components[0].weight == 0.6
		assert spec.components[1].name == "integration tests"
		assert spec.components[1].weight == 0.4
		assert spec.target_score == 0.95
		assert len(spec.constraints) == 2
		assert "No mocking" in spec.constraints[0]
		assert len(spec.actions) == 3

	def test_parse_goal_file_minimal(self, minimal_goal_path: Path) -> None:
		spec = parse_goal_file(minimal_goal_path)
		assert spec.name == "basic check"
		assert spec.fitness_command == "echo 0.75"
		assert spec.components == []
		assert spec.target_score == 1.0
		assert spec.constraints == []
		assert spec.actions == []

	def test_parse_goal_file_missing_name(self, tmp_path: Path) -> None:
		p = tmp_path / "GOAL.md"
		p.write_text("## Fitness\necho 1.0\n")
		with pytest.raises(ValueError, match="must have a"):
			parse_goal_file(p)

	def test_parse_goal_file_missing_fitness_and_components(self, tmp_path: Path) -> None:
		p = tmp_path / "GOAL.md"
		p.write_text("# Goal: empty\n\nJust a description.\n")
		with pytest.raises(ValueError, match="at least a Fitness or Components"):
			parse_goal_file(p)

	def test_parse_goal_file_components(self, full_goal_path: Path) -> None:
		spec = parse_goal_file(full_goal_path)
		assert spec.components[0].command == "pytest tests/unit --cov -q | tail -1"
		assert spec.components[1].command == "pytest tests/integration --cov -q | tail -1"

	def test_parse_goal_file_actions(self, full_goal_path: Path) -> None:
		spec = parse_goal_file(full_goal_path)
		# High impact action
		high = spec.actions[0]
		assert "auth module" in high.description
		assert "src/auth.py" in high.files_hint
		assert "tests/test_auth.py" in high.files_hint
		assert high.estimated_impact == "high"

		# Medium impact action
		med = spec.actions[1]
		assert med.estimated_impact == "medium"
		assert "tests/conftest.py" in med.files_hint

		# Low impact action
		low = spec.actions[2]
		assert low.estimated_impact == "low"
		assert low.files_hint == []


# ---------------------------------------------------------------------------
# run_fitness
# ---------------------------------------------------------------------------

class TestRunFitness:

	def test_run_fitness_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
		def mock_run(*args, **kwargs):
			result = subprocess.CompletedProcess(args=args, returncode=0, stdout="0.85\n", stderr="")
			return result

		monkeypatch.setattr(subprocess, "run", mock_run)
		spec = GoalSpec(name="test", fitness_command="echo 0.85")
		result = run_fitness(spec, tmp_path)
		assert result.success is True
		assert result.composite == 0.85

	def test_run_fitness_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
		def mock_run(*args, **kwargs):
			raise subprocess.TimeoutExpired(cmd="sleep 999", timeout=5)

		monkeypatch.setattr(subprocess, "run", mock_run)
		spec = GoalSpec(name="test", fitness_command="sleep 999")
		result = run_fitness(spec, tmp_path, timeout=5)
		assert result.success is False
		assert "Timeout" in result.error

	def test_run_fitness_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
		def mock_run(*args, **kwargs):
			return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="command failed")

		monkeypatch.setattr(subprocess, "run", mock_run)
		spec = GoalSpec(name="test", fitness_command="false")
		result = run_fitness(spec, tmp_path)
		assert result.success is False
		assert "Exit code 1" in result.error

	def test_run_fitness_components(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
		call_count = 0

		def mock_run(*args, **kwargs):
			nonlocal call_count
			call_count += 1
			# First component returns 0.8, second returns 0.6
			score = "0.80\n" if call_count == 1 else "0.60\n"
			return subprocess.CompletedProcess(args=args, returncode=0, stdout=score, stderr="")

		monkeypatch.setattr(subprocess, "run", mock_run)
		spec = GoalSpec(
			name="test",
			components=[
				GoalComponent(name="unit", command="cmd1", weight=0.6),
				GoalComponent(name="integ", command="cmd2", weight=0.4),
			],
		)
		result = run_fitness(spec, tmp_path)
		assert result.success is True
		assert result.components == {"unit": 0.8, "integ": 0.6}
		# Weighted: (0.8*0.6 + 0.6*0.4) / (0.6+0.4) = (0.48 + 0.24) / 1.0 = 0.72
		assert abs(result.composite - 0.72) < 0.001

	def test_run_fitness_no_command(self, tmp_path: Path) -> None:
		spec = GoalSpec(name="empty")
		result = run_fitness(spec, tmp_path)
		assert result.success is False
		assert "No fitness command" in result.error


# ---------------------------------------------------------------------------
# IterationLog
# ---------------------------------------------------------------------------

class TestIterationLog:

	def test_iteration_log_record_and_load(self, tmp_path: Path) -> None:
		log = IterationLog(tmp_path / "iterations.json")
		before = FitnessResult(composite=0.5)
		after = FitnessResult(composite=0.7)
		log.record(before, after, "added tests")

		entries = log.load()
		assert len(entries) == 1
		assert entries[0].action == "added tests"
		assert entries[0].before.composite == 0.5
		assert entries[0].after.composite == 0.7
		assert abs(entries[0].delta - 0.2) < 0.001

	def test_iteration_log_trend(self, tmp_path: Path) -> None:
		log = IterationLog(tmp_path / "iterations.json")
		for score in [0.3, 0.5, 0.7, 0.9]:
			before = FitnessResult(composite=score - 0.1)
			after = FitnessResult(composite=score)
			log.record(before, after, f"step to {score}")

		trend = log.trend()
		assert trend == [0.3, 0.5, 0.7, 0.9]

	def test_iteration_log_latest(self, tmp_path: Path) -> None:
		log = IterationLog(tmp_path / "iterations.json")
		assert log.latest() is None

		log.record(FitnessResult(composite=0.1), FitnessResult(composite=0.3), "first")
		log.record(FitnessResult(composite=0.3), FitnessResult(composite=0.6), "second")

		latest = log.latest()
		assert latest is not None
		assert latest.action == "second"
		assert latest.after.composite == 0.6

	def test_iteration_log_empty_file(self, tmp_path: Path) -> None:
		log_path = tmp_path / "iterations.json"
		log_path.write_text("")
		log = IterationLog(log_path)
		assert log.load() == []


# ---------------------------------------------------------------------------
# check_stopping
# ---------------------------------------------------------------------------

class TestCheckStopping:

	def test_check_stopping_target_reached(self) -> None:
		spec = GoalSpec(name="t", target_score=0.9)
		result = FitnessResult(composite=0.95)
		assert check_stopping(spec, result) is True

	def test_check_stopping_max_iterations(self) -> None:
		spec = GoalSpec(name="t", target_score=0.9)
		result = FitnessResult(composite=0.5)
		assert check_stopping(spec, result, max_iterations=10, iteration_count=10) is True

	def test_check_stopping_not_yet(self) -> None:
		spec = GoalSpec(name="t", target_score=0.9)
		result = FitnessResult(composite=0.5)
		assert check_stopping(spec, result, max_iterations=10, iteration_count=3) is False

	def test_check_stopping_exact_target(self) -> None:
		spec = GoalSpec(name="t", target_score=0.9)
		result = FitnessResult(composite=0.9)
		assert check_stopping(spec, result) is True

	def test_check_stopping_zero_max_iterations_ignored(self) -> None:
		spec = GoalSpec(name="t", target_score=0.9)
		result = FitnessResult(composite=0.5)
		# max_iterations=0 means no iteration limit
		assert check_stopping(spec, result, max_iterations=0, iteration_count=100) is False


# ---------------------------------------------------------------------------
# rank_actions
# ---------------------------------------------------------------------------

class TestRankActions:

	def test_rank_actions(self) -> None:
		spec = GoalSpec(
			name="t",
			components=[
				GoalComponent(name="unit", command="x", weight=0.7),
				GoalComponent(name="integ", command="y", weight=0.3),
			],
			actions=[
				GoalAction(description="low action", estimated_impact="low"),
				GoalAction(description="high action", estimated_impact="high"),
				GoalAction(description="med action", estimated_impact="medium"),
			],
		)
		current = FitnessResult(
			composite=0.6,
			components={"unit": 0.5, "integ": 0.8},
		)
		ranked = rank_actions(spec, current)
		# high > medium > low
		assert ranked[0].description == "high action"
		assert ranked[1].description == "med action"
		assert ranked[2].description == "low action"

	def test_rank_actions_empty(self) -> None:
		spec = GoalSpec(name="t")
		current = FitnessResult(composite=0.5)
		assert rank_actions(spec, current) == []


# ---------------------------------------------------------------------------
# render_score_summary
# ---------------------------------------------------------------------------

class TestRenderScoreSummary:

	def test_render_score_summary(self) -> None:
		spec = GoalSpec(
			name="coverage",
			target_score=0.95,
			components=[
				GoalComponent(name="unit", command="x", weight=0.6),
				GoalComponent(name="integ", command="y", weight=0.4),
			],
		)
		result = FitnessResult(
			composite=0.72,
			components={"unit": 0.8, "integ": 0.6},
		)
		summary = render_score_summary(spec, result)
		assert "coverage" in summary
		assert "0.720" in summary
		assert "0.950" in summary
		assert "unit" in summary
		assert "integ" in summary
		assert "Gap" in summary

	def test_render_score_summary_error(self) -> None:
		spec = GoalSpec(name="broken", target_score=1.0)
		result = FitnessResult(composite=0.0, success=False, error="command not found")
		summary = render_score_summary(spec, result)
		assert "Error" in summary
		assert "command not found" in summary

	def test_render_score_summary_no_components(self) -> None:
		spec = GoalSpec(name="simple", target_score=0.9)
		result = FitnessResult(composite=0.75)
		summary = render_score_summary(spec, result)
		assert "simple" in summary
		assert "0.750" in summary
		# Should not have Components section
		assert "Components" not in summary
