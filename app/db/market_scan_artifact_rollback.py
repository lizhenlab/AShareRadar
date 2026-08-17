"""Low-memory exact-file rollback for immutable artifact batches."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import stat


class ArtifactRollbackError(RuntimeError):
    """A newly published artifact cannot be proven safe to unlink."""


def unlink_exact_artifact(target: Path, size: int, digest: str) -> None:
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ArtifactRollbackError("artifact 批次回滚证据无效")
    directory = file_descriptor = None
    try:
        directory = os.open(target.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
        file_descriptor = os.open(target.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
        facts = os.fstat(file_descriptor)
        path_facts = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1:
            raise ArtifactRollbackError("artifact 批次回滚目标身份不可信")
        if (facts.st_dev, facts.st_ino) != (path_facts.st_dev, path_facts.st_ino):
            raise ArtifactRollbackError("artifact 批次回滚目标被并发替换")
        if facts.st_size != size or not hmac.compare_digest(_descriptor_sha256(file_descriptor), digest):
            raise ArtifactRollbackError("artifact 批次回滚目标内容不一致")
        final_facts = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
        if (facts.st_dev, facts.st_ino) != (final_facts.st_dev, final_facts.st_ino):
            raise ArtifactRollbackError("artifact 批次回滚目标在校验期间被替换")
        os.unlink(target.name, dir_fd=directory)
        os.fsync(directory)
    except OSError as exc:
        raise ArtifactRollbackError("artifact 批次回滚失败") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory is not None:
            os.close(directory)


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ArtifactRollbackError", "unlink_exact_artifact"]
