"""Structured trace logging for worker lifecycle events.

Writes JSON-lines to a configurable trace file with optional rotation.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TraceLogConfig:
	"""Configuration for trace logging."""

	enabled: bool = False
	path: str = "trace.jsonl"
	max_file_size: int = 50_000_000


@dataclass
class TraceEvent:
	"""A single trace event recording a worker lifecycle action."""

	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
	worker_id: str = ""
	unit_id: str = ""
	event_type: str = ""
	details: dict[str, Any] = field(default_factory=dict)

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> TraceEvent:
		known = {"timestamp", "worker_id", "unit_id", "event_type", "details"}
		filtered = {k: v for k, v in data.items() if k in known}
		return cls(**filtered)


class TraceLogger:
	"""Append-only JSONL trace logger with optional file rotation.

	Thread-safe: all writes are serialized via a lock.
	When disabled, all operations are silent no-ops.
	"""

	def __init__(self, config: TraceLogConfig) -> None:
		self._config = config
		self._lock = threading.Lock()

	@property
	def enabled(self) -> bool:
		return self._config.enabled

	def write(self, event: TraceEvent) -> None:
		if not self._config.enabled:
			return
		with self._lock:
			path = Path(self._config.path)
			self._maybe_rotate(path)
			with open(path, "a") as f:
				f.write(json.dumps(event.to_dict()) + "\n")

	def _maybe_rotate(self, path: Path) -> None:
		if not path.exists():
			return
		if self._config.max_file_size <= 0:
			return
		if path.stat().st_size >= self._config.max_file_size:
			rotated = path.with_suffix(path.suffix + ".1")
			path.rename(rotated)
