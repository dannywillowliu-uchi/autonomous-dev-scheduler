"""Tests for signal table CRUD and controller integration."""

from __future__ import annotations

import json

import pytest

from mission_control.db import Database
from mission_control.models import Mission, Signal, _now_iso


@pytest.fixture
def db():
	d = Database(":memory:")
	yield d
	d.close()


@pytest.fixture
def mission(db):
	m = Mission(objective="test objective", status="running")
	db.insert_mission(m)
	return m


class TestSignalCRUD:
	def test_insert_and_get_pending(self, db, mission):
		signal = Signal(
			mission_id=mission.id,
			signal_type="stop",
			created_at=_now_iso(),
		)
		db.insert_signal(signal)

		pending = db.get_pending_signals(mission.id)
		assert len(pending) == 1
		assert pending[0].id == signal.id
		assert pending[0].signal_type == "stop"
		assert pending[0].status == "pending"

	def test_acknowledge_signal(self, db, mission):
		signal = Signal(
			mission_id=mission.id,
			signal_type="stop",
			created_at=_now_iso(),
		)
		db.insert_signal(signal)

		db.acknowledge_signal(signal.id)

		pending = db.get_pending_signals(mission.id)
		assert len(pending) == 0

	def test_multiple_signals(self, db, mission):
		for sig_type in ("stop", "retry_unit", "adjust"):
			signal = Signal(
				mission_id=mission.id,
				signal_type=sig_type,
				created_at=_now_iso(),
			)
			db.insert_signal(signal)

		pending = db.get_pending_signals(mission.id)
		assert len(pending) == 3

	def test_signals_scoped_to_mission(self, db, mission):
		other = Mission(objective="other", status="running")
		db.insert_mission(other)

		db.insert_signal(Signal(
			mission_id=mission.id, signal_type="stop", created_at=_now_iso(),
		))
		db.insert_signal(Signal(
			mission_id=other.id, signal_type="stop", created_at=_now_iso(),
		))

		pending = db.get_pending_signals(mission.id)
		assert len(pending) == 1

	def test_retry_unit_payload(self, db, mission):
		signal = Signal(
			mission_id=mission.id,
			signal_type="retry_unit",
			payload="unit123",
			created_at=_now_iso(),
		)
		db.insert_signal(signal)

		pending = db.get_pending_signals(mission.id)
		assert pending[0].payload == "unit123"

	def test_adjust_payload_json(self, db, mission):
		payload = json.dumps({"max_rounds": 30, "num_workers": 6})
		signal = Signal(
			mission_id=mission.id,
			signal_type="adjust",
			payload=payload,
			created_at=_now_iso(),
		)
		db.insert_signal(signal)

		pending = db.get_pending_signals(mission.id)
		parsed = json.loads(pending[0].payload)
		assert parsed["max_rounds"] == 30
		assert parsed["num_workers"] == 6

	def test_expire_stale_signals(self, db, mission):
		# Insert a signal with old timestamp
		signal = Signal(
			mission_id=mission.id,
			signal_type="stop",
			created_at="2020-01-01T00:00:00+00:00",
		)
		db.insert_signal(signal)

		expired_count = db.expire_stale_signals(timeout_minutes=10)
		assert expired_count == 1

		pending = db.get_pending_signals(mission.id)
		assert len(pending) == 0

	def test_expire_does_not_touch_fresh(self, db, mission):
		signal = Signal(
			mission_id=mission.id,
			signal_type="stop",
			created_at=_now_iso(),
		)
		db.insert_signal(signal)

		expired_count = db.expire_stale_signals(timeout_minutes=10)
		assert expired_count == 0

		pending = db.get_pending_signals(mission.id)
		assert len(pending) == 1
