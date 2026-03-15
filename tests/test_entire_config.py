"""Tests for Entire.io config: dataclass defaults, TOML parsing, builder, validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autodev.config import (
	BackendConfig,
	EntireConfig,
	MissionConfig,
	_build_entire,
	load_config,
	validate_config,
)


def _make_git_repo(path: Path) -> None:
	"""Initialize a bare git repo at the given path."""
	path.mkdir(parents=True, exist_ok=True)
	subprocess.run(["git", "init", str(path)], capture_output=True, check=True)


def _valid_entire_config(tmp_path: Path, *, api_key: str = "ek-test-123") -> MissionConfig:
	"""Build a MissionConfig with entire backend that passes validation."""
	repo = tmp_path / "repo"
	_make_git_repo(repo)
	cfg = MissionConfig()
	cfg.target.name = "test"
	cfg.target.path = str(repo)
	cfg.target.verification.command = "git status"
	cfg.notifications.telegram.on_heartbeat = False
	cfg.notifications.telegram.on_merge_fail = False
	cfg.notifications.telegram.on_mission_end = False
	cfg.backend.type = "entire"
	cfg.backend.entire.api_key = api_key
	cfg.backend.entire.environment_template = "tpl-default"
	return cfg


class TestEntireConfigDefaults:
	def test_defaults(self) -> None:
		ec = EntireConfig()
		assert ec.api_key == ""
		assert ec.api_base_url == "https://api.entire.io"
		assert ec.org_id == ""
		assert ec.environment_template == ""
		assert ec.startup_timeout == 120
		assert ec.machine_type == ""
		assert ec.region == ""
		assert ec.persist_environments is False

	def test_backend_config_has_entire_field(self) -> None:
		bc = BackendConfig()
		assert isinstance(bc.entire, EntireConfig)
		assert bc.entire.api_key == ""


class TestEntireTomlParsing:
	def test_full_entire_section(self, tmp_path: Path) -> None:
		toml = tmp_path / "autodev.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"

[backend]
type = "entire"

[backend.entire]
api_key = "ek-abc-123"
api_base_url = "https://custom.entire.io"
org_id = "org-xyz"
environment_template = "tpl-python311"
startup_timeout = 180
machine_type = "large"
region = "eu-west-1"
persist_environments = true
""")
		cfg = load_config(toml)
		assert cfg.backend.type == "entire"
		ec = cfg.backend.entire
		assert ec.api_key == "ek-abc-123"
		assert ec.api_base_url == "https://custom.entire.io"
		assert ec.org_id == "org-xyz"
		assert ec.environment_template == "tpl-python311"
		assert ec.startup_timeout == 180
		assert ec.machine_type == "large"
		assert ec.region == "eu-west-1"
		assert ec.persist_environments is True

	def test_minimal_entire_section(self, tmp_path: Path) -> None:
		toml = tmp_path / "autodev.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"

[backend]
type = "entire"

[backend.entire]
api_key = "ek-minimal"
""")
		cfg = load_config(toml)
		assert cfg.backend.type == "entire"
		ec = cfg.backend.entire
		assert ec.api_key == "ek-minimal"
		assert ec.api_base_url == "https://api.entire.io"
		assert ec.startup_timeout == 120
		assert ec.persist_environments is False

	def test_entire_section_ignored_when_type_local(self, tmp_path: Path) -> None:
		"""[backend.entire] is populated but type stays 'local'."""
		toml = tmp_path / "autodev.toml"
		toml.write_text("""\
[target]
name = "test"
path = "/tmp/test"

[backend]
type = "local"

[backend.entire]
api_key = "ek-unused"
""")
		cfg = load_config(toml)
		assert cfg.backend.type == "local"
		# The entire field is still populated from TOML
		assert cfg.backend.entire.api_key == "ek-unused"


class TestBuildEntire:
	def test_all_fields(self) -> None:
		data = {
			"api_key": "ek-full-key",
			"api_base_url": "https://custom.api",
			"org_id": "org-123",
			"environment_template": "tpl-rust",
			"startup_timeout": 240,
			"machine_type": "medium",
			"region": "ap-southeast-1",
			"persist_environments": True,
		}
		ec = _build_entire(data)
		assert ec.api_key == "ek-full-key"
		assert ec.api_base_url == "https://custom.api"
		assert ec.org_id == "org-123"
		assert ec.environment_template == "tpl-rust"
		assert ec.startup_timeout == 240
		assert ec.machine_type == "medium"
		assert ec.region == "ap-southeast-1"
		assert ec.persist_environments is True

	def test_minimal_just_api_key(self) -> None:
		data = {"api_key": "ek-only-key"}
		ec = _build_entire(data)
		assert ec.api_key == "ek-only-key"
		assert ec.api_base_url == "https://api.entire.io"
		assert ec.org_id == ""
		assert ec.environment_template == ""
		assert ec.startup_timeout == 120
		assert ec.machine_type == ""
		assert ec.region == ""
		assert ec.persist_environments is False

	def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""Empty api_key in data picks up ENTIRE_API_KEY from env."""
		monkeypatch.setenv("ENTIRE_API_KEY", "ek-from-env")
		ec = _build_entire({})
		assert ec.api_key == "ek-from-env"

	def test_explicit_key_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""Explicit api_key takes precedence over env var."""
		monkeypatch.setenv("ENTIRE_API_KEY", "ek-from-env")
		ec = _build_entire({"api_key": "ek-explicit"})
		assert ec.api_key == "ek-explicit"

	def test_no_key_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""Without api_key or env var, api_key stays empty."""
		monkeypatch.delenv("ENTIRE_API_KEY", raising=False)
		ec = _build_entire({})
		assert ec.api_key == ""


class TestEntireValidation:
	def test_error_when_api_key_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
		"""Entire backend without api_key produces an error."""
		monkeypatch.delenv("ENTIRE_API_KEY", raising=False)
		cfg = _valid_entire_config(tmp_path, api_key="")
		issues = validate_config(cfg)
		errors = [msg for lvl, msg in issues if lvl == "error"]
		assert any("api_key" in e for e in errors)

	def test_no_error_when_api_key_set(self, tmp_path: Path) -> None:
		"""Entire backend with api_key produces no api_key error."""
		cfg = _valid_entire_config(tmp_path, api_key="ek-valid")
		issues = validate_config(cfg)
		errors = [msg for lvl, msg in issues if lvl == "error"]
		assert not any("api_key" in e for e in errors)

	def test_warning_when_environment_template_empty(self, tmp_path: Path) -> None:
		"""Entire backend without environment_template produces a warning."""
		cfg = _valid_entire_config(tmp_path)
		cfg.backend.entire.environment_template = ""
		issues = validate_config(cfg)
		warnings = [msg for lvl, msg in issues if lvl == "warning"]
		assert any("environment_template" in w for w in warnings)

	def test_no_warning_when_environment_template_set(self, tmp_path: Path) -> None:
		"""Entire backend with environment_template produces no template warning."""
		cfg = _valid_entire_config(tmp_path)
		cfg.backend.entire.environment_template = "tpl-test"
		issues = validate_config(cfg)
		warnings = [msg for lvl, msg in issues if lvl == "warning"]
		assert not any("environment_template" in w for w in warnings)

	def test_error_when_api_base_url_empty(self, tmp_path: Path) -> None:
		"""Entire backend with empty api_base_url produces an error."""
		cfg = _valid_entire_config(tmp_path)
		cfg.backend.entire.api_base_url = ""
		issues = validate_config(cfg)
		errors = [msg for lvl, msg in issues if lvl == "error"]
		assert any("api_base_url" in e for e in errors)

	def test_no_entire_checks_when_type_local(self, tmp_path: Path) -> None:
		"""Entire validation skipped when backend type is local."""
		cfg = _valid_entire_config(tmp_path, api_key="")
		cfg.backend.type = "local"
		issues = validate_config(cfg)
		errors = [msg for lvl, msg in issues if lvl == "error"]
		assert not any("api_key" in e for e in errors)
