"""Tests for LiveDashboard agent trace visualization features."""

from __future__ import annotations

from starlette.testclient import TestClient

from mission_control.dashboard.live import LiveDashboard, _serialize_event
from mission_control.db import Database
from mission_control.models import (
	Epoch,
	Mission,
	Plan,
	UnitEvent,
	Worker,
	WorkUnit,
)


def _make_db() -> Database:
	return Database(":memory:")


def _insert_mission(db: Database, mission_id: str = "m1", **kwargs) -> Mission:
	defaults = {
		"id": mission_id,
		"objective": "Test mission",
		"status": "running",
		"total_cost_usd": 1.0,
	}
	defaults.update(kwargs)
	m = Mission(**defaults)
	db.insert_mission(m)
	return m


def _insert_epoch(db: Database, epoch_id: str = "ep1", mission_id: str = "m1", **kwargs) -> Epoch:
	defaults = {
		"id": epoch_id,
		"mission_id": mission_id,
		"number": 1,
		"units_planned": 3,
		"units_completed": 1,
		"units_failed": 0,
		"score_at_start": 0.0,
		"score_at_end": 40.0,
	}
	defaults.update(kwargs)
	e = Epoch(**defaults)
	db.insert_epoch(e)
	return e


def _insert_work_unit(db: Database, unit_id: str = "wu1", plan_id: str = "plan1", **kwargs) -> WorkUnit:
	defaults = {
		"id": unit_id,
		"plan_id": plan_id,
		"title": f"Task {unit_id}",
		"status": "pending",
	}
	defaults.update(kwargs)
	wu = WorkUnit(**defaults)
	db.insert_work_unit(wu)
	return wu


def _insert_worker(db: Database, worker_id: str = "w1", **kwargs) -> Worker:
	defaults = {
		"id": worker_id,
		"workspace_path": f"/tmp/{worker_id}",
		"status": "idle",
	}
	defaults.update(kwargs)
	w = Worker(**defaults)
	db.insert_worker(w)
	return w


def _insert_unit_event(db: Database, event_id: str = "evt1", **kwargs) -> UnitEvent:
	defaults = {
		"id": event_id,
		"mission_id": "m1",
		"epoch_id": "ep1",
		"work_unit_id": "wu1",
		"event_type": "dispatched",
		"details": "",
		"input_tokens": 100,
		"output_tokens": 50,
	}
	defaults.update(kwargs)
	e = UnitEvent(**defaults)
	db.insert_unit_event(e)
	return e


def _populated_db() -> Database:
	"""Build a DB with mission, epoch, workers, units, and events."""
	db = _make_db()

	_insert_mission(db, "m1", total_cost_usd=2.0)

	# Plan must exist before work units (FK constraint)
	p = Plan(id="plan1", objective="test plan")
	db.insert_plan(p)

	_insert_epoch(db, "ep1", "m1", number=1, finished_at="2025-06-01T11:00:00")
	_insert_epoch(db, "ep2", "m1", number=2)

	_insert_worker(
		db, "w1", status="working", current_unit_id="wu2",
		units_completed=1, units_failed=0, total_cost_usd=0.5,
	)
	_insert_worker(db, "w2", status="idle", units_completed=0, units_failed=1, total_cost_usd=0.3)

	_insert_work_unit(
		db, "wu1", "plan1", status="completed", worker_id="w1",
		epoch_id="ep1", started_at="2025-06-01T10:00:00",
		finished_at="2025-06-01T10:30:00",
		input_tokens=500, output_tokens=200, cost_usd=0.3,
	)
	_insert_work_unit(
		db, "wu2", "plan1", status="running", worker_id="w1",
		epoch_id="ep2", started_at="2025-06-01T11:00:00",
		input_tokens=300, output_tokens=100, cost_usd=0.2,
	)
	_insert_work_unit(
		db, "wu3", "plan1", status="failed", worker_id="w2",
		epoch_id="ep1", started_at="2025-06-01T10:15:00",
		finished_at="2025-06-01T10:45:00",
		input_tokens=400, output_tokens=150, cost_usd=0.25,
	)

	_insert_unit_event(
		db, "evt1", work_unit_id="wu1",
		event_type="dispatched", input_tokens=100, output_tokens=50,
	)
	_insert_unit_event(
		db, "evt2", work_unit_id="wu1",
		event_type="merged", input_tokens=200, output_tokens=100,
	)
	_insert_unit_event(
		db, "evt3", work_unit_id="wu2",
		event_type="dispatched", input_tokens=150, output_tokens=75,
	)
	_insert_unit_event(
		db, "evt4", work_unit_id="wu3",
		event_type="dispatched", epoch_id="ep1",
		input_tokens=50, output_tokens=25,
	)
	_insert_unit_event(
		db, "evt5", work_unit_id="wu3",
		event_type="failed", epoch_id="ep1",
		input_tokens=0, output_tokens=0,
	)

	return db


# -- DB query tests --


class TestGetUnitsForWorker:
	def test_returns_correct_units(self) -> None:
		db = _populated_db()
		units = db.get_units_for_worker("w1")
		assert len(units) == 2
		unit_ids = {u.id for u in units}
		assert unit_ids == {"wu1", "wu2"}

	def test_empty_for_unknown_worker(self) -> None:
		db = _populated_db()
		units = db.get_units_for_worker("nonexistent")
		assert units == []


class TestGetUnitEventsForWorker:
	def test_join_returns_correct_events(self) -> None:
		db = _populated_db()
		events = db.get_unit_events_for_worker("w1")
		# w1 has wu1 (evt1, evt2) and wu2 (evt3) = 3 events
		assert len(events) == 3
		event_ids = {e.id for e in events}
		assert event_ids == {"evt1", "evt2", "evt3"}

	def test_empty_for_unknown_worker(self) -> None:
		db = _populated_db()
		events = db.get_unit_events_for_worker("nonexistent")
		assert events == []

	def test_limit_respected(self) -> None:
		db = _populated_db()
		events = db.get_unit_events_for_worker("w1", limit=2)
		assert len(events) == 2


class TestGetWorkerStats:
	def test_aggregation(self) -> None:
		db = _populated_db()
		stats = db.get_worker_stats("w1")
		assert stats["units_total"] == 2
		assert stats["units_completed"] == 1
		assert stats["units_failed"] == 0
		assert stats["total_input_tokens"] == 800  # 500 + 300
		assert stats["total_output_tokens"] == 300  # 200 + 100
		assert abs(stats["total_cost_usd"] - 0.5) < 0.01
		assert stats["first_unit_at"] is not None

	def test_worker_with_failures(self) -> None:
		db = _populated_db()
		stats = db.get_worker_stats("w2")
		assert stats["units_total"] == 1
		assert stats["units_completed"] == 0
		assert stats["units_failed"] == 1

	def test_unknown_worker(self) -> None:
		db = _populated_db()
		stats = db.get_worker_stats("nonexistent")
		assert stats["units_total"] == 0


# -- Serialize event with tokens --


class TestSerializeEvent:
	def test_includes_tokens(self) -> None:
		db = _populated_db()
		events = db.get_unit_events_for_worker("w1")
		serialized = _serialize_event(events[0])
		assert "input_tokens" in serialized
		assert "output_tokens" in serialized
		assert serialized["input_tokens"] >= 0


# -- LiveDashboard integration tests --


def _make_dashboard(db: Database) -> LiveDashboard:
	"""Create a LiveDashboard backed by an in-memory Database."""
	dash = LiveDashboard.__new__(LiveDashboard)
	dash.db = db
	dash.auth_token = ""
	dash._connections = set()
	dash._broadcast_task = None
	dash._ui_concrete = None
	dash._ui_mtime = 0.0
	dash._signal_timestamps = {}
	return dash


class TestBuildAgentActivity:
	def test_includes_planner_and_workers(self) -> None:
		db = _populated_db()
		dash = _make_dashboard(db)

		workers = db.get_all_workers()
		epochs = db.get_epochs_for_mission("m1")
		units = db.get_units_for_worker("w1") + db.get_units_for_worker("w2")

		activity = dash._build_agent_activity(workers, epochs, units)

		assert len(activity) == 3  # planner + 2 workers
		assert activity[0]["agent_id"] == "planner"
		assert activity[0]["role"] == "planner"

		worker_agents = [a for a in activity if a["role"] == "worker"]
		assert len(worker_agents) == 2
		worker_ids = {a["agent_id"] for a in worker_agents}
		assert worker_ids == {"w1", "w2"}

	def test_planner_status(self) -> None:
		db = _populated_db()
		dash = _make_dashboard(db)

		workers = db.get_all_workers()
		epochs = db.get_epochs_for_mission("m1")
		units = db.get_units_for_worker("w1") + db.get_units_for_worker("w2")

		activity = dash._build_agent_activity(workers, epochs, units)
		planner = activity[0]
		# Epoch 2 has no finished_at and wu2 is running -> not "planning"
		assert planner["status"] in ("idle", "planning")


class TestBuildPlannerDetail:
	def test_returns_synthetic_events(self) -> None:
		db = _populated_db()
		dash = _make_dashboard(db)

		mission = db.get_active_mission()
		detail = dash._build_planner_detail(mission)

		assert detail["agent_id"] == "planner"
		assert detail["role"] == "planner"
		assert len(detail["events"]) >= 2  # at least planning_started for each epoch
		assert len(detail["epochs"]) == 2
		assert detail["stats"]["epochs_total"] == 2

	def test_events_include_epoch_completed(self) -> None:
		db = _populated_db()
		dash = _make_dashboard(db)

		mission = db.get_active_mission()
		detail = dash._build_planner_detail(mission)

		event_types = {e["event_type"] for e in detail["events"]}
		assert "planning_started" in event_types
		assert "epoch_completed" in event_types  # ep1 is finished


class TestAgentDetailEndpoint:
	def _make_app(self) -> TestClient:
		db = _populated_db()
		# Allow cross-thread access for TestClient's async executor
		db.conn.execute("PRAGMA journal_mode=WAL")
		import sqlite3
		db.conn = sqlite3.connect(":memory:", check_same_thread=False)
		db.conn.row_factory = sqlite3.Row
		db.conn.execute("PRAGMA foreign_keys=ON")
		db._create_tables()

		# Re-populate in the new connection
		_insert_mission(db, "m1", total_cost_usd=2.0)
		p = Plan(id="plan1", objective="test plan")
		db.insert_plan(p)
		_insert_epoch(db, "ep1", "m1", number=1, finished_at="2025-06-01T11:00:00")
		_insert_epoch(db, "ep2", "m1", number=2)
		_insert_worker(
			db, "w1", status="working", current_unit_id="wu2",
			units_completed=1, units_failed=0, total_cost_usd=0.5,
		)
		_insert_worker(
			db, "w2", status="idle",
			units_completed=0, units_failed=1, total_cost_usd=0.3,
		)
		_insert_work_unit(
			db, "wu1", "plan1", status="completed", worker_id="w1",
			epoch_id="ep1", started_at="2025-06-01T10:00:00",
			finished_at="2025-06-01T10:30:00",
			input_tokens=500, output_tokens=200, cost_usd=0.3,
		)
		_insert_work_unit(
			db, "wu2", "plan1", status="running", worker_id="w1",
			epoch_id="ep2", started_at="2025-06-01T11:00:00",
			input_tokens=300, output_tokens=100, cost_usd=0.2,
		)
		_insert_unit_event(
			db, "evt1", work_unit_id="wu1",
			event_type="dispatched", input_tokens=100, output_tokens=50,
		)

		dash = LiveDashboard.__new__(LiveDashboard)
		dash.db = db
		dash.auth_token = ""
		dash._connections = set()
		dash._broadcast_task = None
		dash._ui_concrete = None
		dash._ui_mtime = 0.0
		dash._signal_timestamps = {}

		from collections import defaultdict
		from contextlib import asynccontextmanager

		from fastapi import FastAPI
		from fastapi.middleware.cors import CORSMiddleware

		@asynccontextmanager
		async def _lifespan(app):
			yield

		dash.app = FastAPI(title="Mission Control Live", lifespan=_lifespan)
		dash.app.add_middleware(
			CORSMiddleware,
			allow_origins=["*"],
			allow_methods=["*"],
			allow_headers=["*"],
		)
		dash._signal_timestamps = defaultdict(list)
		dash._setup_routes()

		return TestClient(dash.app)

	def test_planner_detail(self) -> None:
		client = self._make_app()
		resp = client.get("/api/agent-detail/planner")
		assert resp.status_code == 200
		data = resp.json()
		assert data["agent_id"] == "planner"
		assert data["role"] == "planner"
		assert "events" in data
		assert "stats" in data

	def test_worker_detail(self) -> None:
		client = self._make_app()
		workers = client.get("/api/workers").json()
		if workers:
			worker_id = workers[0]["id"]
			resp = client.get(f"/api/agent-detail/{worker_id}")
			assert resp.status_code == 200
			data = resp.json()
			assert data["agent_id"] == worker_id
			assert data["role"] == "worker"
			assert "units" in data
			assert "events" in data
			assert "stats" in data

	def test_unknown_agent_404(self) -> None:
		client = self._make_app()
		resp = client.get("/api/agent-detail/nonexistent")
		assert resp.status_code == 404


class TestSnapshotIncludesAgentActivity:
	def test_snapshot_has_agent_activity(self) -> None:
		db = _populated_db()
		dash = _make_dashboard(db)

		workers = db.get_all_workers()
		epochs = db.get_epochs_for_mission("m1")
		units = db.get_units_for_worker("w1") + db.get_units_for_worker("w2")

		activity = dash._build_agent_activity(workers, epochs, units)
		assert isinstance(activity, list)
		assert len(activity) > 0
		# Must have planner as first entry
		assert activity[0]["agent_id"] == "planner"
		# Must have worker entries
		worker_entries = [a for a in activity if a["role"] == "worker"]
		assert len(worker_entries) == 2
