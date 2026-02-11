"""Tests for the worker agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mission_control.config import MissionConfig, TargetConfig, VerificationConfig
from mission_control.db import Database
from mission_control.models import Plan, Worker, WorkUnit
from mission_control.worker import WorkerAgent, render_worker_prompt


@pytest.fixture()
def db() -> Database:
	return Database(":memory:")


@pytest.fixture()
def config() -> MissionConfig:
	cfg = MissionConfig()
	cfg.target = TargetConfig(
		name="test-proj",
		path="/tmp/test",
		branch="main",
		verification=VerificationConfig(command="pytest -q"),
	)
	return cfg


@pytest.fixture()
def worker_and_unit(db: Database) -> tuple[Worker, WorkUnit]:
	db.insert_plan(Plan(id="p1", objective="test"))
	wu = WorkUnit(id="wu1", plan_id="p1", title="Fix tests", description="Fix failing tests")
	db.insert_work_unit(wu)
	w = Worker(id="w1", workspace_path="/tmp/clone1")
	db.insert_worker(w)
	return w, wu


class TestRenderWorkerPrompt:
	def test_contains_title(self, config: MissionConfig) -> None:
		unit = WorkUnit(title="Fix lint", description="Fix ruff errors")
		prompt = render_worker_prompt(unit, config, "/tmp/clone", "mc/unit-abc")
		assert "Fix lint" in prompt
		assert "Fix ruff errors" in prompt

	def test_contains_files_hint(self, config: MissionConfig) -> None:
		unit = WorkUnit(title="X", files_hint="src/foo.py,src/bar.py")
		prompt = render_worker_prompt(unit, config, "/tmp/clone", "mc/unit-abc")
		assert "src/foo.py,src/bar.py" in prompt

	def test_contains_branch_name(self, config: MissionConfig) -> None:
		unit = WorkUnit(title="X")
		prompt = render_worker_prompt(unit, config, "/tmp/clone", "mc/unit-abc123")
		assert "mc/unit-abc123" in prompt

	def test_contains_verification_hint(self, config: MissionConfig) -> None:
		unit = WorkUnit(title="X", verification_hint="Run test_foo.py specifically")
		prompt = render_worker_prompt(unit, config, "/tmp/clone", "mc/unit-x")
		assert "Run test_foo.py specifically" in prompt

	def test_contains_verification_command(self, config: MissionConfig) -> None:
		unit = WorkUnit(title="X")
		prompt = render_worker_prompt(unit, config, "/tmp/clone", "mc/unit-x")
		assert "pytest -q" in prompt

	def test_contains_target_name(self, config: MissionConfig) -> None:
		unit = WorkUnit(title="X")
		prompt = render_worker_prompt(unit, config, "/tmp/clone", "mc/unit-x")
		assert "test-proj" in prompt

	def test_default_files_hint(self, config: MissionConfig) -> None:
		unit = WorkUnit(title="X")
		prompt = render_worker_prompt(unit, config, "/tmp/clone", "mc/unit-x")
		assert "Not specified" in prompt


class TestWorkerAgent:
	async def test_heartbeat_fires(
		self, db: Database, config: MissionConfig, worker_and_unit: tuple[Worker, WorkUnit],
	) -> None:
		w, wu = worker_and_unit
		agent = WorkerAgent(w, db, config, heartbeat_interval=1)

		# Claim the unit manually so heartbeat has something to update
		claimed = db.claim_work_unit(w.id)
		assert claimed is not None

		# Start heartbeat, let it fire once, then cancel
		task = agent._heartbeat_loop()  # noqa: SLF001
		ht = __import__("asyncio").create_task(task)
		await __import__("asyncio").sleep(1.5)
		ht.cancel()
		try:
			await ht
		except __import__("asyncio").CancelledError:
			pass

		# Check heartbeat was updated
		refreshed = db.get_work_unit(claimed.id)
		assert refreshed is not None
		# heartbeat_at should have been updated (it was set during claim, then updated by heartbeat)

	async def test_successful_unit_execution(
		self, db: Database, config: MissionConfig, worker_and_unit: tuple[Worker, WorkUnit],
	) -> None:
		w, _ = worker_and_unit

		mock_proc = AsyncMock()
		mock_proc.communicate = AsyncMock(return_value=(
			b'MC_RESULT:{"status":"completed","commits":["abc123"],"summary":"Fixed it","files_changed":["foo.py"]}',
			b"",
		))
		mock_proc.returncode = 0

		with (
			patch("mission_control.worker.asyncio.create_subprocess_exec", return_value=mock_proc),
			patch("mission_control.worker.asyncio.wait_for", return_value=mock_proc.communicate.return_value),
		):
			agent = WorkerAgent(w, db, config, heartbeat_interval=9999)

			# Run one iteration
			agent.running = True

			async def run_once() -> None:
				unit = db.claim_work_unit(w.id)
				if unit:
					await agent._execute_unit(unit)  # noqa: SLF001

			await run_once()

		# Check work unit was completed
		unit = db.get_work_unit("wu1")
		assert unit is not None
		assert unit.status == "completed"
		assert unit.commit_hash == "abc123"
		assert unit.output_summary == "Fixed it"

		# Check merge request was created
		mr = db.get_next_merge_request()
		assert mr is not None
		assert mr.work_unit_id == "wu1"
		assert mr.worker_id == "w1"

	async def test_failed_unit_marks_correctly(
		self, db: Database, config: MissionConfig, worker_and_unit: tuple[Worker, WorkUnit],
	) -> None:
		w, _ = worker_and_unit

		# Git operations succeed, Claude session fails
		git_proc = AsyncMock()
		git_proc.communicate = AsyncMock(return_value=(b"", b""))
		git_proc.returncode = 0

		claude_proc = AsyncMock()
		claude_proc.communicate = AsyncMock(return_value=(b"Error: something broke", b""))
		claude_proc.returncode = 1

		call_count = 0

		async def mock_create_subprocess(*args: object, **kwargs: object) -> AsyncMock:
			nonlocal call_count
			call_count += 1
			# First call is git checkout -b, rest could be git or claude
			if call_count <= 1:
				return git_proc
			return claude_proc

		with (
			patch("mission_control.worker.asyncio.create_subprocess_exec", side_effect=mock_create_subprocess),
			patch("mission_control.worker.asyncio.wait_for", return_value=(b"Error: something broke", b"")),
		):
			agent = WorkerAgent(w, db, config, heartbeat_interval=9999)
			unit = db.claim_work_unit(w.id)
			assert unit is not None
			await agent._execute_unit(unit)  # noqa: SLF001

		result = db.get_work_unit("wu1")
		assert result is not None
		assert result.status == "failed"
		assert result.attempt == 1
		assert w.units_failed == 1

	async def test_timeout_marks_failed(
		self, db: Database, config: MissionConfig, worker_and_unit: tuple[Worker, WorkUnit],
	) -> None:
		w, _ = worker_and_unit

		mock_proc = AsyncMock()
		mock_proc.communicate = AsyncMock(return_value=(b"", b""))
		mock_proc.returncode = 0

		with (
			patch("mission_control.worker.asyncio.create_subprocess_exec", return_value=mock_proc),
			patch("mission_control.worker.asyncio.wait_for", side_effect=__import__("asyncio").TimeoutError),
		):
			agent = WorkerAgent(w, db, config, heartbeat_interval=9999)
			unit = db.claim_work_unit(w.id)
			assert unit is not None
			await agent._execute_unit(unit)  # noqa: SLF001

		result = db.get_work_unit("wu1")
		assert result is not None
		assert result.status == "failed"
		assert "Timed out" in result.output_summary

	def test_stop(self, db: Database, config: MissionConfig) -> None:
		w = Worker(id="w1", workspace_path="/tmp/clone")
		agent = WorkerAgent(w, db, config)
		assert agent.running is True
		agent.stop()
		assert agent.running is False
