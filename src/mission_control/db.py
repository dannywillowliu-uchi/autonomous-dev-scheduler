"""SQLite database operations for mission-control state."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from mission_control.models import (
	Decision,
	MergeRequest,
	Plan,
	Session,
	Snapshot,
	TaskRecord,
	Worker,
	WorkUnit,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
	id TEXT PRIMARY KEY,
	target_name TEXT NOT NULL,
	task_description TEXT NOT NULL DEFAULT '',
	status TEXT NOT NULL DEFAULT 'pending',
	branch_name TEXT NOT NULL DEFAULT '',
	started_at TEXT NOT NULL,
	finished_at TEXT,
	exit_code INTEGER,
	commit_hash TEXT,
	cost_usd REAL,
	output_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots (
	id TEXT PRIMARY KEY,
	session_id TEXT,
	taken_at TEXT NOT NULL,
	test_total INTEGER NOT NULL DEFAULT 0,
	test_passed INTEGER NOT NULL DEFAULT 0,
	test_failed INTEGER NOT NULL DEFAULT 0,
	lint_errors INTEGER NOT NULL DEFAULT 0,
	type_errors INTEGER NOT NULL DEFAULT 0,
	security_findings INTEGER NOT NULL DEFAULT 0,
	raw_output TEXT NOT NULL DEFAULT '',
	FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS tasks (
	id TEXT PRIMARY KEY,
	source TEXT NOT NULL DEFAULT '',
	description TEXT NOT NULL DEFAULT '',
	priority INTEGER NOT NULL DEFAULT 7,
	status TEXT NOT NULL DEFAULT 'discovered',
	session_id TEXT,
	created_at TEXT NOT NULL,
	resolved_at TEXT,
	FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS decisions (
	id TEXT PRIMARY KEY,
	session_id TEXT NOT NULL,
	decision TEXT NOT NULL DEFAULT '',
	rationale TEXT NOT NULL DEFAULT '',
	timestamp TEXT NOT NULL,
	FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS plans (
	id TEXT PRIMARY KEY,
	objective TEXT NOT NULL DEFAULT '',
	status TEXT NOT NULL DEFAULT 'pending',
	created_at TEXT NOT NULL,
	finished_at TEXT,
	raw_planner_output TEXT NOT NULL DEFAULT '',
	total_units INTEGER NOT NULL DEFAULT 0,
	completed_units INTEGER NOT NULL DEFAULT 0,
	failed_units INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS work_units (
	id TEXT PRIMARY KEY,
	plan_id TEXT NOT NULL,
	title TEXT NOT NULL DEFAULT '',
	description TEXT NOT NULL DEFAULT '',
	files_hint TEXT NOT NULL DEFAULT '',
	verification_hint TEXT NOT NULL DEFAULT '',
	priority INTEGER NOT NULL DEFAULT 1,
	status TEXT NOT NULL DEFAULT 'pending',
	worker_id TEXT,
	depends_on TEXT NOT NULL DEFAULT '',
	branch_name TEXT NOT NULL DEFAULT '',
	claimed_at TEXT,
	heartbeat_at TEXT,
	started_at TEXT,
	finished_at TEXT,
	exit_code INTEGER,
	commit_hash TEXT,
	output_summary TEXT NOT NULL DEFAULT '',
	attempt INTEGER NOT NULL DEFAULT 0,
	max_attempts INTEGER NOT NULL DEFAULT 3,
	FOREIGN KEY (plan_id) REFERENCES plans(id)
);

CREATE INDEX IF NOT EXISTS idx_work_units_status ON work_units(status, priority);

CREATE TABLE IF NOT EXISTS workers (
	id TEXT PRIMARY KEY,
	workspace_path TEXT NOT NULL DEFAULT '',
	status TEXT NOT NULL DEFAULT 'idle',
	current_unit_id TEXT,
	pid INTEGER,
	started_at TEXT NOT NULL,
	last_heartbeat TEXT NOT NULL,
	units_completed INTEGER NOT NULL DEFAULT 0,
	units_failed INTEGER NOT NULL DEFAULT 0,
	total_cost_usd REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS merge_requests (
	id TEXT PRIMARY KEY,
	work_unit_id TEXT NOT NULL,
	worker_id TEXT NOT NULL,
	branch_name TEXT NOT NULL DEFAULT '',
	commit_hash TEXT NOT NULL DEFAULT '',
	status TEXT NOT NULL DEFAULT 'pending',
	position INTEGER NOT NULL DEFAULT 0,
	created_at TEXT NOT NULL,
	verified_at TEXT,
	merged_at TEXT,
	rejection_reason TEXT NOT NULL DEFAULT '',
	rebase_attempts INTEGER NOT NULL DEFAULT 0,
	FOREIGN KEY (work_unit_id) REFERENCES work_units(id)
);

CREATE INDEX IF NOT EXISTS idx_merge_requests_status ON merge_requests(status, position);
"""


class Database:
	"""SQLite database for mission-control state."""

	def __init__(self, path: str | Path = ":memory:") -> None:
		db_path = str(path)
		self.conn = sqlite3.connect(db_path)
		self.conn.row_factory = sqlite3.Row
		if db_path != ":memory:":
			self.conn.execute("PRAGMA journal_mode=WAL")
		self.conn.execute("PRAGMA foreign_keys=ON")
		self._create_tables()

	def _create_tables(self) -> None:
		self.conn.executescript(SCHEMA_SQL)

	def close(self) -> None:
		self.conn.close()

	# -- Sessions --

	def insert_session(self, session: Session) -> None:
		self.conn.execute(
			"""INSERT INTO sessions
			(id, target_name, task_description, status, branch_name,
			 started_at, finished_at, exit_code, commit_hash, cost_usd, output_summary)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				session.id, session.target_name, session.task_description,
				session.status, session.branch_name, session.started_at,
				session.finished_at, session.exit_code, session.commit_hash,
				session.cost_usd, session.output_summary,
			),
		)
		self.conn.commit()

	def update_session(self, session: Session) -> None:
		self.conn.execute(
			"""UPDATE sessions SET
			target_name=?, task_description=?, status=?, branch_name=?,
			started_at=?, finished_at=?, exit_code=?, commit_hash=?,
			cost_usd=?, output_summary=?
			WHERE id=?""",
			(
				session.target_name, session.task_description, session.status,
				session.branch_name, session.started_at, session.finished_at,
				session.exit_code, session.commit_hash, session.cost_usd,
				session.output_summary, session.id,
			),
		)
		self.conn.commit()

	def get_session(self, session_id: str) -> Session | None:
		row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
		if row is None:
			return None
		return self._row_to_session(row)

	def get_recent_sessions(self, limit: int = 10) -> list[Session]:
		rows = self.conn.execute(
			"SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
		).fetchall()
		return [self._row_to_session(r) for r in rows]

	@staticmethod
	def _row_to_session(row: sqlite3.Row) -> Session:
		return Session(
			id=row["id"],
			target_name=row["target_name"],
			task_description=row["task_description"],
			status=row["status"],
			branch_name=row["branch_name"],
			started_at=row["started_at"],
			finished_at=row["finished_at"],
			exit_code=row["exit_code"],
			commit_hash=row["commit_hash"],
			cost_usd=row["cost_usd"],
			output_summary=row["output_summary"],
		)

	# -- Snapshots --

	def insert_snapshot(self, snapshot: Snapshot) -> None:
		self.conn.execute(
			"""INSERT INTO snapshots
			(id, session_id, taken_at, test_total, test_passed, test_failed,
			 lint_errors, type_errors, security_findings, raw_output)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				snapshot.id, snapshot.session_id, snapshot.taken_at,
				snapshot.test_total, snapshot.test_passed, snapshot.test_failed,
				snapshot.lint_errors, snapshot.type_errors, snapshot.security_findings,
				snapshot.raw_output,
			),
		)
		self.conn.commit()

	def get_latest_snapshot(self) -> Snapshot | None:
		row = self.conn.execute(
			"SELECT * FROM snapshots ORDER BY taken_at DESC LIMIT 1"
		).fetchone()
		if row is None:
			return None
		return self._row_to_snapshot(row)

	@staticmethod
	def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
		return Snapshot(
			id=row["id"],
			session_id=row["session_id"],
			taken_at=row["taken_at"],
			test_total=row["test_total"],
			test_passed=row["test_passed"],
			test_failed=row["test_failed"],
			lint_errors=row["lint_errors"],
			type_errors=row["type_errors"],
			security_findings=row["security_findings"],
			raw_output=row["raw_output"],
		)

	# -- Tasks --

	def insert_task(self, task: TaskRecord) -> None:
		self.conn.execute(
			"""INSERT INTO tasks
			(id, source, description, priority, status, session_id, created_at, resolved_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				task.id, task.source, task.description, task.priority,
				task.status, task.session_id, task.created_at, task.resolved_at,
			),
		)
		self.conn.commit()

	def update_task(self, task: TaskRecord) -> None:
		self.conn.execute(
			"""UPDATE tasks SET
			source=?, description=?, priority=?, status=?,
			session_id=?, created_at=?, resolved_at=?
			WHERE id=?""",
			(
				task.source, task.description, task.priority, task.status,
				task.session_id, task.created_at, task.resolved_at, task.id,
			),
		)
		self.conn.commit()

	def get_open_tasks(self, limit: int = 20) -> list[TaskRecord]:
		rows = self.conn.execute(
			"SELECT * FROM tasks WHERE status IN ('discovered', 'assigned') "
			"ORDER BY priority ASC, created_at ASC LIMIT ?",
			(limit,),
		).fetchall()
		return [self._row_to_task(r) for r in rows]

	@staticmethod
	def _row_to_task(row: sqlite3.Row) -> TaskRecord:
		return TaskRecord(
			id=row["id"],
			source=row["source"],
			description=row["description"],
			priority=row["priority"],
			status=row["status"],
			session_id=row["session_id"],
			created_at=row["created_at"],
			resolved_at=row["resolved_at"],
		)

	# -- Decisions --

	def insert_decision(self, decision: Decision) -> None:
		self.conn.execute(
			"""INSERT INTO decisions (id, session_id, decision, rationale, timestamp)
			VALUES (?, ?, ?, ?, ?)""",
			(decision.id, decision.session_id, decision.decision, decision.rationale, decision.timestamp),
		)
		self.conn.commit()

	def get_recent_decisions(self, limit: int = 5) -> list[Decision]:
		rows = self.conn.execute(
			"SELECT * FROM decisions ORDER BY timestamp DESC LIMIT ?", (limit,)
		).fetchall()
		return [self._row_to_decision(r) for r in rows]

	@staticmethod
	def _row_to_decision(row: sqlite3.Row) -> Decision:
		return Decision(
			id=row["id"],
			session_id=row["session_id"],
			decision=row["decision"],
			rationale=row["rationale"],
			timestamp=row["timestamp"],
		)

	# -- Bulk persist --

	def persist_session_result(
		self,
		session: Session,
		before: Snapshot,
		after: Snapshot,
		decisions: Sequence[Decision] | None = None,
	) -> None:
		"""Persist a complete session result atomically."""
		self.insert_session(session)
		before.session_id = session.id
		after.session_id = session.id
		self.insert_snapshot(before)
		self.insert_snapshot(after)
		if decisions:
			for d in decisions:
				d.session_id = session.id
				self.insert_decision(d)

	# -- Plans --

	def insert_plan(self, plan: Plan) -> None:
		self.conn.execute(
			"""INSERT INTO plans
			(id, objective, status, created_at, finished_at,
			 raw_planner_output, total_units, completed_units, failed_units)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				plan.id, plan.objective, plan.status, plan.created_at,
				plan.finished_at, plan.raw_planner_output,
				plan.total_units, plan.completed_units, plan.failed_units,
			),
		)
		self.conn.commit()

	def update_plan(self, plan: Plan) -> None:
		self.conn.execute(
			"""UPDATE plans SET
			objective=?, status=?, finished_at=?,
			raw_planner_output=?, total_units=?,
			completed_units=?, failed_units=?
			WHERE id=?""",
			(
				plan.objective, plan.status, plan.finished_at,
				plan.raw_planner_output, plan.total_units,
				plan.completed_units, plan.failed_units, plan.id,
			),
		)
		self.conn.commit()

	def get_plan(self, plan_id: str) -> Plan | None:
		row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
		if row is None:
			return None
		return self._row_to_plan(row)

	@staticmethod
	def _row_to_plan(row: sqlite3.Row) -> Plan:
		return Plan(
			id=row["id"],
			objective=row["objective"],
			status=row["status"],
			created_at=row["created_at"],
			finished_at=row["finished_at"],
			raw_planner_output=row["raw_planner_output"],
			total_units=row["total_units"],
			completed_units=row["completed_units"],
			failed_units=row["failed_units"],
		)

	# -- Work Units --

	def insert_work_unit(self, unit: WorkUnit) -> None:
		self.conn.execute(
			"""INSERT INTO work_units
			(id, plan_id, title, description, files_hint, verification_hint,
			 priority, status, worker_id, depends_on, branch_name,
			 claimed_at, heartbeat_at, started_at, finished_at,
			 exit_code, commit_hash, output_summary, attempt, max_attempts)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				unit.id, unit.plan_id, unit.title, unit.description,
				unit.files_hint, unit.verification_hint, unit.priority,
				unit.status, unit.worker_id, unit.depends_on, unit.branch_name,
				unit.claimed_at, unit.heartbeat_at, unit.started_at,
				unit.finished_at, unit.exit_code, unit.commit_hash,
				unit.output_summary, unit.attempt, unit.max_attempts,
			),
		)
		self.conn.commit()

	def update_work_unit(self, unit: WorkUnit) -> None:
		self.conn.execute(
			"""UPDATE work_units SET
			plan_id=?, title=?, description=?, files_hint=?,
			verification_hint=?, priority=?, status=?, worker_id=?,
			depends_on=?, branch_name=?, claimed_at=?, heartbeat_at=?,
			started_at=?, finished_at=?, exit_code=?, commit_hash=?,
			output_summary=?, attempt=?, max_attempts=?
			WHERE id=?""",
			(
				unit.plan_id, unit.title, unit.description, unit.files_hint,
				unit.verification_hint, unit.priority, unit.status,
				unit.worker_id, unit.depends_on, unit.branch_name,
				unit.claimed_at, unit.heartbeat_at, unit.started_at,
				unit.finished_at, unit.exit_code, unit.commit_hash,
				unit.output_summary, unit.attempt, unit.max_attempts, unit.id,
			),
		)
		self.conn.commit()

	def get_work_unit(self, unit_id: str) -> WorkUnit | None:
		row = self.conn.execute("SELECT * FROM work_units WHERE id=?", (unit_id,)).fetchone()
		if row is None:
			return None
		return self._row_to_work_unit(row)

	def get_work_units_for_plan(self, plan_id: str) -> list[WorkUnit]:
		rows = self.conn.execute(
			"SELECT * FROM work_units WHERE plan_id=? ORDER BY priority ASC",
			(plan_id,),
		).fetchall()
		return [self._row_to_work_unit(r) for r in rows]

	def claim_work_unit(self, worker_id: str, now: str | None = None) -> WorkUnit | None:
		"""Atomically claim the next available work unit.

		Only claims units that are pending and whose dependencies are all completed.
		Uses a single UPDATE to prevent race conditions.
		"""
		if now is None:
			from mission_control.models import _now_iso
			now = _now_iso()

		# Find claimable unit: pending, no incomplete deps, ordered by priority
		row = self.conn.execute(
			"""UPDATE work_units SET
				status='claimed', worker_id=?, claimed_at=?, heartbeat_at=?
			WHERE id = (
				SELECT wu.id FROM work_units wu
				WHERE wu.status = 'pending'
				AND NOT EXISTS (
					SELECT 1 FROM work_units dep
					WHERE dep.id IN (
						SELECT value FROM (
							WITH RECURSIVE split(value, rest) AS (
								SELECT '', wu.depends_on || ','
								UNION ALL
								SELECT substr(rest, 1, instr(rest, ',') - 1),
									   substr(rest, instr(rest, ',') + 1)
								FROM split WHERE rest != ''
							)
							SELECT value FROM split WHERE value != ''
						)
					)
					AND dep.status != 'completed'
				)
				ORDER BY wu.priority ASC, wu.id ASC
				LIMIT 1
			)
			RETURNING *""",
			(worker_id, now, now),
		).fetchone()
		self.conn.commit()
		if row is None:
			return None
		return self._row_to_work_unit(row)

	def recover_stale_units(self, timeout_seconds: int) -> list[WorkUnit]:
		"""Release work units where heartbeat is stale (worker likely dead)."""
		from mission_control.models import _now_iso
		now = _now_iso()

		rows = self.conn.execute(
			"""UPDATE work_units SET
				status='pending', worker_id=NULL, claimed_at=NULL, heartbeat_at=NULL
			WHERE status IN ('claimed', 'running')
			AND heartbeat_at IS NOT NULL
			AND (julianday(?) - julianday(heartbeat_at)) * 86400 > ?
			AND attempt < max_attempts
			RETURNING *""",
			(now, timeout_seconds),
		).fetchall()
		self.conn.commit()
		return [self._row_to_work_unit(r) for r in rows]

	def update_heartbeat(self, worker_id: str) -> None:
		"""Update heartbeat for all units claimed by this worker."""
		from mission_control.models import _now_iso
		now = _now_iso()

		self.conn.execute(
			"UPDATE work_units SET heartbeat_at=? WHERE worker_id=? AND status IN ('claimed', 'running')",
			(now, worker_id),
		)
		self.conn.execute(
			"UPDATE workers SET last_heartbeat=? WHERE id=?",
			(now, worker_id),
		)
		self.conn.commit()

	@staticmethod
	def _row_to_work_unit(row: sqlite3.Row) -> WorkUnit:
		return WorkUnit(
			id=row["id"],
			plan_id=row["plan_id"],
			title=row["title"],
			description=row["description"],
			files_hint=row["files_hint"],
			verification_hint=row["verification_hint"],
			priority=row["priority"],
			status=row["status"],
			worker_id=row["worker_id"],
			depends_on=row["depends_on"],
			branch_name=row["branch_name"],
			claimed_at=row["claimed_at"],
			heartbeat_at=row["heartbeat_at"],
			started_at=row["started_at"],
			finished_at=row["finished_at"],
			exit_code=row["exit_code"],
			commit_hash=row["commit_hash"],
			output_summary=row["output_summary"],
			attempt=row["attempt"],
			max_attempts=row["max_attempts"],
		)

	# -- Workers --

	def insert_worker(self, worker: Worker) -> None:
		self.conn.execute(
			"""INSERT INTO workers
			(id, workspace_path, status, current_unit_id, pid,
			 started_at, last_heartbeat, units_completed, units_failed, total_cost_usd)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				worker.id, worker.workspace_path, worker.status,
				worker.current_unit_id, worker.pid, worker.started_at,
				worker.last_heartbeat, worker.units_completed,
				worker.units_failed, worker.total_cost_usd,
			),
		)
		self.conn.commit()

	def update_worker(self, worker: Worker) -> None:
		self.conn.execute(
			"""UPDATE workers SET
			workspace_path=?, status=?, current_unit_id=?, pid=?,
			started_at=?, last_heartbeat=?, units_completed=?,
			units_failed=?, total_cost_usd=?
			WHERE id=?""",
			(
				worker.workspace_path, worker.status, worker.current_unit_id,
				worker.pid, worker.started_at, worker.last_heartbeat,
				worker.units_completed, worker.units_failed,
				worker.total_cost_usd, worker.id,
			),
		)
		self.conn.commit()

	def get_worker(self, worker_id: str) -> Worker | None:
		row = self.conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
		if row is None:
			return None
		return self._row_to_worker(row)

	def get_all_workers(self) -> list[Worker]:
		rows = self.conn.execute("SELECT * FROM workers ORDER BY started_at").fetchall()
		return [self._row_to_worker(r) for r in rows]

	@staticmethod
	def _row_to_worker(row: sqlite3.Row) -> Worker:
		return Worker(
			id=row["id"],
			workspace_path=row["workspace_path"],
			status=row["status"],
			current_unit_id=row["current_unit_id"],
			pid=row["pid"],
			started_at=row["started_at"],
			last_heartbeat=row["last_heartbeat"],
			units_completed=row["units_completed"],
			units_failed=row["units_failed"],
			total_cost_usd=row["total_cost_usd"],
		)

	# -- Merge Requests --

	def insert_merge_request(self, mr: MergeRequest) -> None:
		self.conn.execute(
			"""INSERT INTO merge_requests
			(id, work_unit_id, worker_id, branch_name, commit_hash,
			 status, position, created_at, verified_at, merged_at,
			 rejection_reason, rebase_attempts)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				mr.id, mr.work_unit_id, mr.worker_id, mr.branch_name,
				mr.commit_hash, mr.status, mr.position, mr.created_at,
				mr.verified_at, mr.merged_at, mr.rejection_reason,
				mr.rebase_attempts,
			),
		)
		self.conn.commit()

	def update_merge_request(self, mr: MergeRequest) -> None:
		self.conn.execute(
			"""UPDATE merge_requests SET
			work_unit_id=?, worker_id=?, branch_name=?, commit_hash=?,
			status=?, position=?, verified_at=?, merged_at=?,
			rejection_reason=?, rebase_attempts=?
			WHERE id=?""",
			(
				mr.work_unit_id, mr.worker_id, mr.branch_name, mr.commit_hash,
				mr.status, mr.position, mr.verified_at, mr.merged_at,
				mr.rejection_reason, mr.rebase_attempts, mr.id,
			),
		)
		self.conn.commit()

	def get_next_merge_request(self) -> MergeRequest | None:
		"""Get the next pending merge request by position."""
		row = self.conn.execute(
			"SELECT * FROM merge_requests WHERE status='pending' ORDER BY position ASC LIMIT 1"
		).fetchone()
		if row is None:
			return None
		return self._row_to_merge_request(row)

	def get_merge_requests_for_plan(self, plan_id: str) -> list[MergeRequest]:
		"""Get all merge requests for work units in a plan."""
		rows = self.conn.execute(
			"""SELECT mr.* FROM merge_requests mr
			JOIN work_units wu ON mr.work_unit_id = wu.id
			WHERE wu.plan_id = ?
			ORDER BY mr.position ASC""",
			(plan_id,),
		).fetchall()
		return [self._row_to_merge_request(r) for r in rows]

	def get_next_merge_position(self) -> int:
		"""Get the next available merge position."""
		row = self.conn.execute(
			"SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM merge_requests"
		).fetchone()
		return int(row["next_pos"]) if row else 1

	@staticmethod
	def _row_to_merge_request(row: sqlite3.Row) -> MergeRequest:
		return MergeRequest(
			id=row["id"],
			work_unit_id=row["work_unit_id"],
			worker_id=row["worker_id"],
			branch_name=row["branch_name"],
			commit_hash=row["commit_hash"],
			status=row["status"],
			position=row["position"],
			created_at=row["created_at"],
			verified_at=row["verified_at"],
			merged_at=row["merged_at"],
			rejection_reason=row["rejection_reason"],
			rebase_attempts=row["rebase_attempts"],
		)
