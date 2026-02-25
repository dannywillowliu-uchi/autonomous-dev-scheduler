"""Continuous planner -- flat impact-focused planning (no backlog, no recursion)."""

from __future__ import annotations

import logging

from mission_control.config import MissionConfig
from mission_control.db import Database
from mission_control.models import Epoch, Mission, Plan, WorkUnit
from mission_control.overlap import resolve_file_overlaps
from mission_control.recursive_planner import RecursivePlanner

log = logging.getLogger(__name__)


class ContinuousPlanner:
	"""Flat impact-focused planner: invokes LLM every iteration with full state."""

	def __init__(self, config: MissionConfig, db: Database) -> None:
		self._inner = RecursivePlanner(config, db)
		self._config = config
		self._db = db
		self._epoch_count: int = 0

	def set_causal_context(self, risks: str) -> None:
		"""Set causal risk factors, delegating to the inner planner."""
		self._inner.set_causal_context(risks)

	def set_project_snapshot(self, snapshot: str) -> None:
		"""Set project structure snapshot, delegating to the inner planner."""
		self._inner.set_project_snapshot(snapshot)

	async def get_next_units(
		self,
		mission: Mission,
		max_units: int = 3,
		feedback_context: str = "",
		knowledge_context: str = "",
		**kwargs,
	) -> tuple[Plan, list[WorkUnit], Epoch]:
		"""Plan the next batch of units using the flat impact prompt."""
		self._epoch_count += 1
		from mission_control.snapshot import clear_snapshot_cache
		clear_snapshot_cache()

		epoch = Epoch(
			mission_id=mission.id,
			number=self._epoch_count,
		)

		# Build structured state from DB
		structured_state = self._build_structured_state(mission)

		# Build the enriched context
		enriched_context = feedback_context
		if knowledge_context:
			enriched_context = (
				(enriched_context + "\n\n## Accumulated Knowledge\n" + knowledge_context)
				if enriched_context
				else ("## Accumulated Knowledge\n" + knowledge_context)
			)
		if structured_state:
			enriched_context = (
				(enriched_context + "\n\n" + structured_state)
				if enriched_context
				else structured_state
			)

		plan, root_node = await self._inner.plan_round(
			objective=mission.objective,
			snapshot_hash="",
			prior_discoveries=[],
			round_number=self._epoch_count,
			feedback_context=enriched_context,
		)

		# Extract work units from the plan tree
		units = self._extract_units_from_tree(root_node)

		# Resolve file overlaps
		units = resolve_file_overlaps(units)

		plan.status = "active"
		plan.total_units = len(units)
		epoch.units_planned = len(units)

		# Limit to max_units
		units = units[:max_units]

		log.info(
			"Planned epoch %d: %d units",
			self._epoch_count, len(units),
		)

		return plan, units, epoch

	def _build_structured_state(self, mission: Mission) -> str:
		"""Build a structured checklist of completed/failed work from the DB."""
		try:
			all_units = self._db.get_work_units_for_mission(mission.id)
		except Exception:
			return ""

		if not all_units:
			return ""

		lines = ["## What's Been Done"]

		completed = [u for u in all_units if u.status == "completed"]
		failed = [u for u in all_units if u.status == "failed"]

		if completed:
			for u in completed:
				files_part = f" (files: {u.files_hint})" if u.files_hint else ""
				lines.append(f"- [x] {u.title}{files_part}")

		if failed:
			for u in failed:
				files_part = f" (files: {u.files_hint})" if u.files_hint else ""
				lines.append(f"- [FAILED] {u.title}{files_part}")

		if not completed and not failed:
			return ""

		return "\n".join(lines)

	def _extract_units_from_tree(self, node: object) -> list[WorkUnit]:
		"""Extract WorkUnit objects from the in-memory plan tree."""
		units: list[WorkUnit] = []

		if hasattr(node, "_forced_unit"):
			units.append(node._forced_unit)  # type: ignore[union-attr]

		if hasattr(node, "_child_leaves"):
			for _leaf, wu in node._child_leaves:  # type: ignore[union-attr]
				units.append(wu)

		if hasattr(node, "_subdivided_children"):
			for child in node._subdivided_children:  # type: ignore[union-attr]
				units.extend(self._extract_units_from_tree(child))

		return units
