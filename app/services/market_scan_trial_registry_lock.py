"""Serialize registry writers without a mutable or silently replaceable head."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from app.artifacts.io import exclusive_atomic_publish, path_has_only_trusted_aliases
from app.services.market_scan_trial_registry_contract import TrialRegistryError


TRIAL_REGISTRY_LOCK_FILENAME = ".writer.lock"
TRIAL_REGISTRY_EXECUTION_LOCK_FILENAME = ".executor.lock"


@contextmanager
def trial_registry_write_lock(directory: Path) -> Iterator[None]:
    """Hold a stable, empty lock file across read-validate-publish operations."""
    with _registry_file_lock(directory, TRIAL_REGISTRY_LOCK_FILENAME, blocking=True):
        yield


@contextmanager
def trial_registry_execution_lock(directory: Path) -> Iterator[None]:
    """Fail immediately when another process is running or replaying this family."""
    with _registry_file_lock(directory, TRIAL_REGISTRY_EXECUTION_LOCK_FILENAME, blocking=False):
        yield


@contextmanager
def _registry_file_lock(directory: Path, filename: str, *, blocking: bool) -> Iterator[None]:
    path = directory / filename
    exclusive_atomic_publish(path, b"", max_bytes=0)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise TrialRegistryError("research execution already active; retry after its process exits") from exc
        acquired = True
        opened, current = os.fstat(descriptor), path.stat(follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != 0:
            raise TrialRegistryError("registry lock must be an empty regular file")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise TrialRegistryError("registry lock changed before publication")
        if not path_has_only_trusted_aliases(path):
            raise TrialRegistryError("registry lock path changed")
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
