"""Tests for git notes-based reasoning traces (session_trace module)."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from autodev.config import TracingNotesConfig
from autodev.session_trace import (
	attach_git_note,
	extract_trace_summary,
	get_git_note,
	list_git_notes,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path):
	"""Create a real git repo for testing."""
	subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
	subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True, check=True)
	subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True, check=True)
	return tmp_path


def make_commit(repo_path, msg="test commit"):
	"""Create a commit and return its hash."""
	subprocess.run(
		["git", "commit", "--allow-empty", "-m", msg],
		cwd=str(repo_path), capture_output=True, check=True,
	)
	result = subprocess.run(
		["git", "rev-parse", "HEAD"],
		cwd=str(repo_path), capture_output=True, text=True, check=True,
	)
	return result.stdout.strip()


# ---------------------------------------------------------------------------
# Unit tests -- extract_trace_summary
# ---------------------------------------------------------------------------

class TestExtractTraceSummary:
	def test_basic(self):
		"""Typical AD_RESULT produces expected markdown summary."""
		ad_result = {
			"status": "completed",
			"summary": "Did the thing",
			"commits": ["abc123"],
			"files_changed": ["foo.py"],
		}
		config = TracingNotesConfig()
		output = extract_trace_summary(ad_result, agent_name="worker-1", task_title="Implement feature", config=config)

		assert "# Trace: Implement feature" in output
		assert "Agent: worker-1" in output
		assert "Status: completed" in output
		assert "Did the thing" in output
		assert "foo.py" in output

	def test_truncation(self):
		"""Output is truncated to max_note_bytes and ends with [truncated]."""
		ad_result = {
			"status": "completed",
			"summary": "A" * 500,
			"files_changed": ["file.py"],
		}
		config = TracingNotesConfig(max_note_bytes=100)
		output = extract_trace_summary(ad_result, agent_name="worker-1", task_title="Big task", config=config)

		assert output.endswith("[truncated]")
		# The raw content before truncation marker should be within bounds
		assert len(output.encode("utf-8")) < 200  # 100 + truncation marker overhead

	def test_empty_result(self):
		"""Empty dict produces a minimal summary without crashing."""
		config = TracingNotesConfig()
		output = extract_trace_summary({}, agent_name="worker-1", task_title="Empty", config=config)

		assert "# Trace: Empty" in output
		assert "Agent: worker-1" in output
		assert "Status: unknown" in output

	def test_with_discoveries(self):
		"""Discoveries and concerns appear in output."""
		ad_result = {
			"status": "completed",
			"summary": "Checked stuff",
			"discoveries": ["Found X"],
			"concerns": ["Risk Y"],
		}
		config = TracingNotesConfig()
		output = extract_trace_summary(ad_result, agent_name="scout", task_title="Recon", config=config)

		assert "## Discoveries" in output
		assert "Found X" in output
		assert "## Concerns" in output
		assert "Risk Y" in output

	def test_no_discoveries_when_disabled(self):
		"""When include_discoveries=False, discoveries and concerns are omitted."""
		ad_result = {
			"status": "completed",
			"discoveries": ["Found X"],
			"concerns": ["Risk Y"],
		}
		config = TracingNotesConfig(include_discoveries=False)
		output = extract_trace_summary(ad_result, agent_name="scout", task_title="Recon", config=config)

		assert "Found X" not in output
		assert "Risk Y" not in output


class TestTracingNotesConfigDefaults:
	def test_defaults(self):
		"""TracingNotesConfig() has expected default values."""
		config = TracingNotesConfig()
		assert config.enabled is False
		assert config.ref == "refs/notes/autodev-traces"
		assert config.max_note_bytes == 4096
		assert config.include_tool_calls is True
		assert config.include_discoveries is True
		assert config.include_files_changed is True


# ---------------------------------------------------------------------------
# Integration tests -- real git repos
# ---------------------------------------------------------------------------

class TestGitNoteIntegration:
	def test_attach_and_retrieve_git_note(self, git_repo):
		"""Attach a note then retrieve it -- content should match."""
		commit = make_commit(git_repo)
		ref = "refs/notes/autodev-traces"
		content = "Test trace content\nMulti-line"

		ok = asyncio.run(attach_git_note(commit, content, ref=ref, cwd=str(git_repo)))
		assert ok is True

		retrieved = asyncio.run(get_git_note(commit, ref=ref, cwd=str(git_repo)))
		assert retrieved == content

	def test_attach_git_note_custom_ref(self, git_repo):
		"""Notes stored under a custom ref are retrievable with that ref."""
		commit = make_commit(git_repo)
		custom_ref = "refs/notes/custom"
		content = "Custom ref trace"

		ok = asyncio.run(attach_git_note(commit, content, ref=custom_ref, cwd=str(git_repo)))
		assert ok is True

		retrieved = asyncio.run(get_git_note(commit, ref=custom_ref, cwd=str(git_repo)))
		assert retrieved == content

		# Should NOT appear under default ref
		default = asyncio.run(get_git_note(commit, ref="refs/notes/autodev-traces", cwd=str(git_repo)))
		assert default is None

	def test_get_git_note_nonexistent(self, git_repo):
		"""Getting a note from a commit with no note returns None."""
		commit = make_commit(git_repo)
		result = asyncio.run(get_git_note(commit, ref="refs/notes/autodev-traces", cwd=str(git_repo)))
		assert result is None

	def test_list_git_notes(self, git_repo):
		"""Listing notes returns entries for all annotated commits."""
		ref = "refs/notes/autodev-traces"
		commits = []
		for i in range(3):
			c = make_commit(git_repo, msg=f"commit {i}")
			asyncio.run(attach_git_note(c, f"note {i}", ref=ref, cwd=str(git_repo)))
			commits.append(c)

		entries = asyncio.run(list_git_notes(ref=ref, cwd=str(git_repo)))
		assert len(entries) == 3

		listed_commits = {e["commit"] for e in entries}
		for c in commits:
			assert c in listed_commits
