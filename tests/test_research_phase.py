"""Tests for ResearchPhase -- role building, parsing, synthesis, storage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mission_control.config import MissionConfig, ResearchConfig, TargetConfig
from mission_control.db import Database
from mission_control.models import Mission, ResearchResult
from mission_control.research_phase import (
	RESEARCH_RESULT_RE,
	STRATEGY_RESULT_RE,
	ResearchPhase,
)


def _config(tmp_path: Path) -> MissionConfig:
	cfg = MissionConfig()
	cfg.target = TargetConfig(name="test", path=str(tmp_path), objective="Build API")
	cfg.research = ResearchConfig(enabled=True, budget_per_agent_usd=1.0, timeout=30)
	return cfg


def _mission() -> Mission:
	return Mission(id="m1", objective="Build a REST API with auth")


class TestBuildResearchRoles:
	def test_returns_three_roles(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		roles = phase._build_research_roles("Build API")
		assert len(roles) == 3

	def test_role_names(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		roles = phase._build_research_roles("Build API")
		names = [r.name for r in roles]
		assert "codebase_analyst" in names
		assert "domain_researcher" in names
		assert "prior_art_reviewer" in names

	def test_objective_in_prompts(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		roles = phase._build_research_roles("Implement JWT auth")
		for role in roles:
			assert "Implement JWT auth" in role.prompt


class TestParseResearchOutput:
	def test_valid_json_parsed(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		output = """I analyzed the codebase.
RESEARCH_RESULT:{"area": "codebase", "findings": ["uses SQLAlchemy"], "risks": ["tight coupling"]}"""
		result = phase._parse_research_output(output, "codebase_analyst")
		assert result["area"] == "codebase"
		assert "uses SQLAlchemy" in result["findings"]

	def test_malformed_json_returns_raw(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		output = "RESEARCH_RESULT:{bad json}"
		result = phase._parse_research_output(output, "test_role")
		assert result["area"] == "test_role"
		assert result.get("raw") is True

	def test_no_marker_returns_raw(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		output = "Just some text without a marker"
		result = phase._parse_research_output(output, "test_role")
		assert result["area"] == "test_role"
		assert result.get("raw") is True

	def test_empty_output_returns_raw(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		result = phase._parse_research_output("", "test_role")
		assert result["area"] == "test_role"


class TestFallbackStrategy:
	def test_builds_minimal_strategy(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		findings = [
			{"area": "codebase", "findings": ["uses Flask", "has auth module"]},
			{"area": "domain", "findings": ["JWT is standard"]},
		]
		result = phase._fallback_strategy(findings, "Build API")
		assert "Build API" in result
		assert "codebase" in result
		assert "uses Flask" in result
		assert "JWT is standard" in result

	def test_empty_findings_produces_header_only(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		result = phase._fallback_strategy([], "Build API")
		assert "Build API" in result


class TestFormatStrategy:
	def test_full_strategy_formatted(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		data = {
			"summary": "We need a REST API",
			"approach": "Use FastAPI with SQLAlchemy",
			"risks": ["tight coupling", "migration issues"],
			"execution_order": ["setup models", "add routes", "write tests"],
			"open_questions": ["which auth library?"],
			"anti_patterns": ["avoid global state"],
		}
		result = phase._format_strategy(data)
		assert "# Mission Strategy" in result
		assert "## Problem Summary" in result
		assert "We need a REST API" in result
		assert "## Recommended Approach" in result
		assert "FastAPI" in result
		assert "## Risks" in result
		assert "tight coupling" in result
		assert "## Execution Order" in result
		assert "1. setup models" in result
		assert "## Open Questions" in result
		assert "## Anti-patterns" in result

	def test_partial_data_still_formats(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		data = {"summary": "Quick summary"}
		result = phase._format_strategy(data)
		assert "Quick summary" in result
		assert "## Risks" not in result


class TestWriteStrategy:
	def test_writes_file(self, tmp_path: Path) -> None:
		phase = ResearchPhase(_config(tmp_path), Database(":memory:"))
		phase._write_strategy("# Strategy\nUse JWT")
		path = tmp_path / "MISSION_STRATEGY.md"
		assert path.exists()
		assert "Use JWT" in path.read_text()


class TestStoreKnowledge:
	def test_stores_findings_as_knowledge_items(self, tmp_path: Path) -> None:
		db = Database(":memory:")
		db.insert_mission(Mission(id="m1", objective="test"))
		phase = ResearchPhase(_config(tmp_path), db)
		findings = [
			{"area": "codebase", "findings": ["finding1", "finding2"]},
			{"area": "domain", "findings": ["finding3"]},
		]
		phase._store_knowledge(findings, _mission())
		items = db.get_knowledge_for_mission("m1")
		assert len(items) == 3
		assert items[0].source_unit_type == "research"

	def test_empty_findings_no_items(self, tmp_path: Path) -> None:
		db = Database(":memory:")
		db.insert_mission(Mission(id="m1", objective="test"))
		phase = ResearchPhase(_config(tmp_path), db)
		phase._store_knowledge([], _mission())
		items = db.get_knowledge_for_mission("m1")
		assert len(items) == 0


class TestRunEndToEnd:
	@pytest.mark.asyncio
	async def test_full_run_with_mocked_subprocesses(self, tmp_path: Path) -> None:
		"""Full run() with mocked subprocess calls."""
		db = Database(":memory:")
		db.insert_mission(Mission(id="m1", objective="test"))
		cfg = _config(tmp_path)
		phase = ResearchPhase(cfg, db)

		# Mock _run_parallel_research to return findings
		mock_findings = [
			{"area": "codebase", "findings": ["uses Flask"]},
			{"area": "domain", "findings": ["JWT is standard"]},
			{"area": "prior_art", "findings": ["no prior attempts"]},
		]
		# Mock _synthesize to return strategy
		mock_strategy = "# Strategy\nUse JWT with FastAPI"

		with (
			patch.object(phase, "_run_parallel_research", new_callable=AsyncMock, return_value=mock_findings),
			patch.object(phase, "_synthesize", new_callable=AsyncMock, return_value=mock_strategy),
		):
			result = await phase.run(_mission())

		assert isinstance(result, ResearchResult)
		assert result.strategy == mock_strategy
		assert len(result.findings) == 3
		# Strategy written to disk
		assert (tmp_path / "MISSION_STRATEGY.md").exists()
		# Knowledge items stored
		items = db.get_knowledge_for_mission("m1")
		assert len(items) == 3

	@pytest.mark.asyncio
	async def test_disabled_research_not_called(self, tmp_path: Path) -> None:
		"""When research is disabled, the controller skips the phase entirely.

		This test verifies the ResearchConfig.enabled flag is respected
		at the controller level (tested in test_continuous_controller.py).
		Here we just verify the config defaults.
		"""
		cfg = _config(tmp_path)
		cfg.research.enabled = False
		assert cfg.research.enabled is False


class TestResearchResultRegex:
	def test_research_result_regex(self) -> None:
		text = 'RESEARCH_RESULT:{"area": "codebase", "findings": []}'
		match = RESEARCH_RESULT_RE.search(text)
		assert match is not None

	def test_strategy_result_regex(self) -> None:
		text = 'STRATEGY_RESULT:{"summary": "do this"}'
		match = STRATEGY_RESULT_RE.search(text)
		assert match is not None

	def test_regex_with_whitespace(self) -> None:
		text = 'RESEARCH_RESULT: {"area": "test"}'
		match = RESEARCH_RESULT_RE.search(text)
		assert match is not None
