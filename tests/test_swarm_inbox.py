"""Tests for swarm inbox I/O: read/write round-trip, atomicity, error cases."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from autodev.swarm.context import ContextSynthesizer
from autodev.swarm.controller import SwarmController


def _make_config(tmp_path: Path) -> MagicMock:
	config = MagicMock()
	config.target.name = "test-project"
	config.target.objective = "Build something"
	config.target.resolved_path = str(tmp_path)
	return config


def _make_swarm_config() -> MagicMock:
	sc = MagicMock()
	sc.max_agents = 5
	sc.inherit_global_mcps = False
	return sc


def _make_db() -> MagicMock:
	db = MagicMock()
	db.get_knowledge_for_mission.return_value = []
	return db


def _make_controller(tmp_path: Path) -> SwarmController:
	"""Create a SwarmController with home dir overridden to tmp_path."""
	config = _make_config(tmp_path)
	ctrl = SwarmController(config, _make_swarm_config(), _make_db())
	ctrl._team_name = "test-inbox-team"
	return ctrl


class TestInboxRoundTrip:
	"""Normal inbox read/write round-trip."""

	def test_write_then_read(self, tmp_path: Path) -> None:
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "worker-1", "type": "report", "text": "done"})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == 1
		assert messages[0]["from"] == "worker-1"
		assert messages[0]["type"] == "report"
		assert messages[0]["text"] == "done"
		assert "timestamp" in messages[0]

	def test_multiple_writes_append(self, tmp_path: Path) -> None:
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "a", "type": "report", "text": "first"})
			ctrl._write_to_inbox("team-lead", {"from": "b", "type": "discovery", "text": "second"})
			ctrl._write_to_inbox("team-lead", {"from": "c", "type": "blocked", "text": "third"})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == 3
		assert [m["from"] for m in messages] == ["a", "b", "c"]

	def test_write_creates_inbox_dir(self, tmp_path: Path) -> None:
		ctrl = _make_controller(tmp_path)
		# Don't pre-create the directory -- _write_to_inbox should create it
		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("new-agent", {"from": "planner", "type": "shutdown_request", "text": "stop"})

		inbox_file = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes" / "new-agent.json"
		assert inbox_file.exists()
		messages = json.loads(inbox_file.read_text())
		assert len(messages) == 1


class TestConcurrentWriteSafety:
	"""Atomic writes with lock files should not lose messages."""

	def test_concurrent_writes_no_data_loss(self, tmp_path: Path) -> None:
		"""Multiple threads writing to the same inbox should not lose messages."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		num_writers = 10
		barrier = threading.Barrier(num_writers)
		errors: list[str] = []

		def writer(idx: int) -> None:
			try:
				barrier.wait(timeout=5)
				with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
					ctrl._write_to_inbox("team-lead", {"from": f"worker-{idx}", "type": "report", "text": f"msg-{idx}"})
			except Exception as e:
				errors.append(str(e))

		threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_writers)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=10)

		assert not errors, f"Writer errors: {errors}"

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			messages = ctrl.read_leader_inbox()

		assert len(messages) == num_writers
		senders = sorted(m["from"] for m in messages)
		expected = sorted(f"worker-{i}" for i in range(num_writers))
		assert senders == expected

	def test_lock_file_created_during_write(self, tmp_path: Path) -> None:
		"""Lock files (.lock suffix) should exist during write operations."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		lock_observed = threading.Event()

		original_flock = __import__("fcntl").flock

		def slow_flock(fd, op):
			original_flock(fd, op)
			if op == __import__("fcntl").LOCK_EX:
				lock_observed.set()
				time.sleep(0.1)  # Hold lock briefly so observer can see the lock file

		with (
			patch("autodev.swarm.controller.Path.home", return_value=tmp_path),
			patch("autodev.swarm.controller.fcntl.flock", side_effect=slow_flock),
		):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "hi"})

		assert lock_observed.is_set()
		# Lock file may remain after write (it's just an empty advisory lock file)
		# The important thing is it was used during the write


class TestCorruptedInboxJson:
	"""Corrupted/malformed JSON in inbox files."""

	def test_write_recovers_from_corrupt_json(self, tmp_path: Path) -> None:
		"""Writing to a corrupted inbox file should reset and write fresh."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		# Write garbage JSON to the inbox file
		inbox_file = inbox_dir / "team-lead.json"
		inbox_file.write_text("{not valid json at all!!!")

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "recovered"})
			messages = ctrl.read_leader_inbox()

		# Should have recovered: the corrupt data is lost but new message is written
		assert len(messages) == 1
		assert messages[0]["text"] == "recovered"

	def test_read_leader_inbox_returns_empty_on_corrupt(self, tmp_path: Path) -> None:
		"""read_leader_inbox should return [] when file has invalid JSON."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		inbox_file = inbox_dir / "team-lead.json"
		inbox_file.write_text("<<<NOT JSON>>>")

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			messages = ctrl.read_leader_inbox()

		assert messages == []

	def test_context_skips_corrupt_inbox_in_discoveries(self, tmp_path: Path) -> None:
		"""ContextSynthesizer._get_recent_discoveries should skip corrupt inbox files."""
		inbox_dir = tmp_path / ".claude" / "teams" / "test-ctx-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		# One good inbox, one corrupt
		(inbox_dir / "good-agent.json").write_text(json.dumps([
			{"from": "good-agent", "type": "discovery", "text": "found a bug"}
		]))
		(inbox_dir / "bad-agent.json").write_text("{corrupt")

		config = _make_config(tmp_path)
		ctx = ContextSynthesizer(config, _make_db(), "test-ctx-team")

		with patch("autodev.swarm.context.Path.home", return_value=tmp_path):
			discoveries = ctx._get_recent_discoveries(tasks=None)

		assert len(discoveries) == 1
		assert "found a bug" in discoveries[0]

	def test_write_recovers_from_non_array_json(self, tmp_path: Path) -> None:
		"""Inbox with valid JSON but not an array should reset and write fresh."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		inbox_file = inbox_dir / "team-lead.json"
		inbox_file.write_text('{"not": "an array"}')

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "msg"})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == 1
		assert messages[0]["text"] == "msg"


class TestMissingInboxFile:
	"""Missing inbox files (agent never wrote)."""

	def test_read_leader_inbox_missing_file(self, tmp_path: Path) -> None:
		"""read_leader_inbox returns [] when the file doesn't exist."""
		ctrl = _make_controller(tmp_path)
		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			messages = ctrl.read_leader_inbox()
		assert messages == []

	def test_read_missing_inbox_dir(self, tmp_path: Path) -> None:
		"""read_leader_inbox returns [] when the entire inbox dir doesn't exist."""
		ctrl = _make_controller(tmp_path)
		# Don't create any dirs
		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			messages = ctrl.read_leader_inbox()
		assert messages == []

	def test_context_discoveries_missing_inbox_dir(self, tmp_path: Path) -> None:
		"""ContextSynthesizer handles missing inbox directory gracefully."""
		config = _make_config(tmp_path)
		ctx = ContextSynthesizer(config, _make_db(), "nonexistent-team")

		with patch("autodev.swarm.context.Path.home", return_value=tmp_path):
			discoveries = ctx._get_recent_discoveries(tasks=None)

		assert discoveries == []


class TestEmptyInboxFile:
	"""Empty inbox files."""

	def test_read_empty_inbox_file(self, tmp_path: Path) -> None:
		"""Empty inbox file should return []."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		(inbox_dir / "team-lead.json").write_text("")

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			messages = ctrl.read_leader_inbox()
		assert messages == []

	def test_write_to_empty_inbox_file(self, tmp_path: Path) -> None:
		"""Writing to an empty inbox file should create a fresh array."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		(inbox_dir / "team-lead.json").write_text("")

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "fresh"})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == 1
		assert messages[0]["text"] == "fresh"

	def test_empty_json_array_inbox(self, tmp_path: Path) -> None:
		"""Inbox with empty array [] should work normally."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		(inbox_dir / "team-lead.json").write_text("[]")

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "appended"})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == 1
		assert messages[0]["text"] == "appended"

	def test_context_skips_empty_inbox_in_discoveries(self, tmp_path: Path) -> None:
		"""ContextSynthesizer should handle empty inbox files without error."""
		inbox_dir = tmp_path / ".claude" / "teams" / "test-ctx-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		(inbox_dir / "empty-agent.json").write_text("")

		config = _make_config(tmp_path)
		ctx = ContextSynthesizer(config, _make_db(), "test-ctx-team")

		with patch("autodev.swarm.context.Path.home", return_value=tmp_path):
			discoveries = ctx._get_recent_discoveries(tasks=None)

		assert discoveries == []


class TestLargeInboxFile:
	"""Large inbox files with many messages."""

	def test_many_messages_roundtrip(self, tmp_path: Path) -> None:
		"""Inbox with hundreds of messages should read/write correctly."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		num_messages = 200
		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			for i in range(num_messages):
				ctrl._write_to_inbox("team-lead", {"from": f"w-{i}", "type": "report", "text": f"msg-{i}"})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == num_messages
		assert messages[0]["from"] == "w-0"
		assert messages[-1]["from"] == f"w-{num_messages - 1}"

	def test_context_limits_discovery_messages(self, tmp_path: Path) -> None:
		"""ContextSynthesizer only reads last 20 messages per inbox for discoveries."""
		inbox_dir = tmp_path / ".claude" / "teams" / "test-ctx-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		# Write 50 discovery messages
		messages = [
			{"from": f"agent-{i}", "type": "discovery", "text": f"discovery-{i}"}
			for i in range(50)
		]
		(inbox_dir / "worker.json").write_text(json.dumps(messages))

		config = _make_config(tmp_path)
		ctx = ContextSynthesizer(config, _make_db(), "test-ctx-team")

		with patch("autodev.swarm.context.Path.home", return_value=tmp_path):
			discoveries = ctx._get_recent_discoveries(tasks=None)

		# Context reads messages[-20:], so we get the last 20
		assert len(discoveries) == 20
		assert "discovery-30" in discoveries[0]
		assert "discovery-49" in discoveries[-1]

	def test_large_message_content(self, tmp_path: Path) -> None:
		"""Messages with large text payloads should survive round-trip."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		large_text = "x" * 100_000
		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": large_text})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == 1
		assert messages[0]["text"] == large_text


class TestLockFileCleanup:
	"""Lock file cleanup on error."""

	def test_temp_file_cleaned_on_write_error(self, tmp_path: Path) -> None:
		"""If os.rename fails, the temp file should be cleaned up."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		with (
			patch("autodev.swarm.controller.Path.home", return_value=tmp_path),
			patch("autodev.swarm.controller.os.rename", side_effect=OSError("rename failed")),
		):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "fail"})

		# Temp files should be cleaned up
		tmp_files = list(inbox_dir.glob("*.tmp"))
		assert len(tmp_files) == 0

	def test_fd_closed_on_write_error(self, tmp_path: Path) -> None:
		"""File descriptor from mkstemp should be closed even on error."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		close_calls: list[int] = []
		original_close = os.close

		def tracking_close(fd: int) -> None:
			close_calls.append(fd)
			original_close(fd)

		with (
			patch("autodev.swarm.controller.Path.home", return_value=tmp_path),
			patch("autodev.swarm.controller.os.close", side_effect=tracking_close),
			patch("autodev.swarm.controller.os.rename", side_effect=OSError("rename failed")),
		):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "fail"})

		# os.close should have been called (for the fd from mkstemp)
		assert len(close_calls) >= 1

	def test_lock_file_does_not_prevent_subsequent_writes(self, tmp_path: Path) -> None:
		"""A stale .lock file from a previous crash should not block writes."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		# Create a stale lock file
		lock_path = inbox_dir / "team-lead.lock"
		lock_path.write_text("")

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			ctrl._write_to_inbox("team-lead", {"from": "w", "type": "report", "text": "after stale lock"})
			messages = ctrl.read_leader_inbox()

		assert len(messages) == 1
		assert messages[0]["text"] == "after stale lock"

	def test_write_does_not_leave_orphan_temp_files(self, tmp_path: Path) -> None:
		"""Successful writes should not leave .tmp files behind."""
		ctrl = _make_controller(tmp_path)
		inbox_dir = tmp_path / ".claude" / "teams" / "test-inbox-team" / "inboxes"
		inbox_dir.mkdir(parents=True)

		with patch("autodev.swarm.controller.Path.home", return_value=tmp_path):
			for i in range(10):
				ctrl._write_to_inbox("team-lead", {"from": f"w-{i}", "type": "report", "text": f"msg-{i}"})

		tmp_files = list(inbox_dir.glob("*.tmp"))
		assert len(tmp_files) == 0
