"""Tests for swarm controller auth request/response protocol."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autodev.auth.browser import AuthResult
from autodev.config import SwarmConfig
from autodev.swarm.controller import SwarmController
from autodev.swarm.models import AgentRole, AgentStatus, SwarmAgent


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


def _make_controller(tmp_path: Path, **config_overrides: object) -> SwarmController:
	config = _make_config(tmp_path)
	for k, v in config_overrides.items():
		parts = k.split(".")
		obj = config
		for part in parts[:-1]:
			obj = getattr(obj, part)
		setattr(obj, parts[-1], v)
	return SwarmController(config, _make_swarm_config(), _make_db())


class TestHandleAuthRequest:
	@pytest.mark.asyncio
	async def test_successful_auth_writes_response(self, tmp_path: Path) -> None:
		"""handle_auth_request delegates to gateway and writes auth_response to worker inbox."""
		ctrl = _make_controller(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			await ctrl.initialize()

			inbox_dir = tmp_path / ".claude" / "teams" / "autodev-test-project" / "inboxes"
			(inbox_dir / "test-worker.json").write_text("[]")

			fake_result = AuthResult(
				success=True,
				service="github",
				credential_type="oauth_token",
			)

			mock_vault = MagicMock()
			mock_browser = AsyncMock()
			mock_browser.close = AsyncMock()
			mock_gateway = AsyncMock()
			mock_gateway.authenticate.return_value = fake_result

			with patch("autodev.auth.vault.KeychainVault", return_value=mock_vault), \
				patch("autodev.auth.browser.AuthHandler", return_value=mock_browser), \
				patch("autodev.auth.gateway.AuthGateway", return_value=mock_gateway):

				result = await ctrl.handle_auth_request(
					service="github",
					url="https://github.com/login",
					purpose="CLI access",
					requesting_agent="test-worker",
				)

		assert result["success"] is True
		assert result["service"] == "github"
		assert result["credential_type"] == "oauth_token"

		inbox_data = json.loads((inbox_dir / "test-worker.json").read_text())
		assert len(inbox_data) == 1
		msg = inbox_data[0]
		assert msg["type"] == "auth_response"
		assert msg["service"] == "github"
		assert msg["success"] is True
		assert "oauth_token" in msg["credential_type"]
		assert "Keychain" in msg["instructions"]

	@pytest.mark.asyncio
	async def test_failed_auth_writes_failure_response(self, tmp_path: Path) -> None:
		"""Failed auth writes error details to worker inbox."""
		ctrl = _make_controller(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			await ctrl.initialize()

			inbox_dir = tmp_path / ".claude" / "teams" / "autodev-test-project" / "inboxes"
			(inbox_dir / "worker-a.json").write_text("[]")

			fake_result = AuthResult(
				success=False,
				service="google-workspace",
				error="OAuth consent denied",
			)

			mock_browser = AsyncMock()
			mock_browser.close = AsyncMock()
			mock_gateway = AsyncMock()
			mock_gateway.authenticate.return_value = fake_result

			with patch("autodev.auth.vault.KeychainVault", return_value=MagicMock()), \
				patch("autodev.auth.browser.AuthHandler", return_value=mock_browser), \
				patch("autodev.auth.gateway.AuthGateway", return_value=mock_gateway):

				result = await ctrl.handle_auth_request(
					service="google-workspace",
					url="https://accounts.google.com",
					purpose="Calendar API",
					requesting_agent="worker-a",
				)

		assert result["success"] is False
		assert result["error"] == "OAuth consent denied"

		inbox_data = json.loads((inbox_dir / "worker-a.json").read_text())
		msg = inbox_data[0]
		assert msg["type"] == "auth_response"
		assert msg["success"] is False
		assert "failed" in msg["instructions"].lower()

	@pytest.mark.asyncio
	async def test_gateway_exception_returns_error(self, tmp_path: Path) -> None:
		"""Gateway exception is caught and returned as error."""
		ctrl = _make_controller(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			await ctrl.initialize()

			inbox_dir = tmp_path / ".claude" / "teams" / "autodev-test-project" / "inboxes"
			(inbox_dir / "worker-x.json").write_text("[]")

			mock_browser = AsyncMock()
			mock_browser.close = AsyncMock()
			mock_gateway = AsyncMock()
			mock_gateway.authenticate.side_effect = RuntimeError("browser crashed")

			with patch("autodev.auth.vault.KeychainVault", return_value=MagicMock()), \
				patch("autodev.auth.browser.AuthHandler", return_value=mock_browser), \
				patch("autodev.auth.gateway.AuthGateway", return_value=mock_gateway):

				result = await ctrl.handle_auth_request(
					service="slack",
					url="https://slack.com/oauth",
					purpose="Workspace access",
					requesting_agent="worker-x",
				)

		assert result["success"] is False
		assert "browser crashed" in result["error"]

		inbox_data = json.loads((inbox_dir / "worker-x.json").read_text())
		assert inbox_data[0]["success"] is False

	@pytest.mark.asyncio
	async def test_signup_ok_passed_to_gateway(self, tmp_path: Path) -> None:
		"""signup_ok parameter is forwarded to the gateway."""
		ctrl = _make_controller(tmp_path)
		with patch.object(Path, "home", return_value=tmp_path):
			await ctrl.initialize()

			inbox_dir = tmp_path / ".claude" / "teams" / "autodev-test-project" / "inboxes"
			(inbox_dir / "worker-s.json").write_text("[]")

			mock_browser = AsyncMock()
			mock_browser.close = AsyncMock()
			mock_gateway = AsyncMock()
			mock_gateway.authenticate.return_value = AuthResult(
				success=True, service="newservice", credential_type="api_key",
			)

			with patch("autodev.auth.vault.KeychainVault", return_value=MagicMock()), \
				patch("autodev.auth.browser.AuthHandler", return_value=mock_browser), \
				patch("autodev.auth.gateway.AuthGateway", return_value=mock_gateway):

				await ctrl.handle_auth_request(
					service="newservice",
					url="https://new.service/auth",
					purpose="Integration",
					requesting_agent="worker-s",
					signup_ok=True,
				)

			mock_gateway.authenticate.assert_called_once_with(
				service="newservice",
				purpose="Integration",
				url="https://new.service/auth",
				signup_ok=True,
			)


class TestAuthRequestContextIntegration:
	"""Verify auth_request messages are surfaced in planner context."""

	def test_auth_request_in_discovery_filter(self, tmp_path: Path) -> None:
		"""auth_request messages appear in discoveries via context synthesizer."""
		from autodev.swarm.context import ContextSynthesizer

		config = MagicMock()
		config.target.name = "test-project"
		config.target.resolved_path = str(tmp_path)
		db = _make_db()

		team_name = "autodev-test-project"
		inbox_dir = tmp_path / ".claude" / "teams" / team_name / "inboxes"
		inbox_dir.mkdir(parents=True, exist_ok=True)

		messages = [{
			"from": "worker-1",
			"type": "auth_request",
			"service": "github",
			"url": "https://github.com/login",
			"purpose": "CLI access",
			"text": "Need auth for github",
		}]
		(inbox_dir / "team-lead.json").write_text(json.dumps(messages))

		with patch.object(Path, "home", return_value=tmp_path):
			ctx = ContextSynthesizer(config, db, team_name)
			discoveries = ctx._get_recent_discoveries()

		assert any("auth_request" in d for d in discoveries)
		assert any("github" in d for d in discoveries)


class TestWorkerPromptAuth:
	"""Verify auth request instructions appear in worker prompts."""

	def test_auth_section_in_worker_prompt(self, tmp_path: Path) -> None:
		"""build_worker_prompt includes authentication instructions."""
		from autodev.swarm.worker_prompt import build_worker_prompt

		agent = SwarmAgent(name="worker-1", role=AgentRole.IMPLEMENTER, status=AgentStatus.WORKING)
		config = MagicMock()
		config.target.resolved_path = str(tmp_path)
		config.target.verification = None
		sc = SwarmConfig()

		prompt = build_worker_prompt(
			agent=agent,
			task_prompt="Do stuff",
			team_name="autodev-test",
			agents=[agent],
			tasks=[],
			config=config,
			swarm_config=sc,
		)
		assert "## Authentication" in prompt
		assert "auth_request" in prompt
		assert "auth_response" in prompt
		assert "team-lead.json" in prompt

	def test_auth_section_includes_agent_name(self) -> None:
		"""Auth section personalizes agent name in message template."""
		from autodev.swarm.worker_prompt import _auth_request_section

		agent = SwarmAgent(name="my-agent", role=AgentRole.IMPLEMENTER, status=AgentStatus.WORKING)
		text = _auth_request_section(agent, "autodev-test")
		assert "my-agent" in text
		assert "autodev-test" in text
