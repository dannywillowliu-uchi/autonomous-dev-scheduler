"""Data models for mission-control state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def _now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
	return uuid4().hex[:12]


@dataclass
class Session:
	"""A single Claude Code session run."""

	id: str = field(default_factory=_new_id)
	target_name: str = ""
	task_description: str = ""
	status: str = "pending"  # pending/running/completed/failed/reverted
	branch_name: str = ""
	started_at: str = field(default_factory=_now_iso)
	finished_at: str | None = None
	exit_code: int | None = None
	commit_hash: str | None = None
	cost_usd: float | None = None
	output_summary: str = ""


@dataclass
class Snapshot:
	"""Project health snapshot at a point in time."""

	id: str = field(default_factory=_new_id)
	session_id: str | None = None
	taken_at: str = field(default_factory=_now_iso)
	test_total: int = 0
	test_passed: int = 0
	test_failed: int = 0
	lint_errors: int = 0
	type_errors: int = 0
	security_findings: int = 0
	raw_output: str = ""


@dataclass
class TaskRecord:
	"""A discovered work item."""

	id: str = field(default_factory=_new_id)
	source: str = ""  # test_failure/lint/todo/coverage/objective
	description: str = ""
	priority: int = 7
	status: str = "discovered"  # discovered/assigned/completed/skipped
	session_id: str | None = None
	created_at: str = field(default_factory=_now_iso)
	resolved_at: str | None = None


@dataclass
class Decision:
	"""A decision logged during a session."""

	id: str = field(default_factory=_new_id)
	session_id: str = ""
	decision: str = ""
	rationale: str = ""
	timestamp: str = field(default_factory=_now_iso)


@dataclass
class SnapshotDelta:
	"""Difference between two snapshots."""

	tests_added: int = 0
	tests_fixed: int = 0
	tests_broken: int = 0
	lint_delta: int = 0
	type_delta: int = 0
	security_delta: int = 0

	@property
	def improved(self) -> bool:
		return (
			(self.tests_fixed > 0 or self.lint_delta < 0 or self.type_delta < 0 or self.security_delta < 0)
			and self.tests_broken == 0
			and self.security_delta <= 0
		)

	@property
	def regressed(self) -> bool:
		return self.tests_broken > 0 or self.security_delta > 0


# -- Parallel mode models --


@dataclass
class Plan:
	"""A decomposed objective for parallel execution."""

	id: str = field(default_factory=_new_id)
	objective: str = ""
	status: str = "pending"  # pending/active/completed/failed
	created_at: str = field(default_factory=_now_iso)
	finished_at: str | None = None
	raw_planner_output: str = ""
	total_units: int = 0
	completed_units: int = 0
	failed_units: int = 0


@dataclass
class WorkUnit:
	"""A single work item within a Plan, claimable by a worker."""

	id: str = field(default_factory=_new_id)
	plan_id: str = ""
	title: str = ""
	description: str = ""
	files_hint: str = ""  # comma-separated paths this unit likely touches
	verification_hint: str = ""  # specific verification focus
	priority: int = 1  # 1=highest
	status: str = "pending"  # pending/claimed/running/completed/failed/blocked
	worker_id: str | None = None
	depends_on: str = ""  # comma-separated WorkUnit IDs
	branch_name: str = ""
	claimed_at: str | None = None
	heartbeat_at: str | None = None
	started_at: str | None = None
	finished_at: str | None = None
	exit_code: int | None = None
	commit_hash: str | None = None
	output_summary: str = ""
	attempt: int = 0
	max_attempts: int = 3


@dataclass
class Worker:
	"""A parallel worker agent and its workspace."""

	id: str = field(default_factory=_new_id)
	workspace_path: str = ""
	status: str = "idle"  # idle/working/dead
	current_unit_id: str | None = None
	pid: int | None = None
	started_at: str = field(default_factory=_now_iso)
	last_heartbeat: str = field(default_factory=_now_iso)
	units_completed: int = 0
	units_failed: int = 0
	total_cost_usd: float = 0.0


@dataclass
class MergeRequest:
	"""A request to merge a completed work unit into the base branch."""

	id: str = field(default_factory=_new_id)
	work_unit_id: str = ""
	worker_id: str = ""
	branch_name: str = ""
	commit_hash: str = ""
	status: str = "pending"  # pending/verifying/merged/rejected/conflict
	position: int = 0
	created_at: str = field(default_factory=_now_iso)
	verified_at: str | None = None
	merged_at: str | None = None
	rejection_reason: str = ""
	rebase_attempts: int = 0
