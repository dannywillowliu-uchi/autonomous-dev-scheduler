"""Capability manifest -- scans for skills, agents, hooks, and MCP servers."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SkillInfo:
	name: str
	description: str
	path: str
	invocation: str  # e.g. '/<name>'


@dataclass
class AgentDefInfo:
	name: str
	description: str
	model: str
	tools: list[str]


@dataclass
class HookInfo:
	event: str  # e.g. 'PreToolUse', 'PostToolUse'
	matcher: str
	hook_type: str  # 'command' or 'prompt'


@dataclass
class MCPInfo:
	name: str
	server_type: str  # 'stdio' or 'sse'
	tools: list[str]


@dataclass
class CapabilityManifest:
	skills: list[SkillInfo] = field(default_factory=list)
	agents: list[AgentDefInfo] = field(default_factory=list)
	hooks: list[HookInfo] = field(default_factory=list)
	mcp_servers: list[MCPInfo] = field(default_factory=list)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
	"""Parse YAML-like frontmatter from a markdown file (simple key: value pairs)."""
	m = _FRONTMATTER_RE.match(text)
	if not m:
		return {}
	result: dict[str, str] = {}
	for line in m.group(1).splitlines():
		line = line.strip()
		if ":" in line:
			key, _, value = line.partition(":")
			result[key.strip()] = value.strip()
	return result


def _scan_skills(dirs: list[Path]) -> list[SkillInfo]:
	"""Scan directories for SKILL.md files and parse their frontmatter."""
	skills: list[SkillInfo] = []
	seen: set[str] = set()
	for base_dir in dirs:
		if not base_dir.is_dir():
			continue
		for skill_dir in sorted(base_dir.iterdir()):
			if not skill_dir.is_dir():
				continue
			skill_file = skill_dir / "SKILL.md"
			if not skill_file.exists():
				continue
			try:
				fm = _parse_frontmatter(skill_file.read_text())
			except OSError:
				continue
			name = fm.get("name", skill_dir.name)
			if name in seen:
				continue
			seen.add(name)
			skills.append(SkillInfo(
				name=name,
				description=fm.get("description", ""),
				path=str(skill_dir),
				invocation=f"/{name}",
			))
	return skills


def _scan_agents(dirs: list[Path]) -> list[AgentDefInfo]:
	"""Scan directories for agent definition .md files."""
	agents: list[AgentDefInfo] = []
	seen: set[str] = set()
	for base_dir in dirs:
		if not base_dir.is_dir():
			continue
		for md_file in sorted(base_dir.glob("*.md")):
			try:
				fm = _parse_frontmatter(md_file.read_text())
			except OSError:
				continue
			name = fm.get("name", md_file.stem)
			if name in seen:
				continue
			seen.add(name)
			tools_raw = fm.get("tools", "")
			tools = [t.strip() for t in tools_raw.split(",") if t.strip()] if tools_raw else []
			agents.append(AgentDefInfo(
				name=name,
				description=fm.get("description", ""),
				model=fm.get("model", ""),
				tools=tools,
			))
	return agents


def _read_hooks(paths: list[Path]) -> list[HookInfo]:
	"""Read hook definitions from settings.json files."""
	hooks: list[HookInfo] = []
	seen: set[tuple[str, str]] = set()
	for settings_path in paths:
		if not settings_path.is_file():
			continue
		try:
			data = json.loads(settings_path.read_text())
		except (json.JSONDecodeError, OSError):
			continue
		hooks_data = data.get("hooks", {})
		if not isinstance(hooks_data, dict):
			continue
		for event, event_hooks in hooks_data.items():
			if not isinstance(event_hooks, list):
				continue
			for hook in event_hooks:
				if not isinstance(hook, dict):
					continue
				matcher = hook.get("matcher", "")
				hook_type = hook.get("type", "command")
				key = (event, matcher)
				if key in seen:
					continue
				seen.add(key)
				hooks.append(HookInfo(
					event=event,
					matcher=matcher,
					hook_type=hook_type,
				))
	return hooks


def _read_mcp_servers(paths: list[Path]) -> list[MCPInfo]:
	"""Read MCP server definitions from config files."""
	servers: list[MCPInfo] = []
	seen: set[str] = set()
	for config_path in paths:
		if not config_path.is_file():
			continue
		try:
			data = json.loads(config_path.read_text())
		except (json.JSONDecodeError, OSError):
			continue
		mcp_data = data.get("mcpServers", {})
		if not isinstance(mcp_data, dict):
			continue
		for name, server_def in mcp_data.items():
			if name in seen:
				continue
			seen.add(name)
			if not isinstance(server_def, dict):
				continue
			server_type = "sse" if "url" in server_def else "stdio"
			tools = server_def.get("tools", [])
			if not isinstance(tools, list):
				tools = []
			servers.append(MCPInfo(
				name=name,
				server_type=server_type,
				tools=[t for t in tools if isinstance(t, str)],
			))
	return servers


def scan_capabilities(project_path: Path) -> CapabilityManifest:
	"""Scan for all available capabilities (skills, agents, hooks, MCP servers).

	Scans both global (~/.claude/) and project-local directories.
	Project-local entries take precedence over global ones for deduplication.
	"""
	home_claude = Path.home() / ".claude"
	project_claude = project_path / ".claude"

	# Project-local dirs first so they win dedup
	skill_dirs = [
		project_claude / "skills",
		home_claude / "skills",
	]
	agent_dirs = [
		project_claude / "agents",
		home_claude / "agents",
	]
	settings_paths = [
		project_claude / "settings.json",
		home_claude / "settings.json",
	]
	mcp_paths = [
		project_path / ".mcp.json",
		home_claude.parent / ".claude.json",
	]

	return CapabilityManifest(
		skills=_scan_skills(skill_dirs),
		agents=_scan_agents(agent_dirs),
		hooks=_read_hooks(settings_paths),
		mcp_servers=_read_mcp_servers(mcp_paths),
	)
