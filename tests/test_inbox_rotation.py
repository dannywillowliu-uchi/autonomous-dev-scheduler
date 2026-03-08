"""Tests for inbox log rotation."""

from __future__ import annotations

import json
from pathlib import Path

from autodev.swarm.context import (
	DEFAULT_KEEP_MESSAGES,
	DEFAULT_MAX_INBOX_BYTES,
	DEFAULT_MAX_INBOX_MESSAGES,
	rotate_inbox,
)


def _make_messages(n: int) -> list[dict]:
	return [{"from": f"agent-{i}", "type": "report", "text": f"msg {i}"} for i in range(n)]


def _write_inbox(path: Path, messages: list[dict]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(messages, indent=2))


class TestRotateInbox:
	def test_no_rotation_when_under_limits(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "agent.json"
		msgs = _make_messages(10)
		_write_inbox(inbox, msgs)

		result = rotate_inbox(inbox)

		assert result is False
		assert json.loads(inbox.read_text()) == msgs

	def test_rotation_when_exceeding_max_messages(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "agent.json"
		msgs = _make_messages(600)
		_write_inbox(inbox, msgs)

		result = rotate_inbox(inbox, max_messages=500, keep_messages=200)

		assert result is True
		remaining = json.loads(inbox.read_text())
		assert len(remaining) == 200
		# Should keep the most recent messages
		assert remaining[0] == msgs[400]
		assert remaining[-1] == msgs[599]

	def test_rotation_when_exceeding_max_bytes(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "agent.json"
		# Create messages with large text to exceed byte limit
		msgs = [
			{"from": "agent", "type": "report", "text": "x" * 500}
			for _ in range(100)
		]
		_write_inbox(inbox, msgs)

		# Use a small byte limit to trigger rotation
		result = rotate_inbox(inbox, max_bytes=1024, keep_messages=10)

		assert result is True
		remaining = json.loads(inbox.read_text())
		assert len(remaining) == 10
		assert remaining[-1] == msgs[-1]

	def test_rotation_keeps_most_recent(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "agent.json"
		msgs = _make_messages(50)
		_write_inbox(inbox, msgs)

		result = rotate_inbox(inbox, max_messages=30, keep_messages=20)

		assert result is True
		remaining = json.loads(inbox.read_text())
		assert len(remaining) == 20
		# Verify these are the last 20
		assert remaining == msgs[30:]

	def test_nonexistent_file_returns_false(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "missing.json"
		result = rotate_inbox(inbox)
		assert result is False

	def test_empty_file_returns_false(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "empty.json"
		_write_inbox(inbox, [])

		result = rotate_inbox(inbox)

		assert result is False

	def test_malformed_json_returns_false(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "bad.json"
		inbox.parent.mkdir(parents=True, exist_ok=True)
		# Write enough bytes to pass the size short-circuit
		inbox.write_text("{not valid json" + "x" * 2000)

		result = rotate_inbox(inbox)

		assert result is False

	def test_non_array_json_returns_false(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "obj.json"
		inbox.parent.mkdir(parents=True, exist_ok=True)
		inbox.write_text(json.dumps({"not": "a list"}) + " " * 2000)

		result = rotate_inbox(inbox)

		assert result is False

	def test_atomic_write_leaves_no_tmp_files(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "agent.json"
		msgs = _make_messages(100)
		_write_inbox(inbox, msgs)

		rotate_inbox(inbox, max_messages=50, keep_messages=20)

		tmp_files = list(tmp_path.glob("inboxes/*.tmp"))
		assert len(tmp_files) == 0

	def test_lock_file_created(self, tmp_path: Path) -> None:
		inbox = tmp_path / "inboxes" / "agent.json"
		msgs = _make_messages(100)
		_write_inbox(inbox, msgs)

		rotate_inbox(inbox, max_messages=50, keep_messages=20)

		lock_path = inbox.with_suffix(".lock")
		assert lock_path.exists()

	def test_keep_messages_greater_than_total(self, tmp_path: Path) -> None:
		"""When keep_messages > actual count, keep all that remain."""
		inbox = tmp_path / "inboxes" / "agent.json"
		msgs = _make_messages(10)
		_write_inbox(inbox, msgs)

		# Force rotation via byte limit, but keep_messages is larger than count
		result = rotate_inbox(inbox, max_bytes=1, keep_messages=100)

		assert result is True
		remaining = json.loads(inbox.read_text())
		assert remaining == msgs

	def test_default_constants_are_sane(self) -> None:
		assert DEFAULT_MAX_INBOX_BYTES == 500 * 1024
		assert DEFAULT_MAX_INBOX_MESSAGES == 500
		assert DEFAULT_KEEP_MESSAGES == 200
		assert DEFAULT_KEEP_MESSAGES < DEFAULT_MAX_INBOX_MESSAGES


class TestRotationIntegration:
	"""Test that rotation integrates with ContextSynthesizer reads."""

	def test_discoveries_triggers_rotation(self, tmp_path: Path) -> None:
		"""Verify _get_recent_discoveries calls rotate_inbox on each file."""
		from unittest.mock import MagicMock

		from autodev.swarm.context import ContextSynthesizer

		config = MagicMock()
		config.target.name = "test-proj"
		config.target.resolved_path = str(tmp_path)
		db = MagicMock()
		db.get_knowledge_for_mission.return_value = []

		team_name = "test-team"
		ctx = ContextSynthesizer(config, db, team_name)

		# Create a large inbox
		inbox_dir = tmp_path / ".claude" / "teams" / team_name / "inboxes"
		inbox_dir.mkdir(parents=True)
		inbox_file = inbox_dir / "agent-1.json"
		msgs = _make_messages(600)
		_write_inbox(inbox_file, msgs)

		# Reading discoveries should trigger rotation
		from unittest.mock import patch
		with patch("autodev.swarm.context.Path.home", return_value=tmp_path):
			ctx._get_recent_discoveries()

		# Verify the file was rotated
		remaining = json.loads(inbox_file.read_text())
		assert len(remaining) == DEFAULT_KEEP_MESSAGES
