"""In-memory file lock registry for preventing concurrent writes to overlapping paths."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)


class FileLockRegistry:
	"""Tracks which work unit has claimed which file paths.

	Prevents two concurrent units from modifying overlapping files.
	Directory claims (paths ending with '/') conflict with any file under that directory.
	"""

	def __init__(self) -> None:
		self._claims: dict[str, set[str]] = {}  # unit_id -> set of claimed paths

	def claim(self, unit_id: str, paths: list[str]) -> list[str]:
		"""Claim paths for a unit. Returns list of conflicting paths (empty = success)."""
		if not paths:
			return []
		conflicts = self.get_conflicts(paths)
		# Filter out conflicts with ourselves (re-claim on retry)
		conflicts = {p: uid for p, uid in conflicts.items() if uid != unit_id}
		if conflicts:
			return list(conflicts.keys())
		self._claims[unit_id] = set(paths)
		return []

	def release(self, unit_id: str) -> None:
		"""Release all claims for a unit."""
		self._claims.pop(unit_id, None)

	def get_conflicts(self, paths: list[str]) -> dict[str, str]:
		"""Return {path: claiming_unit_id} for any overlapping paths."""
		result: dict[str, str] = {}
		for unit_id, claimed in self._claims.items():
			for requested in paths:
				for held in claimed:
					if _paths_overlap(requested, held):
						result[requested] = unit_id
						break
		return result

	@property
	def active_claims(self) -> dict[str, set[str]]:
		"""Return a copy of the current claims for inspection."""
		return {uid: set(paths) for uid, paths in self._claims.items()}


def _paths_overlap(a: str, b: str) -> bool:
	"""Check if two path specs overlap.

	Rules:
	  - Exact match always overlaps.
	  - A directory claim (ending with '/') overlaps with any path under it.
	  - A file claim overlaps with a directory claim that contains it.
	"""
	if a == b:
		return True

	a_norm = a.rstrip("/")
	b_norm = b.rstrip("/")

	if a_norm == b_norm:
		return True

	a_is_dir = a.endswith("/")
	b_is_dir = b.endswith("/")

	if a_is_dir and _is_under(b_norm, a_norm):
		return True
	if b_is_dir and _is_under(a_norm, b_norm):
		return True

	return False


def _is_under(child: str, parent: str) -> bool:
	"""Check if child path is under parent directory."""
	# Normalize to avoid issues with trailing slashes
	child_parts = PurePosixPath(child).parts
	parent_parts = PurePosixPath(parent).parts
	if len(child_parts) <= len(parent_parts):
		return False
	return child_parts[:len(parent_parts)] == parent_parts
