"""Stable low-level hashing contract shared by scoring and replay."""

from __future__ import annotations

import hashlib
import json


class MarketScanReplayError(ValueError):
    pass


def stable_score_spec_hash(spec: object) -> str:
    try:
        canonical = json.dumps(
            spec,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise MarketScanReplayError("评分规范损坏：不是有限、可序列化的 JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


__all__ = ["MarketScanReplayError", "stable_score_spec_hash"]
