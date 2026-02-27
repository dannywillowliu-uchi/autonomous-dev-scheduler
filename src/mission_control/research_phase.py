"""Pre-planning research phase: parallel investigation + synthesis."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from mission_control.config import MissionConfig, build_claude_cmd, claude_subprocess_env
from mission_control.db import Database
from mission_control.models import KnowledgeItem, Mission, ResearchResult

log = logging.getLogger(__name__)

RESEARCH_RESULT_RE = re.compile(r"RESEARCH_RESULT:\s*(\{.*\})", re.DOTALL)
STRATEGY_RESULT_RE = re.compile(r"STRATEGY_RESULT:\s*(\{.*\})", re.DOTALL)


@dataclass
class ResearchRole:
	"""A single research agent role with its prompt."""

	name: str
	prompt: str


class ResearchPhase:
	"""Pre-planning research: parallel investigation + synthesis."""

	def __init__(self, config: MissionConfig, db: Database) -> None:
		self._config = config
		self._db = db

	async def run(self, mission: Mission) -> ResearchResult:
		"""Run parallel research agents, synthesize findings, write strategy."""
		roles = self._build_research_roles(mission.objective)

		findings = await self._run_parallel_research(roles, mission)

		strategy = await self._synthesize(findings, mission)

		self._write_strategy(strategy)

		self._store_knowledge(findings, mission)

		cost = sum(f.get("_cost_usd", 0.0) for f in findings)
		return ResearchResult(strategy=strategy, findings=findings, cost_usd=cost)

	def _build_research_roles(self, objective: str) -> list[ResearchRole]:
		"""Build research prompts for each role."""
		return [
			ResearchRole(
				name="codebase_analyst",
				prompt=f"""You are analyzing a codebase to prepare for this objective: {objective}

1. Read the relevant source files and understand the current architecture
2. Map the dependency structure in the affected areas
3. Identify coupling points, risks, and complexity hotspots
4. Note any existing patterns or conventions the implementation should follow

Output your findings as:
RESEARCH_RESULT:{{"area": "codebase", "findings": ["..."], "risks": ["..."], "patterns": ["..."]}}""",
			),
			ResearchRole(
				name="domain_researcher",
				prompt=f"""You are researching how to approach this objective: {objective}

1. Search for how this type of problem is typically solved
2. Look up relevant library documentation (use context7 for library docs if available)
3. Identify common patterns, anti-patterns, and tradeoffs
4. Find concrete examples or reference implementations if available

Output your findings as:
RESEARCH_RESULT:{{"area": "domain", "findings": ["..."], "approaches": ["..."], "tradeoffs": ["..."]}}""",
			),
			ResearchRole(
				name="prior_art_reviewer",
				prompt=f"""You are reviewing what has been tried before for this objective: {objective}

1. Check git log for related past work (look for similar changes, reverts, failed attempts)
2. Read BACKLOG.md if it exists for planned work
3. Read any existing design docs or ADRs
4. Check for TODOs, FIXMEs, or known issues in the relevant areas

Output your findings as:
RESEARCH_RESULT:{{"area": "prior_art", "findings": ["..."], "past_attempts": ["..."], "known_issues": ["..."]}}""",
			),
		]

	async def _run_parallel_research(
		self, roles: list[ResearchRole], mission: Mission,
	) -> list[dict]:
		"""Spawn parallel subprocesses (one per role)."""
		rc = self._config.research
		model = rc.model or self._config.scheduler.model
		budget = rc.budget_per_agent_usd
		timeout = rc.timeout
		cwd = str(self._config.target.resolved_path)

		async def run_one(role: ResearchRole) -> dict:
			cmd = build_claude_cmd(self._config, model=model, budget=budget)
			try:
				proc = await asyncio.create_subprocess_exec(
					*cmd,
					stdin=asyncio.subprocess.PIPE,
					stdout=asyncio.subprocess.PIPE,
					stderr=asyncio.subprocess.PIPE,
					env=claude_subprocess_env(self._config),
					cwd=cwd,
				)
				stdout, stderr = await asyncio.wait_for(
					proc.communicate(input=role.prompt.encode()),
					timeout=timeout,
				)
				output = stdout.decode() if stdout else ""
			except asyncio.TimeoutError:
				log.warning("Research agent %s timed out after %ds", role.name, timeout)
				try:
					proc.kill()
					await proc.wait()
				except ProcessLookupError:
					pass
				return {"area": role.name, "error": "timeout", "findings": []}
			except (FileNotFoundError, OSError) as exc:
				log.warning("Research agent %s failed to start: %s", role.name, exc)
				return {"area": role.name, "error": str(exc), "findings": []}

			if proc.returncode != 0:
				log.warning("Research agent %s failed (rc=%d)", role.name, proc.returncode)
				return {"area": role.name, "error": f"exit_code_{proc.returncode}", "findings": []}

			return self._parse_research_output(output, role.name)

		results = await asyncio.gather(*(run_one(r) for r in roles), return_exceptions=True)

		findings: list[dict] = []
		for r in results:
			if isinstance(r, dict):
				findings.append(r)
			elif isinstance(r, Exception):
				log.warning("Research agent error: %s", r)
				findings.append({"area": "unknown", "error": str(r), "findings": []})
		return findings

	def _parse_research_output(self, output: str, role_name: str) -> dict:
		"""Parse RESEARCH_RESULT from agent output."""
		match = RESEARCH_RESULT_RE.search(output)
		if match:
			try:
				data = json.loads(match.group(1))
				if isinstance(data, dict):
					return data
			except json.JSONDecodeError:
				pass
		log.warning("Could not parse RESEARCH_RESULT from %s, using raw output", role_name)
		return {"area": role_name, "findings": [output[:2000]], "raw": True}

	async def _synthesize(self, findings: list[dict], mission: Mission) -> str:
		"""Run synthesis agent to combine all findings into a strategy."""
		rc = self._config.research
		model = rc.model or self._config.scheduler.model
		budget = rc.budget_per_agent_usd
		timeout = rc.timeout
		cwd = str(self._config.target.resolved_path)

		sections: list[str] = []
		for f in findings:
			area = f.get("area", "unknown")
			sections.append(f"## {area}\n{json.dumps(f, indent=2)}")
		findings_text = "\n\n".join(sections)

		objective = mission.objective
		prompt = f"""You are synthesizing research for this objective: {objective}

{findings_text}

Produce a strategy document:
1. Problem Summary: What the problem actually is
2. Recommended Approach: The best approach and WHY
3. Risks and Mitigations: What could go wrong
4. Execution Order: Strategic sequence
5. Open Questions: Things that need validation
6. Anti-patterns: Approaches to avoid

STRATEGY_RESULT:{{"summary": "...", "approach": "...",\
 "risks": ["..."], "execution_order": ["..."],\
 "open_questions": ["..."], "anti_patterns": ["..."]}}"""

		cmd = build_claude_cmd(self._config, model=model, budget=budget)
		try:
			proc = await asyncio.create_subprocess_exec(
				*cmd,
				stdin=asyncio.subprocess.PIPE,
				stdout=asyncio.subprocess.PIPE,
				stderr=asyncio.subprocess.PIPE,
				env=claude_subprocess_env(self._config),
				cwd=cwd,
			)
			stdout, _ = await asyncio.wait_for(
				proc.communicate(input=prompt.encode()),
				timeout=timeout,
			)
			output = stdout.decode() if stdout else ""
		except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
			log.warning("Synthesis agent failed: %s", exc)
			return self._fallback_strategy(findings, mission.objective)

		if proc.returncode != 0:
			log.warning("Synthesis agent failed (rc=%d)", proc.returncode)
			return self._fallback_strategy(findings, mission.objective)

		# Try to parse structured result, fall back to raw output
		match = STRATEGY_RESULT_RE.search(output)
		if match:
			try:
				data = json.loads(match.group(1))
				if isinstance(data, dict):
					return self._format_strategy(data)
			except json.JSONDecodeError:
				pass

		# Use the full output as strategy text
		return output[:4000]

	def _fallback_strategy(self, findings: list[dict], objective: str) -> str:
		"""Build a minimal strategy from raw findings when synthesis fails."""
		lines = [f"# Strategy for: {objective}", ""]
		for f in findings:
			area = f.get("area", "unknown")
			items = f.get("findings", [])
			if items:
				lines.append(f"## {area}")
				for item in items[:5]:
					lines.append(f"- {str(item)[:200]}")
				lines.append("")
		return "\n".join(lines)

	def _format_strategy(self, data: dict) -> str:
		"""Format structured strategy data as readable markdown."""
		lines = ["# Mission Strategy", ""]
		if data.get("summary"):
			lines.extend(["## Problem Summary", str(data["summary"]), ""])
		if data.get("approach"):
			lines.extend(["## Recommended Approach", str(data["approach"]), ""])
		if data.get("risks"):
			lines.append("## Risks and Mitigations")
			for r in data["risks"]:
				lines.append(f"- {r}")
			lines.append("")
		if data.get("execution_order"):
			lines.append("## Execution Order")
			for i, step in enumerate(data["execution_order"], 1):
				lines.append(f"{i}. {step}")
			lines.append("")
		if data.get("open_questions"):
			lines.append("## Open Questions")
			for q in data["open_questions"]:
				lines.append(f"- {q}")
			lines.append("")
		if data.get("anti_patterns"):
			lines.append("## Anti-patterns to Avoid")
			for ap in data["anti_patterns"]:
				lines.append(f"- {ap}")
			lines.append("")
		return "\n".join(lines)

	def _write_strategy(self, strategy: str) -> None:
		"""Write MISSION_STRATEGY.md to disk."""
		target_path = self._config.target.resolved_path
		strategy_path = target_path / "MISSION_STRATEGY.md"
		try:
			strategy_path.write_text(strategy + "\n")
			log.info("Wrote MISSION_STRATEGY.md (%d chars)", len(strategy))
		except OSError as exc:
			log.warning("Could not write MISSION_STRATEGY.md: %s", exc)

	def _store_knowledge(self, findings: list[dict], mission: Mission) -> None:
		"""Store research findings as KnowledgeItems in DB."""
		for f in findings:
			area = f.get("area", "unknown")
			items = f.get("findings", [])
			for item in items[:10]:
				ki = KnowledgeItem(
					mission_id=mission.id,
					source_unit_id="research_phase",
					source_unit_type="research",
					title=f"Research: {area}",
					content=str(item)[:500],
					scope=area,
					confidence=0.7,
				)
				try:
					self._db.insert_knowledge_item(ki)
				except Exception as exc:
					log.debug("Failed to store knowledge item: %s", exc)
