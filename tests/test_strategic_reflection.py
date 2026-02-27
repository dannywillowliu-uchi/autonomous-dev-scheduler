"""Tests for StrategicReflectionAgent -- prompt building and result parsing."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from mission_control.batch_analyzer import BatchSignals
from mission_control.config import MissionConfig, TargetConfig
from mission_control.models import KnowledgeItem
from mission_control.strategic_reflection import (
	REFLECTION_RESULT_RE,
	ReflectionResult,
	StrategicReflectionAgent,
)


def _config() -> MissionConfig:
	cfg = MissionConfig()
	cfg.target = TargetConfig(name="test", path="/tmp/test", objective="Build API")
	return cfg


def _signals(**kwargs) -> BatchSignals:
	defaults = {
		"file_hotspots": [("src/auth.py", 5)],
		"failure_clusters": {"Import error": 3},
		"stalled_areas": ["src/db.py"],
		"effort_distribution": {"src/auth.py": 0.6, "tests/": 0.4},
		"retry_depth": {},
		"knowledge_gaps": [],
	}
	defaults.update(kwargs)
	return BatchSignals(**defaults)


class TestParseReflection:
	def test_valid_json_parsed(self) -> None:
		agent = StrategicReflectionAgent(_config())
		data = json.dumps({
			"patterns": ["high coupling in auth"],
			"tensions": ["strategy says microservices but code is monolith"],
			"open_questions": ["should we split?"],
			"strategy_revision": None,
		})
		output = f"Some analysis text.\nREFLECTION_RESULT:{data}"

		result = agent._parse_reflection(output)
		assert result.patterns == ["high coupling in auth"]
		assert result.tensions == ["strategy says microservices but code is monolith"]
		assert result.open_questions == ["should we split?"]
		assert result.strategy_revision is None

	def test_strategy_revision_present(self) -> None:
		agent = StrategicReflectionAgent(_config())
		data = json.dumps({
			"patterns": [], "tensions": [],
			"open_questions": [], "strategy_revision": "Switch to JWT",
		})
		output = f"REFLECTION_RESULT:{data}"

		result = agent._parse_reflection(output)
		assert result.strategy_revision == "Switch to JWT"

	def test_malformed_json_returns_empty(self) -> None:
		agent = StrategicReflectionAgent(_config())
		output = "REFLECTION_RESULT:{invalid json here}"

		result = agent._parse_reflection(output)
		assert result.patterns == []
		assert result.tensions == []
		assert result.strategy_revision is None

	def test_no_marker_returns_empty(self) -> None:
		agent = StrategicReflectionAgent(_config())
		output = "The agent just rambled without producing a result."

		result = agent._parse_reflection(output)
		assert result == ReflectionResult()

	def test_empty_output_returns_empty(self) -> None:
		agent = StrategicReflectionAgent(_config())
		result = agent._parse_reflection("")
		assert result == ReflectionResult()

	def test_partial_fields_filled(self) -> None:
		agent = StrategicReflectionAgent(_config())
		output = 'REFLECTION_RESULT:{"patterns": ["p1", "p2"]}'

		result = agent._parse_reflection(output)
		assert result.patterns == ["p1", "p2"]
		assert result.tensions == []
		assert result.open_questions == []
		assert result.strategy_revision is None


class TestBuildReflectionPrompt:
	def test_includes_objective(self) -> None:
		agent = StrategicReflectionAgent(_config())
		prompt = agent._build_reflection_prompt(
			"Build a REST API", _signals(), [], "Use FastAPI",
		)
		assert "Build a REST API" in prompt

	def test_includes_strategy(self) -> None:
		agent = StrategicReflectionAgent(_config())
		prompt = agent._build_reflection_prompt(
			"obj", _signals(), [], "Use FastAPI with SQLAlchemy",
		)
		assert "Use FastAPI with SQLAlchemy" in prompt

	def test_includes_hotspots(self) -> None:
		agent = StrategicReflectionAgent(_config())
		prompt = agent._build_reflection_prompt(
			"obj", _signals(file_hotspots=[("src/main.py", 7)]), [], "",
		)
		assert "src/main.py" in prompt
		assert "7 touches" in prompt

	def test_includes_failure_clusters(self) -> None:
		agent = StrategicReflectionAgent(_config())
		prompt = agent._build_reflection_prompt(
			"obj", _signals(failure_clusters={"timeout in CI": 4}), [], "",
		)
		assert "timeout in CI" in prompt
		assert "4 failures" in prompt

	def test_includes_stalled_areas(self) -> None:
		agent = StrategicReflectionAgent(_config())
		prompt = agent._build_reflection_prompt(
			"obj", _signals(stalled_areas=["src/db.py"]), [], "",
		)
		assert "src/db.py" in prompt

	def test_includes_knowledge_items(self) -> None:
		agent = StrategicReflectionAgent(_config())
		ki = KnowledgeItem(
			mission_id="m1", source_unit_id="u1",
			source_unit_type="research", title="Auth patterns",
			content="JWT is preferred over sessions",
		)
		prompt = agent._build_reflection_prompt("obj", _signals(), [ki], "")
		assert "JWT is preferred" in prompt
		assert "[research]" in prompt

	def test_empty_signals_shows_none(self) -> None:
		agent = StrategicReflectionAgent(_config())
		prompt = agent._build_reflection_prompt(
			"obj", BatchSignals(), [], "",
		)
		assert "(none)" in prompt

	def test_no_strategy_shows_placeholder(self) -> None:
		agent = StrategicReflectionAgent(_config())
		prompt = agent._build_reflection_prompt(
			"obj", _signals(), [], "",
		)
		assert "(no strategy document)" in prompt


class TestReflectEndToEnd:
	@pytest.mark.asyncio
	async def test_reflect_with_mocked_llm(self) -> None:
		"""Full reflect() flow with mocked LLM returning valid JSON."""
		agent = StrategicReflectionAgent(_config())
		fake_output = json.dumps({
			"patterns": ["auth module is a hotspot"],
			"tensions": ["strategy says split but code is coupled"],
			"open_questions": ["should we refactor first?"],
			"strategy_revision": None,
		})
		llm_output = f"Analysis complete.\nREFLECTION_RESULT:{fake_output}"

		with patch.object(agent, "_invoke_llm", new_callable=AsyncMock, return_value=llm_output):
			result = await agent.reflect(
				objective="Build API",
				signals=_signals(),
				knowledge_items=[],
				strategy="Use FastAPI",
			)

		assert result.patterns == ["auth module is a hotspot"]
		assert result.tensions == ["strategy says split but code is coupled"]
		assert result.strategy_revision is None

	@pytest.mark.asyncio
	async def test_reflect_with_strategy_revision(self) -> None:
		agent = StrategicReflectionAgent(_config())
		fake_output = json.dumps({
			"patterns": [],
			"tensions": ["fundamental mismatch"],
			"open_questions": [],
			"strategy_revision": "Switch from REST to GraphQL",
		})
		llm_output = f"REFLECTION_RESULT:{fake_output}"

		with patch.object(agent, "_invoke_llm", new_callable=AsyncMock, return_value=llm_output):
			result = await agent.reflect(
				objective="Build API",
				signals=_signals(),
				knowledge_items=[],
				strategy="Use REST",
			)

		assert result.strategy_revision == "Switch from REST to GraphQL"

	@pytest.mark.asyncio
	async def test_reflect_llm_failure_returns_empty(self) -> None:
		"""When LLM returns empty string, reflect returns empty result."""
		agent = StrategicReflectionAgent(_config())

		with patch.object(agent, "_invoke_llm", new_callable=AsyncMock, return_value=""):
			result = await agent.reflect(
				objective="Build API",
				signals=_signals(),
				knowledge_items=[],
				strategy="",
			)

		assert result == ReflectionResult()


class TestReflectionResultRegex:
	def test_regex_matches_valid(self) -> None:
		text = 'REFLECTION_RESULT:{"patterns": []}'
		match = REFLECTION_RESULT_RE.search(text)
		assert match is not None

	def test_regex_matches_with_whitespace(self) -> None:
		text = 'REFLECTION_RESULT: {"patterns": []}'
		match = REFLECTION_RESULT_RE.search(text)
		assert match is not None

	def test_regex_no_match_on_missing_marker(self) -> None:
		text = '{"patterns": []}'
		match = REFLECTION_RESULT_RE.search(text)
		assert match is None
