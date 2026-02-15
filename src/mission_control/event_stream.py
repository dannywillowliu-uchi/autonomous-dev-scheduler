"""JSONL event stream for structured post-mission analysis."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


class EventStream:
	"""Append-only JSONL writer for mission events.

	Complements the DB event store with a portable, jq-friendly format.
	"""

	def __init__(self, path: Path) -> None:
		self._path = path
		self._file: IO[str] | None = None

	def open(self) -> None:
		self._path.parent.mkdir(parents=True, exist_ok=True)
		self._file = self._path.open("a", encoding="utf-8")

	def close(self) -> None:
		if self._file is not None:
			self._file.close()
			self._file = None

	def emit(
		self,
		event_type: str,
		*,
		mission_id: str = "",
		epoch_id: str = "",
		unit_id: str = "",
		worker_id: str = "",
		details: dict[str, Any] | None = None,
		input_tokens: int = 0,
		output_tokens: int = 0,
		cost_usd: float = 0.0,
	) -> None:
		if self._file is None:
			return
		record: dict[str, Any] = {
			"timestamp": datetime.now(timezone.utc).isoformat(),
			"event_type": event_type,
			"mission_id": mission_id,
			"epoch_id": epoch_id,
			"unit_id": unit_id,
			"worker_id": worker_id,
			"details": details or {},
			"input_tokens": input_tokens,
			"output_tokens": output_tokens,
			"cost_usd": cost_usd,
		}
		self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
		self._file.flush()
