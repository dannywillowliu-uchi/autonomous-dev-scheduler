"""Tests for workspace pool -- shared git clone management."""

from __future__ import annotations

from pathlib import Path

import pytest

from mission_control.workspace import WorkspacePool


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
	"""Create a real git repo to use as the clone source."""
	import subprocess

	repo = tmp_path / "source"
	repo.mkdir()
	subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
	subprocess.run(["git", "checkout", "-b", "main"], cwd=str(repo), check=True, capture_output=True)

	# Create an initial commit so the branch exists
	readme = repo / "README.md"
	readme.write_text("# Test repo\n")
	subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
	subprocess.run(
		["git", "commit", "-m", "Initial commit"],
		cwd=str(repo), check=True, capture_output=True,
		env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
			"GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com",
			"PATH": subprocess.check_output(["bash", "-c", "echo $PATH"]).decode().strip()},
	)
	return repo


@pytest.fixture()
def pool_dir(tmp_path: Path) -> Path:
	"""Directory for workspace clones."""
	return tmp_path / "pool"


class TestWorkspacePool:
	async def test_create_clone_has_git_dir(
		self, source_repo: Path, pool_dir: Path,
	) -> None:
		"""Create a clone and verify .git exists."""
		pool = WorkspacePool(source_repo, pool_dir, max_clones=3)
		await pool.initialize()

		workspace = await pool.acquire()
		assert workspace is not None
		assert (workspace / ".git").exists()

		await pool.cleanup()

	async def test_acquire_release_lifecycle(
		self, source_repo: Path, pool_dir: Path,
	) -> None:
		"""Acquire returns a path, release makes it available again."""
		pool = WorkspacePool(source_repo, pool_dir, max_clones=3)
		await pool.initialize()

		workspace = await pool.acquire()
		assert workspace is not None
		assert pool.total_clones == 1
		assert len(pool._in_use) == 1
		assert len(pool._available) == 0

		await pool.release(workspace)
		assert len(pool._in_use) == 0
		assert len(pool._available) == 1

		# Acquiring again should return the same clone (from stack)
		workspace2 = await pool.acquire()
		assert workspace2 == workspace

		await pool.cleanup()

	async def test_reset_clone_removes_dirty_files(
		self, source_repo: Path, pool_dir: Path,
	) -> None:
		"""Release resets the clone to clean state, removing untracked files."""
		pool = WorkspacePool(source_repo, pool_dir, max_clones=3)
		await pool.initialize()

		workspace = await pool.acquire()
		assert workspace is not None

		# Create a dirty file
		dirty_file = workspace / "dirty.txt"
		dirty_file.write_text("this should be cleaned up")
		assert dirty_file.exists()

		# Release should reset the clone
		await pool.release(workspace)

		# The dirty file should be gone after reset
		assert not dirty_file.exists()

		await pool.cleanup()

	async def test_max_clones_enforced(
		self, source_repo: Path, pool_dir: Path,
	) -> None:
		"""Acquire returns None when max_clones limit is reached."""
		pool = WorkspacePool(source_repo, pool_dir, max_clones=2)
		await pool.initialize()

		w1 = await pool.acquire()
		w2 = await pool.acquire()
		assert w1 is not None
		assert w2 is not None
		assert pool.total_clones == 2

		# Third acquire should fail
		w3 = await pool.acquire()
		assert w3 is None

		await pool.cleanup()

	async def test_cleanup_removes_all_clones(
		self, source_repo: Path, pool_dir: Path,
	) -> None:
		"""Cleanup removes all clones and the pool directory."""
		pool = WorkspacePool(source_repo, pool_dir, max_clones=3)
		await pool.initialize()

		w1 = await pool.acquire()
		w2 = await pool.acquire()
		assert w1 is not None
		assert w2 is not None

		# Release one so we have both available and in-use
		await pool.release(w1)

		await pool.cleanup()
		assert pool.total_clones == 0
		assert not pool_dir.exists()

	async def test_initialize_with_warm_clones(
		self, source_repo: Path, pool_dir: Path,
	) -> None:
		"""Initialize with warm_count pre-creates clones."""
		pool = WorkspacePool(source_repo, pool_dir, max_clones=5)
		await pool.initialize(warm_count=3)

		assert pool.total_clones == 3
		assert len(pool._available) == 3
		assert len(pool._in_use) == 0

		# All pre-warmed clones should have .git
		for clone in pool._available:
			assert (clone / ".git").exists()

		await pool.cleanup()
