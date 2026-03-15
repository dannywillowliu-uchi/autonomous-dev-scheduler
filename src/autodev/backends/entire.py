"""Entire.io cloud backend -- runs workers in cloud environments."""

from __future__ import annotations

import logging
import os

from autodev.backends.base import WorkerBackend, WorkerHandle
from autodev.config import EntireConfig

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


class EntireBackend(WorkerBackend):
	"""Execute workers in Entire.io cloud environments.

	Phase 1 skeleton -- all methods raise NotImplementedError until
	the Entire.io API is documented and available.
	"""

	def __init__(
		self,
		config: EntireConfig,
		max_output_mb: int = 50,
	) -> None:
		self._config = config
		self._max_output_mb = max_output_mb
		self._api_key = self._resolve_api_key()
		self._environments: dict[str, str] = {}  # worker_id -> env_id

	def _resolve_api_key(self) -> str:
		"""Resolve API key from config or environment."""
		if self._config.api_key:
			return self._config.api_key
		env_key = os.environ.get("ENTIRE_API_KEY", "")
		if env_key:
			return env_key
		raise ValueError(
			"Entire.io API key not configured. Set backend.entire.api_key "
			"in config or ENTIRE_API_KEY environment variable."
		)

	async def _api_request(
		self, method: str, path: str, body: dict | None = None
	) -> dict:
		"""HTTP wrapper with auth header, retries, timeout."""
		raise NotImplementedError("Entire.io API not yet available")

	async def initialize(self, warm_count: int = 0) -> None:
		"""Validate API connectivity."""
		raise NotImplementedError("Entire.io API not yet available")

	async def provision_workspace(
		self, worker_id: str, source_repo: str, base_branch: str
	) -> str:
		raise NotImplementedError("Entire.io API not yet available")

	async def spawn(
		self, worker_id: str, workspace_path: str, command: list[str], timeout: int
	) -> WorkerHandle:
		raise NotImplementedError("Entire.io API not yet available")

	async def check_status(self, handle: WorkerHandle) -> str:
		raise NotImplementedError("Entire.io API not yet available")

	async def get_output(self, handle: WorkerHandle) -> str:
		raise NotImplementedError("Entire.io API not yet available")

	async def kill(self, handle: WorkerHandle) -> None:
		raise NotImplementedError("Entire.io API not yet available")

	async def release_workspace(self, workspace_path: str) -> None:
		raise NotImplementedError("Entire.io API not yet available")

	async def cleanup(self) -> None:
		raise NotImplementedError("Entire.io API not yet available")
