"""Mission checkpoint persistence for pause/resume support."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


@dataclass
class MissionCheckpoint:
	"""Snapshot of mission progress for resume after interruption."""

	mission_id: str = ""
	last_epoch_id: str = ""
	merged_files: set[str] = field(default_factory=set)
	completed_unit_ids: set[str] = field(default_factory=set)
	total_cost_usd: float = 0.0
	total_dispatched: int = 0
	total_merged: int = 0
	total_failed: int = 0
	strategy: str = ""
	timestamp: str = field(default_factory=_now_iso)


def _checkpoint_path(target_path: Path) -> Path:
	return target_path / ".mc" / "checkpoint.json"


def save_checkpoint(checkpoint: MissionCheckpoint, target_path: Path) -> Path:
	"""Write checkpoint atomically (write .tmp then rename).

	Returns the path to the written checkpoint file.
	"""
	dest = _checkpoint_path(target_path)
	dest.parent.mkdir(parents=True, exist_ok=True)
	tmp = dest.with_suffix(".json.tmp")

	data = asdict(checkpoint)
	# Convert sets to sorted lists for JSON serialisation
	data["merged_files"] = sorted(data["merged_files"])
	data["completed_unit_ids"] = sorted(data["completed_unit_ids"])

	try:
		tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
		os.replace(str(tmp), str(dest))
	except BaseException:
		# Clean up temp file on any failure
		tmp.unlink(missing_ok=True)
		raise
	return dest


def load_checkpoint(target_path: Path) -> MissionCheckpoint | None:
	"""Read and validate checkpoint, returning None if missing or corrupt."""
	dest = _checkpoint_path(target_path)
	if not dest.exists():
		return None
	try:
		data = json.loads(dest.read_text(encoding="utf-8"))
		if not isinstance(data.get("merged_files"), list):
			return None
		if not isinstance(data.get("completed_unit_ids"), list):
			return None
		data["merged_files"] = set(data["merged_files"])
		data["completed_unit_ids"] = set(data["completed_unit_ids"])
		return MissionCheckpoint(**data)
	except (json.JSONDecodeError, KeyError, TypeError, ValueError):
		return None


def clear_checkpoint(target_path: Path) -> None:
	"""Delete the checkpoint file if it exists."""
	dest = _checkpoint_path(target_path)
	dest.unlink(missing_ok=True)
