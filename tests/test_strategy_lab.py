from __future__ import annotations

import sqlite3

from pydantic import ValidationError
import pytest

from app.db.schema import initialize_schema
from app.models.strategy_lab import (
    StrategyHardFilter,
    StrategyNaturalLanguageRequest,
    StrategySpecCreate,
    StrategySpecInput,
    StrategySpecUpdate,
    StrategyUniverse,
)
from app.repositories.strategy_lab import StrategyLabRepository, StrategyRevisionConflictError
from app.services.strategy_compiler import compile_strategy_spec, strategy_spec_fingerprint
from app.services.strategy_lab import StrategyLabService
from app.services.strategy_natural_language import parse_chinese_strategy


def test_chinese_parser_returns_structured_draft_defaults_ambiguities_and_dry_run() -> None:
    parsed = parse_chinese_strategy(
        StrategyNaturalLanguageRequest(
            text=(
                "排除 ST 和上市不足 120 天，选择沪深 A 股中近 20 日趋势较强、"
                "成交额超过 1 亿、风险较低的股票，行业最多 3 只，持有 5 天。"
            )
        )
    )

    assert parsed.original_text.startswith("排除 ST")
    assert parsed.requires_confirmation is True
    assert parsed.draft.universe.boards == ["sh_main", "star", "sz_main", "chinext"]
    assert parsed.draft.exclusions.exclude_st is True
    assert parsed.draft.exclusions.min_listing_days == 120
    assert parsed.draft.portfolio_constraints.max_industry_positions == 3
    assert parsed.draft.rebalance_policy.hold_sessions == 5
    assert parsed.draft.hard_filters == [
        StrategyHardFilter(field="amount", operator="gt", value=100_000_000.0)
    ]
    assert any("趋势较强" in item for item in parsed.ambiguities)
    assert any("风险较低" in item for item in parsed.ambiguities)
    assert parsed.compile.execution_plan.will_start_scan is False
    assert parsed.compile.execution_plan.executable is False
    assert "存在需要用户确认的歧义" in parsed.compile.execution_plan.blocked_reasons
    assert "上海A股（主板）" in parsed.compile.execution_plan.board_labels
    assert "科创板" in parsed.compile.execution_plan.board_labels


def test_parser_surfaces_unsupported_fundamental_clause_instead_of_approximating() -> None:
    parsed = parse_chinese_strategy(
        StrategyNaturalLanguageRequest(text="选择市盈率低于20并且成交额超过2亿元的股票")
    )

    assert parsed.unsupported_clauses == ["当前StrategySpec v1尚未接入该条件：市盈率"]
    assert parsed.compile.execution_plan.executable is False
    assert parsed.draft.hard_filters[0].field == "amount"
    assert parsed.draft.hard_filters[0].value == 200_000_000.0


def test_compiler_whitelists_metrics_periods_types_and_never_emits_sql() -> None:
    compiled = compile_strategy_spec(
        StrategySpecInput(
            name="白名单",
            hard_filters=[
                StrategyHardFilter(
                    field="return_pct",
                    operator="gte",
                    value=5.0,
                    period_sessions=20,
                )
            ],
        )
    )

    assert compiled.execution_plan.executable is True
    expression = next(item for item in compiled.execution_plan.expressions if item.field == "return_pct")
    assert expression.source_field.endswith("return_20d_pct")
    assert expression.display == "最近20个交易日 区间收益率 gte 5%"
    assert "SELECT" not in compiled.model_dump_json().upper()
    assert "DROP TABLE" not in compiled.model_dump_json().upper()

    blocked = compile_strategy_spec(
        StrategySpecInput(
            name="未知字段",
            hard_filters=[StrategyHardFilter(field="amount_drop", operator="gte", value=1.0)],
        )
    )
    assert blocked.execution_plan.executable is False
    assert blocked.unsupported_clauses == ["未支持指标：amount_drop"]

    with pytest.raises(ValidationError):
        StrategyHardFilter(field="amount;DROP_TABLE", operator="gte", value=1.0)


def test_fingerprint_is_semantic_stable_and_normalizes_order() -> None:
    first = StrategySpecInput(
        name="原名称",
        description="说明A",
        universe=StrategyUniverse(boards=["beijing", "sh_main", "star"]),
        hard_filters=[
            StrategyHardFilter(field="amount", operator="gte", value=100_000_000.0),
            StrategyHardFilter(field="data_quality_score", operator="gte", value=80),
        ],
    )
    reordered = StrategySpecInput(
        name="重命名不改变执行语义",
        description="说明B",
        universe=StrategyUniverse(boards=["star", "sh_main", "beijing"]),
        hard_filters=list(reversed(first.hard_filters)),
    )
    changed = reordered.model_copy(
        update={
            "hard_filters": [
                StrategyHardFilter(field="amount", operator="gte", value=200_000_000.0),
                reordered.hard_filters[0],
            ]
        }
    )

    assert strategy_spec_fingerprint(first) == strategy_spec_fingerprint(reordered)
    assert strategy_spec_fingerprint(first) != strategy_spec_fingerprint(changed)


def test_strategy_versions_are_immutable_optimistically_locked_and_diffable(tmp_path) -> None:
    service = _service(tmp_path)
    created = service.create(
        StrategySpecCreate(spec=StrategySpecInput(name="策略A"), confirmed=True)
    )
    first_fingerprint = created.fingerprint
    updated_spec = created.spec.model_copy(
        update={
            "hard_filters": [
                StrategyHardFilter(field="amount", operator="gte", value=100_000_000.0)
            ]
        }
    )
    updated = service.update(
        created.strategy_id,
        StrategySpecUpdate(spec=updated_spec, expected_revision=1, confirmed=True),
    )

    assert created.revision == 1
    assert updated.revision == 2
    assert updated.fingerprint != first_fingerprint
    assert service.get(created.strategy_id, revision=1) == created.model_copy(
        update={"current_revision": 2, "updated_at": updated.updated_at}
    )
    assert service.get(created.strategy_id).revision == 2
    assert service.versions(created.strategy_id).total == 2
    diff = service.diff(created.strategy_id, left_revision=1, right_revision=2)
    assert diff.changed_paths == ["hard_filters"]

    with pytest.raises(StrategyRevisionConflictError, match="修订冲突"):
        service.update(
            created.strategy_id,
            StrategySpecUpdate(spec=updated_spec, expected_revision=1, confirmed=True),
        )


def test_copy_and_archive_preserve_source_version_and_fingerprint(tmp_path) -> None:
    service = _service(tmp_path)
    source = service.create(
        StrategySpecCreate(spec=StrategySpecInput(name="源策略"), confirmed=True)
    )
    from app.models.strategy_lab import StrategySpecArchiveRequest, StrategySpecCopyRequest

    copied = service.copy(
        source.strategy_id,
        StrategySpecCopyRequest(name="副本", revision=1, confirmed=True),
    )
    archived = service.archive(
        source.strategy_id,
        StrategySpecArchiveRequest(expected_revision=1, archived=True),
    )

    assert copied.strategy_id != source.strategy_id
    assert copied.fingerprint == source.fingerprint
    assert copied.spec.name == "副本"
    assert archived.archived is True
    assert archived.revision == 1
    assert service.list(page=1, page_size=20, include_archived=False).total == 1
    assert service.list(page=1, page_size=20, include_archived=True).total == 2


def test_schema_is_idempotent_and_versions_cascade_only_with_strategy(tmp_path) -> None:
    path = tmp_path / "schema.sqlite3"
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
        initialize_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"strategy_spec", "strategy_spec_version"} <= tables

    service = StrategyLabService(StrategyLabRepository(path))
    strategy = service.create(
        StrategySpecCreate(spec=StrategySpecInput(name="级联"), confirmed=True)
    )
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM strategy_spec WHERE id = ?", (strategy.strategy_id,))
        assert conn.execute(
            "SELECT COUNT(*) FROM strategy_spec_version WHERE strategy_id = ?",
            (strategy.strategy_id,),
        ).fetchone()[0] == 0


def test_save_requires_explicit_confirmation() -> None:
    with pytest.raises(ValidationError):
        StrategySpecCreate.model_validate({"spec": {"name": "未确认"}, "confirmed": False})


def _service(tmp_path) -> StrategyLabService:
    path = tmp_path / "strategy-lab.sqlite3"
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    return StrategyLabService(StrategyLabRepository(path))
