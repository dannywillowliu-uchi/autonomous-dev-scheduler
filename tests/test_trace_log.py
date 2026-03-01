"""Tests for TraceEvent and TraceLogger."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from mission_control.trace_log import TraceEvent, TraceLogConfig, TraceLogger


class TestTraceEvent:
	def test_to_dict_produces_json_serializable_dict(self) -> None:
		event = TraceEvent(
			timestamp="2026-01-01T00:00:00+00:00",
			worker_id="w1",
			unit_id="u1",
			event_type="spawn",
			details={"pid": 1234},
		)
		d = event.to_dict()
		assert d == {
			"timestamp": "2026-01-01T00:00:00+00:00",
			"worker_id": "w1",
			"unit_id": "u1",
			"event_type": "spawn",
			"details": {"pid": 1234},
		}
		# Must be JSON-serializable
		serialized = json.dumps(d)
		assert json.loads(serialized) == d

	def test_from_dict_round_trips(self) -> None:
		original = TraceEvent(
			timestamp="2026-02-28T12:00:00+00:00",
			worker_id="w2",
			unit_id="u3",
			event_type="merge",
			details={"branch": "mc/green", "success": True},
		)
		d = original.to_dict()
		restored = TraceEvent.from_dict(d)
		assert restored.timestamp == original.timestamp
		assert restored.worker_id == original.worker_id
		assert restored.unit_id == original.unit_id
		assert restored.event_type == original.event_type
		assert restored.details == original.details

	def test_from_dict_tolerates_extra_keys(self) -> None:
		data = {
			"timestamp": "2026-01-01T00:00:00+00:00",
			"worker_id": "w1",
			"unit_id": "u1",
			"event_type": "spawn",
			"details": {},
			"extra_field": "should be ignored",
			"another": 42,
		}
		event = TraceEvent.from_dict(data)
		assert event.worker_id == "w1"
		assert event.event_type == "spawn"
		assert not hasattr(event, "extra_field")


class TestTraceLoggerDisabled:
	def test_disabled_logger_does_not_create_file(self, tmp_path: Path) -> None:
		trace_file = tmp_path / "trace.jsonl"
		config = TraceLogConfig(enabled=False, path=str(trace_file))
		logger = TraceLogger(config)
		event = TraceEvent(event_type="test")
		logger.write(event)
		assert not trace_file.exists()


class TestTraceLoggerWrite:
	def test_write_appends_valid_jsonl(self, tmp_path: Path) -> None:
		trace_file = tmp_path / "trace.jsonl"
		config = TraceLogConfig(enabled=True, path=str(trace_file))
		logger = TraceLogger(config)
		event = TraceEvent(
			timestamp="2026-01-01T00:00:00+00:00",
			worker_id="w1",
			unit_id="u1",
			event_type="spawn",
			details={"pid": 99},
		)
		logger.write(event)
		lines = trace_file.read_text().strip().splitlines()
		assert len(lines) == 1
		record = json.loads(lines[0])
		assert record["worker_id"] == "w1"
		assert record["event_type"] == "spawn"
		assert record["details"] == {"pid": 99}

	def test_multiple_writes_produce_one_json_per_line(self, tmp_path: Path) -> None:
		trace_file = tmp_path / "trace.jsonl"
		config = TraceLogConfig(enabled=True, path=str(trace_file))
		logger = TraceLogger(config)
		for i in range(5):
			event = TraceEvent(worker_id=f"w{i}", event_type="tick")
			logger.write(event)
		lines = trace_file.read_text().strip().splitlines()
		assert len(lines) == 5
		for i, line in enumerate(lines):
			record = json.loads(line)
			assert record["worker_id"] == f"w{i}"


class TestTraceLoggerRotation:
	def test_file_rotated_when_max_size_exceeded(self, tmp_path: Path) -> None:
		trace_file = tmp_path / "trace.jsonl"
		config = TraceLogConfig(enabled=True, path=str(trace_file), max_file_size=100)
		logger = TraceLogger(config)

		# Write enough events to exceed 100 bytes
		for i in range(10):
			event = TraceEvent(
				worker_id=f"worker-{i}",
				unit_id=f"unit-{i}",
				event_type="tick",
				details={"seq": i},
			)
			logger.write(event)

		rotated = tmp_path / "trace.jsonl.1"
		assert rotated.exists(), "Expected rotated file trace.jsonl.1"
		# Current trace file should also exist (new events after rotation)
		assert trace_file.exists()
		# Rotated file should have content
		assert rotated.stat().st_size > 0


class TestTraceLoggerThreadSafety:
	def test_concurrent_writes_produce_correct_line_count(self, tmp_path: Path) -> None:
		trace_file = tmp_path / "trace.jsonl"
		# Use a large max_file_size to avoid rotation during this test
		config = TraceLogConfig(enabled=True, path=str(trace_file), max_file_size=50_000_000)
		logger = TraceLogger(config)

		num_threads = 10
		events_per_thread = 50
		barrier = threading.Barrier(num_threads)

		def writer(tid: int) -> None:
			barrier.wait()
			for i in range(events_per_thread):
				event = TraceEvent(
					worker_id=f"t{tid}",
					event_type="concurrent",
					details={"seq": i},
				)
				logger.write(event)

		threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()

		lines = trace_file.read_text().strip().splitlines()
		assert len(lines) == num_threads * events_per_thread  # 500

		# Every line must be valid JSON
		for line in lines:
			json.loads(line)


class TestTraceLogConfig:
	def test_defaults(self) -> None:
		config = TraceLogConfig()
		assert config.enabled is False
		assert config.path == "trace.jsonl"
		assert config.max_file_size == 50_000_000
