"""Tests for the JSONL event stream."""

from __future__ import annotations

import json

from mission_control.event_stream import EventStream


class TestEventStream:
	def test_emit_writes_jsonl(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "events.jsonl"
		stream = EventStream(p)
		stream.open()
		stream.emit("dispatched", mission_id="m1", unit_id="u1")
		stream.close()

		lines = p.read_text().strip().split("\n")
		assert len(lines) == 1
		record = json.loads(lines[0])
		assert record["event_type"] == "dispatched"
		assert record["mission_id"] == "m1"
		assert record["unit_id"] == "u1"
		assert "timestamp" in record

	def test_emit_multiple_events(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "events.jsonl"
		stream = EventStream(p)
		stream.open()
		stream.emit("mission_started", mission_id="m1")
		stream.emit("dispatched", mission_id="m1", unit_id="u1")
		stream.emit("merged", mission_id="m1", unit_id="u1", cost_usd=0.05)
		stream.close()

		lines = p.read_text().strip().split("\n")
		assert len(lines) == 3
		types = [json.loads(line)["event_type"] for line in lines]
		assert types == ["mission_started", "dispatched", "merged"]

	def test_emit_with_details(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "events.jsonl"
		stream = EventStream(p)
		stream.open()
		stream.emit(
			"worker_started",
			mission_id="m1",
			worker_id="w1",
			details={"pid": 1234, "workspace": "/tmp/ws"},
		)
		stream.close()

		record = json.loads(p.read_text().strip())
		assert record["details"]["pid"] == 1234
		assert record["details"]["workspace"] == "/tmp/ws"

	def test_emit_with_token_usage(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "events.jsonl"
		stream = EventStream(p)
		stream.open()
		stream.emit(
			"merged",
			mission_id="m1",
			unit_id="u1",
			input_tokens=5000,
			output_tokens=2000,
			cost_usd=0.12,
		)
		stream.close()

		record = json.loads(p.read_text().strip())
		assert record["input_tokens"] == 5000
		assert record["output_tokens"] == 2000
		assert record["cost_usd"] == 0.12

	def test_emit_noop_when_not_opened(self) -> None:
		from pathlib import Path
		p = Path("/tmp/should-not-exist-event-stream-test.jsonl")
		stream = EventStream(p)
		# emit without open() should be a no-op
		stream.emit("dispatched", mission_id="m1")
		assert not p.exists()

	def test_emit_noop_after_close(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "events.jsonl"
		stream = EventStream(p)
		stream.open()
		stream.emit("dispatched", mission_id="m1")
		stream.close()
		# emit after close should be a no-op
		stream.emit("merged", mission_id="m1")

		lines = p.read_text().strip().split("\n")
		assert len(lines) == 1

	def test_creates_parent_directories(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "nested" / "dir" / "events.jsonl"
		stream = EventStream(p)
		stream.open()
		stream.emit("mission_started", mission_id="m1")
		stream.close()

		assert p.exists()
		record = json.loads(p.read_text().strip())
		assert record["event_type"] == "mission_started"

	def test_appends_to_existing_file(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "events.jsonl"
		# Write initial content
		p.write_text('{"event_type":"old"}\n')

		stream = EventStream(p)
		stream.open()
		stream.emit("new_event", mission_id="m1")
		stream.close()

		lines = p.read_text().strip().split("\n")
		assert len(lines) == 2
		assert json.loads(lines[0])["event_type"] == "old"
		assert json.loads(lines[1])["event_type"] == "new_event"

	def test_default_field_values(self, tmp_path: object) -> None:
		from pathlib import Path
		p = Path(str(tmp_path)) / "events.jsonl"
		stream = EventStream(p)
		stream.open()
		stream.emit("test_event")
		stream.close()

		record = json.loads(p.read_text().strip())
		assert record["mission_id"] == ""
		assert record["epoch_id"] == ""
		assert record["unit_id"] == ""
		assert record["worker_id"] == ""
		assert record["details"] == {}
		assert record["input_tokens"] == 0
		assert record["output_tokens"] == 0
		assert record["cost_usd"] == 0.0
