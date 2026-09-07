from __future__ import annotations

import errno
import os

import pytest

from app.services import market_scan_trial_registry_lock as locking


def _closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


def test_unlock_failure_still_closes_descriptor(tmp_path, monkeypatch) -> None:
    opened: list[int] = []
    original_open, original_flock = os.open, locking.fcntl.flock

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        if str(args[0]).endswith(locking.TRIAL_REGISTRY_LOCK_FILENAME):
            opened.append(descriptor)
        return descriptor

    def failing_unlock(descriptor, operation):
        if operation == locking.fcntl.LOCK_UN:
            raise OSError("unlock interrupted")
        return original_flock(descriptor, operation)

    monkeypatch.setattr(locking.os, "open", tracked_open)
    monkeypatch.setattr(locking.fcntl, "flock", failing_unlock)
    try:
        with pytest.raises(OSError, match="unlock interrupted"), locking.trial_registry_write_lock(tmp_path / "registry"):
            pass
        assert opened and _closed(opened[-1])
    finally:
        for descriptor in opened:
            if not _closed(descriptor):
                os.close(descriptor)


def test_failed_acquisition_does_not_unlock_unacquired_descriptor(tmp_path, monkeypatch) -> None:
    operations: list[int] = []
    descriptors: list[int] = []

    def failed_acquisition(descriptor, operation):
        descriptors.append(descriptor)
        operations.append(operation)
        raise OSError("lock acquisition unavailable" if operation == locking.fcntl.LOCK_EX else "unexpected unlock")

    monkeypatch.setattr(locking.fcntl, "flock", failed_acquisition)
    try:
        with pytest.raises(OSError, match="lock acquisition unavailable"), locking.trial_registry_write_lock(tmp_path / "registry"):
            pass
        assert operations == [locking.fcntl.LOCK_EX]
        assert descriptors and _closed(descriptors[0])
    finally:
        for descriptor in set(descriptors):
            if not _closed(descriptor):
                os.close(descriptor)
