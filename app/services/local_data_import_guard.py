"""Server-owned preview claims for destructive local-data imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time

from app.models.local_data import LocalDataImportMode, UserDataBundle
from app.services.user_data_portability import export_user_data


IMPORT_PREVIEW_TTL_SECONDS = 10 * 60
MAX_IMPORT_PREVIEW_CLAIMS = 64


class LocalDataImportPreviewError(ValueError):
    """Raised when a commit is not backed by the matching fresh preview."""


@dataclass(frozen=True)
class LocalDataImportPreviewClaim:
    token: str
    database_path: str
    bundle_digest: str
    database_digest: str
    mode: LocalDataImportMode
    expires_monotonic: float
    expires_at: str


class LocalDataImportPreviewRegistry:
    def __init__(
        self,
        *,
        ttl_seconds: int = IMPORT_PREVIEW_TTL_SECONDS,
        max_claims: int = MAX_IMPORT_PREVIEW_CLAIMS,
    ) -> None:
        if ttl_seconds <= 0 or max_claims <= 0:
            raise ValueError("预览令牌配置必须为正整数")
        self._ttl_seconds = ttl_seconds
        self._max_claims = max_claims
        self._claims: dict[str, LocalDataImportPreviewClaim] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        path: Path,
        bundle: UserDataBundle,
        mode: LocalDataImportMode,
        *,
        database_digest: str | None = None,
    ) -> LocalDataImportPreviewClaim:
        now_monotonic = time.monotonic()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl_seconds)
        claim = LocalDataImportPreviewClaim(
            token=secrets.token_urlsafe(32),
            database_path=str(Path(path).expanduser().resolve()),
            bundle_digest=user_data_bundle_digest(bundle),
            database_digest=database_digest or user_data_state_digest(path),
            mode=mode,
            expires_monotonic=now_monotonic + self._ttl_seconds,
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            self._prune(now_monotonic)
            while len(self._claims) >= self._max_claims:
                oldest = min(self._claims.values(), key=lambda item: item.expires_monotonic)
                self._claims.pop(oldest.token, None)
            self._claims[claim.token] = claim
        return claim

    def consume(
        self,
        token: str | None,
        path: Path,
        bundle: UserDataBundle,
        mode: LocalDataImportMode,
    ) -> LocalDataImportPreviewClaim:
        cleaned = str(token or "").strip()
        if not cleaned:
            raise LocalDataImportPreviewError("提交导入前必须先完成服务端预览")
        now_monotonic = time.monotonic()
        with self._lock:
            self._prune(now_monotonic)
            claim = self._claims.pop(cleaned, None)
        if claim is None:
            raise LocalDataImportPreviewError("导入预览已失效，请重新预览")
        resolved_path = str(Path(path).expanduser().resolve())
        if claim.database_path != resolved_path or claim.mode != mode:
            raise LocalDataImportPreviewError("导入文件、模式或目标数据库已变化，请重新预览")
        if claim.bundle_digest != user_data_bundle_digest(bundle):
            raise LocalDataImportPreviewError("导入文件内容已变化，请重新预览")
        if claim.database_digest != user_data_state_digest(path):
            raise LocalDataImportPreviewError("预览后本地用户数据已变化，请重新预览")
        return claim

    def _prune(self, now_monotonic: float) -> None:
        expired = [token for token, claim in self._claims.items() if claim.expires_monotonic <= now_monotonic]
        for token in expired:
            self._claims.pop(token, None)


def user_data_bundle_digest(bundle: UserDataBundle) -> str:
    return _stable_digest(bundle.model_dump(mode="json"))


def user_data_state_digest(path: Path) -> str:
    payload = export_user_data(path).model_dump(mode="json")
    payload.pop("exported_at", None)
    return _stable_digest(payload)


def _stable_digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "IMPORT_PREVIEW_TTL_SECONDS",
    "LocalDataImportPreviewClaim",
    "LocalDataImportPreviewError",
    "LocalDataImportPreviewRegistry",
    "user_data_bundle_digest",
    "user_data_state_digest",
]
