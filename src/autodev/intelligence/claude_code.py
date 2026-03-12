"""Claude Code release scanner -- monitors for new features relevant to autodev."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from autodev.intelligence.models import Finding

logger = logging.getLogger(__name__)

REPO = "anthropics/claude-code"

# Automation-relevant keyword categories with weights
FEATURE_CATEGORIES: dict[str, list[str]] = {
	"skills": ["skill", "slash command", "plugin"],
	"hooks": ["hook", "pre-tool", "post-tool", "pretooluse", "posttooluse", "notification"],
	"agents": ["agent", "subagent", "spawn", "parallel", "worktree", "teams", "inbox"],
	"mcp": ["mcp", "model context protocol", "tool server", "mcp server"],
	"permissions": ["permission", "permission mode", "auto mode", "sandbox"],
	"automation": ["automation", "cli", "config", "setting", "custom", "tool"],
}

# Flat list for backward compat
AUTOMATION_KEYWORDS = [kw for kws in FEATURE_CATEGORIES.values() for kw in kws]


@dataclass
class ParsedFeatures:
	"""Automation-relevant features extracted from a release note."""

	categories: dict[str, list[str]] = field(default_factory=dict)
	is_breaking: bool = False

	@property
	def matched_keywords(self) -> list[str]:
		return [kw for kws in self.categories.values() for kw in kws]

	@property
	def category_names(self) -> list[str]:
		return list(self.categories.keys())


class ClaudeCodeScanner:
	"""Monitor Claude Code GitHub releases for automation-relevant features.

	Fetches releases from anthropics/claude-code, diffs against the locally
	installed version, parses release notes for skills/hooks/agents/MCP/permissions
	features, scores relevance, and returns Finding objects.
	"""

	REPO = REPO

	def __init__(self, per_page: int = 10) -> None:
		self._per_page = per_page

	@staticmethod
	def get_installed_version() -> str | None:
		"""Get the installed Claude Code version via 'claude --version'."""
		try:
			proc = subprocess.run(
				["claude", "--version"],
				capture_output=True,
				text=True,
				timeout=10,
			)
			if proc.returncode != 0:
				return None
			output = proc.stdout.strip()
			match = re.search(r"(\d+\.\d+\.\d+)", output)
			return match.group(1) if match else output or None
		except Exception:
			return None

	@staticmethod
	def is_newer(release_tag: str, installed: str | None) -> bool | None:
		"""Check if a release tag is newer than the installed version.

		Returns True if newer, False if same/older, None if comparison impossible.
		"""
		if not installed:
			return None
		tag_match = re.search(r"(\d+)\.(\d+)\.(\d+)", release_tag)
		inst_match = re.search(r"(\d+)\.(\d+)\.(\d+)", installed)
		if not tag_match or not inst_match:
			return None
		tag_parts = tuple(int(tag_match.group(i)) for i in (1, 2, 3))
		inst_parts = tuple(int(inst_match.group(i)) for i in (1, 2, 3))
		if tag_parts > inst_parts:
			return True
		return False

	@staticmethod
	def parse_features(text: str) -> ParsedFeatures:
		"""Parse release notes for automation-relevant feature mentions."""
		lower = text.lower()
		categories: dict[str, list[str]] = {}
		for cat, keywords in FEATURE_CATEGORIES.items():
			matched = [kw for kw in keywords if kw in lower]
			if matched:
				categories[cat] = matched
		is_breaking = "breaking change" in lower or "breaking:" in lower
		return ParsedFeatures(categories=categories, is_breaking=is_breaking)

	@staticmethod
	def score_relevance(text: str, features: ParsedFeatures | None = None) -> float:
		"""Score how relevant a release is to autonomous development.

		Scoring:
		- 1 point per matched keyword (capped at 5)
		- Category diversity bonus: +0.5 per extra category beyond 1
		- Breaking change bonus: +0.5
		"""
		if features is None:
			features = ClaudeCodeScanner.parse_features(text)
		base = float(len(features.matched_keywords))
		cat_count = len(features.category_names)
		if cat_count > 1:
			base += (cat_count - 1) * 0.5
		if features.is_breaking:
			base += 0.5
		return min(base, 5.0)

	async def fetch_releases(self, client: httpx.AsyncClient) -> list[dict]:
		"""Fetch recent releases from GitHub API."""
		resp = await client.get(
			f"https://api.github.com/repos/{self.REPO}/releases",
			params={"per_page": self._per_page},
			headers={"Accept": "application/vnd.github+json"},
		)
		resp.raise_for_status()
		return resp.json()

	async def scan(self, client: httpx.AsyncClient | None = None) -> list[Finding]:
		"""Fetch Claude Code releases and find automation-relevant changes."""
		findings: list[Finding] = []
		own_client = client is None
		if own_client:
			client = httpx.AsyncClient(timeout=15.0)

		try:
			installed = self.get_installed_version()
			releases = await self.fetch_releases(client)

			for release in releases:
				tag = release.get("tag_name", "")
				body = release.get("body", "") or ""
				published = release.get("published_at", "")

				combined = f"{tag} {body}"
				features = self.parse_features(combined)
				score = self.score_relevance(combined, features)

				if score < 0.5:
					continue

				newer = self.is_newer(tag, installed)
				if newer is True:
					score = min(score + 1.0, 5.0)

				summary_parts = []
				if features.category_names:
					summary_parts.append(f"Features: {', '.join(features.category_names)}")
				if features.is_breaking:
					summary_parts.append("BREAKING CHANGE")
				if newer is True:
					summary_parts.append("newer than installed")
				elif newer is False:
					summary_parts.append("already installed")
				summary_prefix = " | ".join(summary_parts)
				body_excerpt = body[:400]
				summary = f"{summary_prefix}\n{body_excerpt}" if summary_prefix else body_excerpt

				findings.append(Finding(
					source="claude_code",
					title=f"Claude Code {tag}",
					url=release.get("html_url", ""),
					summary=summary[:500],
					published_at=published if published else datetime.now(timezone.utc).isoformat(),
					raw_data={
						**release,
						"_parsed_features": {
							"categories": features.categories,
							"is_breaking": features.is_breaking,
						},
						"_installed_version": installed,
						"_is_newer": newer,
					},
					relevance_score=score,
				))
		except httpx.HTTPError as exc:
			logger.warning("Claude Code scan failed: %s", exc)
		finally:
			if own_client:
				await client.aclose()

		return findings


# Module-level convenience function (used by sources.py registration)
async def scan_claude_code(client: httpx.AsyncClient | None = None) -> list[Finding]:
	"""Fetch Claude Code releases and find automation-relevant changes."""
	scanner = ClaudeCodeScanner()
	return await scanner.scan(client)
