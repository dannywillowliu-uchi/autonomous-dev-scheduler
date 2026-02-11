"""Tests for session spawning."""

from __future__ import annotations

from mission_control.config import (
	MissionConfig,
	SchedulerConfig,
	TargetConfig,
	VerificationConfig,
)
from mission_control.models import Snapshot, TaskRecord
from mission_control.session import (
	build_branch_name,
	parse_mc_result,
	render_prompt,
)


def _config() -> MissionConfig:
	return MissionConfig(
		target=TargetConfig(
			name="test-proj",
			path="/tmp/test",
			branch="main",
			objective="Build something",
			verification=VerificationConfig(command="pytest -q"),
		),
		scheduler=SchedulerConfig(model="sonnet"),
	)


def _snapshot() -> Snapshot:
	return Snapshot(test_total=10, test_passed=8, test_failed=2, lint_errors=3, type_errors=1)


def _task(desc: str = "Fix the failing tests") -> TaskRecord:
	return TaskRecord(source="test_failure", description=desc, priority=2)


class TestRenderPrompt:
	def test_contains_task(self) -> None:
		prompt = render_prompt(_task(), _snapshot(), _config(), "mc/session-abc")
		assert "Fix the failing tests" in prompt

	def test_contains_stats(self) -> None:
		prompt = render_prompt(_task(), _snapshot(), _config(), "mc/session-abc")
		assert "8/10 passing" in prompt
		assert "Lint errors: 3" in prompt
		assert "Type errors: 1" in prompt

	def test_contains_branch(self) -> None:
		prompt = render_prompt(_task(), _snapshot(), _config(), "mc/session-abc")
		assert "mc/session-abc" in prompt

	def test_contains_verification_command(self) -> None:
		prompt = render_prompt(_task(), _snapshot(), _config(), "mc/session-abc")
		assert "pytest -q" in prompt

	def test_contains_context(self) -> None:
		prompt = render_prompt(_task(), _snapshot(), _config(), "mc/session-abc", context="Previous session failed")
		assert "Previous session failed" in prompt

	def test_default_context(self) -> None:
		prompt = render_prompt(_task(), _snapshot(), _config(), "mc/session-abc")
		assert "No additional context" in prompt

	def test_contains_target_name(self) -> None:
		prompt = render_prompt(_task(), _snapshot(), _config(), "mc/session-abc")
		assert "test-proj" in prompt


class TestParseMcResult:
	def test_valid_result(self) -> None:
		output = (
			"Some output\n"
			'MC_RESULT:{"status":"completed","commits":["abc123"],'
			'"summary":"Fixed tests","files_changed":["src/foo.py"]}'
		)
		result = parse_mc_result(output)
		assert result is not None
		assert result["status"] == "completed"
		assert result["commits"] == ["abc123"]
		assert result["summary"] == "Fixed tests"

	def test_no_result(self) -> None:
		output = "Just some regular output\nNo structured result here"
		result = parse_mc_result(output)
		assert result is None

	def test_malformed_json(self) -> None:
		output = "MC_RESULT:{bad json}"
		result = parse_mc_result(output)
		assert result is None

	def test_result_in_middle(self) -> None:
		output = 'line 1\nMC_RESULT:{"status":"failed","commits":[],"summary":"Could not fix"}\nline 3'
		result = parse_mc_result(output)
		assert result is not None
		assert result["status"] == "failed"

	def test_empty_output(self) -> None:
		result = parse_mc_result("")
		assert result is None


class TestBranchName:
	def test_format(self) -> None:
		assert build_branch_name("abc123") == "mc/session-abc123"

	def test_unique(self) -> None:
		assert build_branch_name("a") != build_branch_name("b")
