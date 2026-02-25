"""Tests for backlog item CRUD, query operations, and backlog manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mission_control.backlog_manager import BacklogManager
from mission_control.config import MissionConfig
from mission_control.continuous_controller import ContinuousController
from mission_control.db import Database
from mission_control.models import BacklogItem, Handoff, Mission, WorkUnit


class TestBacklogCRUD:
	def test_insert_and_get_backlog_item(self, db: Database) -> None:
		item = BacklogItem(
			id="bl1",
			title="Add auth module",
			description="Implement JWT authentication",
			priority_score=8.5,
			impact=9,
			effort=3,
			track="feature",
			status="pending",
			source_mission_id="m1",
			attempt_count=1,
			last_failure_reason="timeout",
			pinned_score=10.0,
			depends_on="bl0",
			tags="auth,security",
		)
		db.insert_backlog_item(item)
		result = db.get_backlog_item("bl1")
		assert result is not None
		assert result.id == "bl1"
		assert result.title == "Add auth module"
		assert result.description == "Implement JWT authentication"
		assert result.priority_score == 8.5
		assert result.impact == 9
		assert result.effort == 3
		assert result.track == "feature"
		assert result.status == "pending"
		assert result.source_mission_id == "m1"
		assert result.attempt_count == 1
		assert result.last_failure_reason == "timeout"
		assert result.pinned_score == 10.0
		assert result.depends_on == "bl0"
		assert result.tags == "auth,security"
		assert result.created_at == item.created_at
		assert result.updated_at == item.updated_at


class TestBacklogQueries:
	def test_list_backlog_items_all(self, db: Database) -> None:
		for i in range(5):
			db.insert_backlog_item(BacklogItem(id=f"bl{i}", title=f"Item {i}", priority_score=float(i)))
		items = db.list_backlog_items()
		assert len(items) == 5
		# Ordered by priority_score DESC
		scores = [it.priority_score for it in items]
		assert scores == sorted(scores, reverse=True)

	def test_get_pending_backlog_ordering(self, db: Database) -> None:
		"""Pinned items use pinned_score for ordering; unpinned use priority_score."""
		db.insert_backlog_item(BacklogItem(id="low", title="Low", priority_score=1.0, status="pending"))
		db.insert_backlog_item(BacklogItem(id="high", title="High", priority_score=9.0, status="pending"))
		db.insert_backlog_item(BacklogItem(
			id="pinned", title="Pinned", priority_score=2.0, pinned_score=20.0, status="pending",
		))
		results = db.get_pending_backlog()
		assert results[0].id == "pinned"  # pinned_score=20 sorts highest
		assert results[1].id == "high"  # priority_score=9
		assert results[2].id == "low"  # priority_score=1

	def test_search_backlog_items(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(id="bl1", title="Fix auth bug", tags="auth,security"))
		db.insert_backlog_item(BacklogItem(id="bl2", title="Add logging", description="structured logging for auth"))
		db.insert_backlog_item(BacklogItem(id="bl3", title="Update docs"))
		results = db.search_backlog_items(["auth"])
		assert len(results) == 2
		ids = {r.id for r in results}
		assert "bl1" in ids
		assert "bl2" in ids


class TestBacklogMutations:
	def test_update_attempt_count(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(id="bl1", title="Flaky", attempt_count=0))
		db.update_attempt_count("bl1", failure_reason="merge conflict")
		result = db.get_backlog_item("bl1")
		assert result is not None
		assert result.attempt_count == 1
		assert result.last_failure_reason == "merge conflict"
		# Increment again
		db.update_attempt_count("bl1", failure_reason="timeout")
		result = db.get_backlog_item("bl1")
		assert result is not None
		assert result.attempt_count == 2
		assert result.last_failure_reason == "timeout"

	def test_pin_backlog_score_affects_ordering(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(id="bl1", title="Low", priority_score=1.0, status="pending"))
		db.insert_backlog_item(BacklogItem(id="bl2", title="High", priority_score=9.0, status="pending"))
		# Pin the low-priority item above the high one
		db.pin_backlog_score("bl1", 50.0)
		results = db.get_pending_backlog()
		assert results[0].id == "bl1"
		assert results[1].id == "bl2"


# ============================================================================
# Tests consolidated from test_backlog_continuity.py
# ============================================================================


class TestTitleBasedMatching:
	"""Test that _update_backlog_from_completion finds backlog items by title."""

	def test_matches_by_title_keywords(self, config: MissionConfig, db: Database) -> None:
		"""Unit title keywords match against backlog item titles."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Add user authentication",
			description="OAuth2 flow", priority_score=8.0, track="feature",
			status="in_progress",
		))

		unit = WorkUnit(
			id="u1", plan_id="p1", title="Add user authentication module",
			status="completed", commit_hash="abc123", attempt=1, max_attempts=3,
		)
		handoff = Handoff(
			work_unit_id="u1", status="completed", summary="Done",
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, True, handoff, "mission-1")

		item = db.get_backlog_item("b1")
		assert item.status == "completed"

	def test_picks_best_match_by_keyword_overlap(self, config: MissionConfig, db: Database) -> None:
		"""When multiple items match, the one with most keyword overlap wins."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Fix database connection pooling",
			description="desc", priority_score=5.0, track="quality",
			status="in_progress",
		))
		db.insert_backlog_item(BacklogItem(
			id="b2", title="Add database migration scripts",
			description="desc", priority_score=6.0, track="feature",
			status="in_progress",
		))

		# Title overlaps more with b1: "fix", "database", "connection", "pooling"
		unit = WorkUnit(
			id="u1", plan_id="p1",
			title="Fix database connection pooling issues",
			status="completed", commit_hash="abc123", attempt=1, max_attempts=3,
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, True, None, "mission-1")

		# b1 should be completed (better match), b2 should remain in_progress
		assert db.get_backlog_item("b1").status == "completed"
		assert db.get_backlog_item("b2").status == "in_progress"

	def test_no_match_with_unrelated_title(self, config: MissionConfig, db: Database) -> None:
		"""Unit with completely unrelated title doesn't match any backlog item."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Implement caching layer",
			description="Redis caching", priority_score=7.0, track="feature",
			status="in_progress",
		))

		unit = WorkUnit(
			id="u1", plan_id="p1",
			title="Refactor authentication middleware",
			status="completed", commit_hash="abc123", attempt=1, max_attempts=3,
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, True, None, "mission-1")

		# Item should remain unchanged
		assert db.get_backlog_item("b1").status == "in_progress"


class TestStatusTransitionsOnSuccess:
	"""Test backlog item status updates on successful merge."""

	def test_sets_completed_on_merge(self, config: MissionConfig, db: Database) -> None:
		"""Successful merge marks matching backlog item as completed."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Implement search feature",
			description="Full-text search", priority_score=7.0, track="feature",
			status="in_progress",
		))

		unit = WorkUnit(
			id="u1", plan_id="p1", title="Implement search feature",
			status="completed", commit_hash="abc123", attempt=1, max_attempts=3,
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, True, None, "mission-1")

		item = db.get_backlog_item("b1")
		assert item.status == "completed"


class TestStatusTransitionsOnFailure:
	"""Test backlog item status updates on unit failure after max retries."""

	def test_increments_attempt_count(self, config: MissionConfig, db: Database) -> None:
		"""Failed unit after max retries increments backlog attempt_count."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Fix memory leak",
			description="desc", priority_score=6.0, track="quality",
			status="in_progress", attempt_count=1,
		))

		unit = WorkUnit(
			id="u1", plan_id="p1", title="Fix memory leak in worker",
			status="failed", attempt=3, max_attempts=3,
			output_summary="Segfault during test",
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, False, None, "m1")

		item = db.get_backlog_item("b1")
		assert item.attempt_count == 2  # Was 1, incremented to 2

	def test_sets_failure_reason_from_handoff_concerns(self, config: MissionConfig, db: Database) -> None:
		"""Failure reason comes from handoff concerns."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Fix memory leak",
			description="desc", priority_score=6.0, track="quality",
			status="in_progress",
		))

		handoff = Handoff(
			work_unit_id="u1", status="failed",
			concerns=["OOM at test_large_dataset"],
			summary="Failed",
		)

		unit = WorkUnit(
			id="u1", plan_id="p1", title="Fix memory leak in worker",
			status="failed", attempt=3, max_attempts=3,
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, False, handoff, "m1")

		item = db.get_backlog_item("b1")
		assert "OOM at test_large_dataset" in item.last_failure_reason


class TestPartialCompletionContextCarryForward:
	"""Test that partial completions append context to backlog descriptions."""

	def test_appends_discoveries_to_description(self, config: MissionConfig, db: Database) -> None:
		"""Partial completion appends handoff discoveries to backlog item description."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Refactor database layer",
			description="Original description",
			priority_score=5.0, track="quality", status="in_progress",
		))

		handoff = Handoff(
			work_unit_id="u1", status="completed",
			discoveries=["Found circular import in db.py"],
			concerns=[],
		)

		# attempt < max_attempts AND not merged -> partial
		unit = WorkUnit(
			id="u1", plan_id="p1", title="Refactor database layer",
			status="failed", attempt=1, max_attempts=3,
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, False, handoff, "m1")

		item = db.get_backlog_item("b1")
		assert item.status == "in_progress"
		assert "Found circular import in db.py" in item.description
		assert "Original description" in item.description


class TestNoMatchGracefulHandling:
	"""Test graceful handling when no backlog items match."""

	def test_no_backlog_items_in_db(self, config: MissionConfig, db: Database) -> None:
		"""No error when backlog is completely empty."""
		unit = WorkUnit(
			id="u1", plan_id="p1", title="Some task",
			status="completed", commit_hash="abc", attempt=1, max_attempts=3,
		)

		ctrl = ContinuousController(config, db)
		# Should not raise
		ctrl._update_backlog_from_completion(unit, True, None, "m1")


class TestMissionIdTracking:
	"""Test source_mission_id is tracked on backlog items."""

	def test_source_mission_id_set_on_successful_completion(self, config: MissionConfig, db: Database) -> None:
		"""source_mission_id is set when unit merge succeeds."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Build notification system",
			description="desc", priority_score=7.0, track="feature",
			status="in_progress",
		))

		unit = WorkUnit(
			id="u1", plan_id="p1", title="Build notification system",
			status="completed", commit_hash="abc", attempt=1, max_attempts=3,
		)

		ctrl = ContinuousController(config, db)
		ctrl._update_backlog_from_completion(unit, True, None, "mission-99")

		item = db.get_backlog_item("b1")
		assert item.source_mission_id == "mission-99"


# ============================================================================
# Tests consolidated from test_backlog_intake.py
# ============================================================================


class TestLoadBacklogObjective:
	"""Test _load_backlog_objective() method."""

	def test_loads_pending_items_as_objective(self, config: MissionConfig, db: Database) -> None:
		"""Top pending backlog items compose into an objective string."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Add auth", description="Implement OAuth",
			priority_score=8.0, track="feature",
		))
		db.insert_backlog_item(BacklogItem(
			id="b2", title="Fix XSS", description="Sanitize inputs",
			priority_score=9.0, track="security",
		))

		ctrl = ContinuousController(config, db)
		objective = ctrl._load_backlog_objective(limit=5)

		assert objective is not None
		assert "Fix XSS" in objective
		assert "Add auth" in objective
		assert "backlog_item_id=b1" in objective
		assert "backlog_item_id=b2" in objective

	def test_returns_none_on_empty_backlog(self, config: MissionConfig, db: Database) -> None:
		"""Returns None when no pending backlog items exist."""
		ctrl = ContinuousController(config, db)
		objective = ctrl._load_backlog_objective(limit=5)

		assert objective is None
		assert ctrl._backlog_item_ids == []

	def test_only_loads_pending_items(self, config: MissionConfig, db: Database) -> None:
		"""Does not load items that are already in_progress or completed."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Pending", description="desc",
			priority_score=5.0, track="quality", status="pending",
		))
		db.insert_backlog_item(BacklogItem(
			id="b2", title="In Progress", description="desc",
			priority_score=9.0, track="quality", status="in_progress",
		))
		db.insert_backlog_item(BacklogItem(
			id="b3", title="Completed", description="desc",
			priority_score=9.0, track="quality", status="completed",
		))

		ctrl = ContinuousController(config, db)
		objective = ctrl._load_backlog_objective(limit=5)

		assert objective is not None
		assert "Pending" in objective
		assert "In Progress" not in objective
		assert "Completed" not in objective
		assert ctrl._backlog_item_ids == ["b1"]


class TestUpdateBacklogOnCompletion:
	"""Test _update_backlog_on_completion() method."""

	def test_marks_completed_on_success(self, config: MissionConfig, db: Database) -> None:
		"""When objective_met=True, all targeted items become completed."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Task 1", description="desc",
			priority_score=5.0, track="quality", status="in_progress",
		))
		db.insert_backlog_item(BacklogItem(
			id="b2", title="Task 2", description="desc",
			priority_score=3.0, track="feature", status="in_progress",
		))

		ctrl = ContinuousController(config, db)
		ctrl._backlog_item_ids = ["b1", "b2"]
		ctrl._update_backlog_on_completion(objective_met=True, handoffs=[])

		assert db.get_backlog_item("b1").status == "completed"
		assert db.get_backlog_item("b2").status == "completed"

	def test_resets_to_pending_on_failure(self, config: MissionConfig, db: Database) -> None:
		"""When objective_met=False, items reset to pending with incremented attempt_count."""
		db.insert_backlog_item(BacklogItem(
			id="b1", title="Task 1", description="desc",
			priority_score=5.0, track="quality", status="in_progress",
			attempt_count=0,
		))

		ctrl = ContinuousController(config, db)
		ctrl._backlog_item_ids = ["b1"]
		ctrl._update_backlog_on_completion(objective_met=False, handoffs=[])

		item = db.get_backlog_item("b1")
		assert item.status == "pending"
		assert item.attempt_count == 1


class TestDiscoveryToBacklog:
	"""Test that post-mission discovery items flow into the persistent backlog."""

	@pytest.mark.asyncio
	async def test_discovery_items_inserted_to_backlog(self, config: MissionConfig, db: Database) -> None:
		"""Discovery items are converted to BacklogItems and inserted."""
		mock_items = [
			BacklogItem(
				id="d1", title="Add caching", description="Redis caching layer",
				priority_score=7.0, impact=8, effort=5, track="feature",
			),
			BacklogItem(
				id="d2", title="Fix SQL injection", description="Parameterize queries",
				priority_score=9.0, impact=10, effort=3, track="security",
			),
		]

		mock_engine = MagicMock()
		mock_engine.discover = AsyncMock(return_value=(MagicMock(), mock_items))

		ctrl = ContinuousController(config, db)

		with patch("mission_control.auto_discovery.DiscoveryEngine", return_value=mock_engine):
			await ctrl._run_post_mission_discovery()

		# Check backlog items were created
		backlog = db.list_backlog_items(limit=10)
		assert len(backlog) >= 2
		titles = {item.title for item in backlog}
		assert "Add caching" in titles
		assert "Fix SQL injection" in titles

		# Verify fields are mapped correctly
		caching_items = [i for i in backlog if i.title == "Add caching"]
		assert len(caching_items) == 1
		assert caching_items[0].priority_score == 7.0
		assert caching_items[0].impact == 8
		assert caching_items[0].effort == 5
		assert caching_items[0].track == "feature"


class TestBacklogIntegrationInRun:
	"""Test backlog integration points in the run() method."""

	def test_backlog_merged_with_existing_objective(self, config: MissionConfig, db: Database) -> None:
		"""Backlog items are appended to existing config objective."""
		config.target.objective = "Build the widget"
		config.discovery.enabled = True

		db.insert_backlog_item(BacklogItem(
			id="b1", title="Add tests", description="Coverage for auth module",
			priority_score=7.0, track="quality",
		))

		ctrl = ContinuousController(config, db)
		backlog_objective = ctrl._load_backlog_objective(limit=5)

		assert backlog_objective is not None
		# Simulate what run() does
		merged = config.target.objective + "\n\n" + backlog_objective
		assert "Build the widget" in merged
		assert "Add tests" in merged
		assert "backlog_item_id=b1" in merged



# ============================================================================
# Tests consolidated from test_backlog_manager.py
# ============================================================================


class TestManagerLoadBacklogObjective:
	def test_no_pending_items_returns_none(self, config: MissionConfig, db: Database) -> None:
		mgr = BacklogManager(db, config)
		result = mgr.load_backlog_objective()
		assert result is None
		assert mgr.backlog_item_ids == []

	def test_loads_pending_items(self, config: MissionConfig, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Add auth", description="Implement auth module",
			priority_score=8.0, track="feature", status="pending",
		))
		db.insert_backlog_item(BacklogItem(
			id="bl2", title="Fix tests", description="Fix broken tests",
			priority_score=6.0, track="quality", status="pending",
		))

		mgr = BacklogManager(db, config)
		result = mgr.load_backlog_objective()

		assert result is not None
		assert "Add auth" in result
		assert "Fix tests" in result
		assert "backlog_item_id=bl1" in result
		assert mgr.backlog_item_ids == ["bl1", "bl2"]

		# Items should be marked in_progress
		item1 = db.get_backlog_item("bl1")
		assert item1 is not None
		assert item1.status == "in_progress"
		item2 = db.get_backlog_item("bl2")
		assert item2 is not None
		assert item2.status == "in_progress"

	def test_respects_limit(self, config: MissionConfig, db: Database) -> None:
		for i in range(5):
			db.insert_backlog_item(BacklogItem(
				id=f"bl{i}", title=f"Item {i}", description=f"Desc {i}",
				priority_score=float(10 - i), track="feature", status="pending",
			))

		mgr = BacklogManager(db, config)
		result = mgr.load_backlog_objective(limit=2)

		assert result is not None
		assert len(mgr.backlog_item_ids) == 2

	def test_skips_non_pending(self, config: MissionConfig, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Completed", description="Already done",
			priority_score=8.0, track="feature", status="completed",
		))
		db.insert_backlog_item(BacklogItem(
			id="bl2", title="Pending", description="To do",
			priority_score=6.0, track="quality", status="pending",
		))

		mgr = BacklogManager(db, config)
		result = mgr.load_backlog_objective()

		assert result is not None
		assert "Pending" in result
		assert "Completed" not in result
		assert mgr.backlog_item_ids == ["bl2"]


class TestManagerUpdateBacklogOnCompletion:
	def test_marks_completed_on_success(self, config: MissionConfig, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Task A", priority_score=5.0,
			track="feature", status="in_progress",
		))

		mgr = BacklogManager(db, config)
		mgr.backlog_item_ids = ["bl1"]
		mgr.update_backlog_on_completion(objective_met=True, handoffs=[])

		item = db.get_backlog_item("bl1")
		assert item is not None
		assert item.status == "completed"

	def test_resets_to_pending_on_failure(self, config: MissionConfig, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Task A", priority_score=5.0,
			track="feature", status="in_progress",
		))

		mgr = BacklogManager(db, config)
		mgr.backlog_item_ids = ["bl1"]
		mgr.update_backlog_on_completion(objective_met=False, handoffs=[])

		item = db.get_backlog_item("bl1")
		assert item is not None
		assert item.status == "pending"
		assert item.attempt_count == 1

	def test_stores_failure_reasons(self, config: MissionConfig, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Task A", priority_score=5.0,
			track="feature", status="in_progress",
		))

		handoff = Handoff(
			id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
			status="failed", summary="Import error",
			concerns=["Could not import module X"],
		)

		mgr = BacklogManager(db, config)
		mgr.backlog_item_ids = ["bl1"]
		mgr.update_backlog_on_completion(objective_met=False, handoffs=[handoff])

		item = db.get_backlog_item("bl1")
		assert item is not None
		assert "Could not import module X" in item.last_failure_reason

	def test_no_items_does_nothing(self, config: MissionConfig, db: Database) -> None:
		mgr = BacklogManager(db, config)
		mgr.backlog_item_ids = []
		mgr.update_backlog_on_completion(objective_met=True, handoffs=[])

	def test_missing_item_skipped(self, config: MissionConfig, db: Database) -> None:
		mgr = BacklogManager(db, config)
		mgr.backlog_item_ids = ["nonexistent"]
		mgr.update_backlog_on_completion(objective_met=True, handoffs=[])


class TestUpdateBacklogFromCompletion:
	def _setup_backlog(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Add authentication module",
			description="Implement JWT auth",
			priority_score=8.0, track="feature", status="in_progress",
		))

	def test_merged_unit_marks_completed(self, config: MissionConfig, db: Database) -> None:
		self._setup_backlog(db)
		db.insert_mission(Mission(id="m1", objective="test"))

		unit = WorkUnit(id="wu1", plan_id="p1", title="Add authentication module")
		mgr = BacklogManager(db, config)
		mgr.update_backlog_from_completion(unit, merged=True, handoff=None, mission_id="m1")

		item = db.get_backlog_item("bl1")
		assert item is not None
		assert item.status == "completed"
		assert item.source_mission_id == "m1"

	def test_failed_max_attempts_records_failure(self, config: MissionConfig, db: Database) -> None:
		self._setup_backlog(db)

		unit = WorkUnit(
			id="wu1", plan_id="p1", title="Add authentication module",
			attempt=3, max_attempts=3, output_summary="Tests failed",
		)
		mgr = BacklogManager(db, config)
		mgr.update_backlog_from_completion(unit, merged=False, handoff=None, mission_id="m1")

		item = db.get_backlog_item("bl1")
		assert item is not None
		assert item.attempt_count == 1
		assert "Tests failed" in item.last_failure_reason

	def test_partial_completion_appends_context(self, config: MissionConfig, db: Database) -> None:
		self._setup_backlog(db)

		handoff = Handoff(
			id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
			status="failed",
			discoveries=["Found pattern X"],
			concerns=["Watch out for Y"],
		)
		unit = WorkUnit(
			id="wu1", plan_id="p1", title="Add authentication module",
			attempt=1, max_attempts=3,
		)
		mgr = BacklogManager(db, config)
		mgr.update_backlog_from_completion(unit, merged=False, handoff=handoff, mission_id="m1")

		item = db.get_backlog_item("bl1")
		assert item is not None
		assert item.status == "in_progress"
		assert "Found pattern X" in item.description

	def test_no_matching_item_does_nothing(self, config: MissionConfig, db: Database) -> None:
		unit = WorkUnit(id="wu1", plan_id="p1", title="Completely unrelated task")
		mgr = BacklogManager(db, config)
		mgr.update_backlog_from_completion(unit, merged=True, handoff=None, mission_id="m1")

	def test_short_title_words_skipped(self, config: MissionConfig, db: Database) -> None:
		"""Unit titles with only short words (<=2 chars) should be skipped."""
		self._setup_backlog(db)
		unit = WorkUnit(id="wu1", plan_id="p1", title="do it")
		mgr = BacklogManager(db, config)
		mgr.update_backlog_from_completion(unit, merged=True, handoff=None, mission_id="m1")

		# "do" and "it" are both <= 2 chars, so no matching happens
		item = db.get_backlog_item("bl1")
		assert item is not None
		assert item.status == "in_progress"  # unchanged


class TestRecalculatePrioritiesIntegration:
	"""Test that recalculate_priorities is wired into BacklogManager."""

	def test_recalculate_called_before_selection(
		self, db: Database, config: MissionConfig,
	) -> None:
		item = BacklogItem(
			id="bl1", title="Task A", impact=10, effort=1,
			priority_score=0.0, track="feature", status="pending",
		)
		db.insert_backlog_item(item)
		mgr = BacklogManager(db, config)

		with patch(
			"mission_control.backlog_manager.recalculate_priorities"
		) as mock_recalc:
			mgr.load_backlog_objective(limit=5)
			mock_recalc.assert_called_once_with(db)

	def test_scores_are_fresh_when_selecting(
		self, db: Database, config: MissionConfig,
	) -> None:
		item = BacklogItem(
			id="bl1", title="Task A", impact=10, effort=1,
			priority_score=0.0, track="feature", status="pending",
		)
		db.insert_backlog_item(item)
		mgr = BacklogManager(db, config)

		objective = mgr.load_backlog_objective(limit=5)
		assert objective is not None
		assert "priority=10.0" in objective

	def test_recalculate_called_after_failure(
		self, db: Database, config: MissionConfig,
	) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Task A", priority_score=5.0,
			track="feature", status="in_progress",
		))
		mgr = BacklogManager(db, config)
		mgr.backlog_item_ids = ["bl1"]

		with patch(
			"mission_control.backlog_manager.recalculate_priorities"
		) as mock_recalc:
			mgr.update_backlog_on_completion(objective_met=False, handoffs=[])
			mock_recalc.assert_called_once_with(db)

	def test_recalculate_called_after_success(
		self, db: Database, config: MissionConfig,
	) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Task A", priority_score=5.0,
			track="feature", status="in_progress",
		))
		mgr = BacklogManager(db, config)
		mgr.backlog_item_ids = ["bl1"]

		with patch(
			"mission_control.backlog_manager.recalculate_priorities"
		) as mock_recalc:
			mgr.update_backlog_on_completion(objective_met=True, handoffs=[])
			mock_recalc.assert_called_once_with(db)

	def test_no_recalculate_when_no_items(
		self, db: Database, config: MissionConfig,
	) -> None:
		mgr = BacklogManager(db, config)
		with patch(
			"mission_control.backlog_manager.recalculate_priorities"
		) as mock_recalc:
			mgr.update_backlog_on_completion(objective_met=True, handoffs=[])
			mock_recalc.assert_not_called()


class TestDependencyFiltering:
	"""Test depends_on filtering in get_pending_backlog."""

	def test_unblocked_items_returned(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="No deps", priority_score=5.0,
			track="feature", status="pending",
		))
		result = db.get_pending_backlog()
		assert len(result) == 1
		assert result[0].title == "No deps"

	def test_blocked_items_excluded(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="dep1", title="Dependency", priority_score=5.0,
			track="feature", status="pending",
		))
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Blocked", priority_score=8.0,
			track="feature", status="pending", depends_on="dep1",
		))
		result = db.get_pending_backlog()
		titles = [i.title for i in result]
		assert "Dependency" in titles
		assert "Blocked" not in titles

	def test_unblocked_when_dep_completed(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="dep1", title="Dependency", priority_score=5.0,
			track="feature", status="completed",
		))
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Ready", priority_score=8.0,
			track="feature", status="pending", depends_on="dep1",
		))
		result = db.get_pending_backlog()
		titles = [i.title for i in result]
		assert "Ready" in titles

	def test_multiple_deps_all_must_complete(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="dep1", title="Dep 1", priority_score=5.0,
			track="feature", status="completed",
		))
		db.insert_backlog_item(BacklogItem(
			id="dep2", title="Dep 2", priority_score=5.0,
			track="feature", status="pending",
		))
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Multi-dep", priority_score=8.0,
			track="feature", status="pending", depends_on="dep1,dep2",
		))
		result = db.get_pending_backlog()
		titles = [i.title for i in result]
		assert "Multi-dep" not in titles

	def test_empty_depends_on_treated_as_unblocked(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Free", priority_score=5.0,
			track="feature", status="pending", depends_on="",
		))
		result = db.get_pending_backlog()
		assert len(result) == 1

	def test_missing_dep_id_blocks(self, db: Database) -> None:
		db.insert_backlog_item(BacklogItem(
			id="bl1", title="Bad dep", priority_score=5.0,
			track="feature", status="pending", depends_on="nonexistent-id",
		))
		result = db.get_pending_backlog()
		assert len(result) == 0
