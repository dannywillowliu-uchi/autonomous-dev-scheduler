"""Tests for swarm Telegram notification fixes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from autodev.config import SwarmConfig
from autodev.swarm.controller import SwarmController
from autodev.swarm.models import (
	SwarmState,
	TaskStatus,
)
from autodev.swarm.planner import DrivingPlanner


def _make_config(tmp_path: Path) -> MagicMock:
	config = MagicMock()
	config.target.name = "test-project"
	config.target.objective = "Build a compiler"
	config.target.resolved_path = str(tmp_path)
	config.notifications.telegram.bot_token = ""
	config.notifications.telegram.chat_id = ""
	return config


def _make_swarm_config(**overrides: object) -> SwarmConfig:
	sc = SwarmConfig()
	for k, v in overrides.items():
		setattr(sc, k, v)
	return sc


def _make_db() -> MagicMock:
	db = MagicMock()
	db.get_knowledge_for_mission.return_value = []
	return db


def _make_controller_mock() -> MagicMock:
	ctrl = MagicMock()
	ctrl._config = MagicMock()
	ctrl._config.target.resolved_path = "/tmp/test"
	ctrl._notify = AsyncMock()
	ctrl.execute_decisions = AsyncMock(return_value=[])
	ctrl.monitor_agents = AsyncMock(return_value=[])
	ctrl.cleanup = AsyncMock()
	ctrl.build_state = MagicMock(return_value=SwarmState(
		mission_objective="Test mission",
		agents=[],
		tasks=[],
	))
	ctrl.render_state = MagicMock(return_value="## State\nNo agents.")
	ctrl.team_name = "autodev-test-project"
	return ctrl


class TestStartNotifiedFlag:
	"""Issue 1: _start_notified prevents duplicate start notifications on daemon restart."""

	async def test_first_initialize_sends_notification(self, tmp_path: Path) -> None:
		ctrl = SwarmController(_make_config(tmp_path), _make_swarm_config(), _make_db())
		assert ctrl._start_notified is False
		with patch.object(Path, "home", return_value=tmp_path), \
			patch.object(ctrl, "_notify", new_callable=AsyncMock) as mock_notify:
			await ctrl.initialize()
		mock_notify.assert_called_once()
		assert "[autodev] Swarm started:" in mock_notify.call_args[0][0]
		assert ctrl._start_notified is True

	async def test_second_initialize_skips_notification(self, tmp_path: Path) -> None:
		ctrl = SwarmController(_make_config(tmp_path), _make_swarm_config(), _make_db())
		with patch.object(Path, "home", return_value=tmp_path), \
			patch.object(ctrl, "_notify", new_callable=AsyncMock) as mock_notify:
			await ctrl.initialize()
			await ctrl.initialize()
		mock_notify.assert_called_once()


class TestIdleNotification:
	"""Issue 3: One-time idle notification when entering daemon idle mode."""

	async def test_idle_notification_sent_on_first_idle(self) -> None:
		ctrl = _make_controller_mock()
		planner = DrivingPlanner(ctrl, _make_swarm_config(daemon_mode=True))
		task = MagicMock()
		task.status = TaskStatus.COMPLETED
		state = SwarmState(
			mission_objective="Test",
			agents=[],
			tasks=[task],
		)
		assert planner._daemon_idling is False
		planner._should_stop(state)
		assert planner._daemon_idling is True
		ctrl._notify.assert_called_once_with(
			"[autodev] Swarm idling -- waiting for new directives"
		)

	async def test_idle_notification_not_repeated(self) -> None:
		ctrl = _make_controller_mock()
		planner = DrivingPlanner(ctrl, _make_swarm_config(daemon_mode=True))
		task = MagicMock()
		task.status = TaskStatus.COMPLETED
		state = SwarmState(
			mission_objective="Test",
			agents=[],
			tasks=[task],
		)
		planner._should_stop(state)
		planner._should_stop(state)
		ctrl._notify.assert_called_once()


class TestDirectiveNotification:
	"""Issue 2: Directive text stored and available for notification on resume."""

	def test_directive_text_stored_on_check(self, tmp_path: Path) -> None:
		ctrl = _make_controller_mock()
		ctrl.team_name = "test-team"
		planner = DrivingPlanner(ctrl, _make_swarm_config())

		inbox_dir = tmp_path / ".claude" / "teams" / "test-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		inbox_path = inbox_dir / "team-lead.json"
		inbox_path.write_text(json.dumps([
			{"type": "directive", "text": "Deploy the new feature to staging"}
		]))

		state = SwarmState(mission_objective="Test", agents=[], tasks=[])
		with patch.object(Path, "home", return_value=tmp_path):
			result = planner._check_inbox_for_directives(state)

		assert result is True
		assert planner._last_directive_text == "Deploy the new feature to staging"

	def test_no_directive_leaves_text_none(self, tmp_path: Path) -> None:
		ctrl = _make_controller_mock()
		ctrl.team_name = "test-team"
		planner = DrivingPlanner(ctrl, _make_swarm_config())

		inbox_dir = tmp_path / ".claude" / "teams" / "test-team" / "inboxes"
		inbox_dir.mkdir(parents=True)
		inbox_path = inbox_dir / "team-lead.json"
		inbox_path.write_text(json.dumps([
			{"type": "report", "text": "Just a progress report"}
		]))

		state = SwarmState(mission_objective="Test", agents=[], tasks=[])
		with patch.object(Path, "home", return_value=tmp_path):
			result = planner._check_inbox_for_directives(state)

		assert result is False
		assert planner._last_directive_text is None
