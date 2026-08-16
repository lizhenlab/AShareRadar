"""Idempotent saved-screen change evaluation over frozen published scan rows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.artifacts.io import canonical_json_text, sha256_hex
from app.market_scan_screening import screen_spec_digest, screen_spec_from_discovery
from app.models.market_scan import MarketScanRun
from app.models.market_scan_screen_alert import (
    MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION,
    MarketScanScreenAlertPresetRef,
    MarketScanScreenAlertResponse,
    MarketScanScreenAlertRunRef,
    MarketScanScreenAlertUnavailableReason,
)
from app.models.market_scan_screening import ScreenSpecV2
from app.repositories.market_scan_screen_alert import (
    MarketScanScreenAlertComparisonSnapshot,
    MarketScanScreenAlertPresetRevisionError,
    MarketScanScreenAlertPresetSnapshot,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.utils.audit_time import audit_now_text


_PUBLISHED = frozenset({"success", "degraded"})


class MarketScanScreenAlertRepositoryProtocol(Protocol):
    def preset_snapshot(self, preset_id: int) -> MarketScanScreenAlertPresetSnapshot: ...

    def comparison_snapshot(
        self,
        *,
        preset_id: int,
        preset_revision: int,
        current_run_id: int,
        spec: ScreenSpecV2,
    ) -> MarketScanScreenAlertComparisonSnapshot: ...

    def insert_event(
        self,
        *,
        preset_id: int,
        preset_revision: int,
        current_run_id: int,
        previous_run_id: int,
        event_digest: str,
        entered_symbols: tuple[str, ...],
        exited_symbols: tuple[str, ...],
        suppressed_unrankable_symbols: tuple[str, ...],
        created_at: str,
    ) -> bool: ...


class MarketScanScreenAlertService:
    """Compile one saved preset and persist one immutable membership delta."""

    def __init__(
        self,
        repository: MarketScanScreenAlertRepositoryProtocol,
        *,
        now: Callable[[], str] = audit_now_text,
    ) -> None:
        self._repository = repository
        self._now = now

    def record(
        self,
        *,
        preset_id: int,
        current_run_id: int,
        expected_preset_revision: int | None = None,
    ) -> MarketScanScreenAlertResponse:
        _positive_identifier(preset_id, "preset_id")
        _positive_identifier(current_run_id, "current_run_id")
        if expected_preset_revision is not None:
            _positive_identifier(expected_preset_revision, "expected_preset_revision")
        preset = self._repository.preset_snapshot(preset_id)
        _require_expected_revision(preset, expected_preset_revision)
        spec = screen_spec_from_discovery(preset.criteria, list(preset.sort))
        digest = screen_spec_digest(spec)
        snapshot = self._repository.comparison_snapshot(
            preset_id=preset.preset_id,
            preset_revision=preset.revision,
            current_run_id=current_run_id,
            spec=spec,
        )
        unavailable = _unavailable_reason(snapshot)
        if unavailable is not None:
            return _unavailable_response(preset, snapshot.current, digest, unavailable)
        return self._record_ready(preset, snapshot, digest)

    def _record_ready(
        self,
        preset: MarketScanScreenAlertPresetSnapshot,
        snapshot: MarketScanScreenAlertComparisonSnapshot,
        spec_digest_value: str,
    ) -> MarketScanScreenAlertResponse:
        previous = snapshot.previous
        if previous is None:
            raise RuntimeError("筛选变化缺少上一批次")
        entered, exited, suppressed = _membership_delta(snapshot)
        event_digest = _event_digest(
            preset,
            current=snapshot.current,
            previous=previous,
            spec_digest_value=spec_digest_value,
            entered=entered,
            exited=exited,
            suppressed=suppressed,
        )
        created = self._repository.insert_event(
            preset_id=preset.preset_id,
            preset_revision=preset.revision,
            current_run_id=snapshot.current.id,
            previous_run_id=previous.id,
            event_digest=event_digest,
            entered_symbols=entered,
            exited_symbols=exited,
            suppressed_unrankable_symbols=suppressed,
            created_at=self._now(),
        )
        return _ready_response(
            preset,
            current=snapshot.current,
            previous=previous,
            spec_digest_value=spec_digest_value,
            entered=entered,
            exited=exited,
            suppressed=suppressed,
            event_digest=event_digest,
            created=created,
        )


def _unavailable_reason(
    snapshot: MarketScanScreenAlertComparisonSnapshot,
) -> MarketScanScreenAlertUnavailableReason | None:
    if snapshot.current.status not in _PUBLISHED:
        return "current_not_published"
    if snapshot.current.scope != FULL_MARKET_SCOPE:
        return "current_not_full_market"
    if snapshot.previous is None:
        return "previous_same_cohort_not_found"
    return None


def _membership_delta(
    snapshot: MarketScanScreenAlertComparisonSnapshot,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    current = set(snapshot.current_matches)
    previous = set(snapshot.previous_matches)
    entered = tuple(sorted(current - previous))
    exit_candidates = previous - current
    suppressed = tuple(
        sorted(
            symbol
            for symbol in exit_candidates
            if snapshot.current_status_by_symbol.get(symbol) in {"pending", "missing", "skipped"}
        )
    )
    exited = tuple(sorted(exit_candidates - set(suppressed)))
    return entered, exited, suppressed


def _event_digest(
    preset: MarketScanScreenAlertPresetSnapshot,
    *,
    current: MarketScanRun,
    previous: MarketScanRun,
    spec_digest_value: str,
    entered: tuple[str, ...],
    exited: tuple[str, ...],
    suppressed: tuple[str, ...],
) -> str:
    return _digest(
        {
            "schema_version": MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION,
            "preset_id": preset.preset_id,
            "preset_revision": preset.revision,
            "spec_digest": spec_digest_value,
            "current_run_id": current.id,
            "previous_run_id": previous.id,
            "entered_symbols": list(entered),
            "exited_symbols": list(exited),
            "suppressed_unrankable_symbols": list(suppressed),
        }
    )


def _unavailable_response(
    preset: MarketScanScreenAlertPresetSnapshot,
    current: MarketScanRun,
    spec_digest_value: str,
    reason: MarketScanScreenAlertUnavailableReason,
) -> MarketScanScreenAlertResponse:
    digest = _digest(
        {
            "schema_version": MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION,
            "status": "unavailable",
            "reason": reason,
            "preset_id": preset.preset_id,
            "preset_revision": preset.revision,
            "spec_digest": spec_digest_value,
            "current_run_id": current.id,
        }
    )
    return MarketScanScreenAlertResponse(
        status="unavailable",
        unavailable_reason=reason,
        preset=_preset_ref(preset, spec_digest_value),
        current=_run_ref(current),
        event_digest=digest,
        created=False,
    )


def _ready_response(
    preset: MarketScanScreenAlertPresetSnapshot,
    *,
    current: MarketScanRun,
    previous: MarketScanRun,
    spec_digest_value: str,
    entered: tuple[str, ...],
    exited: tuple[str, ...],
    suppressed: tuple[str, ...],
    event_digest: str,
    created: bool,
) -> MarketScanScreenAlertResponse:
    return MarketScanScreenAlertResponse(
        status="ready",
        preset=_preset_ref(preset, spec_digest_value),
        current=_run_ref(current),
        previous=_run_ref(previous),
        entered_symbols=entered,
        exited_symbols=exited,
        suppressed_unrankable_symbols=suppressed,
        event_digest=event_digest,
        created=created,
    )


def _preset_ref(
    preset: MarketScanScreenAlertPresetSnapshot,
    spec_digest_value: str,
) -> MarketScanScreenAlertPresetRef:
    return MarketScanScreenAlertPresetRef(
        preset_id=preset.preset_id,
        preset_revision=preset.revision,
        preset_name=preset.name,
        spec_digest=spec_digest_value,
    )


def _run_ref(run: MarketScanRun) -> MarketScanScreenAlertRunRef:
    return MarketScanScreenAlertRunRef(
        run_id=run.id,
        status=run.status,
        mode=run.mode,
        scope=run.scope,
        rule_version=run.rule_version,
        data_date=run.data_date,
        finished_at=run.finished_at,
    )


def _require_expected_revision(
    preset: MarketScanScreenAlertPresetSnapshot,
    expected: int | None,
) -> None:
    if expected is not None and expected != preset.revision:
        raise MarketScanScreenAlertPresetRevisionError(
            f"筛选方案修订冲突：期望 {expected}，当前 {preset.revision}"
        )


def _positive_identifier(value: int, name: str) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} 必须是正整数")


def _digest(payload: object) -> str:
    return sha256_hex(canonical_json_text(payload))


__all__ = ["MarketScanScreenAlertRepositoryProtocol", "MarketScanScreenAlertService"]
