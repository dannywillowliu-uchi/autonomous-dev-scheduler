"""Worker agent -- claim tasks, spawn Claude sessions, push results."""

from __future__ import annotations

import asyncio
import logging

from mission_control.config import MissionConfig
from mission_control.db import Database
from mission_control.models import MergeRequest, Worker, WorkUnit, _now_iso
from mission_control.session import parse_mc_result

logger = logging.getLogger(__name__)

WORKER_PROMPT_TEMPLATE = """\
You are a parallel worker agent for {target_name} at {workspace_path}.

## Task
{title}

{description}

## Scope
ONLY modify files related to this task.
Files likely involved: {files_hint}

## Current Project State
- Tests: {test_passed}/{test_total} passing
- Lint errors: {lint_errors}
- Type errors: {type_errors}
- Branch: {branch_name}

## Verification Focus
{verification_hint}

## Context
{context_block}

## Instructions
1. Implement the task described above
2. ONLY modify files listed in the scope (or closely related files)
3. Run verification: {verification_command}
4. If verification passes, commit with a descriptive message
5. If verification fails after 3 attempts, stop and report what went wrong
6. Do NOT modify unrelated files or tests

## Output
When done, write a summary as the LAST line of output:
MC_RESULT:{{"status":"completed|failed|blocked","commits":["hash"],"summary":"what you did","files_changed":["list"]}}
"""


def render_worker_prompt(
	unit: WorkUnit,
	config: MissionConfig,
	workspace_path: str,
	branch_name: str,
	test_passed: int = 0,
	test_total: int = 0,
	lint_errors: int = 0,
	type_errors: int = 0,
	context: str = "",
) -> str:
	"""Render the prompt template for a worker session."""
	return WORKER_PROMPT_TEMPLATE.format(
		target_name=config.target.name,
		workspace_path=workspace_path,
		title=unit.title,
		description=unit.description,
		files_hint=unit.files_hint or "Not specified",
		test_passed=test_passed,
		test_total=test_total,
		lint_errors=lint_errors,
		type_errors=type_errors,
		branch_name=branch_name,
		verification_hint=unit.verification_hint or "Run full verification suite",
		context_block=context or "No additional context.",
		verification_command=config.target.verification.command,
	)


class WorkerAgent:
	"""A parallel worker that claims tasks, spawns Claude sessions, and pushes results."""

	def __init__(
		self,
		worker: Worker,
		db: Database,
		config: MissionConfig,
		heartbeat_interval: int = 30,
	) -> None:
		self.worker = worker
		self.db = db
		self.config = config
		self.heartbeat_interval = heartbeat_interval
		self.running = True
		self._heartbeat_task: asyncio.Task[None] | None = None

	async def run(self) -> None:
		"""Main loop: claim -> execute -> report, until stopped."""
		while self.running:
			unit = self.db.claim_work_unit(self.worker.id)
			if unit is None:
				await asyncio.sleep(2)
				continue

			self.worker.status = "working"
			self.worker.current_unit_id = unit.id
			self.db.update_worker(self.worker)

			await self._execute_unit(unit)

			self.worker.status = "idle"
			self.worker.current_unit_id = None
			self.db.update_worker(self.worker)

	async def _execute_unit(self, unit: WorkUnit) -> None:
		"""Execute a single work unit: branch, prompt, spawn claude, push."""
		cwd = self.worker.workspace_path
		branch_name = f"mc/unit-{unit.id}"
		unit.branch_name = branch_name
		unit.status = "running"
		unit.started_at = _now_iso()
		self.db.update_work_unit(unit)

		# Start heartbeat
		self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

		try:
			# Create branch
			branch_ok = await self._run_git("checkout", "-b", branch_name, cwd=cwd)
			if not branch_ok:
				unit.status = "failed"
				unit.output_summary = "Failed to create branch"
				unit.finished_at = _now_iso()
				self.db.update_work_unit(unit)
				self.worker.units_failed += 1
				return

			# Build prompt
			prompt = render_worker_prompt(
				unit=unit,
				config=self.config,
				workspace_path=cwd,
				branch_name=branch_name,
			)

			# Spawn Claude
			budget = self.config.scheduler.budget.max_per_session_usd
			cmd = [
				"claude",
				"-p",
				"--output-format", "stream-json",
				"--permission-mode", "bypassPermissions",
				"--model", self.config.scheduler.model,
				"--max-budget-usd", str(budget),
				prompt,
			]

			try:
				proc = await asyncio.create_subprocess_exec(
					*cmd,
					stdout=asyncio.subprocess.PIPE,
					stderr=asyncio.subprocess.STDOUT,
					cwd=cwd,
				)
				stdout_bytes, _ = await asyncio.wait_for(
					proc.communicate(),
					timeout=self.config.scheduler.session_timeout,
				)
				output = stdout_bytes.decode("utf-8", errors="replace")
				unit.exit_code = proc.returncode

			except asyncio.TimeoutError:
				unit.status = "failed"
				unit.output_summary = f"Timed out after {self.config.scheduler.session_timeout}s"
				unit.finished_at = _now_iso()
				self.db.update_work_unit(unit)
				self.worker.units_failed += 1
				return

			# Parse result
			mc_result = parse_mc_result(output)
			if mc_result:
				status = str(mc_result.get("status", "completed"))
				unit.output_summary = str(mc_result.get("summary", ""))
				commits = mc_result.get("commits", [])
				if isinstance(commits, list) and commits:
					unit.commit_hash = str(commits[0])
			else:
				status = "completed" if unit.exit_code == 0 else "failed"
				unit.output_summary = output[-500:]

			if status == "completed" and unit.commit_hash:
				# Push branch (it stays local in the clone, accessible via filesystem)
				unit.status = "completed"
				unit.finished_at = _now_iso()
				self.db.update_work_unit(unit)

				# Submit merge request
				mr = MergeRequest(
					work_unit_id=unit.id,
					worker_id=self.worker.id,
					branch_name=branch_name,
					commit_hash=unit.commit_hash or "",
					position=self.db.get_next_merge_position(),
				)
				self.db.insert_merge_request(mr)
				self.worker.units_completed += 1
			else:
				unit.status = "failed"
				unit.finished_at = _now_iso()
				unit.attempt += 1
				self.db.update_work_unit(unit)
				self.worker.units_failed += 1

				# Reset workspace to base branch for next task
				await self._run_git("checkout", self.config.target.branch, cwd=cwd)
				await self._run_git("branch", "-D", branch_name, cwd=cwd)

		finally:
			# Stop heartbeat
			if self._heartbeat_task:
				self._heartbeat_task.cancel()
				try:
					await self._heartbeat_task
				except asyncio.CancelledError:
					pass
				self._heartbeat_task = None

	async def _heartbeat_loop(self) -> None:
		"""Periodically update heartbeat in the DB."""
		while True:
			await asyncio.sleep(self.heartbeat_interval)
			self.db.update_heartbeat(self.worker.id)

	async def _run_git(self, *args: str, cwd: str) -> bool:
		"""Run a git command."""
		proc = await asyncio.create_subprocess_exec(
			"git", *args,
			cwd=cwd,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.STDOUT,
		)
		await proc.communicate()
		return proc.returncode == 0

	def stop(self) -> None:
		self.running = False
