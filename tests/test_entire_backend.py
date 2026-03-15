"""Tests for EntireBackend: instantiation, API key resolution, NotImplementedError stubs, controller wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autodev.backends.base import WorkerHandle
from autodev.backends.entire import EntireBackend
from autodev.config import EntireConfig, MissionConfig


def _make_config(**kwargs) -> EntireConfig:
	"""Build an EntireConfig with defaults overridden by kwargs."""
	defaults = {"api_key": "ek-test-123"}
	defaults.update(kwargs)
	return EntireConfig(**defaults)


class TestEntireBackendInit:
	def test_instantiation_with_api_key(self) -> None:
		config = _make_config(api_key="ek-my-key")
		backend = EntireBackend(config)
		assert backend._api_key == "ek-my-key"
		assert backend._config is config
		assert backend._max_output_mb == 50

	def test_instantiation_with_custom_max_output(self) -> None:
		config = _make_config()
		backend = EntireBackend(config, max_output_mb=100)
		assert backend._max_output_mb == 100

	def test_environments_dict_starts_empty(self) -> None:
		backend = EntireBackend(_make_config())
		assert backend._environments == {}


class TestResolveApiKey:
	def test_returns_config_key(self) -> None:
		config = _make_config(api_key="ek-config-key")
		backend = EntireBackend(config)
		assert backend._api_key == "ek-config-key"

	def test_falls_back_to_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
		monkeypatch.setenv("ENTIRE_API_KEY", "ek-env-key")
		config = _make_config(api_key="")
		backend = EntireBackend(config)
		assert backend._api_key == "ek-env-key"

	def test_raises_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
		monkeypatch.delenv("ENTIRE_API_KEY", raising=False)
		config = _make_config(api_key="")
		with pytest.raises(ValueError, match="API key not configured"):
			EntireBackend(config)


class TestNotImplementedStubs:
	@pytest.fixture()
	def backend(self) -> EntireBackend:
		return EntireBackend(_make_config())

	@pytest.fixture()
	def handle(self) -> WorkerHandle:
		return WorkerHandle(worker_id="w-1", pid=None, workspace_path="/tmp/ws")

	@pytest.mark.asyncio
	async def test_initialize(self, backend: EntireBackend) -> None:
		with pytest.raises(NotImplementedError):
			await backend.initialize()

	@pytest.mark.asyncio
	async def test_provision_workspace(self, backend: EntireBackend) -> None:
		with pytest.raises(NotImplementedError):
			await backend.provision_workspace("w-1", "/repo", "main")

	@pytest.mark.asyncio
	async def test_spawn(self, backend: EntireBackend) -> None:
		with pytest.raises(NotImplementedError):
			await backend.spawn("w-1", "/tmp/ws", ["echo", "hi"], 300)

	@pytest.mark.asyncio
	async def test_check_status(self, backend: EntireBackend, handle: WorkerHandle) -> None:
		with pytest.raises(NotImplementedError):
			await backend.check_status(handle)

	@pytest.mark.asyncio
	async def test_get_output(self, backend: EntireBackend, handle: WorkerHandle) -> None:
		with pytest.raises(NotImplementedError):
			await backend.get_output(handle)

	@pytest.mark.asyncio
	async def test_kill(self, backend: EntireBackend, handle: WorkerHandle) -> None:
		with pytest.raises(NotImplementedError):
			await backend.kill(handle)

	@pytest.mark.asyncio
	async def test_release_workspace(self, backend: EntireBackend) -> None:
		with pytest.raises(NotImplementedError):
			await backend.release_workspace("/tmp/ws")

	@pytest.mark.asyncio
	async def test_cleanup(self, backend: EntireBackend) -> None:
		with pytest.raises(NotImplementedError):
			await backend.cleanup()

	@pytest.mark.asyncio
	async def test_api_request(self, backend: EntireBackend) -> None:
		with pytest.raises(NotImplementedError):
			await backend._api_request("GET", "/health")
