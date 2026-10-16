"""Bounded filesystem metadata inspection; never opens file contents or databases."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from app.models.workspace_storage import (
    StorageCounts, WorkspaceStorageBudget, WorkspaceStorageIssue, WorkspaceStorageReport,
    WorkspaceStorageRoot, WorkspaceStorageRootResult,
)
from app.services.workspace_storage_policy import EXCLUSION_DESCRIPTIONS, admitted_lexical_path, storage_exclusion
from app.utils.audit_time import audit_now_text
from app.utils.clock import monotonic_now


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_NOTES = [
    "仅检查显式根清单；不推断整个工作区已覆盖，不创建、删除或恢复任何文件，也不连接SQLite。",
    "logical_bytes按纳入的普通文件路径求和；每根unique_bytes/allocated_bytes在该根内按设备号和inode去重。",
    "totals.unique_bytes/allocated_bytes跨全部根去重，不能用各根去重值直接相加；overlap_count指重复路径。",
    "allocated_bytes使用普通文件st_blocks×512；不含目录inode、文件系统元数据、排除项及未遍历部分。",
    "inode去重不能识别写时复制文件共享的磁盘区块，allocated_bytes不是可释放空间承诺。",
    "符号链接不跟随；runtime中的链接依赖单独披露，不能据此宣称环境可完整复制或恢复。",
    "这是运行中元数据观察，不是原子快照；partial时字节数仅代表已观察部分，不是完整容量。",
    "时间预算在遍历检查点执行，不能中断正在阻塞的操作系统调用；排除项仍占目录项预算。",
    "默认排除基于明确文件名规则，不读取内容识别秘密；备份范围仍须由操作者核对。",
]


@dataclass
class _Accumulator:
    counts: StorageCounts = field(default_factory=StorageCounts)
    inodes: dict[tuple[int, int], tuple[int | None, ...]] = field(default_factory=dict)
    paths: dict[Path, tuple[int, int]] = field(default_factory=dict)

    def add_file(self, path: Path, facts: os.stat_result) -> None:
        identity = (facts.st_dev, facts.st_ino)
        size = max(0, facts.st_size)
        self.counts.logical_bytes += size
        self.counts.file_count += 1
        if path in self.paths:
            self.counts.overlap_count += 1
        elif identity in self.inodes:
            self.counts.duplicate_hardlink_count += 1
        self.paths[path] = identity
        if identity in self.inodes:
            return
        self.inodes[identity] = _file_fingerprint(facts)
        self.counts.unique_bytes += size
        self.counts.unique_file_count += 1
        blocks = getattr(facts, "st_blocks", None)
        if blocks is None or blocks < 0:
            self.counts.allocated_bytes_complete = False
        else:
            self.counts.allocated_bytes += blocks * 512


@dataclass
class _Inspection:
    budget: WorkspaceStorageBudget
    started: float
    started_at: str
    totals: _Accumulator = field(default_factory=_Accumulator)
    consumed: dict[str, int] = field(default_factory=lambda: {"entries": 0, "files": 0, "directories": 0})
    exhausted: list[str] = field(default_factory=list)
    halt_reason: str | None = None
    issue_samples: int = 0


def inspect_workspace_storage(
    roots: Sequence[WorkspaceStorageRoot], budget: WorkspaceStorageBudget | None = None,
) -> WorkspaceStorageReport:
    """Inspect explicit roots using metadata and descriptor-relative traversal only."""
    selected = tuple(roots)
    if not selected or len(selected) > 256:
        raise ValueError("provide between 1 and 256 explicit storage roots")
    if len({root.name for root in selected}) != len(selected):
        raise ValueError("storage root names must be unique")
    run = _Inspection(budget or WorkspaceStorageBudget(), monotonic_now(), audit_now_text())
    results = [_inspect_root(root, run) for root in selected]
    return WorkspaceStorageReport(
        status="partial" if any(root.status != "complete" for root in results) else "complete",
        roots=results, totals=run.totals.counts, budget=run.budget,
        entries_inspected=run.consumed["entries"], files_inspected=run.consumed["files"],
        directories_inspected=run.consumed["directories"], budget_exhausted=run.exhausted,
        started_at=run.started_at, finished_at=audit_now_text(),
        elapsed_seconds=max(0.0, monotonic_now() - run.started),
        exclusions=list(EXCLUSION_DESCRIPTIONS), notes=list(_NOTES),
    )


def _inspect_root(root: WorkspaceStorageRoot, run: _Inspection) -> WorkspaceStorageRootResult:
    result = WorkspaceStorageRootResult(**root.model_dump())
    accumulator = _Accumulator(counts=result.counts)
    if not _admit(run, result, root.path, "entries"):
        return result
    excluded = storage_exclusion(root.path, root.category, is_root=True)
    if excluded:
        _issue(run, result, root.path, excluded, "显式根被默认范围规则排除。", bucket="excluded", partial=True)
        return result
    try:
        path = admitted_lexical_path(root.path)
        if path is None:
            _symlink(run, result, root.path)
            return result
        with _parent_descriptor(path) as parent:
            observed = _visit(run, result, accumulator, parent, path.name or ".", path, 0, is_root=True)
        if observed is not None:
            _verify_root_path(run, result, observed)
    except (OSError, RuntimeError) as exc:
        _filesystem_error(run, result, root.path, exc, is_root=True)
    if not run.halt_reason and monotonic_now() - run.started >= run.budget.max_seconds:
        _budget_issue(run, result, root.path, "max_seconds")
    return result


def _verify_root_path(run: _Inspection, result: WorkspaceStorageRootResult, observed: os.stat_result) -> None:
    try:
        path = admitted_lexical_path(result.path)
        if path is None:
            _symlink(run, result, result.path)
            return
        with _parent_descriptor(path) as parent:
            current = os.stat(path.name or ".", dir_fd=parent, follow_symlinks=False)
    except (OSError, RuntimeError):
        _issue(run, result, result.path, "root_path_changed", "显式根的当前路径无法重新核对，观察期间可能被替换或移除。")
        return
    if _file_fingerprint(current) != _file_fingerprint(observed):
        _issue(run, result, result.path, "root_path_changed", "显式根当前路径与遍历时身份或元数据不同，不能视为完整结果。")


@contextmanager
def _parent_descriptor(path: Path) -> Iterator[int]:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _visit(
    run: _Inspection, result: WorkspaceStorageRootResult, accumulator: _Accumulator,
    parent: int, name: str, path: Path, depth: int, *, is_root: bool = False,
) -> os.stat_result | None:
    if not is_root and not _admit(run, result, path, "entries"):
        return None
    try:
        facts = os.stat(name, dir_fd=parent, follow_symlinks=False)
        kind = "directories" if stat.S_ISDIR(facts.st_mode) else "files"
        if not _admit(run, result, path, kind):
            return None
        _inspect_entry(run, result, accumulator, parent, name, path, depth, facts, is_root=is_root)
        return facts
    except OSError as exc:
        _filesystem_error(run, result, path, exc, is_root=is_root)
        return None


def _inspect_entry(
    run: _Inspection, result: WorkspaceStorageRootResult, accumulator: _Accumulator,
    parent: int, name: str, path: Path, depth: int, facts: os.stat_result, *, is_root: bool,
) -> None:
    if stat.S_ISLNK(facts.st_mode):
        _symlink(run, result, path)
        return
    excluded = storage_exclusion(path, result.category, is_root=is_root)
    if excluded:
        _issue(run, result, path, excluded, "根据默认范围规则跳过，不读取内容。", bucket="excluded", partial=False)
    elif stat.S_ISDIR(facts.st_mode):
        _inspect_directory(run, result, accumulator, parent, name, path, depth, facts)
    elif stat.S_ISREG(facts.st_mode):
        _inspect_file(run, result, accumulator, parent, name, path, facts)
    else:
        _issue(run, result, path, "unsupported_type", "非常规文件，不打开或计入容量。")


def _inspect_file(
    run: _Inspection, result: WorkspaceStorageRootResult, accumulator: _Accumulator,
    parent: int, name: str, path: Path, facts: os.stat_result,
) -> None:
    confirmed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _file_fingerprint(facts) != _file_fingerprint(confirmed):
        _issue(run, result, path, "changed", "文件元数据在检查期间变化，未计入容量。")
        return
    previous_path = run.totals.paths.get(path)
    if previous_path is not None and previous_path != (facts.st_dev, facts.st_ino):
        _issue(run, result, path, "path_changed", "重复路径指向的文件身份在观察之间变化，此次路径未计入容量。")
        return
    previous = run.totals.inodes.get((facts.st_dev, facts.st_ino))
    if previous is not None and previous != _file_fingerprint(facts):
        _issue(run, result, path, "inode_changed", "同一文件身份在多次观察之间发生变化，此次路径未计入容量。")
        return
    accumulator.add_file(path, facts)
    run.totals.add_file(path, facts)
    blocks = getattr(facts, "st_blocks", None)
    if blocks is None or blocks < 0:
        _issue(run, result, path, "allocation_unavailable", "文件系统未提供磁盘分配块数，分配字节不完整。")


def _inspect_directory(
    run: _Inspection, result: WorkspaceStorageRootResult, accumulator: _Accumulator,
    parent: int, name: str, path: Path, depth: int, facts: os.stat_result,
) -> None:
    if depth > run.budget.max_depth:
        _budget_issue(run, result, path, "max_depth", halt=False)
        return
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if _fingerprint(os.fstat(descriptor)) != _fingerprint(facts):
            _issue(run, result, path, "changed", "目录在打开期间变化，未遍历。")
            return
        result.counts.directory_count += 1
        run.totals.counts.directory_count += 1
        _walk_directory(run, result, accumulator, descriptor, path, depth)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _fingerprint(current) != _fingerprint(facts):
            _issue(run, result, path, "changed", "目录在遍历期间变化，结果不是完整快照。")
    finally:
        os.close(descriptor)


def _walk_directory(
    run: _Inspection, result: WorkspaceStorageRootResult, accumulator: _Accumulator,
    descriptor: int, path: Path, depth: int,
) -> None:
    with os.scandir(descriptor) as entries:
        for entry in entries:
            _visit(run, result, accumulator, descriptor, entry.name, path / entry.name, depth + 1)
            if run.halt_reason:
                break


def _fingerprint(facts: os.stat_result) -> tuple[int, ...]:
    return (facts.st_dev, facts.st_ino, facts.st_mode, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)


def _file_fingerprint(facts: os.stat_result) -> tuple[int | None, ...]:
    return (*_fingerprint(facts), getattr(facts, "st_blocks", None))


def _admit(run: _Inspection, result: WorkspaceStorageRootResult, path: Path, kind: str) -> bool:
    if run.halt_reason:
        _budget_issue(run, result, path, run.halt_reason)
        return False
    if monotonic_now() - run.started >= run.budget.max_seconds:
        _budget_issue(run, result, path, "max_seconds")
        return False
    field_name = f"max_{kind}"
    if run.consumed[kind] >= getattr(run.budget, field_name):
        _budget_issue(run, result, path, field_name)
        return False
    run.consumed[kind] += 1
    return True


def _budget_issue(
    run: _Inspection, result: WorkspaceStorageRootResult, path: Path, reason: str, *, halt: bool = True,
) -> None:
    if reason not in run.exhausted:
        run.exhausted.append(reason)
    if halt:
        run.halt_reason = reason
    _issue(run, result, path, reason, "遍历预算已达到；其余范围未检查。", bucket="budget")


def _symlink(run: _Inspection, result: WorkspaceStorageRootResult, path: Path) -> None:
    runtime = result.category == "runtime"
    reason = "runtime_symlink_dependency" if runtime else "symlink"
    detail = "封存运行环境包含未复制的链接依赖；未跟随，不能据此保证环境可恢复。" if runtime else "根、祖先或目录项包含未授权符号链接，未跟随。"
    _issue(run, result, path, reason, detail, bucket="symlink")


def _filesystem_error(
    run: _Inspection, result: WorkspaceStorageRootResult, path: Path, error: BaseException, *, is_root: bool,
) -> None:
    if isinstance(error, FileNotFoundError):
        reason, detail = ("missing", "显式根不存在。") if is_root else ("vanished", "目录项在检查期间消失。")
        if is_root:
            result.status = "missing"
    elif isinstance(error, PermissionError):
        reason, detail = "permission_denied", "没有读取目录或元数据的权限。"
    else:
        reason, detail = "filesystem_error", "目录或元数据无法安全读取；未包含原始操作系统错误内容。"
    _issue(run, result, path, reason, detail)


def _issue(
    run: _Inspection, result: WorkspaceStorageRootResult, path: Path, reason: str, detail: str,
    *, bucket: str = "error", partial: bool = True,
) -> None:
    counter = {"error": "error_count", "excluded": "excluded_count", "symlink": "symlink_count"}.get(bucket)
    if counter:
        setattr(result.counts, counter, getattr(result.counts, counter) + 1)
        setattr(run.totals.counts, counter, getattr(run.totals.counts, counter) + 1)
    if partial:
        if result.status == "complete":
            result.status = "partial"
        if reason not in result.incomplete_reasons:
            result.incomplete_reasons.append(reason)
    if run.issue_samples < run.budget.max_issues:
        result.issues.append(WorkspaceStorageIssue(path=str(path), reason=reason, detail=detail))
        run.issue_samples += 1
    else:
        result.issues_truncated += 1


__all__ = ["inspect_workspace_storage"]
