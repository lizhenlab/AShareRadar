from __future__ import annotations

import hashlib
import hmac

from app.models.discovery import (
    DISCOVERY_PRESET_FORMAT,
    DISCOVERY_PRESET_SCHEMA_VERSION,
    DiscoveryLeaderboardPage,
    DiscoveryPreset,
    DiscoveryPresetArchive,
    DiscoveryPresetCreate,
    DiscoveryPresetDeleteResponse,
    DiscoveryPresetPage,
    DiscoveryPresetPortable,
    DiscoveryPresetRename,
    DiscoveryPresetUpdate,
    DiscoveryRankChangePage,
    DiscoveryResearchQueueRequest,
    DiscoveryResearchQueueResponse,
)
from app.repositories.discovery import (
    DiscoveryPresetNameExistsError,
    DiscoveryPresetRevisionError,
    DiscoveryRepository,
)
from app.repositories.discovery_sql import canonical_json
from app.utils.audit_time import audit_now_text


class DiscoveryConflictError(ValueError):
    pass


class DiscoveryImportError(ValueError):
    pass


_COMPLETED_RUN_STATUSES = frozenset({"success", "degraded"})


class DiscoveryService:
    def __init__(self, repository: DiscoveryRepository) -> None:
        self.repository = repository

    def create_preset(self, payload: DiscoveryPresetCreate) -> DiscoveryPreset:
        try:
            return self.repository.create_preset(payload, timestamp=audit_now_text())
        except DiscoveryPresetNameExistsError as exc:
            raise DiscoveryConflictError(str(exc)) from exc

    def get_preset(self, preset_id: int) -> DiscoveryPreset:
        return self.repository.preset(preset_id)

    def list_presets(self, *, page: int, page_size: int) -> DiscoveryPresetPage:
        items, total = self.repository.presets(page=page, page_size=page_size)
        return DiscoveryPresetPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            page_count=_page_count(total, page_size),
        )

    def rename_preset(self, preset_id: int, payload: DiscoveryPresetRename) -> DiscoveryPreset:
        try:
            return self.repository.rename_preset(
                preset_id,
                name=payload.name,
                expected_revision=payload.expected_revision,
                timestamp=audit_now_text(),
            )
        except (DiscoveryPresetNameExistsError, DiscoveryPresetRevisionError) as exc:
            raise DiscoveryConflictError(str(exc)) from exc

    def update_preset(self, preset_id: int, payload: DiscoveryPresetUpdate) -> DiscoveryPreset:
        try:
            return self.repository.update_preset(
                preset_id,
                payload,
                timestamp=audit_now_text(),
            )
        except (DiscoveryPresetNameExistsError, DiscoveryPresetRevisionError) as exc:
            raise DiscoveryConflictError(str(exc)) from exc

    def delete_preset(self, preset_id: int, *, expected_revision: int) -> DiscoveryPresetDeleteResponse:
        try:
            self.repository.delete_preset(preset_id, expected_revision=expected_revision)
        except DiscoveryPresetRevisionError as exc:
            raise DiscoveryConflictError(str(exc)) from exc
        return DiscoveryPresetDeleteResponse(preset_id=preset_id)

    def apply_preset(
        self,
        preset_id: int,
        *,
        run_id: int,
        page: int,
        page_size: int,
    ) -> DiscoveryLeaderboardPage:
        preset = self.repository.preset(preset_id)
        run = self.repository.run_reference(run_id)
        _require_completed_run(run.id, run.status)
        items, total = self.repository.leaderboard(
            preset,
            run_id=run_id,
            page=page,
            page_size=page_size,
        )
        return DiscoveryLeaderboardPage(
            preset=preset,
            run_id=run.id,
            rule_version=run.rule_version,
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            page_count=_page_count(total, page_size),
        )

    def export_preset(self, preset_id: int) -> DiscoveryPresetArchive:
        preset = self.repository.preset(preset_id)
        portable = DiscoveryPresetPortable(
            name=preset.name,
            criteria=preset.criteria,
            sort=preset.sort,
        )
        return DiscoveryPresetArchive(
            format=DISCOVERY_PRESET_FORMAT,
            schema_version=DISCOVERY_PRESET_SCHEMA_VERSION,
            checksum_algorithm="sha256",
            checksum=_archive_checksum(DISCOVERY_PRESET_SCHEMA_VERSION, portable),
            exported_at=audit_now_text(),
            preset=portable,
        )

    def import_preset(self, archive: DiscoveryPresetArchive) -> DiscoveryPreset:
        if archive.schema_version != DISCOVERY_PRESET_SCHEMA_VERSION:
            raise DiscoveryImportError(
                f"不支持的筛选方案版本：{archive.schema_version}，当前支持 {DISCOVERY_PRESET_SCHEMA_VERSION}"
            )
        expected = _archive_checksum(archive.schema_version, archive.preset)
        if not hmac.compare_digest(archive.checksum, expected):
            raise DiscoveryImportError("筛选方案校验和不匹配，文件可能已损坏或被篡改")
        return self.create_preset(
            DiscoveryPresetCreate(
                name=archive.preset.name,
                criteria=archive.preset.criteria,
                sort=archive.preset.sort,
            )
        )

    def enqueue_research(
        self,
        preset_id: int,
        request: DiscoveryResearchQueueRequest,
    ) -> DiscoveryResearchQueueResponse:
        try:
            items = self.repository.enqueue_research(
                preset_id,
                request,
                timestamp=audit_now_text(),
            )
        except DiscoveryPresetRevisionError as exc:
            raise DiscoveryConflictError(str(exc)) from exc
        added_count = sum(item.added for item in items)
        return DiscoveryResearchQueueResponse(
            items=items,
            added_count=added_count,
            existing_count=len(items) - added_count,
        )

    def rank_changes(self, run_id: int, *, page: int, page_size: int) -> DiscoveryRankChangePage:
        current = self.repository.run_reference(run_id)
        _require_completed_run(current.id, current.status)
        previous = self.repository.previous_completed_run_same_mode_any_rule(current)
        if previous is None:
            return _empty_rank_change_page(
                current_run_id=current.id,
                current_rule_version=current.rule_version,
                previous_run_id=None,
                previous_rule_version=None,
                reason="no_previous_run",
                page=page,
                page_size=page_size,
            )
        if previous.rule_version != current.rule_version:
            return _empty_rank_change_page(
                current_run_id=current.id,
                current_rule_version=current.rule_version,
                previous_run_id=previous.id,
                previous_rule_version=previous.rule_version,
                reason="rule_version_mismatch",
                page=page,
                page_size=page_size,
            )
        items, total = self.repository.rank_change_rows(
            current.id,
            previous.id,
            page=page,
            page_size=page_size,
        )
        return DiscoveryRankChangePage(
            current_run_id=current.id,
            previous_run_id=previous.id,
            current_rule_version=current.rule_version,
            previous_rule_version=previous.rule_version,
            comparable=True,
            reason=None,
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            page_count=_page_count(total, page_size),
        )


def _archive_checksum(schema_version: int, preset: DiscoveryPresetPortable) -> str:
    checksum_payload = canonical_json(
        {
            "format": DISCOVERY_PRESET_FORMAT,
            "schema_version": schema_version,
            "preset": preset.model_dump(mode="json"),
        }
    )
    return hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest()


def _empty_rank_change_page(
    *,
    current_run_id: int,
    current_rule_version: str,
    previous_run_id: int | None,
    previous_rule_version: str | None,
    reason: str,
    page: int,
    page_size: int,
) -> DiscoveryRankChangePage:
    return DiscoveryRankChangePage(
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        current_rule_version=current_rule_version,
        previous_rule_version=previous_rule_version,
        comparable=False,
        reason=reason,
        items=[],
        total=0,
        page=page,
        page_size=page_size,
        page_count=0,
    )


def _require_completed_run(run_id: int, status: str) -> None:
    if status not in _COMPLETED_RUN_STATUSES:
        raise ValueError(f"全市场扫描批次尚未完成：{run_id}")


def _page_count(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size


__all__ = [
    "DiscoveryConflictError",
    "DiscoveryImportError",
    "DiscoveryService",
]
