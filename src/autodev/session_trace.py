"""Git notes-based reasoning traces for autodev commits."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from autodev.config import TracingNotesConfig

logger = logging.getLogger(__name__)


def extract_trace_summary(
	ad_result: dict[str, Any],
	agent_name: str,
	task_title: str,
	config: TracingNotesConfig,
) -> str:
	"""Build a structured markdown summary from AD_RESULT data.

	Extracts status, summary, files changed, discoveries, and concerns.
	Truncates to config.max_note_bytes.
	"""
	lines: list[str] = []
	lines.append(f"# Trace: {task_title}")
	lines.append(f"Agent: {agent_name}")
	lines.append(f"Status: {ad_result.get('status', 'unknown')}")
	lines.append("")

	summary = ad_result.get("summary", "")
	if summary:
		lines.append("## Summary")
		lines.append(summary)
		lines.append("")

	if config.include_files_changed:
		files = ad_result.get("files_changed", [])
		if files:
			lines.append("## Files Changed")
			for f in files:
				lines.append(f"- {f}")
			lines.append("")

	if config.include_discoveries:
		discoveries = ad_result.get("discoveries", [])
		if discoveries:
			lines.append("## Discoveries")
			for d in discoveries:
				lines.append(f"- {d}")
			lines.append("")

		concerns = ad_result.get("concerns", [])
		if concerns:
			lines.append("## Concerns")
			for c in concerns:
				lines.append(f"- {c}")
			lines.append("")

	result = "\n".join(lines)

	# Truncate to max bytes
	encoded = result.encode("utf-8")
	if len(encoded) > config.max_note_bytes:
		result = encoded[: config.max_note_bytes].decode("utf-8", errors="ignore")
		result += "\n... [truncated]"

	return result


async def attach_git_note(
	commit_hash: str,
	note_content: str,
	ref: str,
	cwd: str,
) -> bool:
	"""Attach a git note to a commit. Returns True on success."""
	try:
		proc = await asyncio.create_subprocess_exec(
			"git", "notes", "--ref", ref, "add", "-f", "-m", note_content, commit_hash,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			cwd=cwd,
		)
		_, stderr = await proc.communicate()
		if proc.returncode != 0:
			logger.warning("Failed to attach git note to %s: %s", commit_hash[:8], stderr.decode().strip())
			return False
		logger.info("Attached trace note to commit %s", commit_hash[:8])
		return True
	except Exception as exc:
		logger.warning("Git note attachment failed for %s: %s", commit_hash[:8], exc)
		return False


async def get_git_note(
	commit_hash: str,
	ref: str,
	cwd: str,
) -> str | None:
	"""Retrieve a git note for a commit. Returns None if not found."""
	try:
		proc = await asyncio.create_subprocess_exec(
			"git", "notes", "--ref", ref, "show", commit_hash,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			cwd=cwd,
		)
		stdout, _ = await proc.communicate()
		if proc.returncode != 0:
			return None
		return stdout.decode().strip()
	except Exception:
		return None


async def list_git_notes(
	ref: str,
	cwd: str,
	limit: int = 50,
) -> list[dict[str, str]]:
	"""List commits with attached trace notes. Returns [{commit, note_hash}]."""
	try:
		proc = await asyncio.create_subprocess_exec(
			"git", "notes", "--ref", ref, "list",
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			cwd=cwd,
		)
		stdout, _ = await proc.communicate()
		if proc.returncode != 0:
			return []
		results = []
		for line in stdout.decode().strip().split("\n"):
			if not line.strip():
				continue
			parts = line.strip().split()
			if len(parts) >= 2:
				results.append({"note_hash": parts[0], "commit": parts[1]})
			if len(results) >= limit:
				break
		return results
	except Exception:
		return []
