"""Tests for strategic context data layer: table creation, CRUD, migration, Mission fields."""

from __future__ import annotations

import pytest

from mission_control.db import Database
from mission_control.models import Mission, StrategicContext


@pytest.fixture()
def db() -> Database:
	"""In-memory Database with schema initialized."""
	return Database(":memory:")


def _insert_mission(db: Database, mission_id: str) -> None:
	"""Helper to insert a minimal mission for FK satisfaction."""
	db.insert_mission(Mission(id=mission_id, objective="test"))


class TestStrategicContextTable:
	def test_table_exists(self, db: Database) -> None:
		row = db.conn.execute(
			"SELECT name FROM sqlite_master WHERE type='table' AND name='strategic_context'"
		).fetchone()
		assert row is not None

	def test_insert_and_retrieve(self, db: Database) -> None:
		_insert_mission(db, "m1")
		ctx = StrategicContext(
			id="sc1",
			mission_id="m1",
			what_attempted="Built auth system",
			what_worked="JWT tokens",
			what_failed="Session cookies",
			recommended_next="Add refresh tokens",
		)
		db.insert_strategic_context(ctx)
		results = db.get_strategic_context(limit=10)
		assert len(results) == 1
		assert results[0].id == "sc1"
		assert results[0].mission_id == "m1"
		assert results[0].what_attempted == "Built auth system"
		assert results[0].what_worked == "JWT tokens"
		assert results[0].what_failed == "Session cookies"
		assert results[0].recommended_next == "Add refresh tokens"

	def test_limit_param(self, db: Database) -> None:
		for i in range(5):
			_insert_mission(db, f"m{i}")
			ctx = StrategicContext(
				id=f"sc{i}",
				mission_id=f"m{i}",
				what_attempted=f"Task {i}",
			)
			db.insert_strategic_context(ctx)
		results = db.get_strategic_context(limit=3)
		assert len(results) == 3

	def test_append_strategic_context(self, db: Database) -> None:
		_insert_mission(db, "m1")
		ctx = db.append_strategic_context(
			mission_id="m1",
			what_attempted="Refactored DB layer",
			what_worked="Migration pattern",
			what_failed="Nothing",
			recommended_next="Add indexes",
		)
		assert ctx.id  # auto-generated
		assert ctx.mission_id == "m1"
		results = db.get_strategic_context(limit=10)
		assert len(results) == 1
		assert results[0].what_attempted == "Refactored DB layer"

	def test_ordering_by_timestamp_desc(self, db: Database) -> None:
		_insert_mission(db, "m1")
		_insert_mission(db, "m2")
		ctx1 = StrategicContext(id="sc1", mission_id="m1", timestamp="2025-01-01T00:00:00Z")
		ctx2 = StrategicContext(id="sc2", mission_id="m2", timestamp="2025-06-01T00:00:00Z")
		db.insert_strategic_context(ctx1)
		db.insert_strategic_context(ctx2)
		results = db.get_strategic_context(limit=10)
		assert results[0].id == "sc2"
		assert results[1].id == "sc1"


class TestMissionNewFields:
	def test_insert_with_new_fields(self, db: Database) -> None:
		m = Mission(
			id="m2",
			objective="Build feature",
			ambition_score=7,
			next_objective="Optimize performance",
			proposed_by_strategist=True,
		)
		db.insert_mission(m)
		result = db.get_mission("m2")
		assert result is not None
		assert result.ambition_score == 7
		assert result.next_objective == "Optimize performance"
		assert result.proposed_by_strategist is True

	def test_update_new_fields(self, db: Database) -> None:
		m = Mission(id="m3", objective="Initial")
		db.insert_mission(m)
		m.ambition_score = 5
		m.next_objective = "Follow-up work"
		m.proposed_by_strategist = True
		db.update_mission(m)
		result = db.get_mission("m3")
		assert result is not None
		assert result.ambition_score == 5
		assert result.next_objective == "Follow-up work"
		assert result.proposed_by_strategist is True
