"""Opaque local database binding, without exposing its filesystem path."""

from hashlib import sha256
from pathlib import Path


def database_identity(path: Path) -> str:
    return sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()
