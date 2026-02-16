"""Tests for MISSION_STATE.md commit in green branch merges."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from mission_control.config import (
	GreenBranchConfig,
	MissionConfig,
	TargetConfig,
	VerificationConfig,
)
from mission_control.db import Database
from mission_control.green_branch import GreenBranchManager


def _config(target_path: str = "/tmp/test") -> MissionConfig:
	mc = MissionConfig()
	mc.target = TargetConfig(
		name="test",
		path=target_path,
		branch="main",
		verification=VerificationConfig(command="pytest -q"),
	)
	mc.green_branch = GreenBranchConfig(
		working_branch="mc/working",
		green_branch="mc/green",
	)
	return mc


def _manager(target_path: str = "/tmp/test", workspace: str = "/tmp/test-workspace") -> GreenBranchManager:
	config = _config(target_path)
	db = Database(":memory:")
	mgr = GreenBranchManager(config, db)
	mgr.workspace = workspace
	return mgr


class TestCommitStateFile:
	"""Tests for commit_state_file() method."""

	async def test_writes_and_commits_file(self, tmp_path: Path) -> None:
		"""commit_state_file writes content to workspace and commits it."""
		workspace = tmp_path / "workspace"
		workspace.mkdir()
		mgr = _manager(workspace=str(workspace))

		git_calls: list[tuple[str, ...]] = []

		async def mock_run_git(*args: str) -> tuple[bool, str]:
			git_calls.append(args)
			return (True, "")

		mgr._run_git = AsyncMock(side_effect=mock_run_git)

		result = await mgr.commit_state_file("# State\nObjective: test\n")

		assert result is True
		# File was written to workspace
		state_file = workspace / "MISSION_STATE.md"
		assert state_file.exists()
		assert state_file.read_text() == "# State\nObjective: test\n"
		# Git add and commit were called
		assert ("add", "MISSION_STATE.md") in git_calls
		assert ("commit", "-m", "Update MISSION_STATE.md") in git_calls

	async def test_returns_true_on_nothing_to_commit(self, tmp_path: Path) -> None:
		"""commit_state_file returns True when git says nothing to commit."""
		workspace = tmp_path / "workspace"
		workspace.mkdir()
		mgr = _manager(workspace=str(workspace))

		async def mock_run_git(*args: str) -> tuple[bool, str]:
			if args[0] == "commit":
				return (False, "nothing to commit, working tree clean")
			return (True, "")

		mgr._run_git = AsyncMock(side_effect=mock_run_git)

		result = await mgr.commit_state_file("# State\n")

		assert result is True

	async def test_returns_false_on_commit_failure(self, tmp_path: Path) -> None:
		"""commit_state_file returns False when git commit fails for other reasons."""
		workspace = tmp_path / "workspace"
		workspace.mkdir()
		mgr = _manager(workspace=str(workspace))

		async def mock_run_git(*args: str) -> tuple[bool, str]:
			if args[0] == "commit":
				return (False, "error: some git error")
			return (True, "")

		mgr._run_git = AsyncMock(side_effect=mock_run_git)

		result = await mgr.commit_state_file("# State\n")

		assert result is False


class TestMergeUnitCommitsState:
	"""Tests for merge_unit() calling commit_state_file when MISSION_STATE.md exists."""

	async def test_merge_commits_state_file_when_exists(self, tmp_path: Path) -> None:
		"""merge_unit calls commit_state_file when MISSION_STATE.md exists in target repo."""
		target_dir = tmp_path / "target"
		target_dir.mkdir()
		state_file = target_dir / "MISSION_STATE.md"
		state_file.write_text("# Mission State\nObjective: build stuff\n")

		workspace = tmp_path / "workspace"
		workspace.mkdir()

		mgr = _manager(target_path=str(target_dir), workspace=str(workspace))
		mgr._run_git = AsyncMock(return_value=(True, ""))
		mgr._sync_to_source = AsyncMock()  # type: ignore[method-assign]
		mgr.commit_state_file = AsyncMock(return_value=True)  # type: ignore[method-assign]

		result = await mgr.merge_unit("/tmp/worker", "feat/branch")

		assert result.merged is True
		mgr.commit_state_file.assert_awaited_once_with(
			"# Mission State\nObjective: build stuff\n",
		)

	async def test_merge_skips_state_when_not_exists(self, tmp_path: Path) -> None:
		"""merge_unit skips commit_state_file when MISSION_STATE.md doesn't exist."""
		target_dir = tmp_path / "target"
		target_dir.mkdir()
		# No MISSION_STATE.md created

		workspace = tmp_path / "workspace"
		workspace.mkdir()

		mgr = _manager(target_path=str(target_dir), workspace=str(workspace))
		mgr._run_git = AsyncMock(return_value=(True, ""))
		mgr._sync_to_source = AsyncMock()  # type: ignore[method-assign]
		mgr.commit_state_file = AsyncMock(return_value=True)  # type: ignore[method-assign]

		result = await mgr.merge_unit("/tmp/worker", "feat/branch")

		assert result.merged is True
		mgr.commit_state_file.assert_not_awaited()

	async def test_merge_succeeds_even_if_state_commit_fails(self, tmp_path: Path) -> None:
		"""merge_unit still returns merged=True if commit_state_file raises."""
		target_dir = tmp_path / "target"
		target_dir.mkdir()
		(target_dir / "MISSION_STATE.md").write_text("# State\n")

		workspace = tmp_path / "workspace"
		workspace.mkdir()

		mgr = _manager(target_path=str(target_dir), workspace=str(workspace))
		mgr._run_git = AsyncMock(return_value=(True, ""))
		mgr._sync_to_source = AsyncMock()  # type: ignore[method-assign]
		mgr.commit_state_file = AsyncMock(side_effect=RuntimeError("git exploded"))  # type: ignore[method-assign]

		result = await mgr.merge_unit("/tmp/worker", "feat/branch")

		assert result.merged is True

	async def test_state_not_committed_on_merge_conflict(self, tmp_path: Path) -> None:
		"""commit_state_file is NOT called when the merge itself fails."""
		target_dir = tmp_path / "target"
		target_dir.mkdir()
		(target_dir / "MISSION_STATE.md").write_text("# State\n")

		workspace = tmp_path / "workspace"
		workspace.mkdir()

		mgr = _manager(target_path=str(target_dir), workspace=str(workspace))
		mgr.commit_state_file = AsyncMock(return_value=True)  # type: ignore[method-assign]

		async def side_effect(*args: str) -> tuple[bool, str]:
			if args[0] == "merge" and args[1] == "--no-ff":
				return (False, "CONFLICT")
			return (True, "")

		mgr._run_git = AsyncMock(side_effect=side_effect)

		result = await mgr.merge_unit("/tmp/worker", "feat/conflict")

		assert result.merged is False
		mgr.commit_state_file.assert_not_awaited()
