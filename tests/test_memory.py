"""Tests for memory/context loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from mission_control.config import MissionConfig, TargetConfig
from mission_control.db import Database
from mission_control.memory import (
	compress_history,
	load_context,
	load_context_for_work_unit,
	summarize_session,
)
from mission_control.models import Decision, Plan, Session, TaskRecord, WorkUnit
from mission_control.reviewer import ReviewVerdict


@pytest.fixture()
def db() -> Database:
	return Database(":memory:")


def _config(path: str = "/tmp/test") -> MissionConfig:
	return MissionConfig(target=TargetConfig(name="test", path=path))


class TestLoadContext:
	def test_empty_db(self, db: Database) -> None:
		task = TaskRecord(source="test_failure", description="Fix tests")
		context = load_context(task, db, _config())
		assert context == ""

	def test_includes_session_history(self, db: Database) -> None:
		db.insert_session(Session(id="s1", target_name="p", task_description="Fixed stuff", status="completed"))
		task = TaskRecord(source="test_failure", description="Fix tests")
		context = load_context(task, db, _config())
		assert "s1" in context
		assert "Fixed stuff" in context

	def test_includes_decisions(self, db: Database) -> None:
		db.insert_session(Session(id="s1", target_name="p"))
		db.insert_decision(Decision(id="d1", session_id="s1", decision="Use async", rationale="Performance"))
		task = TaskRecord(source="lint", description="Fix lint")
		context = load_context(task, db, _config())
		assert "Use async" in context

	def test_includes_claude_md(self, db: Database, tmp_path: Path) -> None:
		(tmp_path / "CLAUDE.md").write_text("# Test Project\nSome instructions")
		db.insert_session(Session(id="s1", target_name="p"))
		task = TaskRecord(source="lint", description="Fix lint")
		context = load_context(task, db, _config(str(tmp_path)))
		assert "Test Project" in context


class TestSummarizeSession:
	def test_basic_summary(self) -> None:
		session = Session(id="abc", task_description="Fix tests", output_summary="All done")
		verdict = ReviewVerdict(verdict="helped", improvements=["2 tests fixed"])
		summary = summarize_session(session, verdict)
		assert "abc" in summary
		assert "helped" in summary
		assert "2 tests fixed" in summary

	def test_regression_summary(self) -> None:
		session = Session(id="xyz", task_description="Refactor")
		verdict = ReviewVerdict(verdict="hurt", regressions=["3 tests broken"])
		summary = summarize_session(session, verdict)
		assert "hurt" in summary
		assert "3 tests broken" in summary


class TestCompressHistory:
	def test_empty(self) -> None:
		assert compress_history([]) == ""

	def test_fits_budget(self) -> None:
		sessions = [Session(id=f"s{i}", task_description=f"Task {i}", status="completed") for i in range(5)]
		result = compress_history(sessions, max_chars=10000)
		assert "s0" in result
		assert "s4" in result

	def test_truncates(self) -> None:
		sessions = [Session(id=f"s{i}", task_description=f"Task {i}", status="completed") for i in range(100)]
		result = compress_history(sessions, max_chars=200)
		assert "... and" in result
		assert "more sessions" in result


class TestLoadContextForWorkUnit:
	def test_includes_plan_objective(self, db: Database) -> None:
		plan = Plan(id="p1", objective="Fix all lint errors")
		db.insert_plan(plan)
		unit = WorkUnit(id="wu1", plan_id="p1", title="Fix file A")
		db.insert_work_unit(unit)

		context = load_context_for_work_unit(unit, db, _config())
		assert "Fix all lint errors" in context

	def test_includes_sibling_status(self, db: Database) -> None:
		plan = Plan(id="p1", objective="Fix things")
		db.insert_plan(plan)
		wu1 = WorkUnit(id="wu1", plan_id="p1", title="Fix A", status="completed")
		wu2 = WorkUnit(id="wu2", plan_id="p1", title="Fix B", status="running")
		wu3 = WorkUnit(id="wu3", plan_id="p1", title="Fix C", status="pending")
		db.insert_work_unit(wu1)
		db.insert_work_unit(wu2)
		db.insert_work_unit(wu3)

		context = load_context_for_work_unit(wu2, db, _config())
		assert "Fix A" in context
		assert "completed" in context
		assert "Fix C" in context
		# Should not include the unit itself
		assert context.count("Fix B") == 0

	def test_no_plan_returns_empty(self, db: Database) -> None:
		unit = WorkUnit(id="wu1", title="Orphan task")
		context = load_context_for_work_unit(unit, db, _config())
		assert context == ""

	def test_includes_project_claude_md(self, db: Database, tmp_path: Path) -> None:
		(tmp_path / "CLAUDE.md").write_text("# Project Rules\nAlways use tabs")
		plan = Plan(id="p1", objective="Refactor")
		db.insert_plan(plan)
		unit = WorkUnit(id="wu1", plan_id="p1", title="Task")
		db.insert_work_unit(unit)

		context = load_context_for_work_unit(unit, db, _config(str(tmp_path)))
		assert "Project Rules" in context
