"""Tests for SwarmController._validate_ad_result -- worker output schema validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autodev.config import SwarmConfig
from autodev.swarm.controller import SwarmController


def _make_config(tmp_path: Path) -> MagicMock:
	config = MagicMock()
	config.target.name = "test-project"
	config.target.objective = "Test objective"
	config.target.resolved_path = str(tmp_path)
	config.notification = MagicMock()
	return config


def _make_db() -> MagicMock:
	db = MagicMock()
	db.get_knowledge_for_mission.return_value = []
	return db


@pytest.fixture
def ctrl(tmp_path: Path) -> SwarmController:
	return SwarmController(_make_config(tmp_path), SwarmConfig(), _make_db())


class TestValidAdResult:
	def test_valid_result_passthrough(self, ctrl: SwarmController) -> None:
		"""Well-formed AD_RESULT passes through with no warnings."""
		raw = {
			"status": "completed",
			"summary": "Implemented the parser module with full test coverage",
			"files_changed": ["src/parser.py", "tests/test_parser.py"],
			"discoveries": ["Found a race condition in the pool"],
			"commits": ["abc123"],
		}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["status"] == "completed"
		assert result["summary"] == raw["summary"]
		assert result["files_changed"] == raw["files_changed"]
		assert result["discoveries"] == raw["discoveries"]
		assert result["commits"] == raw["commits"]
		assert warnings == []

	def test_valid_blocked_status(self, ctrl: SwarmController) -> None:
		"""Blocked status is a valid status."""
		raw = {"status": "blocked", "summary": "Waiting on auth credentials for the API"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["status"] == "blocked"
		# No warning about status
		assert not any("status" in w.lower() for w in warnings)

	def test_valid_failed_status(self, ctrl: SwarmController) -> None:
		"""Failed status is a valid status."""
		raw = {"status": "failed", "summary": "Could not resolve the dependency conflict"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["status"] == "failed"
		assert not any("status" in w.lower() for w in warnings)


class TestMissingFields:
	def test_missing_status_defaults_to_failed(self, ctrl: SwarmController) -> None:
		"""Result without status gets 'failed'."""
		raw = {"summary": "Did some work on the parser"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["status"] == "failed"
		assert any("status" in w.lower() for w in warnings)

	def test_missing_summary_defaults_to_empty(self, ctrl: SwarmController) -> None:
		"""Result without summary gets empty string."""
		raw = {"status": "completed"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["summary"] == ""
		assert any("summary" in w.lower() for w in warnings)

	def test_missing_files_changed_defaults_to_empty_list(self, ctrl: SwarmController) -> None:
		"""Result without files_changed gets []."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["files_changed"] == []

	def test_missing_discoveries_defaults_to_empty_list(self, ctrl: SwarmController) -> None:
		"""Result without discoveries gets []."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["discoveries"] == []

	def test_missing_commits_defaults_to_empty_list(self, ctrl: SwarmController) -> None:
		"""Result without commits gets []."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["commits"] == []


class TestMalformedStatusCoercion:
	def test_invalid_status_string_coerced_to_failed(self, ctrl: SwarmController) -> None:
		"""Unrecognized status string defaults to 'failed'."""
		raw = {"status": "success", "summary": "Did some work on the module"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["status"] == "failed"
		assert any("success" in w for w in warnings)

	def test_none_status_coerced_to_failed(self, ctrl: SwarmController) -> None:
		"""None status defaults to 'failed'."""
		raw = {"status": None, "summary": "Did some work on the module"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["status"] == "failed"

	def test_numeric_status_coerced_to_failed(self, ctrl: SwarmController) -> None:
		"""Numeric status defaults to 'failed'."""
		raw = {"status": 0, "summary": "Did some work on the module"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["status"] == "failed"


class TestEmptySummaryHandling:
	def test_empty_string_summary_warns_short(self, ctrl: SwarmController) -> None:
		"""Empty string summary triggers a short-summary warning."""
		raw = {"status": "completed", "summary": ""}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["summary"] == ""
		assert any("short summary" in w.lower() for w in warnings)

	def test_short_summary_warns(self, ctrl: SwarmController) -> None:
		"""Summary under 20 chars triggers a warning."""
		raw = {"status": "completed", "summary": "done"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["summary"] == "done"
		assert any("short summary" in w.lower() for w in warnings)

	def test_non_string_summary_coerced(self, ctrl: SwarmController) -> None:
		"""Non-string summary (e.g. int) defaults to empty string."""
		raw = {"status": "completed", "summary": 42}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["summary"] == ""
		assert any("summary" in w.lower() for w in warnings)

	def test_adequate_summary_no_warning(self, ctrl: SwarmController) -> None:
		"""Summary >= 20 chars produces no warning."""
		raw = {"status": "completed", "summary": "Implemented full parser with tests"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["summary"] == "Implemented full parser with tests"
		assert not any("summary" in w.lower() for w in warnings)


class TestFilesChangedCoercion:
	def test_string_files_changed_coerced_to_list(self, ctrl: SwarmController) -> None:
		"""Comma-separated string becomes a list."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "files_changed": "a.py, b.py"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["files_changed"] == ["a.py", "b.py"]
		assert any("coerced" in w.lower() for w in warnings)

	def test_single_file_string_coerced(self, ctrl: SwarmController) -> None:
		"""Single file string becomes a one-element list."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "files_changed": "src/main.py"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["files_changed"] == ["src/main.py"]

	def test_non_list_non_string_defaults_to_empty(self, ctrl: SwarmController) -> None:
		"""Non-list, non-string files_changed defaults to []."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "files_changed": 42}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["files_changed"] == []
		assert any("files_changed" in w.lower() for w in warnings)


class TestDiscoveriesCoercion:
	def test_single_string_discovery_wrapped(self, ctrl: SwarmController) -> None:
		"""Single string discovery becomes a one-element list."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "discoveries": "found a bug"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["discoveries"] == ["found a bug"]
		assert any("discoveries" in w.lower() for w in warnings)

	def test_non_list_non_string_defaults_to_empty(self, ctrl: SwarmController) -> None:
		"""Non-list, non-string discoveries defaults to []."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "discoveries": 123}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["discoveries"] == []

	def test_list_discoveries_unchanged(self, ctrl: SwarmController) -> None:
		"""List discoveries pass through unchanged."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "discoveries": ["a", "b"]}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["discoveries"] == ["a", "b"]


class TestCommitsCoercion:
	def test_non_list_commits_defaults_to_empty(self, ctrl: SwarmController) -> None:
		"""Non-list commits defaults to []."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "commits": "abc123"}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["commits"] == []
		assert any("commits" in w.lower() for w in warnings)

	def test_list_commits_unchanged(self, ctrl: SwarmController) -> None:
		"""List commits pass through unchanged."""
		raw = {"status": "completed", "summary": "Refactored the entire auth module", "commits": ["abc", "def"]}
		result, warnings = ctrl._validate_ad_result(raw)
		assert result["commits"] == ["abc", "def"]


class TestOriginalNotMutated:
	def test_original_dict_not_mutated(self, ctrl: SwarmController) -> None:
		"""_validate_ad_result should not mutate the input dict."""
		raw = {"status": "banana", "summary": "short"}
		original_status = raw["status"]
		ctrl._validate_ad_result(raw)
		assert raw["status"] == original_status
