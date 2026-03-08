"""Tests for swarm learnings -- persistent memory across runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodev.swarm.learnings import HEADER, LEARNINGS_FILE, SwarmLearnings


@pytest.fixture
def learnings(tmp_path: Path) -> SwarmLearnings:
	"""Create a SwarmLearnings instance in a temp directory."""
	return SwarmLearnings(tmp_path)


@pytest.fixture
def learnings_path(tmp_path: Path) -> Path:
	return tmp_path / LEARNINGS_FILE


def test_add_discovery(learnings: SwarmLearnings, learnings_path: Path) -> None:
	result = learnings.add_discovery("test-agent", "Found that X causes Y")
	assert result is True
	content = learnings_path.read_text()
	assert "## Discovery" in content
	assert "**Source:** test-agent" in content
	assert "Found that X causes Y" in content


def test_add_successful_approach(learnings: SwarmLearnings, learnings_path: Path) -> None:
	result = learnings.add_successful_approach("Fix login bug", "Using retry logic with backoff solved the flaky test")
	assert result is True
	content = learnings_path.read_text()
	assert "## What Worked" in content
	assert "**Task:** Fix login bug" in content
	assert "Using retry logic with backoff solved the flaky test" in content


def test_add_failed_approach(learnings: SwarmLearnings, learnings_path: Path) -> None:
	result = learnings.add_failed_approach("Refactor DB layer", "Connection pool exhausted under load", attempt=3)
	assert result is True
	content = learnings_path.read_text()
	assert "## What Failed" in content
	assert "**Task:** Refactor DB layer (attempt 3)" in content
	assert "Connection pool exhausted under load" in content


def test_add_stagnation_insight(learnings: SwarmLearnings, learnings_path: Path) -> None:
	result = learnings.add_stagnation_insight("flat_tests", "Switched from unit to integration testing approach")
	assert result is True
	content = learnings_path.read_text()
	assert "## Stagnation Pivot" in content
	assert "**Metric:** flat_tests" in content
	assert "Switched from unit to integration testing approach" in content


def test_deduplication(learnings: SwarmLearnings, learnings_path: Path) -> None:
	learnings.add_discovery("agent-1", "Database indexes are missing on user_id column")
	first_content = learnings_path.read_text()
	result = learnings.add_discovery("agent-2", "Database indexes are missing on user_id column")
	assert result is False
	assert learnings_path.read_text() == first_content


def test_fuzzy_dedup(learnings: SwarmLearnings, learnings_path: Path) -> None:
	"""Similar entries where first 80 chars match should be deduplicated."""
	long_prefix = "A" * 80
	learnings.add_discovery("agent-1", f"{long_prefix} -- ending one")
	first_content = learnings_path.read_text()
	result = learnings.add_discovery("agent-2", f"{long_prefix} -- ending two")
	assert result is False
	assert learnings_path.read_text() == first_content


def test_different_entries_not_deduped(learnings: SwarmLearnings, learnings_path: Path) -> None:
	learnings.add_discovery("agent-1", "First unique discovery about caching")
	learnings.add_discovery("agent-2", "Second unique discovery about logging")
	content = learnings_path.read_text()
	assert "First unique discovery about caching" in content
	assert "Second unique discovery about logging" in content


def test_line_count_bounding(tmp_path: Path) -> None:
	"""File should be trimmed to ~200 lines when it grows too large."""
	sl = SwarmLearnings(tmp_path)
	# Add many entries to exceed 200 lines
	for i in range(100):
		sl.add_discovery(f"agent-{i}", f"Unique discovery number {i}: " + "x" * 60)
	content = (tmp_path / LEARNINGS_FILE).read_text()
	line_count = len(content.split("\n"))
	# Should be trimmed: header lines + ~150 entry lines
	assert line_count <= 210, f"File has {line_count} lines, expected <= 210"


def test_get_for_planner(learnings: SwarmLearnings) -> None:
	learnings.add_discovery("agent-1", "Important insight A")
	learnings.add_successful_approach("Task B", "Approach B worked well")
	result = learnings.get_for_planner()
	assert "Accumulated Learnings" in result
	assert "Important insight A" in result
	assert "Approach B worked well" in result


def test_get_for_planner_respects_max_lines(tmp_path: Path) -> None:
	sl = SwarmLearnings(tmp_path)
	for i in range(30):
		sl.add_discovery(f"agent-{i}", f"Discovery {i}")
	result = sl.get_for_planner(max_lines=10)
	lines = result.strip().split("\n")
	# Header line + blank + 10 content lines
	assert len(lines) <= 12


def test_empty_file(tmp_path: Path) -> None:
	"""get_for_planner on a fresh file returns empty string."""
	sl = SwarmLearnings(tmp_path)
	assert sl.get_for_planner() == ""


def test_corrupted_file(tmp_path: Path) -> None:
	"""Handles file with no valid sections gracefully."""
	path = tmp_path / LEARNINGS_FILE
	path.write_text("some garbage\nno headers\nrandom stuff\n")
	sl = SwarmLearnings(tmp_path)
	# Should not crash, get_for_planner returns empty for no entries
	assert sl.get_for_planner() == ""
	# Should still be able to add entries
	result = sl.add_discovery("agent", "New finding after corruption")
	assert result is True
	assert "New finding after corruption" in path.read_text()


def test_timestamps(learnings: SwarmLearnings, learnings_path: Path) -> None:
	"""Entries should have ISO-format timestamps."""
	learnings.add_discovery("agent", "Timestamp test entry")
	content = learnings_path.read_text()
	# Timestamps are in YYYY-MM-DD HH:MM format
	import re
	assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", content)


def test_file_created_on_init(tmp_path: Path) -> None:
	"""Learnings file should be created on initialization."""
	SwarmLearnings(tmp_path)
	path = tmp_path / LEARNINGS_FILE
	assert path.exists()
	assert path.read_text() == HEADER


def test_strips_whitespace_in_entries(learnings: SwarmLearnings, learnings_path: Path) -> None:
	"""Entry text should be stripped of leading/trailing whitespace."""
	learnings.add_discovery("agent", "  padded text  \n\n")
	content = learnings_path.read_text()
	assert "  padded text  " not in content
	assert "padded text" in content
