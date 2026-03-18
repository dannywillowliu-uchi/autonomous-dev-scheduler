"""Tests for two-step planner prompt templates, config flag, and behavioral flow."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from autodev.config import SwarmConfig
from autodev.swarm.models import DecisionType, SwarmState
from autodev.swarm.planner import DrivingPlanner
from autodev.swarm.prompts import (
	ANALYSIS_PROMPT_TEMPLATE,
	CYCLE_PROMPT_TEMPLATE,
	DECISION_FROM_ANALYSIS_PROMPT,
	SYSTEM_PROMPT,
)


class TestAnalysisPromptTemplate:
	def test_exists_and_is_string(self) -> None:
		assert isinstance(ANALYSIS_PROMPT_TEMPLATE, str)
		assert len(ANALYSIS_PROMPT_TEMPLATE) > 100

	def test_contains_state_placeholder(self) -> None:
		"""Template must accept {state_text} for rendering."""
		assert "{state_text}" in ANALYSIS_PROMPT_TEMPLATE

	def test_contains_status_assessment_section(self) -> None:
		assert "Status Assessment" in ANALYSIS_PROMPT_TEMPLATE

	def test_contains_priorities_section(self) -> None:
		assert "Priorities" in ANALYSIS_PROMPT_TEMPLATE

	def test_contains_risk_factors_section(self) -> None:
		assert "Risk" in ANALYSIS_PROMPT_TEMPLATE

	def test_contains_resource_assessment_section(self) -> None:
		assert "Resource" in ANALYSIS_PROMPT_TEMPLATE

	def test_contains_json_schema_with_status_values(self) -> None:
		"""Template should show the valid status values."""
		assert "on_track" in ANALYSIS_PROMPT_TEMPLATE
		assert "stagnating" in ANALYSIS_PROMPT_TEMPLATE
		assert "blocked" in ANALYSIS_PROMPT_TEMPLATE

	def test_renders_with_state_text(self) -> None:
		"""Template should render without error when given state_text."""
		rendered = ANALYSIS_PROMPT_TEMPLATE.format(state_text="## Agents\nNo active agents.")
		assert "No active agents" in rendered
		assert "{state_text}" not in rendered

	def test_instructs_no_decisions(self) -> None:
		"""Analysis step should explicitly say NOT to make decisions."""
		lower = ANALYSIS_PROMPT_TEMPLATE.lower()
		assert "do not make decisions" in lower or "not make decisions" in lower


class TestDecisionFromAnalysisPrompt:
	def test_exists_and_is_string(self) -> None:
		assert isinstance(DECISION_FROM_ANALYSIS_PROMPT, str)
		assert len(DECISION_FROM_ANALYSIS_PROMPT) > 50

	def test_contains_analysis_placeholder(self) -> None:
		assert "{analysis_json}" in DECISION_FROM_ANALYSIS_PROMPT

	def test_contains_state_summary_placeholder(self) -> None:
		assert "{state_summary}" in DECISION_FROM_ANALYSIS_PROMPT

	def test_contains_decision_types_reference_placeholder(self) -> None:
		assert "{decision_types_reference}" in DECISION_FROM_ANALYSIS_PROMPT

	def test_renders_with_all_placeholders(self) -> None:
		rendered = DECISION_FROM_ANALYSIS_PROMPT.format(
			analysis_json='{"status": "on_track"}',
			state_summary="3 agents active, 5 tasks pending",
			decision_types_reference="spawn, kill, wait",
		)
		assert "on_track" in rendered
		assert "3 agents active" in rendered
		assert "spawn, kill, wait" in rendered

	def test_asks_for_json_decisions(self) -> None:
		"""Should instruct the LLM to produce JSON decisions."""
		assert "JSON" in DECISION_FROM_ANALYSIS_PROMPT


class TestTwoStepPlanningConfig:
	def test_config_flag_exists(self) -> None:
		"""SwarmConfig should have a two_step_planning field."""
		sc = SwarmConfig()
		assert hasattr(sc, "two_step_planning")

	def test_config_flag_defaults_true(self) -> None:
		"""two_step_planning should default to True."""
		sc = SwarmConfig()
		assert sc.two_step_planning is True

	def test_config_flag_can_be_disabled(self) -> None:
		"""two_step_planning can be set to False."""
		sc = SwarmConfig()
		sc.two_step_planning = False
		assert sc.two_step_planning is False


class TestPromptEngineeringImprovements:
	def test_system_prompt_has_common_mistakes(self) -> None:
		"""System prompt should include negative examples section."""
		assert "Common Mistakes" in SYSTEM_PROMPT or "Mistakes to Avoid" in SYSTEM_PROMPT

	def test_system_prompt_warns_about_file_conflicts(self) -> None:
		assert "merge conflict" in SYSTEM_PROMPT.lower() or "same files" in SYSTEM_PROMPT.lower()

	def test_system_prompt_warns_about_vague_tasks(self) -> None:
		assert "vague" in SYSTEM_PROMPT.lower()

	def test_system_prompt_warns_against_premature_kill(self) -> None:
		assert "5 minutes" in SYSTEM_PROMPT or "less than 5" in SYSTEM_PROMPT

	def test_cycle_prompt_has_step_by_step(self) -> None:
		"""Cycle prompt should include step-by-step reasoning block."""
		assert "think through" in CYCLE_PROMPT_TEMPLATE.lower() or "before deciding" in CYCLE_PROMPT_TEMPLATE.lower()

	def test_cycle_prompt_has_examples(self) -> None:
		"""Cycle prompt should include decision-making examples."""
		assert "Example" in CYCLE_PROMPT_TEMPLATE or "example" in CYCLE_PROMPT_TEMPLATE

	def test_cycle_prompt_mentions_file_conflicts(self) -> None:
		"""Step-by-step block should ask about file conflicts."""
		assert "file conflict" in CYCLE_PROMPT_TEMPLATE.lower() or "file conflicts" in CYCLE_PROMPT_TEMPLATE.lower()


# --- Behavioral tests for two-step planning flow ---


def _make_planner(**config_overrides: object) -> DrivingPlanner:
	ctrl = MagicMock()
	ctrl._config = MagicMock()
	ctrl._config.target.resolved_path = "/tmp/test"
	sc = SwarmConfig()
	for k, v in config_overrides.items():
		setattr(sc, k, v)
	return DrivingPlanner(ctrl, sc)


class TestTwoStepPlanBehavior:
	"""Behavioral tests for the _two_step_plan method."""

	@pytest.mark.asyncio
	async def test_analysis_then_decision_flow(self) -> None:
		"""Valid analysis followed by valid decisions -- both parsed correctly."""
		planner = _make_planner()

		analysis_json = json.dumps({
			"status": "on_track",
			"priorities": [{"focus": "finish tests", "reason": "coverage low", "impact": "high"}],
			"risks": ["test flakiness"],
			"resource_recommendation": "maintain",
		})
		decisions_json = json.dumps([
			{
				"type": "spawn",
				"payload": {"role": "implementer", "name": "a1", "prompt": "Write tests for auth module"},
				"priority": 1,
			},
		])

		call_count = 0

		async def mock_call_llm(prompt: str, state: object = None) -> str:
			nonlocal call_count
			call_count += 1
			if call_count == 1:
				return analysis_json
			return decisions_json

		planner._call_llm = mock_call_llm  # type: ignore[assignment]

		result = await planner._two_step_plan("## State\nAll good")
		assert result is not None
		assert len(result) == 1
		assert result[0].type == DecisionType.SPAWN
		assert call_count == 2

	@pytest.mark.asyncio
	async def test_analysis_parse_failure_falls_back(self) -> None:
		"""Invalid analysis JSON causes fallback (returns None)."""
		planner = _make_planner()

		async def mock_call_llm(prompt: str, state: object = None) -> str:
			return "This is not valid JSON at all, just random text"

		planner._call_llm = mock_call_llm  # type: ignore[assignment]

		result = await planner._two_step_plan("## State\nSome state")
		assert result is None

	@pytest.mark.asyncio
	async def test_analysis_invalid_status_falls_back(self) -> None:
		"""Analysis with status 'banana' triggers fallback (returns None)."""
		planner = _make_planner()

		analysis_json = json.dumps({
			"status": "banana",
			"priorities": [],
			"risks": [],
			"resource_recommendation": "maintain",
		})

		async def mock_call_llm(prompt: str, state: object = None) -> str:
			return analysis_json

		planner._call_llm = mock_call_llm  # type: ignore[assignment]

		result = await planner._two_step_plan("## State\nSome state")
		assert result is None

	@pytest.mark.asyncio
	async def test_two_step_disabled_uses_single_call(self) -> None:
		"""two_step_planning=False skips analysis step, makes only one LLM call."""
		planner = _make_planner(two_step_planning=False)

		# Mock controller methods used by _plan_cycle
		planner._controller.render_state.return_value = "## State"
		planner._controller.get_scaling_recommendation.return_value = {}

		decisions_json = json.dumps([
			{"type": "wait", "payload": {"duration": 30}},
		])

		call_count = 0

		async def mock_call_llm(prompt: str, state: object = None) -> str:
			nonlocal call_count
			call_count += 1
			return decisions_json

		planner._call_llm = mock_call_llm  # type: ignore[assignment]

		state = SwarmState(mission_objective="Test")
		result = await planner._plan_cycle(state)

		# Only one LLM call (single-call cycle prompt), no analysis step
		assert call_count == 1
		assert len(result) == 1
		assert result[0].type == DecisionType.WAIT
