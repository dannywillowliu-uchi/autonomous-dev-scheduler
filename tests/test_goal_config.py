"""Tests for GoalConfig dataclass and TOML parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodev.config import GoalConfig, load_config


@pytest.fixture()
def _target_dir(tmp_path: Path) -> Path:
	"""Create a fake target directory with .git so validation doesn't complain."""
	target = tmp_path / "project"
	target.mkdir()
	(target / ".git").mkdir()
	return target


def _write_config(tmp_path: Path, target_path: Path, goal_toml: str = "") -> Path:
	toml = tmp_path / "autodev.toml"
	content = f"""\
[target]
name = "test"
path = "{target_path}"
"""
	if goal_toml:
		content += f"\n{goal_toml}\n"
	toml.write_text(content)
	return toml


def test_goal_config_defaults() -> None:
	gc = GoalConfig()
	assert gc.enabled is False
	assert gc.goal_file == "GOAL.md"
	assert gc.auto_detect is True
	assert gc.fitness_timeout == 60
	assert gc.revert_on_regression is True
	assert gc.min_improvement == 0.01
	assert gc.target_score == 1.0
	assert gc.max_iterations == 0
	assert gc.log_file == ".goal-iterations.jsonl"


def test_goal_config_toml_parsing(tmp_path: Path, _target_dir: Path) -> None:
	toml = _write_config(tmp_path, _target_dir, """\
[goal]
enabled = true
goal_file = "MY_GOAL.md"
auto_detect = false
fitness_timeout = 120
revert_on_regression = false
min_improvement = 0.05
target_score = 0.95
max_iterations = 10
log_file = "custom-log.jsonl"
""")
	cfg = load_config(toml)
	assert cfg.goal.enabled is True
	assert cfg.goal.goal_file == "MY_GOAL.md"
	assert cfg.goal.auto_detect is False
	assert cfg.goal.fitness_timeout == 120
	assert cfg.goal.revert_on_regression is False
	assert cfg.goal.min_improvement == 0.05
	assert cfg.goal.target_score == 0.95
	assert cfg.goal.max_iterations == 10
	assert cfg.goal.log_file == "custom-log.jsonl"


def test_goal_auto_detect_enabled(tmp_path: Path, _target_dir: Path) -> None:
	"""When GOAL.md exists in target and auto_detect is True, enabled becomes True."""
	(_target_dir / "GOAL.md").write_text("# Goal\nPass all tests.")
	toml = _write_config(tmp_path, _target_dir)
	cfg = load_config(toml)
	assert cfg.goal.auto_detect is True
	assert cfg.goal.enabled is True


def test_goal_auto_detect_disabled(tmp_path: Path, _target_dir: Path) -> None:
	"""When auto_detect is False, don't auto-enable even if GOAL.md exists."""
	(_target_dir / "GOAL.md").write_text("# Goal\nPass all tests.")
	toml = _write_config(tmp_path, _target_dir, """\
[goal]
auto_detect = false
""")
	cfg = load_config(toml)
	assert cfg.goal.auto_detect is False
	assert cfg.goal.enabled is False


def test_goal_custom_file(tmp_path: Path, _target_dir: Path) -> None:
	"""goal_file='custom-goal.md' works with auto-detect."""
	(_target_dir / "custom-goal.md").write_text("# Custom Goal")
	toml = _write_config(tmp_path, _target_dir, """\
[goal]
goal_file = "custom-goal.md"
""")
	cfg = load_config(toml)
	assert cfg.goal.goal_file == "custom-goal.md"
	assert cfg.goal.enabled is True
