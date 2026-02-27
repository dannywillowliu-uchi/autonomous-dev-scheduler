"""Strategic reflection -- LLM synthesis of batch execution signals."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from mission_control.batch_analyzer import BatchSignals
from mission_control.config import MissionConfig, build_claude_cmd, claude_subprocess_env
from mission_control.models import KnowledgeItem

log = logging.getLogger(__name__)

REFLECTION_RESULT_RE = re.compile(r"REFLECTION_RESULT:\s*(\{.*\})", re.DOTALL)


@dataclass
class ReflectionResult:
	"""Output of a strategic reflection cycle."""

	patterns: list[str] = field(default_factory=list)
	tensions: list[str] = field(default_factory=list)
	open_questions: list[str] = field(default_factory=list)
	strategy_revision: str | None = None


class StrategicReflectionAgent:
	"""Synthesizes batch execution signals into a reflection briefing."""

	def __init__(self, config: MissionConfig) -> None:
		self._config = config

	async def reflect(
		self,
		objective: str,
		signals: BatchSignals,
		knowledge_items: list[KnowledgeItem],
		strategy: str,
	) -> ReflectionResult:
		"""Synthesize batch signals into a reflection briefing."""
		prompt = self._build_reflection_prompt(objective, signals, knowledge_items, strategy)
		output = await self._invoke_llm(prompt)
		return self._parse_reflection(output)

	def _build_reflection_prompt(
		self,
		objective: str,
		signals: BatchSignals,
		knowledge_items: list[KnowledgeItem],
		strategy: str,
	) -> str:
		hotspots_text = "\n".join(f"  - {f} ({c} touches)" for f, c in signals.file_hotspots) or "  (none)"
		failures_text = "\n".join(f"  - {k}: {v} failures" for k, v in signals.failure_clusters.items()) or "  (none)"
		stalled_text = "\n".join(f"  - {s}" for s in signals.stalled_areas) or "  (none)"
		effort_text = "\n".join(f"  - {k}: {v:.0%}" for k, v in signals.effort_distribution.items()) or "  (none)"
		knowledge_text = "\n".join(
			f"  - [{k.source_unit_type}] {k.title}: {k.content[:150]}"
			for k in knowledge_items[-10:]
		) or "  (none)"

		strategy_summary = strategy[:500] if strategy else "(no strategy document)"

		return f"""You are analyzing execution patterns for a mission.

Objective: {objective}
Strategy: {strategy_summary}

## Execution Signals
- File hotspots (3+ touches):
{hotspots_text}
- Failure clusters:
{failures_text}
- Stalled areas (2+ attempts, no success):
{stalled_text}
- Effort distribution:
{effort_text}

## Accumulated Knowledge
{knowledge_text}

Based on these signals:
1. What PATTERNS do you see? (recurring issues, coupling, bottlenecks)
2. What TENSIONS exist between the strategy and what's actually happening?
3. What OPEN QUESTIONS should be resolved before more work?
4. Should the strategy be REVISED? If so, how?

REFLECTION_RESULT:{{"patterns": ["..."], "tensions": ["..."],\
 "open_questions": ["..."], "strategy_revision": "..." or null}}"""

	async def _invoke_llm(self, prompt: str) -> str:
		"""Single cheap LLM call for reflection."""
		model = "haiku"
		budget = 0.20
		timeout = 120

		cmd = build_claude_cmd(self._config, model=model, budget=budget)
		try:
			proc = await asyncio.create_subprocess_exec(
				*cmd,
				stdin=asyncio.subprocess.PIPE,
				stdout=asyncio.subprocess.PIPE,
				stderr=asyncio.subprocess.PIPE,
				env=claude_subprocess_env(self._config),
				cwd=str(self._config.target.resolved_path),
			)
			stdout, _ = await asyncio.wait_for(
				proc.communicate(input=prompt.encode()),
				timeout=timeout,
			)
			return stdout.decode() if stdout else ""
		except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
			log.warning("Reflection LLM failed: %s", exc)
			return ""

	def _parse_reflection(self, output: str) -> ReflectionResult:
		"""Parse REFLECTION_RESULT from LLM output."""
		match = REFLECTION_RESULT_RE.search(output)
		if match:
			try:
				data = json.loads(match.group(1))
				if isinstance(data, dict):
					return ReflectionResult(
						patterns=data.get("patterns", []),
						tensions=data.get("tensions", []),
						open_questions=data.get("open_questions", []),
						strategy_revision=data.get("strategy_revision"),
					)
			except json.JSONDecodeError:
				pass
		log.warning("Could not parse REFLECTION_RESULT, returning empty reflection")
		return ReflectionResult()
