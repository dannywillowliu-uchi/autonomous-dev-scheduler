"""Tests for swarm capability manifest scanning."""

from __future__ import annotations

import json
from pathlib import Path

from autodev.swarm.capabilities import (
	CapabilityManifest,
	_parse_frontmatter,
	_read_hooks,
	_read_mcp_servers,
	_scan_agents,
	_scan_skills,
	scan_capabilities,
)


class TestParseFrontmatter:
	def test_basic(self) -> None:
		text = "---\nname: my-skill\ndescription: does stuff\n---\nbody"
		fm = _parse_frontmatter(text)
		assert fm["name"] == "my-skill"
		assert fm["description"] == "does stuff"

	def test_no_frontmatter(self) -> None:
		assert _parse_frontmatter("no frontmatter here") == {}

	def test_empty_frontmatter(self) -> None:
		fm = _parse_frontmatter("---\n---\nbody")
		assert fm == {}


class TestScanSkills:
	def test_finds_skills(self, tmp_path: Path) -> None:
		skill_dir = tmp_path / "my-skill"
		skill_dir.mkdir()
		(skill_dir / "SKILL.md").write_text("---\nname: my-skill\ndescription: A tool\n---\nBody")
		skills = _scan_skills([tmp_path])
		assert len(skills) == 1
		assert skills[0].name == "my-skill"
		assert skills[0].description == "A tool"
		assert skills[0].invocation == "/my-skill"

	def test_skips_missing_dirs(self) -> None:
		skills = _scan_skills([Path("/nonexistent")])
		assert skills == []

	def test_uses_dirname_as_fallback_name(self, tmp_path: Path) -> None:
		skill_dir = tmp_path / "fallback-skill"
		skill_dir.mkdir()
		(skill_dir / "SKILL.md").write_text("---\ndescription: no name field\n---\n")
		skills = _scan_skills([tmp_path])
		assert skills[0].name == "fallback-skill"

	def test_dedup_across_dirs(self, tmp_path: Path) -> None:
		dir1 = tmp_path / "d1"
		dir2 = tmp_path / "d2"
		for d in [dir1, dir2]:
			sd = d / "dupe"
			sd.mkdir(parents=True)
			(sd / "SKILL.md").write_text("---\nname: dupe\n---\n")
		skills = _scan_skills([dir1, dir2])
		assert len(skills) == 1


class TestScanAgents:
	def test_finds_agents(self, tmp_path: Path) -> None:
		(tmp_path / "builder.md").write_text(
			"---\nname: builder\ndescription: builds things\nmodel: opus\ntools: Read, Write, Bash\n---\n"
		)
		agents = _scan_agents([tmp_path])
		assert len(agents) == 1
		assert agents[0].name == "builder"
		assert agents[0].model == "opus"
		assert agents[0].tools == ["Read", "Write", "Bash"]

	def test_skips_missing_dirs(self) -> None:
		assert _scan_agents([Path("/nonexistent")]) == []

	def test_empty_tools(self, tmp_path: Path) -> None:
		(tmp_path / "simple.md").write_text("---\nname: simple\n---\n")
		agents = _scan_agents([tmp_path])
		assert agents[0].tools == []


class TestReadHooks:
	def test_reads_hooks(self, tmp_path: Path) -> None:
		settings = {
			"hooks": {
				"PreToolUse": [
					{"matcher": "Bash", "type": "prompt", "prompt": "Check safety"}
				]
			}
		}
		path = tmp_path / "settings.json"
		path.write_text(json.dumps(settings))
		hooks = _read_hooks([path])
		assert len(hooks) == 1
		assert hooks[0].event == "PreToolUse"
		assert hooks[0].matcher == "Bash"
		assert hooks[0].hook_type == "prompt"

	def test_missing_file(self) -> None:
		assert _read_hooks([Path("/nonexistent/settings.json")]) == []

	def test_invalid_json(self, tmp_path: Path) -> None:
		path = tmp_path / "settings.json"
		path.write_text("not json")
		assert _read_hooks([path]) == []

	def test_dedup_across_files(self, tmp_path: Path) -> None:
		hook_data = {"hooks": {"PreToolUse": [{"matcher": "Write", "type": "command"}]}}
		p1 = tmp_path / "s1.json"
		p2 = tmp_path / "s2.json"
		p1.write_text(json.dumps(hook_data))
		p2.write_text(json.dumps(hook_data))
		hooks = _read_hooks([p1, p2])
		assert len(hooks) == 1


class TestReadMcpServers:
	def test_reads_stdio_server(self, tmp_path: Path) -> None:
		config = {"mcpServers": {"obsidian": {"command": "mcp-obsidian", "args": []}}}
		path = tmp_path / ".claude.json"
		path.write_text(json.dumps(config))
		servers = _read_mcp_servers([path])
		assert len(servers) == 1
		assert servers[0].name == "obsidian"
		assert servers[0].server_type == "stdio"

	def test_reads_sse_server(self, tmp_path: Path) -> None:
		config = {"mcpServers": {"remote": {"url": "https://example.com/mcp"}}}
		path = tmp_path / ".claude.json"
		path.write_text(json.dumps(config))
		servers = _read_mcp_servers([path])
		assert servers[0].server_type == "sse"

	def test_missing_file(self) -> None:
		assert _read_mcp_servers([Path("/nonexistent")]) == []

	def test_dedup_across_files(self, tmp_path: Path) -> None:
		config = {"mcpServers": {"s1": {"command": "cmd"}}}
		p1 = tmp_path / "a.json"
		p2 = tmp_path / "b.json"
		p1.write_text(json.dumps(config))
		p2.write_text(json.dumps(config))
		servers = _read_mcp_servers([p1, p2])
		assert len(servers) == 1


class TestScanCapabilities:
	def test_full_scan(self, tmp_path: Path) -> None:
		# Set up skills
		skills_dir = tmp_path / ".claude" / "skills" / "my-skill"
		skills_dir.mkdir(parents=True)
		(skills_dir / "SKILL.md").write_text("---\nname: my-skill\ndescription: test\n---\n")

		# Set up hooks
		settings_dir = tmp_path / ".claude"
		(settings_dir / "settings.json").write_text(json.dumps({
			"hooks": {"PostToolUse": [{"matcher": "Edit", "type": "command"}]}
		}))

		# Set up MCP
		(tmp_path / ".mcp.json").write_text(json.dumps({
			"mcpServers": {"local-mcp": {"command": "run-mcp"}}
		}))

		manifest = scan_capabilities(tmp_path)
		assert len(manifest.skills) >= 1
		assert any(s.name == "my-skill" for s in manifest.skills)
		assert len(manifest.hooks) >= 1
		assert len(manifest.mcp_servers) >= 1

	def test_empty_project(self, tmp_path: Path) -> None:
		manifest = scan_capabilities(tmp_path)
		assert isinstance(manifest, CapabilityManifest)
		# May pick up global capabilities, but won't crash

	def test_scan_with_agents(self, tmp_path: Path) -> None:
		agents_dir = tmp_path / ".claude" / "agents"
		agents_dir.mkdir(parents=True)
		(agents_dir / "tester.md").write_text(
			"---\nname: tester\ndescription: runs tests\nmodel: haiku\n---\nRun all tests"
		)
		manifest = scan_capabilities(tmp_path)
		assert any(a.name == "tester" for a in manifest.agents)


class TestCapabilityManifestDefaults:
	def test_default_empty_lists(self) -> None:
		m = CapabilityManifest()
		assert m.skills == []
		assert m.agents == []
		assert m.hooks == []
		assert m.mcp_servers == []


class TestScanSkillsEdgeCases:
	def test_skips_non_directory_entries(self, tmp_path: Path) -> None:
		(tmp_path / "not-a-dir.txt").write_text("hello")
		skills = _scan_skills([tmp_path])
		assert skills == []

	def test_skips_skill_dir_without_skill_md(self, tmp_path: Path) -> None:
		(tmp_path / "some-skill").mkdir()
		(tmp_path / "some-skill" / "README.md").write_text("not a skill file")
		skills = _scan_skills([tmp_path])
		assert skills == []

	def test_oserror_on_read_skipped(self, tmp_path: Path) -> None:
		skill_dir = tmp_path / "bad-skill"
		skill_dir.mkdir()
		skill_file = skill_dir / "SKILL.md"
		skill_file.write_text("---\nname: bad\n---\n")
		# Make the file unreadable to simulate OSError
		skill_file.chmod(0o000)
		try:
			skills = _scan_skills([tmp_path])
			assert not any(s.name == "bad" for s in skills)
		finally:
			skill_file.chmod(0o644)

	def test_empty_description_fallback(self, tmp_path: Path) -> None:
		skill_dir = tmp_path / "no-desc"
		skill_dir.mkdir()
		(skill_dir / "SKILL.md").write_text("---\nname: no-desc\n---\n")
		skills = _scan_skills([tmp_path])
		assert skills[0].description == ""


class TestScanAgentsEdgeCases:
	def test_dedup_across_dirs(self, tmp_path: Path) -> None:
		d1 = tmp_path / "d1"
		d2 = tmp_path / "d2"
		d1.mkdir()
		d2.mkdir()
		(d1 / "runner.md").write_text("---\nname: runner\n---\n")
		(d2 / "runner.md").write_text("---\nname: runner\n---\n")
		agents = _scan_agents([d1, d2])
		assert len(agents) == 1

	def test_uses_filestem_as_fallback_name(self, tmp_path: Path) -> None:
		(tmp_path / "my-agent.md").write_text("---\ndescription: no name\n---\n")
		agents = _scan_agents([tmp_path])
		assert agents[0].name == "my-agent"

	def test_tools_with_whitespace(self, tmp_path: Path) -> None:
		(tmp_path / "a.md").write_text("---\nname: a\ntools: Read , Write , Bash\n---\n")
		agents = _scan_agents([tmp_path])
		assert agents[0].tools == ["Read", "Write", "Bash"]

	def test_oserror_on_read_skipped(self, tmp_path: Path) -> None:
		md = tmp_path / "broken.md"
		md.write_text("---\nname: broken\n---\n")
		md.chmod(0o000)
		try:
			agents = _scan_agents([tmp_path])
			assert not any(a.name == "broken" for a in agents)
		finally:
			md.chmod(0o644)


class TestReadHooksEdgeCases:
	def test_non_dict_hooks_data(self, tmp_path: Path) -> None:
		path = tmp_path / "settings.json"
		path.write_text(json.dumps({"hooks": "not a dict"}))
		assert _read_hooks([path]) == []

	def test_non_list_event_hooks(self, tmp_path: Path) -> None:
		path = tmp_path / "settings.json"
		path.write_text(json.dumps({"hooks": {"PreToolUse": "not a list"}}))
		assert _read_hooks([path]) == []

	def test_non_dict_individual_hook(self, tmp_path: Path) -> None:
		path = tmp_path / "settings.json"
		path.write_text(json.dumps({"hooks": {"PreToolUse": ["not a dict"]}}))
		assert _read_hooks([path]) == []

	def test_default_hook_type_is_command(self, tmp_path: Path) -> None:
		path = tmp_path / "settings.json"
		path.write_text(json.dumps({
			"hooks": {"PreToolUse": [{"matcher": "Bash"}]}
		}))
		hooks = _read_hooks([path])
		assert hooks[0].hook_type == "command"

	def test_oserror_on_read(self, tmp_path: Path) -> None:
		path = tmp_path / "settings.json"
		path.write_text("{}")
		path.chmod(0o000)
		try:
			assert _read_hooks([path]) == []
		finally:
			path.chmod(0o644)


class TestReadMcpServersEdgeCases:
	def test_non_dict_mcp_servers(self, tmp_path: Path) -> None:
		path = tmp_path / "config.json"
		path.write_text(json.dumps({"mcpServers": ["not", "a", "dict"]}))
		assert _read_mcp_servers([path]) == []

	def test_non_dict_server_def(self, tmp_path: Path) -> None:
		path = tmp_path / "config.json"
		path.write_text(json.dumps({"mcpServers": {"bad": "not a dict"}}))
		assert _read_mcp_servers([path]) == []

	def test_non_list_tools(self, tmp_path: Path) -> None:
		path = tmp_path / "config.json"
		path.write_text(json.dumps({
			"mcpServers": {"srv": {"command": "cmd", "tools": "not a list"}}
		}))
		servers = _read_mcp_servers([path])
		assert servers[0].tools == []

	def test_filters_non_string_tools(self, tmp_path: Path) -> None:
		path = tmp_path / "config.json"
		path.write_text(json.dumps({
			"mcpServers": {"srv": {"command": "cmd", "tools": ["read", 42, "write"]}}
		}))
		servers = _read_mcp_servers([path])
		assert servers[0].tools == ["read", "write"]

	def test_invalid_json(self, tmp_path: Path) -> None:
		path = tmp_path / "config.json"
		path.write_text("not json {{{")
		assert _read_mcp_servers([path]) == []

	def test_empty_mcp_servers(self, tmp_path: Path) -> None:
		path = tmp_path / "config.json"
		path.write_text(json.dumps({"mcpServers": {}}))
		assert _read_mcp_servers([path]) == []


class TestParseFrontmatterEdgeCases:
	def test_multiline_values_ignored(self) -> None:
		text = "---\nname: test\nkey: val1\n  continuation\n---\n"
		fm = _parse_frontmatter(text)
		# continuation line should be treated separately (no colon -> skipped)
		assert fm["name"] == "test"
		assert fm["key"] == "val1"

	def test_colon_in_value(self) -> None:
		text = "---\nname: my:skill\n---\n"
		fm = _parse_frontmatter(text)
		assert fm["name"] == "my:skill"
