"""Tests for priority recalculation engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mission_control.db import Database
from mission_control.models import BacklogItem
from mission_control.priority import (
	_compute_base_score,
	_compute_failure_penalty,
	_is_stale,
	parse_backlog_md,
	recalculate_priorities,
)


@pytest.fixture()
def db() -> Database:
	return Database(":memory:")


def _make_item(**overrides: object) -> BacklogItem:
	defaults = {
		"title": "Test item",
		"description": "A test backlog item",
		"impact": 8,
		"effort": 3,
		"status": "pending",
	}
	defaults.update(overrides)
	return BacklogItem(**defaults)


class TestBaseScore:
	def test_formula(self) -> None:
		assert _compute_base_score(10, 1) == 10.0
		assert _compute_base_score(10, 10) == 1.0
		assert _compute_base_score(8, 3) == pytest.approx(6.4)


class TestFailurePenalty:
	def test_no_failures(self) -> None:
		assert _compute_failure_penalty(0, None) == 0.0

	def test_one_failure(self) -> None:
		assert _compute_failure_penalty(1, "test failed") == pytest.approx(0.2)

	def test_cap_at_60_percent(self) -> None:
		assert _compute_failure_penalty(5, "test failed") == pytest.approx(0.6)
		assert _compute_failure_penalty(10, "some error") == pytest.approx(0.6)

	def test_infrastructure_exception(self) -> None:
		assert _compute_failure_penalty(3, "infrastructure error on deploy") == 0.0


class TestStaleness:
	def test_not_stale(self) -> None:
		now = datetime.now(timezone.utc)
		recent = (now - timedelta(hours=1)).isoformat()
		assert _is_stale(recent, now) is False

	def test_stale(self) -> None:
		now = datetime.now(timezone.utc)
		old = (now - timedelta(hours=100)).isoformat()
		assert _is_stale(old, now) is True


class TestRecalculatePriorities:
	def test_basic_scoring(self, db: Database) -> None:
		item = _make_item(impact=10, effort=1)
		db.insert_backlog_item(item)
		updated = recalculate_priorities(db)
		assert len(updated) == 1
		assert updated[0].priority_score == pytest.approx(10.0)

	def test_failure_penalty_applied(self, db: Database) -> None:
		item = _make_item(impact=10, effort=1, attempt_count=2, last_failure_reason="test failed")
		db.insert_backlog_item(item)
		updated = recalculate_priorities(db)
		assert len(updated) == 1
		expected = 10.0 * (1.0 - 0.4)
		assert updated[0].priority_score == pytest.approx(expected)

	def test_pinned_score_override(self, db: Database) -> None:
		item = _make_item(impact=10, effort=1, pinned_score=42.0)
		db.insert_backlog_item(item)
		updated = recalculate_priorities(db)
		assert len(updated) == 1
		assert updated[0].priority_score == 42.0

	def test_completed_items_skipped(self, db: Database) -> None:
		item = _make_item(impact=10, effort=1, status="completed", priority_score=5.0)
		db.insert_backlog_item(item)
		updated = recalculate_priorities(db)
		assert len(updated) == 0

	def test_in_progress_items_recalculated(self, db: Database) -> None:
		item = _make_item(impact=10, effort=1, status="in_progress")
		db.insert_backlog_item(item)
		updated = recalculate_priorities(db)
		assert len(updated) == 1
		assert updated[0].priority_score == pytest.approx(10.0)


class TestParseBacklogMd:
	def test_parse_basic(self, tmp_path: object) -> None:
		from pathlib import Path
		md = tmp_path / "BACKLOG.md"  # type: ignore[operator]
		md.write_text(
			"# Backlog\n\n"
			"## P0: Critical task\n"
			"This is the description.\n"
			"More details here.\n"
			"\n"
			"## P3: Medium task\n"
			"Medium priority description.\n"
		)
		items = parse_backlog_md(Path(str(md)))
		assert len(items) == 2
		assert items[0].title == "Critical task"
		assert items[0].impact == 10
		assert items[0].priority_score == pytest.approx(6.0)
		assert items[1].title == "Medium task"
		assert items[1].impact == 7
		assert items[1].priority_score == pytest.approx(4.2)

	def test_parse_real_format(self, tmp_path: object) -> None:
		from pathlib import Path
		md = tmp_path / "BACKLOG.md"  # type: ignore[operator]
		md.write_text(
			"# Backlog\n\n"
			"## P0: Replace LLM Evaluator\n\n"
			"**Problem**: The evaluator is expensive.\n\n"
			"**Files**: evaluator.py\n\n"
			"---\n\n"
			"## P1: N-of-M Candidate Selection\n\n"
			"**Problem**: Fixup makes one attempt.\n"
		)
		items = parse_backlog_md(Path(str(md)))
		assert len(items) == 2
		assert items[0].title == "Replace LLM Evaluator"
		assert items[0].impact == 10
		assert "expensive" in items[0].description
		assert items[1].title == "N-of-M Candidate Selection"
		assert items[1].impact == 9

	def test_parse_description_multiline(self, tmp_path: object) -> None:
		from pathlib import Path
		md = tmp_path / "BACKLOG.md"  # type: ignore[operator]
		md.write_text(
			"## P2: Multi-line task\n"
			"Line one.\n"
			"Line two.\n"
			"Line three.\n"
		)
		items = parse_backlog_md(Path(str(md)))
		assert len(items) == 1
		assert "Line one." in items[0].description
		assert "Line three." in items[0].description
