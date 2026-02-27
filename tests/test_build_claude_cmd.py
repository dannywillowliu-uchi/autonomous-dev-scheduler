"""Tests for build_claude_cmd and MCP/Research config parsing."""

from __future__ import annotations

from pathlib import Path

from mission_control.config import (
	MCPConfig,
	MissionConfig,
	TargetConfig,
	build_claude_cmd,
	load_config,
)


def _config(**kwargs) -> MissionConfig:
	cfg = MissionConfig()
	cfg.target = TargetConfig(name="test", path="/tmp/test", objective="Build API")
	for k, v in kwargs.items():
		setattr(cfg, k, v)
	return cfg


class TestBuildClaudeCmd:
	def test_minimal_cmd(self) -> None:
		"""Basic command with just model."""
		cfg = _config()
		cmd = build_claude_cmd(cfg, model="sonnet")
		assert cmd == ["claude", "-p", "--output-format", "text", "--model", "sonnet"]

	def test_with_budget(self) -> None:
		cmd = build_claude_cmd(_config(), model="opus", budget=5.0)
		assert "--max-budget-usd" in cmd
		assert "5.0" in cmd

	def test_with_max_turns(self) -> None:
		cmd = build_claude_cmd(_config(), model="haiku", max_turns=10)
		assert "--max-turns" in cmd
		assert "10" in cmd

	def test_with_permission_mode(self) -> None:
		cmd = build_claude_cmd(_config(), model="sonnet", permission_mode="plan")
		assert "--permission-mode" in cmd
		assert "plan" in cmd

	def test_with_session_id(self) -> None:
		cmd = build_claude_cmd(_config(), model="sonnet", session_id="sess-123")
		assert "--session-id" in cmd
		assert "sess-123" in cmd

	def test_with_prompt_appended(self) -> None:
		cmd = build_claude_cmd(_config(), model="sonnet", prompt="do the thing")
		assert cmd[-1] == "do the thing"

	def test_output_format_override(self) -> None:
		cmd = build_claude_cmd(
			_config(), model="sonnet", output_format="stream-json",
		)
		assert "--output-format" in cmd
		idx = cmd.index("--output-format")
		assert cmd[idx + 1] == "stream-json"

	def test_resume_session(self) -> None:
		cmd = build_claude_cmd(_config(), model="sonnet", resume_session="rs-456")
		assert "--resume" in cmd
		assert "rs-456" in cmd
		assert "-p" in cmd

	def test_mcp_config_included_when_enabled(self) -> None:
		cfg = _config(mcp=MCPConfig(config_path="/home/user/.claude/mcp.json", enabled=True))
		cmd = build_claude_cmd(cfg, model="sonnet")
		assert "--mcp-config" in cmd
		idx = cmd.index("--mcp-config")
		assert cmd[idx + 1] == "/home/user/.claude/mcp.json"

	def test_mcp_config_excluded_when_disabled(self) -> None:
		cfg = _config(mcp=MCPConfig(config_path="/some/path.json", enabled=False))
		cmd = build_claude_cmd(cfg, model="sonnet")
		assert "--mcp-config" not in cmd

	def test_mcp_config_excluded_when_empty_path(self) -> None:
		cfg = _config(mcp=MCPConfig(config_path="", enabled=True))
		cmd = build_claude_cmd(cfg, model="sonnet")
		assert "--mcp-config" not in cmd

	def test_mcp_config_tilde_expansion(self) -> None:
		cfg = _config(mcp=MCPConfig(config_path="~/mcp.json", enabled=True))
		cmd = build_claude_cmd(cfg, model="sonnet")
		assert "--mcp-config" in cmd
		idx = cmd.index("--mcp-config")
		assert "~" not in cmd[idx + 1]

	def test_all_flags_combined(self) -> None:
		cfg = _config(mcp=MCPConfig(config_path="/mcp.json", enabled=True))
		cmd = build_claude_cmd(
			cfg, model="opus", budget=10.0, max_turns=5,
			permission_mode="plan", session_id="s1",
		)
		assert "--model" in cmd
		assert "--max-budget-usd" in cmd
		assert "--max-turns" in cmd
		assert "--permission-mode" in cmd
		assert "--session-id" in cmd
		assert "--mcp-config" in cmd


class TestMCPConfigParsing:
	def test_mcp_from_toml(self, tmp_path: Path) -> None:
		toml = tmp_path / "mission-control.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"
objective = "build"

[mcp]
config_path = "~/.claude/settings.local.json"
enabled = true
""")
		cfg = load_config(toml)
		assert cfg.mcp.config_path == "~/.claude/settings.local.json"
		assert cfg.mcp.enabled is True

	def test_mcp_disabled_from_toml(self, tmp_path: Path) -> None:
		toml = tmp_path / "mission-control.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"
objective = "build"

[mcp]
config_path = "/some/path"
enabled = false
""")
		cfg = load_config(toml)
		assert cfg.mcp.enabled is False

	def test_mcp_defaults_when_omitted(self, tmp_path: Path) -> None:
		toml = tmp_path / "mission-control.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"
objective = "build"
""")
		cfg = load_config(toml)
		assert cfg.mcp.config_path == ""
		assert cfg.mcp.enabled is True


class TestResearchConfigParsing:
	def test_research_from_toml(self, tmp_path: Path) -> None:
		toml = tmp_path / "mission-control.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"
objective = "build"

[research]
enabled = true
budget_per_agent_usd = 2.5
timeout = 600
model = "opus"
""")
		cfg = load_config(toml)
		assert cfg.research.enabled is True
		assert cfg.research.budget_per_agent_usd == 2.5
		assert cfg.research.timeout == 600
		assert cfg.research.model == "opus"

	def test_research_disabled(self, tmp_path: Path) -> None:
		toml = tmp_path / "mission-control.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"
objective = "build"

[research]
enabled = false
""")
		cfg = load_config(toml)
		assert cfg.research.enabled is False

	def test_research_defaults_when_omitted(self, tmp_path: Path) -> None:
		toml = tmp_path / "mission-control.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"
objective = "build"
""")
		cfg = load_config(toml)
		assert cfg.research.enabled is True
		assert cfg.research.budget_per_agent_usd == 1.0
		assert cfg.research.timeout == 300
		assert cfg.research.model == ""


class TestEnvAllowlist:
	def test_agent_teams_env_passes_through(self, tmp_path: Path) -> None:
		"""CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS passes through env."""
		from mission_control.config import _ENV_ALLOWLIST

		assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in _ENV_ALLOWLIST
