"""Tests for structured progress report parsing in swarm context."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from autodev.swarm.context import ContextSynthesizer, parse_structured_report


def _make_config() -> MagicMock:
	config = MagicMock()
	config.target.name = "test-project"
	config.target.objective = "Build a compiler"
	config.target.resolved_path = "/tmp/test-project"
	return config


def _make_db() -> MagicMock:
	db = MagicMock()
	db.get_knowledge_for_mission.return_value = []
	return db


class TestParseStructuredReport:
	def test_full_structured_report(self) -> None:
		msg = {
			"from": "worker-1",
			"type": "report",
			"status": "working",
			"progress": "Implementing parser",
			"files_changed": ["src/parser.py", "tests/test_parser.py"],
			"tests_passing": 42,
			"text": "Working on parser implementation",
		}
		result = parse_structured_report(msg)
		assert result["from"] == "worker-1"
		assert result["type"] == "report"
		assert result["status"] == "working"
		assert result["progress"] == "Implementing parser"
		assert result["files_changed"] == ["src/parser.py", "tests/test_parser.py"]
		assert result["tests_passing"] == 42
		assert result["text"] == "Working on parser implementation"

	def test_unstructured_message_returns_base_fields_only(self) -> None:
		msg = {"from": "worker-1", "type": "report", "text": "Just a text message"}
		result = parse_structured_report(msg)
		assert result["from"] == "worker-1"
		assert result["text"] == "Just a text message"
		assert "status" not in result
		assert "progress" not in result
		assert "files_changed" not in result
		assert "tests_passing" not in result

	def test_invalid_status_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "status": "dancing", "text": "hi"}
		result = parse_structured_report(msg)
		assert "status" not in result

	def test_status_working(self) -> None:
		msg = {"from": "w", "type": "report", "status": "working", "text": "hi"}
		assert parse_structured_report(msg)["status"] == "working"

	def test_status_blocked(self) -> None:
		msg = {"from": "w", "type": "report", "status": "blocked", "text": "hi"}
		assert parse_structured_report(msg)["status"] == "blocked"

	def test_status_completed(self) -> None:
		msg = {"from": "w", "type": "report", "status": "completed", "text": "hi"}
		assert parse_structured_report(msg)["status"] == "completed"

	def test_non_string_status_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "status": 123, "text": "hi"}
		result = parse_structured_report(msg)
		assert "status" not in result

	def test_empty_progress_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "progress": "", "text": "hi"}
		result = parse_structured_report(msg)
		assert "progress" not in result

	def test_non_string_progress_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "progress": 42, "text": "hi"}
		result = parse_structured_report(msg)
		assert "progress" not in result

	def test_non_list_files_changed_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "files_changed": "single.py", "text": "hi"}
		result = parse_structured_report(msg)
		assert "files_changed" not in result

	def test_files_changed_with_non_string_elements_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "files_changed": ["ok.py", 42], "text": "hi"}
		result = parse_structured_report(msg)
		assert "files_changed" not in result

	def test_empty_files_changed_list_accepted(self) -> None:
		msg = {"from": "w", "type": "report", "files_changed": [], "text": "hi"}
		result = parse_structured_report(msg)
		assert result["files_changed"] == []

	def test_negative_tests_passing_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "tests_passing": -1, "text": "hi"}
		result = parse_structured_report(msg)
		assert "tests_passing" not in result

	def test_float_tests_passing_truncated_to_int(self) -> None:
		msg = {"from": "w", "type": "report", "tests_passing": 42.7, "text": "hi"}
		result = parse_structured_report(msg)
		assert result["tests_passing"] == 42

	def test_string_tests_passing_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "tests_passing": "many", "text": "hi"}
		result = parse_structured_report(msg)
		assert "tests_passing" not in result

	def test_zero_tests_passing_accepted(self) -> None:
		msg = {"from": "w", "type": "report", "tests_passing": 0, "text": "hi"}
		result = parse_structured_report(msg)
		assert result["tests_passing"] == 0

	def test_error_field_extracted(self) -> None:
		msg = {"from": "w", "type": "report", "status": "blocked", "error": "Missing API key", "text": "stuck"}
		result = parse_structured_report(msg)
		assert result["error"] == "Missing API key"

	def test_empty_error_ignored(self) -> None:
		msg = {"from": "w", "type": "report", "error": "", "text": "hi"}
		result = parse_structured_report(msg)
		assert "error" not in result

	def test_timestamp_preserved(self) -> None:
		msg = {"from": "w", "type": "report", "text": "hi", "timestamp": "2025-01-01T12:00:00Z"}
		result = parse_structured_report(msg)
		assert result["timestamp"] == "2025-01-01T12:00:00Z"

	def test_missing_from_defaults_to_unknown(self) -> None:
		msg = {"type": "report", "text": "hi"}
		result = parse_structured_report(msg)
		assert result["from"] == "unknown"

	def test_missing_text_defaults_to_empty(self) -> None:
		msg = {"from": "w", "type": "report"}
		result = parse_structured_report(msg)
		assert result["text"] == ""

	def test_extra_fields_not_leaked(self) -> None:
		msg = {"from": "w", "type": "report", "text": "hi", "secret": "value", "internal_id": 99}
		result = parse_structured_report(msg)
		assert "secret" not in result
		assert "internal_id" not in result


class TestGetAgentReports:
	def _make_ctx(self, tmp_path: Path, team_name: str = "test-team") -> ContextSynthesizer:
		config = _make_config()
		config.target.resolved_path = str(tmp_path)
		return ContextSynthesizer(config, _make_db(), team_name)

	def _setup_inbox_dir(self, tmp_path: Path, team_name: str = "test-team") -> Path:
		inbox_dir = tmp_path / ".claude" / "teams" / team_name / "inboxes"
		inbox_dir.mkdir(parents=True)
		return inbox_dir

	def test_returns_latest_structured_report_per_agent(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		messages = [
			{"from": "worker-1", "type": "report", "status": "working", "progress": "Starting", "text": "early"},
			{"from": "worker-1", "type": "report", "status": "completed", "progress": "All done", "text": "final"},
		]
		(inbox_dir / "team-lead.json").write_text(json.dumps(messages))
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert reports["worker-1"]["status"] == "completed"
		assert reports["worker-1"]["progress"] == "All done"

	def test_multiple_agents(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		(inbox_dir / "team-lead.json").write_text(json.dumps([
			{"from": "worker-1", "type": "report", "status": "working", "text": "busy"},
			{"from": "worker-2", "type": "report", "status": "blocked", "error": "No access", "text": "stuck"},
		]))
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert "worker-1" in reports
		assert "worker-2" in reports
		assert reports["worker-2"]["error"] == "No access"

	def test_skips_unstructured_messages(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		messages = [
			{"from": "worker-1", "type": "report", "text": "Just chatting, no status field"},
		]
		(inbox_dir / "team-lead.json").write_text(json.dumps(messages))
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert "worker-1" not in reports

	def test_empty_inbox_dir(self, tmp_path: Path) -> None:
		self._setup_inbox_dir(tmp_path)
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert reports == {}

	def test_missing_inbox_dir(self, tmp_path: Path) -> None:
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert reports == {}

	def test_corrupt_inbox_skipped(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		(inbox_dir / "bad.json").write_text("{corrupt")
		(inbox_dir / "good.json").write_text(json.dumps([
			{"from": "agent-1", "type": "report", "status": "working", "text": "ok"},
		]))
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert "agent-1" in reports

	def test_non_list_inbox_skipped(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		(inbox_dir / "bad.json").write_text('{"not": "a list"}')
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert reports == {}

	def test_non_dict_messages_skipped(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		(inbox_dir / "mixed.json").write_text(json.dumps([
			"just a string",
			42,
			{"from": "valid", "type": "report", "status": "working", "text": "ok"},
		]))
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert "valid" in reports

	def test_reads_from_multiple_inbox_files(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		(inbox_dir / "agent-1.json").write_text(json.dumps([
			{"from": "agent-1", "type": "report", "status": "working", "text": "busy"},
		]))
		(inbox_dir / "agent-2.json").write_text(json.dumps([
			{"from": "agent-2", "type": "report", "status": "completed", "text": "done"},
		]))
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert reports["agent-1"]["status"] == "working"
		assert reports["agent-2"]["status"] == "completed"

	def test_files_changed_included_in_report(self, tmp_path: Path) -> None:
		inbox_dir = self._setup_inbox_dir(tmp_path)
		(inbox_dir / "w.json").write_text(json.dumps([
			{
				"from": "w",
				"type": "report",
				"status": "working",
				"files_changed": ["a.py", "b.py"],
				"tests_passing": 10,
				"text": "progress",
			},
		]))
		ctx = self._make_ctx(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			reports = ctx.get_agent_reports()
		assert reports["w"]["files_changed"] == ["a.py", "b.py"]
		assert reports["w"]["tests_passing"] == 10


class TestRenderAgentReports:
	def test_render_includes_agent_reports_section(self, tmp_path: Path) -> None:
		config = _make_config()
		config.target.resolved_path = str(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		(inbox_dir / "team-lead.json").write_text(json.dumps([
			{
				"from": "worker-1",
				"type": "report",
				"status": "working",
				"progress": "Fixing parser",
				"tests_passing": 42,
				"files_changed": ["src/parser.py"],
				"text": "in progress",
			},
		]))
		ctx = ContextSynthesizer(config, _make_db(), "test-team")
		with patch.object(Path, "home", return_value=tmp_path):
			state = ctx.build_state(agents=[], tasks=[])
			rendered = ctx.render_for_planner(state)
		assert "Agent Progress Reports" in rendered
		assert "worker-1" in rendered
		assert "status=working" in rendered
		assert 'progress="Fixing parser"' in rendered
		assert "tests_passing=42" in rendered
		assert "src/parser.py" in rendered

	def test_render_no_reports_section_when_empty(self, tmp_path: Path) -> None:
		config = _make_config()
		config.target.resolved_path = str(tmp_path)
		ctx = ContextSynthesizer(config, _make_db(), "test-team")
		with patch.object(Path, "home", return_value=tmp_path):
			state = ctx.build_state(agents=[], tasks=[])
			rendered = ctx.render_for_planner(state)
		assert "Agent Progress Reports" not in rendered

	def test_render_blocked_agent_shows_error(self, tmp_path: Path) -> None:
		config = _make_config()
		config.target.resolved_path = str(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		(inbox_dir / "w.json").write_text(json.dumps([
			{
				"from": "worker-2",
				"type": "report",
				"status": "blocked",
				"error": "Missing dependency",
				"text": "blocked",
			},
		]))
		ctx = ContextSynthesizer(config, _make_db(), "test-team")
		with patch.object(Path, "home", return_value=tmp_path):
			state = ctx.build_state(agents=[], tasks=[])
			rendered = ctx.render_for_planner(state)
		assert "status=blocked" in rendered
		assert 'error="Missing dependency"' in rendered
