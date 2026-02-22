"""Tests for A2A protocol support."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mission_control.a2a import (
	A2AClient,
	A2AServer,
	A2ATaskCreate,
	A2ATaskState,
	AgentCard,
	_wu_status_to_a2a,
)
from mission_control.config import A2AConfig, load_config
from mission_control.db import Database


@pytest.fixture()
def a2a_config() -> A2AConfig:
	return A2AConfig(
		enabled=True,
		host="127.0.0.1",
		port=8420,
		agent_name="test-agent",
		agent_version="0.2.0",
		agent_capabilities=["code_edit", "test_write"],
	)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
	"""File-backed DB with cross-thread access for TestClient."""
	db_path = tmp_path / "test.db"
	database = Database(db_path)
	# Reopen with check_same_thread=False for FastAPI TestClient
	database.conn.close()
	database.conn = sqlite3.connect(str(db_path), check_same_thread=False)
	database.conn.row_factory = sqlite3.Row
	database.conn.execute("PRAGMA foreign_keys=ON")
	database.conn.execute("PRAGMA journal_mode=WAL")
	database.conn.execute("PRAGMA busy_timeout=5000")
	return database


@pytest.fixture()
def server(a2a_config: A2AConfig, db: Database) -> A2AServer:
	return A2AServer(a2a_config, db)


@pytest.fixture()
def client(server: A2AServer) -> TestClient:
	return TestClient(server.app)


# -- AgentCard tests --

def test_agent_card_serialization() -> None:
	card = AgentCard(
		name="mc",
		version="1.0",
		capabilities=["code_edit"],
		endpoint="http://localhost:8420",
	)
	data = card.model_dump()
	assert data["name"] == "mc"
	assert data["version"] == "1.0"
	assert data["capabilities"] == ["code_edit"]
	assert data["endpoint"] == "http://localhost:8420"


def test_agent_card_required_fields() -> None:
	card = AgentCard(name="x", version="0.1")
	assert card.description == "mission-control autonomous development agent"
	assert card.capabilities == []
	assert card.auth == {}


# -- A2ATaskCreate validation --

def test_task_create_defaults() -> None:
	task = A2ATaskCreate(title="Do something")
	assert task.description == ""
	assert task.task_type == "code_edit"
	assert task.metadata == {}


def test_task_create_full() -> None:
	task = A2ATaskCreate(
		title="Fix bug",
		description="Fix the login bug",
		task_type="refactor",
		metadata={"priority": "high"},
	)
	assert task.title == "Fix bug"
	assert task.metadata["priority"] == "high"


# -- Status mapping --

def test_wu_status_pending_to_submitted() -> None:
	assert _wu_status_to_a2a("pending") == "submitted"


def test_wu_status_queued_to_submitted() -> None:
	assert _wu_status_to_a2a("queued") == "submitted"


def test_wu_status_dispatched_to_working() -> None:
	assert _wu_status_to_a2a("dispatched") == "working"


def test_wu_status_running_to_working() -> None:
	assert _wu_status_to_a2a("running") == "working"


def test_wu_status_completed_to_completed() -> None:
	assert _wu_status_to_a2a("completed") == "completed"


def test_wu_status_merged_to_completed() -> None:
	assert _wu_status_to_a2a("merged") == "completed"


def test_wu_status_failed_to_failed() -> None:
	assert _wu_status_to_a2a("failed") == "failed"


def test_wu_status_cancelled_to_canceled() -> None:
	assert _wu_status_to_a2a("cancelled") == "canceled"


def test_wu_status_unknown_defaults_to_submitted() -> None:
	assert _wu_status_to_a2a("unknown_state") == "submitted"


# -- Server route tests --

def test_get_agent_card_route(client: TestClient, a2a_config: A2AConfig) -> None:
	resp = client.get("/.well-known/agent.json")
	assert resp.status_code == 200
	data = resp.json()
	assert data["name"] == "test-agent"
	assert data["version"] == "0.2.0"
	assert "code_edit" in data["capabilities"]


def test_create_task_route(client: TestClient) -> None:
	resp = client.post("/a2a/tasks", json={
		"title": "Implement feature X",
		"description": "Add feature X to the API",
	})
	assert resp.status_code == 201
	data = resp.json()
	assert data["state"] == "submitted"
	assert data["summary"] == "Implement feature X"
	assert data["id"]  # non-empty


def test_get_task_route(client: TestClient) -> None:
	create_resp = client.post("/a2a/tasks", json={"title": "Test task"})
	task_id = create_resp.json()["id"]

	resp = client.get(f"/a2a/tasks/{task_id}")
	assert resp.status_code == 200
	data = resp.json()
	assert data["id"] == task_id
	assert data["state"] == "submitted"


def test_get_task_not_found(client: TestClient) -> None:
	resp = client.get("/a2a/tasks/nonexistent-id")
	assert resp.status_code == 404


def test_cancel_task_route(client: TestClient) -> None:
	create_resp = client.post("/a2a/tasks", json={"title": "Cancel me"})
	task_id = create_resp.json()["id"]

	resp = client.post(f"/a2a/tasks/{task_id}/cancel")
	assert resp.status_code == 200
	data = resp.json()
	assert data["state"] == "canceled"


def test_cancel_nonexistent_task(client: TestClient) -> None:
	resp = client.post("/a2a/tasks/nonexistent-id/cancel")
	assert resp.status_code == 404


# -- Client tests (mocked httpx) --

@pytest.mark.asyncio
async def test_client_discover() -> None:
	card_data = {
		"name": "remote-agent",
		"version": "1.0",
		"capabilities": ["code_edit"],
	}
	mock_resp = MagicMock()
	mock_resp.json.return_value = card_data
	mock_resp.raise_for_status = MagicMock()

	a2a_client = A2AClient()
	with patch.object(a2a_client._client, "get", AsyncMock(return_value=mock_resp)) as mock_get:
		card = await a2a_client.discover("http://example.com")
		mock_get.assert_called_once_with("http://example.com/.well-known/agent.json")
		assert card.name == "remote-agent"
	await a2a_client.close()


@pytest.mark.asyncio
async def test_client_delegate() -> None:
	status_data = {"id": "t1", "state": "submitted", "summary": "Do X"}
	mock_resp = MagicMock()
	mock_resp.json.return_value = status_data
	mock_resp.raise_for_status = MagicMock()

	a2a_client = A2AClient()
	task = A2ATaskCreate(title="Do X")
	with patch.object(a2a_client._client, "post", AsyncMock(return_value=mock_resp)) as mock_post:
		result = await a2a_client.delegate("http://example.com", task)
		mock_post.assert_called_once()
		assert result.id == "t1"
		assert result.state == "submitted"
	await a2a_client.close()


# -- Config tests --

def test_a2a_config_defaults() -> None:
	cfg = A2AConfig()
	assert cfg.enabled is False
	assert cfg.host == "0.0.0.0"
	assert cfg.port == 8420
	assert cfg.agent_name == "mission-control"
	assert cfg.agent_version == "0.1.0"
	assert "code_edit" in cfg.agent_capabilities


def test_a2a_config_toml_parsing(tmp_path: object) -> None:
	from pathlib import Path
	p = Path(str(tmp_path)) / "mission-control.toml"
	p.write_text("""\
[target]
name = "test"
path = "."

[a2a]
enabled = true
host = "127.0.0.1"
port = 9999
agent_name = "custom-agent"
agent_capabilities = ["test_write"]
""")
	config = load_config(p)
	assert config.a2a.enabled is True
	assert config.a2a.host == "127.0.0.1"
	assert config.a2a.port == 9999
	assert config.a2a.agent_name == "custom-agent"
	assert config.a2a.agent_capabilities == ["test_write"]


# -- A2ATaskState enum --

def test_task_state_values() -> None:
	assert A2ATaskState.SUBMITTED == "submitted"
	assert A2ATaskState.WORKING == "working"
	assert A2ATaskState.COMPLETED == "completed"
	assert A2ATaskState.FAILED == "failed"
	assert A2ATaskState.CANCELED == "canceled"
