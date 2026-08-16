"""Application service for versioned StrategySpec workflows."""

from __future__ import annotations

from app.models.market_strategy_templates import MarketStrategyTemplateCatalog
from app.models.strategy_lab import (
    StrategyCompileRequest,
    StrategyCompileResponse,
    StrategyNaturalLanguageRequest,
    StrategyNaturalLanguageResponse,
    StrategySpec,
    StrategySpecArchiveRequest,
    StrategySpecCopyRequest,
    StrategySpecCreate,
    StrategySpecInput,
    StrategySpecPage,
    StrategySpecUpdate,
    StrategyVersionDiff,
    StrategyVersionPage,
)
from app.repositories.strategy_lab import StrategyLabRepository
from app.services.market_strategy_templates import market_strategy_template_catalog
from app.services.strategy_compiler import compile_strategy_spec
from app.services.strategy_metrics import strategy_metric_registry
from app.services.strategy_natural_language import parse_chinese_strategy
from app.utils.audit_time import audit_now_text


class StrategyLabService:
    def __init__(self, repository: StrategyLabRepository) -> None:
        self.repository = repository

    def metrics(self):
        return strategy_metric_registry()

    def templates(self) -> MarketStrategyTemplateCatalog:
        return market_strategy_template_catalog()

    def compile(self, request: StrategyCompileRequest) -> StrategyCompileResponse:
        return compile_strategy_spec(request.spec)

    def parse_natural_language(
        self,
        request: StrategyNaturalLanguageRequest,
    ) -> StrategyNaturalLanguageResponse:
        return parse_chinese_strategy(request)

    def create(self, request: StrategySpecCreate) -> StrategySpec:
        compiled = _require_executable(request.spec)
        return self.repository.create(
            compiled.normalized_spec,
            fingerprint=compiled.fingerprint,
            timestamp=audit_now_text(),
        )

    def get(self, strategy_id: int, *, revision: int | None = None) -> StrategySpec:
        return self.repository.strategy(strategy_id, revision=revision)

    def list(
        self,
        *,
        page: int,
        page_size: int,
        include_archived: bool,
    ) -> StrategySpecPage:
        items, total = self.repository.list(
            page=page,
            page_size=page_size,
            include_archived=include_archived,
        )
        return StrategySpecPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            page_count=(total + page_size - 1) // page_size,
        )

    def update(self, strategy_id: int, request: StrategySpecUpdate) -> StrategySpec:
        compiled = _require_executable(request.spec)
        return self.repository.update(
            strategy_id,
            compiled.normalized_spec,
            expected_revision=request.expected_revision,
            fingerprint=compiled.fingerprint,
            timestamp=audit_now_text(),
        )

    def copy(self, strategy_id: int, request: StrategySpecCopyRequest) -> StrategySpec:
        source = self.repository.strategy(strategy_id, revision=request.revision)
        copied = source.spec.model_copy(update={"name": request.name}, deep=True)
        compiled = _require_executable(copied)
        return self.repository.create(
            compiled.normalized_spec,
            fingerprint=compiled.fingerprint,
            timestamp=audit_now_text(),
        )

    def archive(self, strategy_id: int, request: StrategySpecArchiveRequest) -> StrategySpec:
        return self.repository.set_archived(
            strategy_id,
            expected_revision=request.expected_revision,
            archived=request.archived,
            timestamp=audit_now_text(),
        )

    def versions(self, strategy_id: int) -> StrategyVersionPage:
        items = self.repository.versions(strategy_id)
        return StrategyVersionPage(items=items, total=len(items))

    def diff(
        self,
        strategy_id: int,
        *,
        left_revision: int,
        right_revision: int,
    ) -> StrategyVersionDiff:
        left = self.repository.strategy(strategy_id, revision=left_revision)
        right = self.repository.strategy(strategy_id, revision=right_revision)
        return StrategyVersionDiff(
            strategy_id=strategy_id,
            left_revision=left_revision,
            right_revision=right_revision,
            left_fingerprint=left.fingerprint,
            right_fingerprint=right.fingerprint,
            changed_paths=_changed_paths(
                left.spec.model_dump(mode="json"),
                right.spec.model_dump(mode="json"),
            ),
        )


def _require_executable(spec: StrategySpecInput) -> StrategyCompileResponse:
    compiled = compile_strategy_spec(spec)
    if not compiled.execution_plan.executable:
        reasons = [*compiled.execution_plan.blocked_reasons, *compiled.unsupported_clauses]
        raise ValueError("策略尚不可执行：" + "；".join(dict.fromkeys(reasons)))
    return compiled


def _changed_paths(left: object, right: object, *, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "$"]
    if isinstance(left, dict) and isinstance(right, dict):
        return _changed_mapping_paths(left, right, prefix=prefix)
    if isinstance(left, list) and isinstance(right, list):
        if left == right:
            return []
        return [prefix or "$"]
    return [] if left == right else [prefix or "$"]


def _changed_mapping_paths(
    left: dict[object, object],
    right: dict[object, object],
    *,
    prefix: str,
) -> list[str]:
    paths: list[str] = []
    for key in sorted(set(left) | set(right), key=str):
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in left or key not in right:
            paths.append(path)
        else:
            paths.extend(_changed_paths(left[key], right[key], prefix=path))
    return paths


__all__ = ["StrategyLabService"]
