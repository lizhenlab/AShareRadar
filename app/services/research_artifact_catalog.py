"""Read-only catalog and conservative retention preview for research artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from app.models.system import (
    ResearchArtifactCategoryDiagnostics,
    ResearchArtifactRetentionPreview,
    ResearchArtifactStorageDiagnostics,
)


RETENTION_POLICY_VERSION = "research-artifact-retention-preview-v1"
_PREVIEW_ONLY_REASON = "当前诊断未校验文件与保留榜单、训练证据及完整性清单的引用关系，不能证明任何文件可安全删除。"
_VINTAGE_EVIDENCE_REASON = "source、history 与 replay 可能是唯一的时点或数据源版本证据，默认长期保留。"
_SYMLINK_REASON = "符号链接不属于受管研究存储，已忽略且不会作为清理候选。"


@dataclass(frozen=True)
class ResearchArtifactDirectorySpec:
    category: str
    relative_path: Path
    retention_disposition: str
    ignored_entry_names: frozenset[str] = frozenset()


KNOWN_RESEARCH_ARTIFACT_DIRECTORIES = (
    ResearchArtifactDirectorySpec(
        category="probability_projection",
        relative_path=Path("market-scan-probability"),
        retention_disposition="manual_review_required",
    ),
    ResearchArtifactDirectorySpec(
        category="research_reports",
        relative_path=Path("research"),
        retention_disposition="manual_review_required",
        ignored_entry_names=frozenset(
            {
                "market_scan_future_range",
                "market_scan_probability_history",
                "market_scan_probability_fit",
                "market_scan_probability_outcomes",
                "market_scan_probability_replay",
                "market_scan_probability_source",
            }
        ),
    ),
    ResearchArtifactDirectorySpec(
        category="probability_source",
        relative_path=Path("research/market_scan_probability_source"),
        retention_disposition="retain_point_in_time_evidence",
    ),
    ResearchArtifactDirectorySpec(
        category="probability_outcomes",
        relative_path=Path("research/market_scan_probability_outcomes"),
        retention_disposition="retain_label_evidence",
    ),
    ResearchArtifactDirectorySpec(
        category="probability_fit_assessment",
        relative_path=Path("research/market_scan_probability_fit"),
        retention_disposition="retain_fit_evidence",
    ),
    ResearchArtifactDirectorySpec(
        category="future_range",
        relative_path=Path("research/market_scan_future_range"),
        retention_disposition="manual_review_required",
    ),
    ResearchArtifactDirectorySpec(
        category="probability_history",
        relative_path=Path("research/market_scan_probability_history"),
        retention_disposition="retain_provider_vintage_evidence",
    ),
    ResearchArtifactDirectorySpec(
        category="probability_replay",
        relative_path=Path("research/market_scan_probability_replay"),
        retention_disposition="retain_provider_vintage_evidence",
    ),
)


def research_artifact_storage(database_path: Path) -> ResearchArtifactStorageDiagnostics:
    """Inventory known sibling artifact directories without following links."""

    if str(database_path) == ":memory:":
        return ResearchArtifactStorageDiagnostics()
    root = database_path.parent
    categories = [_scan_category(root, spec) for spec in KNOWN_RESEARCH_ARTIFACT_DIRECTORIES]
    file_count = sum(item.regular_file_count for item in categories)
    size_bytes = sum(item.size_bytes for item in categories)
    symlink_count = sum(item.ignored_symlink_count for item in categories)
    non_regular_count = sum(item.ignored_non_regular_count for item in categories)
    error_count = sum(item.scan_error_count for item in categories)
    return ResearchArtifactStorageDiagnostics(
        root_path=str(root),
        scan_status=_catalog_scan_status(categories, error_count, symlink_count, non_regular_count),
        categories=categories,
        regular_file_count=file_count,
        size_bytes=size_bytes,
        ignored_symlink_count=symlink_count,
        ignored_non_regular_count=non_regular_count,
        scan_error_count=error_count,
        retention_preview=_retention_preview(categories, symlink_count),
    )


def _scan_category(root: Path, spec: ResearchArtifactDirectorySpec) -> ResearchArtifactCategoryDiagnostics:
    base = ResearchArtifactCategoryDiagnostics(
        category=spec.category,
        relative_path=spec.relative_path.as_posix(),
        retention_disposition=spec.retention_disposition,
    )
    descriptor, status = _open_relative_directory(root, spec.relative_path)
    if descriptor is not None:
        return _scan_regular_files(descriptor, base, ignored_entry_names=spec.ignored_entry_names)
    if status == "ignored_symlink":
        return base.model_copy(update={"scan_status": status, "ignored_symlink_count": 1})
    if status == "not_directory":
        return base.model_copy(update={"scan_status": status, "ignored_non_regular_count": 1})
    if status == "unreadable":
        return base.model_copy(update={"scan_status": status, "scan_error_count": 1})
    return base


def _open_relative_directory(root: Path, relative_path: Path) -> tuple[int | None, str]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if stat.S_ISLNK(root_mode):
        return None, "ignored_symlink"
    if not stat.S_ISDIR(root_mode):
        return None, "not_directory"
    try:
        descriptor = os.open(root, flags)
    except OSError:
        return None, "unreadable"
    for part in relative_path.parts:
        next_descriptor, status = _open_child_directory(descriptor, part, flags)
        os.close(descriptor)
        if next_descriptor is None:
            return None, status
        descriptor = next_descriptor
    return descriptor, "ok"


def _open_child_directory(parent_descriptor: int, name: str, flags: int) -> tuple[int | None, str]:
    try:
        mode = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if stat.S_ISLNK(mode):
        return None, "ignored_symlink"
    if not stat.S_ISDIR(mode):
        return None, "not_directory"
    try:
        return os.open(name, flags, dir_fd=parent_descriptor), "ok"
    except OSError:
        return None, "unreadable"


def _scan_regular_files(
    descriptor: int,
    base: ResearchArtifactCategoryDiagnostics,
    *,
    ignored_entry_names: frozenset[str],
) -> ResearchArtifactCategoryDiagnostics:
    try:
        counts = _directory_entry_counts(descriptor, ignored_entry_names=ignored_entry_names)
    finally:
        os.close(descriptor)
    status = "partial" if counts[3] else "ok"
    return base.model_copy(
        update={
            "scan_status": status,
            "regular_file_count": counts[0],
            "size_bytes": counts[1],
            "ignored_symlink_count": counts[2],
            "scan_error_count": counts[3],
            "ignored_non_regular_count": counts[4],
        }
    )


def _directory_entry_counts(
    descriptor: int,
    *,
    ignored_entry_names: frozenset[str],
) -> tuple[int, int, int, int, int]:
    file_count = size_bytes = symlink_count = error_count = non_regular_count = 0
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.name in ignored_entry_names:
                    continue
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    error_count += 1
                    continue
                entry_mode = entry_stat.st_mode
                if stat.S_ISLNK(entry_mode):
                    symlink_count += 1
                elif stat.S_ISREG(entry_mode):
                    file_count += 1
                    size_bytes += max(0, entry_stat.st_size)
                else:
                    non_regular_count += 1
    except OSError:
        error_count += 1
    return file_count, size_bytes, symlink_count, error_count, non_regular_count


def _catalog_scan_status(
    categories: list[ResearchArtifactCategoryDiagnostics],
    error_count: int,
    symlink_count: int,
    non_regular_count: int,
) -> str:
    if error_count:
        return "partial"
    if symlink_count or non_regular_count:
        return "ok_with_ignored_entries"
    if any(item.scan_status == "ok" for item in categories):
        return "ok"
    return "empty"


def _retention_preview(
    categories: list[ResearchArtifactCategoryDiagnostics],
    symlink_count: int,
) -> ResearchArtifactRetentionPreview:
    review = [item for item in categories if item.retention_disposition == "manual_review_required"]
    protected = [item for item in categories if item.retention_disposition != "manual_review_required"]
    total_count = sum(item.regular_file_count for item in categories)
    reasons = [_PREVIEW_ONLY_REASON]
    if sum(item.regular_file_count for item in protected):
        reasons.append(_VINTAGE_EVIDENCE_REASON)
    if symlink_count:
        reasons.append(_SYMLINK_REASON)
    return ResearchArtifactRetentionPreview(
        policy_version=RETENTION_POLICY_VERSION,
        manual_review_file_count=sum(item.regular_file_count for item in review),
        manual_review_size_bytes=sum(item.size_bytes for item in review),
        protected_evidence_file_count=sum(item.regular_file_count for item in protected),
        protected_evidence_size_bytes=sum(item.size_bytes for item in protected),
        blocked_reasons=reasons,
        summary=_retention_summary(total_count),
    )


def _retention_summary(total_count: int) -> str:
    if not total_count:
        return "未发现受管研究 artifact；预览策略不会执行删除。"
    return f"共识别 {total_count} 个研究 artifact；0 个已证明可安全自动删除，其余仅保留或供人工复核。"


__all__ = [
    "KNOWN_RESEARCH_ARTIFACT_DIRECTORIES",
    "RETENTION_POLICY_VERSION",
    "research_artifact_storage",
]
