"""Relevance evaluator and proposal generator for intelligence findings."""

from __future__ import annotations

from autodev.intelligence.models import (
	AdaptationProposal,
	Finding,
)

# Weighted keyword lists per category. Each keyword has a weight (higher = more relevant).
# Categories map to proposal_type and target_modules for proposal generation.
RELEVANCE_KEYWORDS: dict[str, dict[str, float]] = {
	"mcp": {
		"mcp": 1.0,
		"model context protocol": 1.0,
		"mcp server": 0.9,
		"mcp tool": 0.8,
		"tool server": 0.6,
	},
	"agent_coordination": {
		"multi-agent": 1.0,
		"agent coordination": 1.0,
		"agent orchestration": 0.9,
		"swarm": 0.8,
		"agent pool": 0.8,
		"task dispatch": 0.7,
		"parallel agents": 0.7,
		"agent communication": 0.7,
	},
	"claude_code": {
		"claude code": 1.0,
		"claude cli": 0.9,
		"anthropic cli": 0.8,
		"claude subprocess": 0.7,
		"claude session": 0.7,
	},
	"autonomous_coding": {
		"autonomous coding": 1.0,
		"autonomous development": 1.0,
		"self-improving": 0.9,
		"auto-coding": 0.8,
		"code generation": 0.6,
		"agentic coding": 0.9,
		"continuous development": 0.7,
		"ai developer": 0.6,
	},
	"tool_use": {
		"tool use": 1.0,
		"function calling": 0.9,
		"tool calling": 0.9,
		"tool integration": 0.8,
		"api integration": 0.6,
		"tool chain": 0.7,
	},
	"claude_code_release": {
		"breaking change": 1.0,
		"new hook": 1.0,
		"new skill": 1.0,
		"permission mode": 1.0,
		"mcp server": 1.0,
		"subagent": 0.7,
		"worktree": 0.7,
		"teams": 0.7,
		"spawn": 0.7,
		"cli flag": 0.7,
		"bugfix": 0.4,
		"performance": 0.4,
		"documentation": 0.4,
	},
}

# Maps category -> proposal_type
_CATEGORY_TO_TYPE: dict[str, str] = {
	"mcp": "integration",
	"agent_coordination": "architecture",
	"claude_code": "pattern",
	"autonomous_coding": "architecture",
	"tool_use": "integration",
	"claude_code_release": "integration",
}

# Maps category -> likely target modules in autodev
_CATEGORY_TO_MODULES: dict[str, list[str]] = {
	"mcp": ["mcp_server.py", "config.py"],
	"agent_coordination": ["continuous_controller.py", "scheduler.py", "backends/"],
	"claude_code": ["session.py", "worker.py"],
	"autonomous_coding": ["deliberative_planner.py", "continuous_controller.py"],
	"tool_use": ["mcp_server.py", "worker.py", "config.py"],
	"claude_code_release": ["swarm/controller.py", "config.py", "swarm/capabilities.py", "auto_update.py"],
}

# Maps proposal_type -> effort_estimate
_TYPE_TO_EFFORT: dict[str, str] = {
	"integration": "medium",
	"pattern": "small",
	"architecture": "large",
}

_HIGH_RISK_KEYWORDS = {"architecture", "spawn", "config schema", "breaking", "breaking change"}

# Claude Code release-specific risk classification
_CLAUDE_CODE_LOW_RISK_KEYWORDS = {
	"new skill", "documentation", "docs", "test", "bugfix", "bug fix",
	"performance", "minor", "improvement", "typo", "readme",
}

_CLAUDE_CODE_HIGH_RISK_KEYWORDS = {
	"architecture", "spawn", "config schema", "breaking change", "breaking",
	"permission mode", "security", "authentication", "subprocess",
	"config format", "deprecated", "removed",
}

# Maps feature areas mentioned in release notes to autodev modules that would need updating.
_FEATURE_TO_MODULES: dict[str, list[str]] = {
	"skill": ["swarm/capabilities.py", "swarm/worker_prompt.py"],
	"hook": ["swarm/capabilities.py", "config.py"],
	"mcp": ["mcp_server.py", "mcp_registry.py", "config.py"],
	"agent": ["swarm/controller.py", "swarm/models.py"],
	"permission": ["config.py", "session.py"],
	"worktree": ["swarm/controller.py", "backends/local.py"],
	"spawn": ["config.py", "session.py", "swarm/controller.py"],
	"teams": ["swarm/controller.py", "swarm/context.py"],
	"inbox": ["swarm/controller.py", "swarm/context.py"],
	"cli": ["cli.py", "config.py"],
	"config": ["config.py"],
	"subagent": ["swarm/controller.py", "session.py"],
	"tool": ["mcp_server.py", "tool_synthesis.py"],
}


def _score_text(text: str, keywords: dict[str, float]) -> float:
	"""Score a text against a keyword dict. Returns sum of weights for matched keywords."""
	lower = text.lower()
	return sum(weight for kw, weight in keywords.items() if kw in lower)


def _classify_risk(text: str, category: str = "") -> str:
	"""Classify risk level based on high-risk keyword presence.

	For claude_code_release findings, uses category-specific keyword sets
	that distinguish low-risk changes (docs, skills, tests) from high-risk
	ones (architecture, spawn, config, security).
	"""
	lower = text.lower()
	if category == "claude_code_release":
		high_hits = sum(1 for kw in _CLAUDE_CODE_HIGH_RISK_KEYWORDS if kw in lower)
		low_hits = sum(1 for kw in _CLAUDE_CODE_LOW_RISK_KEYWORDS if kw in lower)
		if high_hits > 0 and high_hits >= low_hits:
			return "high"
		return "low"
	for kw in _HIGH_RISK_KEYWORDS:
		if kw in lower:
			return "high"
	return "low"


def _resolve_target_modules(text: str, fallback: list[str]) -> list[str]:
	"""Resolve target modules from feature-area keywords in the text.

	Scans the text for feature-area keywords from _FEATURE_TO_MODULES and
	collects the corresponding autodev modules. Falls back to the provided
	default list if no feature areas are matched.
	"""
	lower = text.lower()
	modules: list[str] = []
	seen: set[str] = set()
	for feature, mods in _FEATURE_TO_MODULES.items():
		if feature in lower:
			for m in mods:
				if m not in seen:
					modules.append(m)
					seen.add(m)
	return modules if modules else list(fallback)


def evaluate_findings(findings: list[Finding]) -> list[Finding]:
	"""Score each finding's relevance to autodev using keyword matching.

	Updates each finding's relevance_score based on title + summary keyword hits
	across all categories. Returns findings sorted by relevance_score descending.
	"""
	for finding in findings:
		combined = f"{finding.title} {finding.summary}"
		total = 0.0
		for keywords in RELEVANCE_KEYWORDS.values():
			total += _score_text(combined, keywords)
		finding.relevance_score = total

	return sorted(findings, key=lambda f: f.relevance_score, reverse=True)


def generate_proposals(findings: list[Finding], threshold: float = 0.3) -> list[AdaptationProposal]:
	"""Generate adaptation proposals from findings that meet the relevance threshold.

	For each finding with relevance_score >= threshold, creates an AdaptationProposal
	with type, target_modules, priority, and effort inferred from keyword category matches.
	"""
	proposals: list[AdaptationProposal] = []

	for finding in findings:
		if finding.relevance_score < threshold:
			continue

		# Find the best-matching category
		combined = f"{finding.title} {finding.summary}"
		best_category = ""
		best_score = 0.0
		for category, keywords in RELEVANCE_KEYWORDS.items():
			cat_score = _score_text(combined, keywords)
			if cat_score > best_score:
				best_score = cat_score
				best_category = category

		if not best_category:
			continue

		proposal_type = _CATEGORY_TO_TYPE.get(best_category, "integration")
		target_modules = list(_CATEGORY_TO_MODULES.get(best_category, []))
		effort_estimate = _TYPE_TO_EFFORT.get(proposal_type, "medium")

		# Priority 1-5 based on relevance_score bands
		score = finding.relevance_score
		if score >= 3.0:
			priority = 1
		elif score >= 2.0:
			priority = 2
		elif score >= 1.0:
			priority = 3
		elif score >= 0.5:
			priority = 4
		else:
			priority = 5

		risk_level = _classify_risk(combined, category=best_category)

		# For claude_code_release, resolve modules from feature-area keywords
		if best_category == "claude_code_release":
			target_modules = _resolve_target_modules(combined, target_modules)

		proposal = AdaptationProposal(
			finding_id=finding.id,
			title=f"Adapt: {finding.title}",
			description=f"Based on finding from {finding.source}: {finding.summary}",
			proposal_type=proposal_type,
			target_modules=target_modules,
			priority=priority,
			effort_estimate=effort_estimate,
			risk_level=risk_level,
		)
		proposals.append(proposal)

	return proposals
