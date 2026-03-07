"""Tests for mission checkpoint save/load/clear."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.checkpoint import (
	MissionCheckpoint,
	clear_checkpoint,
	load_checkpoint,
	save_checkpoint,
)


@pytest.fixture
def target(tmp_path: Path) -> Path:
	return tmp_path / "project"


def _sample_checkpoint() -> MissionCheckpoint:
	return MissionCheckpoint(
		mission_id="m-001",
		last_epoch_id="e-005",
		merged_files={"src/a.py", "src/b.py"},
		completed_unit_ids={"u-1", "u-2", "u-3"},
		total_cost_usd=1.42,
		total_dispatched=10,
		total_merged=7,
		total_failed=2,
		strategy="deliberative",
		timestamp="2026-03-03T00:00:00+00:00",
	)


class TestSaveLoadRoundTrip:
	def test_round_trip_preserves_all_fields(self, target: Path) -> None:
		original = _sample_checkpoint()
		save_checkpoint(original, target)
		loaded = load_checkpoint(target)

		assert loaded is not None
		assert loaded.mission_id == original.mission_id
		assert loaded.last_epoch_id == original.last_epoch_id
		assert loaded.merged_files == original.merged_files
		assert loaded.completed_unit_ids == original.completed_unit_ids
		assert loaded.total_cost_usd == original.total_cost_usd
		assert loaded.total_dispatched == original.total_dispatched
		assert loaded.total_merged == original.total_merged
		assert loaded.total_failed == original.total_failed
		assert loaded.strategy == original.strategy
		assert loaded.timestamp == original.timestamp

	def test_empty_sets_round_trip(self, target: Path) -> None:
		cp = MissionCheckpoint(mission_id="m-empty")
		save_checkpoint(cp, target)
		loaded = load_checkpoint(target)

		assert loaded is not None
		assert loaded.merged_files == set()
		assert loaded.completed_unit_ids == set()

	def test_overwrite_existing(self, target: Path) -> None:
		cp1 = MissionCheckpoint(mission_id="m-first", total_dispatched=1)
		save_checkpoint(cp1, target)

		cp2 = MissionCheckpoint(mission_id="m-second", total_dispatched=5)
		save_checkpoint(cp2, target)

		loaded = load_checkpoint(target)
		assert loaded is not None
		assert loaded.mission_id == "m-second"
		assert loaded.total_dispatched == 5


class TestAtomicWrite:
	def test_no_tmp_file_after_save(self, target: Path) -> None:
		save_checkpoint(_sample_checkpoint(), target)
		mc_dir = target / ".mc"
		tmp_files = list(mc_dir.glob("*.tmp"))
		assert tmp_files == []

	def test_checkpoint_file_is_valid_json(self, target: Path) -> None:
		save_checkpoint(_sample_checkpoint(), target)
		raw = (target / ".mc" / "checkpoint.json").read_text()
		data = json.loads(raw)
		assert data["mission_id"] == "m-001"

	def test_creates_mc_directory(self, target: Path) -> None:
		assert not (target / ".mc").exists()
		save_checkpoint(_sample_checkpoint(), target)
		assert (target / ".mc").is_dir()


class TestCorruptFile:
	def test_corrupt_json_returns_none(self, target: Path) -> None:
		mc_dir = target / ".mc"
		mc_dir.mkdir(parents=True)
		(mc_dir / "checkpoint.json").write_text("{not valid json!!!")
		assert load_checkpoint(target) is None

	def test_missing_fields_returns_none(self, target: Path) -> None:
		mc_dir = target / ".mc"
		mc_dir.mkdir(parents=True)
		(mc_dir / "checkpoint.json").write_text('{"mission_id": "x"}')
		assert load_checkpoint(target) is None

	def test_wrong_type_returns_none(self, target: Path) -> None:
		mc_dir = target / ".mc"
		mc_dir.mkdir(parents=True)
		# merged_files should be a list, not a string
		data = {
			"mission_id": "x",
			"last_epoch_id": "",
			"merged_files": "not-a-list",
			"completed_unit_ids": [],
			"total_cost_usd": 0,
			"total_dispatched": 0,
			"total_merged": 0,
			"total_failed": 0,
			"strategy": "",
			"timestamp": "",
		}
		(mc_dir / "checkpoint.json").write_text(json.dumps(data))
		assert load_checkpoint(target) is None


class TestMissingFile:
	def test_no_mc_dir_returns_none(self, target: Path) -> None:
		assert load_checkpoint(target) is None

	def test_mc_dir_but_no_file_returns_none(self, target: Path) -> None:
		(target / ".mc").mkdir(parents=True)
		assert load_checkpoint(target) is None


class TestClearCheckpoint:
	def test_clear_removes_file(self, target: Path) -> None:
		save_checkpoint(_sample_checkpoint(), target)
		assert (target / ".mc" / "checkpoint.json").exists()

		clear_checkpoint(target)
		assert not (target / ".mc" / "checkpoint.json").exists()

	def test_clear_missing_is_noop(self, target: Path) -> None:
		clear_checkpoint(target)  # should not raise

	def test_load_after_clear_returns_none(self, target: Path) -> None:
		save_checkpoint(_sample_checkpoint(), target)
		clear_checkpoint(target)
		assert load_checkpoint(target) is None
