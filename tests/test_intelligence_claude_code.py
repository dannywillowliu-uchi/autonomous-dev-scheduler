"""Tests for the Claude Code release scanner."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from autodev.intelligence.claude_code import (
	ClaudeCodeScanner,
	ParsedFeatures,
	scan_claude_code,
)


def _make_release(
	tag: str = "v1.0.50",
	body: str = "New skill and hook support for automation workflows",
	published_at: str = "2026-01-20T10:00:00Z",
) -> dict:
	return {
		"tag_name": tag,
		"html_url": f"https://github.com/anthropics/claude-code/releases/tag/{tag}",
		"body": body,
		"published_at": published_at,
	}


def _mock_releases_response(releases: list[dict] | None = None) -> httpx.Response:
	if releases is None:
		releases = [_make_release()]
	return httpx.Response(200, json=releases)


def _route_mock(request: httpx.Request) -> httpx.Response:
	url = str(request.url)
	if "anthropics/claude-code/releases" in url:
		return _mock_releases_response()
	return httpx.Response(404)


class TestGetInstalledVersion:
	"""Tests for ClaudeCodeScanner.get_installed_version."""

	def test_parses_version_from_output(self) -> None:
		with patch("autodev.intelligence.claude_code.subprocess.run") as mock_run:
			mock_run.return_value.returncode = 0
			mock_run.return_value.stdout = "claude 1.2.3\n"
			result = ClaudeCodeScanner.get_installed_version()
		assert result == "1.2.3"

	def test_parses_bare_version(self) -> None:
		with patch("autodev.intelligence.claude_code.subprocess.run") as mock_run:
			mock_run.return_value.returncode = 0
			mock_run.return_value.stdout = "1.0.50"
			result = ClaudeCodeScanner.get_installed_version()
		assert result == "1.0.50"

	def test_returns_none_on_nonzero_exit(self) -> None:
		with patch("autodev.intelligence.claude_code.subprocess.run") as mock_run:
			mock_run.return_value.returncode = 1
			mock_run.return_value.stdout = ""
			result = ClaudeCodeScanner.get_installed_version()
		assert result is None

	def test_returns_none_on_exception(self) -> None:
		with patch("autodev.intelligence.claude_code.subprocess.run", side_effect=FileNotFoundError):
			result = ClaudeCodeScanner.get_installed_version()
		assert result is None

	def test_returns_none_on_timeout(self) -> None:
		import subprocess
		err = subprocess.TimeoutExpired("claude", 10)
		with patch("autodev.intelligence.claude_code.subprocess.run", side_effect=err):
			result = ClaudeCodeScanner.get_installed_version()
		assert result is None


class TestIsNewer:
	"""Tests for ClaudeCodeScanner.is_newer."""

	def test_newer_version(self) -> None:
		assert ClaudeCodeScanner.is_newer("v2.0.0", "1.0.0") is True

	def test_same_version(self) -> None:
		assert ClaudeCodeScanner.is_newer("v1.0.0", "1.0.0") is False

	def test_older_version(self) -> None:
		assert ClaudeCodeScanner.is_newer("v0.9.0", "1.0.0") is False

	def test_minor_newer(self) -> None:
		assert ClaudeCodeScanner.is_newer("v1.1.0", "1.0.0") is True

	def test_patch_newer(self) -> None:
		assert ClaudeCodeScanner.is_newer("v1.0.1", "1.0.0") is True

	def test_none_installed(self) -> None:
		assert ClaudeCodeScanner.is_newer("v1.0.0", None) is None

	def test_unparseable_tag(self) -> None:
		assert ClaudeCodeScanner.is_newer("latest", "1.0.0") is None

	def test_unparseable_installed(self) -> None:
		assert ClaudeCodeScanner.is_newer("v1.0.0", "unknown") is None


class TestParseFeatures:
	"""Tests for ClaudeCodeScanner.parse_features."""

	def test_detects_skills(self) -> None:
		features = ClaudeCodeScanner.parse_features("Added new skill support and slash command")
		assert "skills" in features.categories
		assert "skill" in features.categories["skills"]
		assert "slash command" in features.categories["skills"]

	def test_detects_hooks(self) -> None:
		features = ClaudeCodeScanner.parse_features("New hook system with PreToolUse events")
		assert "hooks" in features.categories
		assert "hook" in features.categories["hooks"]

	def test_detects_agents(self) -> None:
		features = ClaudeCodeScanner.parse_features("Improved agent spawning with worktree isolation")
		assert "agents" in features.categories
		assert "agent" in features.categories["agents"]
		assert "spawn" in features.categories["agents"]
		assert "worktree" in features.categories["agents"]

	def test_detects_mcp(self) -> None:
		features = ClaudeCodeScanner.parse_features("MCP server improvements and new tool server features")
		assert "mcp" in features.categories
		assert "mcp" in features.categories["mcp"]

	def test_detects_permissions(self) -> None:
		features = ClaudeCodeScanner.parse_features("New permission mode options for sandbox")
		assert "permissions" in features.categories

	def test_detects_breaking_change(self) -> None:
		features = ClaudeCodeScanner.parse_features("Breaking change: new config format")
		assert features.is_breaking is True

	def test_no_breaking_change(self) -> None:
		features = ClaudeCodeScanner.parse_features("Minor bug fixes")
		assert features.is_breaking is False

	def test_empty_text(self) -> None:
		features = ClaudeCodeScanner.parse_features("")
		assert features.categories == {}
		assert features.is_breaking is False
		assert features.matched_keywords == []
		assert features.category_names == []

	def test_irrelevant_text(self) -> None:
		features = ClaudeCodeScanner.parse_features("Updated README with better examples")
		assert features.categories == {}

	def test_multiple_categories(self) -> None:
		features = ClaudeCodeScanner.parse_features("New skill and hook support with MCP integration")
		assert len(features.category_names) >= 3


class TestScoreRelevance:
	"""Tests for ClaudeCodeScanner.score_relevance."""

	def test_empty_text_scores_zero(self) -> None:
		score = ClaudeCodeScanner.score_relevance("")
		assert score == 0.0

	def test_single_keyword_scores_one(self) -> None:
		score = ClaudeCodeScanner.score_relevance("new skill added")
		assert score >= 1.0

	def test_multiple_categories_get_diversity_bonus(self) -> None:
		# Single category
		score_single = ClaudeCodeScanner.score_relevance("new skill added")
		# Multiple categories
		score_multi = ClaudeCodeScanner.score_relevance("new skill and hook with mcp support")
		assert score_multi > score_single

	def test_breaking_change_bonus(self) -> None:
		base = ClaudeCodeScanner.score_relevance("new skill support")
		breaking = ClaudeCodeScanner.score_relevance("breaking change: new skill support")
		assert breaking > base

	def test_capped_at_five(self) -> None:
		heavy = "skill hook mcp agent permission automation cli config spawn worktree teams inbox plugin"
		score = ClaudeCodeScanner.score_relevance(heavy)
		assert score <= 5.0

	def test_accepts_precomputed_features(self) -> None:
		features = ParsedFeatures(categories={"skills": ["skill"], "hooks": ["hook"]})
		score = ClaudeCodeScanner.score_relevance("ignored", features)
		# 2 keywords + 0.5 diversity bonus for 2 categories
		assert score == 2.5


class TestScan:
	"""Tests for ClaudeCodeScanner.scan and scan_claude_code."""

	@pytest.mark.asyncio
	async def test_scan_returns_findings(self) -> None:
		transport = httpx.MockTransport(_route_mock)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert len(findings) >= 1
		assert findings[0].source == "claude_code"
		assert "v1.0.50" in findings[0].title

	@pytest.mark.asyncio
	async def test_scan_includes_parsed_features_in_raw_data(self) -> None:
		transport = httpx.MockTransport(_route_mock)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert len(findings) >= 1
		raw = findings[0].raw_data
		assert "_parsed_features" in raw
		assert "categories" in raw["_parsed_features"]
		assert "_installed_version" in raw
		assert raw["_installed_version"] == "1.0.0"

	@pytest.mark.asyncio
	async def test_newer_releases_get_score_boost(self) -> None:
		releases = [_make_release("v2.0.0", "New skill and hook support")]

		def route(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json=releases)

		transport = httpx.MockTransport(route)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert len(findings) == 1
		# Score should include the +1.0 newer boost
		assert findings[0].relevance_score > 1.0

	@pytest.mark.asyncio
	async def test_irrelevant_releases_filtered(self) -> None:
		releases = [_make_release("v1.0.1", "Fixed a typo in README")]

		def route(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json=releases)

		transport = httpx.MockTransport(route)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert len(findings) == 0

	@pytest.mark.asyncio
	async def test_scan_handles_http_error(self) -> None:
		def route(request: httpx.Request) -> httpx.Response:
			return httpx.Response(500)

		transport = httpx.MockTransport(route)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert findings == []

	@pytest.mark.asyncio
	async def test_scan_creates_own_client_when_none(self) -> None:
		releases = [_make_release()]
		mock_client = httpx.AsyncClient(transport=httpx.MockTransport(
			lambda r: httpx.Response(200, json=releases)
		))

		with patch("autodev.intelligence.claude_code.httpx.AsyncClient", return_value=mock_client):
			with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
				scanner = ClaudeCodeScanner()
				findings = await scanner.scan(client=None)

		assert len(findings) >= 1

	@pytest.mark.asyncio
	async def test_scan_summary_includes_feature_categories(self) -> None:
		releases = [_make_release("v2.0.0", "New skill, hook, and MCP server support")]

		def route(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json=releases)

		transport = httpx.MockTransport(route)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert len(findings) == 1
		assert "Features:" in findings[0].summary
		assert "newer than installed" in findings[0].summary

	@pytest.mark.asyncio
	async def test_scan_summary_marks_breaking(self) -> None:
		releases = [_make_release("v2.0.0", "Breaking change: new config schema for hooks")]

		def route(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json=releases)

		transport = httpx.MockTransport(route)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert len(findings) == 1
		assert "BREAKING CHANGE" in findings[0].summary

	@pytest.mark.asyncio
	async def test_module_level_scan_claude_code(self) -> None:
		transport = httpx.MockTransport(_route_mock)
		client = httpx.AsyncClient(transport=transport)

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scan_claude_code(client)
		await client.aclose()

		assert len(findings) >= 1
		assert findings[0].source == "claude_code"

	@pytest.mark.asyncio
	async def test_per_page_configurable(self) -> None:
		captured_params: dict = {}

		def route(request: httpx.Request) -> httpx.Response:
			captured_params.update(dict(request.url.params))
			return httpx.Response(200, json=[])

		transport = httpx.MockTransport(route)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner(per_page=5)

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			await scanner.scan(client)
		await client.aclose()

		assert captured_params.get("per_page") == "5"

	@pytest.mark.asyncio
	async def test_multiple_releases_scored_independently(self) -> None:
		releases = [
			_make_release("v2.0.0", "New skill and hook support"),
			_make_release("v1.5.0", "MCP server improvements and agent spawning"),
			_make_release("v1.0.1", "Fixed typo"),  # Should be filtered
		]

		def route(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json=releases)

		transport = httpx.MockTransport(route)
		client = httpx.AsyncClient(transport=transport)
		scanner = ClaudeCodeScanner()

		with patch.object(ClaudeCodeScanner, "get_installed_version", return_value="1.0.0"):
			findings = await scanner.scan(client)
		await client.aclose()

		assert len(findings) == 2
		tags = [f.title for f in findings]
		assert "Claude Code v2.0.0" in tags
		assert "Claude Code v1.5.0" in tags
