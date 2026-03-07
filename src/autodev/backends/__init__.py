"""Worker execution backends for autodev."""

from __future__ import annotations

from autodev.backends.base import WorkerBackend, WorkerHandle
from autodev.backends.container import ContainerBackend
from autodev.backends.local import LocalBackend
from autodev.backends.ssh import SSHBackend

__all__ = [
	"ContainerBackend",
	"LocalBackend",
	"SSHBackend",
	"WorkerBackend",
	"WorkerHandle",
]
