"""Tests for FileLockRegistry."""

from __future__ import annotations

from mission_control.file_lock_registry import FileLockRegistry, _paths_overlap


class TestPathsOverlap:
	def test_exact_match(self) -> None:
		assert _paths_overlap("src/foo.py", "src/foo.py")

	def test_different_files(self) -> None:
		assert not _paths_overlap("src/foo.py", "src/bar.py")

	def test_dir_overlaps_file_under_it(self) -> None:
		assert _paths_overlap("src/", "src/foo.py")

	def test_file_overlaps_parent_dir(self) -> None:
		assert _paths_overlap("src/foo.py", "src/")

	def test_nested_dir_overlap(self) -> None:
		assert _paths_overlap("src/", "src/mission_control/models.py")

	def test_dir_does_not_overlap_sibling(self) -> None:
		assert not _paths_overlap("src/", "tests/foo.py")

	def test_same_prefix_not_dir(self) -> None:
		# "src/foo" is not a directory claim, so "src/foobar.py" should not conflict
		assert not _paths_overlap("src/foo", "src/foobar.py")

	def test_trailing_slash_normalization(self) -> None:
		assert _paths_overlap("src/", "src/")

	def test_deep_nested(self) -> None:
		assert _paths_overlap("src/mission_control/", "src/mission_control/db.py")

	def test_no_overlap_different_trees(self) -> None:
		assert not _paths_overlap("src/mission_control/", "tests/test_db.py")


class TestFileLockRegistry:
	def test_claim_no_conflict(self) -> None:
		reg = FileLockRegistry()
		conflicts = reg.claim("u1", ["src/foo.py"])
		assert conflicts == []
		assert "u1" in reg.active_claims

	def test_claim_returns_conflicts(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/foo.py"])
		conflicts = reg.claim("u2", ["src/foo.py"])
		assert "src/foo.py" in conflicts

	def test_release_frees_paths(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/foo.py"])
		reg.release("u1")
		conflicts = reg.claim("u2", ["src/foo.py"])
		assert conflicts == []

	def test_release_nonexistent_unit(self) -> None:
		reg = FileLockRegistry()
		reg.release("nonexistent")  # should not raise

	def test_directory_conflict(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/mission_control/"])
		conflicts = reg.claim("u2", ["src/mission_control/db.py"])
		assert "src/mission_control/db.py" in conflicts

	def test_file_conflicts_with_dir_claim(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/mission_control/db.py"])
		conflicts = reg.claim("u2", ["src/mission_control/"])
		assert "src/mission_control/" in conflicts

	def test_no_conflict_disjoint_files(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/foo.py"])
		conflicts = reg.claim("u2", ["src/bar.py"])
		assert conflicts == []

	def test_get_conflicts(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/foo.py", "src/bar.py"])
		result = reg.get_conflicts(["src/foo.py", "src/baz.py"])
		assert result == {"src/foo.py": "u1"}

	def test_empty_paths_no_conflict(self) -> None:
		reg = FileLockRegistry()
		conflicts = reg.claim("u1", [])
		assert conflicts == []

	def test_reclaim_same_unit(self) -> None:
		"""Re-claiming for the same unit_id should succeed (retry scenario)."""
		reg = FileLockRegistry()
		reg.claim("u1", ["src/foo.py"])
		conflicts = reg.claim("u1", ["src/foo.py"])
		assert conflicts == []

	def test_multiple_units_no_overlap(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/a.py"])
		reg.claim("u2", ["src/b.py"])
		reg.claim("u3", ["src/c.py"])
		assert len(reg.active_claims) == 3

	def test_multi_path_partial_conflict(self) -> None:
		reg = FileLockRegistry()
		reg.claim("u1", ["src/foo.py"])
		conflicts = reg.claim("u2", ["src/foo.py", "src/bar.py"])
		assert "src/foo.py" in conflicts
		# u2 should NOT be registered since there was a conflict
		assert "u2" not in reg.active_claims
