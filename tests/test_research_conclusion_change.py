from __future__ import annotations

import sqlite3

from app.db.user_mappers import row_to_advice, row_to_advice_timeline
from app.models.schemas import AdviceTimelineItem
from app.services.research_conclusion_change import (
    CONCLUSION_BASIS,
    MODEL_VERSION,
    SNAPSHOT_CONTRACT_VERSION,
    build_conclusion_timeline,
    compare_conclusions,
    conclusion_identity,
)
from app.services.stock_rule_contracts import RULE_VERSION


def _item(row_id: int, **updates: object) -> AdviceTimelineItem:
    values: dict[str, object] = {
        "id": row_id,
        "symbol": "600519.SH",
        "code": "600519",
        "market": "SH",
        "name": "贵州茅台",
        "action": "观察",
        "confidence": 60,
        "trend_score": 65,
        "trend_label": "偏强",
        "risk_level": "中等",
        "price": 1418.88,
        "change_pct": 1.2,
        "support": 1400.01,
        "resistance": 1450.01,
        "data_quality_score": 90,
        "data_quality_level": "良好",
        "data_quality_source": "腾讯行情",
        "reason": "核心分析建议",
        "summary": "保持观察",
        "created_at": f"2026-07-15 10:00:0{row_id}",
        "updated_at": f"2026-07-15 10:00:0{row_id}",
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "conclusion_basis": CONCLUSION_BASIS,
        "rule_version": RULE_VERSION,
        "model_version": MODEL_VERSION,
        "market_time": f"2026-07-15 09:59:0{row_id}",
    }
    values.update(updates)
    return AdviceTimelineItem(**values)


def test_compare_conclusions_emits_six_categories_in_stable_field_order() -> None:
    previous = _item(1)
    current = _item(
        2,
        action="谨慎观察",
        confidence=61,
        trend_label="震荡",
        trend_score=66,
        risk_level="偏高",
        support=1400.02,
        resistance=1450.00,
        data_quality_score=89,
        data_quality_level="一般",
        data_quality_source="备用行情",
    )

    comparison = compare_conclusions(current, previous)

    assert comparison.comparison_status == "comparable"
    assert comparison.has_changes is True
    assert [(change.category, change.field) for change in comparison.changes] == [
        ("action", "action"),
        ("advice", "confidence"),
        ("trend", "trend_label"),
        ("trend", "trend_score"),
        ("risk", "risk_level"),
        ("price_level", "support"),
        ("price_level", "resistance"),
        ("data_quality", "data_quality_score"),
        ("data_quality", "data_quality_level"),
        ("data_quality", "data_quality_source"),
    ]
    assert [change.direction for change in comparison.changes] == [
        "changed",
        "up",
        "changed",
        "up",
        "changed",
        "up",
        "down",
        "down",
        "changed",
        "changed",
    ]
    assert comparison.changes[1].delta == 1
    assert comparison.changes[5].delta == 0.01
    assert comparison.changes[6].delta == -0.01
    assert all(change.comparable for change in comparison.changes)


def test_version_changed_and_legacy_differences_are_neutral() -> None:
    previous = _item(1)
    changed_version = _item(2, confidence=61, rule_version="rules.v3")

    version_comparison = compare_conclusions(changed_version, previous)

    assert version_comparison.comparison_status == "version_changed"
    assert version_comparison.has_changes is True
    assert all(change.comparable is False for change in version_comparison.changes)
    assert all(change.direction == "not_comparable" for change in version_comparison.changes)
    assert all(change.delta is None for change in version_comparison.changes)

    legacy = _item(
        1,
        snapshot_contract_version="legacy",
        conclusion_basis="legacy_unknown",
        rule_version="unknown",
        model_version="unknown",
    )
    legacy_comparison = compare_conclusions(_item(2, confidence=61), legacy)

    assert legacy_comparison.comparison_status == "legacy"
    assert all(change.comparable is False for change in legacy_comparison.changes)
    assert all(change.direction == "not_comparable" for change in legacy_comparison.changes)


def test_dirty_values_do_not_create_false_changes() -> None:
    previous = _item(
        1,
        confidence=None,
        trend_score=None,
        support=None,
        data_quality_source=None,
    )
    current = _item(2, confidence=61, trend_score=66, support=1400.02, data_quality_source="备用行情")

    comparison = compare_conclusions(current, previous)

    assert comparison.comparison_status == "comparable"
    assert comparison.has_changes is False
    assert comparison.changes == ()
    assert conclusion_identity(previous) is None


def test_conclusion_identity_uses_scores_sources_versions_and_price_cents() -> None:
    base = _item(1)
    identity = conclusion_identity(base)

    assert identity is not None
    assert conclusion_identity(_item(2, support=1400.014)) == identity
    for updates in (
        {"confidence": 61},
        {"trend_score": 66},
        {"support": 1400.02},
        {"resistance": 1450.02},
        {"data_quality_score": 89},
        {"data_quality_level": "一般"},
        {"data_quality_source": "备用行情"},
        {"snapshot_contract_version": "conclusion.v2"},
        {"conclusion_basis": "other_basis"},
        {"rule_version": "rules.v3"},
        {"model_version": "model.v1"},
    ):
        assert conclusion_identity(_item(2, **updates)) != identity


def test_conclusion_text_identity_strips_surrounding_whitespace() -> None:
    base = _item(1)
    padded = _item(
        2,
        action=" 观察 ",
        trend_label="偏强 ",
        risk_level=" 中等",
        data_quality_level="良好 ",
        data_quality_source=" 腾讯行情 ",
        snapshot_contract_version=f" {SNAPSHOT_CONTRACT_VERSION} ",
        conclusion_basis=f"{CONCLUSION_BASIS} ",
        rule_version=f" {RULE_VERSION}",
        model_version=f"{MODEL_VERSION} ",
    )

    assert conclusion_identity(padded) == conclusion_identity(base)
    comparison = compare_conclusions(padded, base)
    assert comparison.comparison_status == "comparable"
    assert comparison.has_changes is False
    assert comparison.changes == ()


def test_build_conclusion_timeline_uses_extra_item_as_last_baseline() -> None:
    items = [_item(3, confidence=62), _item(2, confidence=61), _item(1, confidence=60)]

    timeline = build_conclusion_timeline(items, limit=2)

    assert [item.id for item in timeline] == [3, 2]
    assert [item.previous_id for item in timeline] == [2, 1]
    assert [item.comparison_status for item in timeline] == ["comparable", "comparable"]
    assert all(item.has_changes for item in timeline)


def test_build_conclusion_timeline_handles_empty_single_and_non_positive_limit() -> None:
    assert build_conclusion_timeline([], limit=3) == []
    assert build_conclusion_timeline([_item(1)], limit=0) == []

    timeline = build_conclusion_timeline([_item(1)], limit=3)

    assert len(timeline) == 1
    assert timeline[0].previous_id is None
    assert timeline[0].comparison_status == "no_previous"
    assert timeline[0].has_changes is False
    assert timeline[0].changes == []


def test_legacy_mapper_reads_missing_columns_without_fabricating_comparison_values() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT 1 AS id, '600519.SH' AS symbol, '600519' AS code, 'SH' AS market,
               'legacy' AS name, '观察' AS action, 'bad' AS confidence,
               65 AS trend_score, '偏强' AS trend_label, '中等' AS risk_level,
               1418.88 AS price, 1.2 AS change_pct, 'inf' AS support,
               1450.01 AS resistance, 90 AS data_quality_score,
               '良好' AS data_quality_level, 'reason' AS reason,
               'summary' AS summary, '2026-07-15 10:00:00' AS created_at
        """
    ).fetchone()
    assert row is not None

    history = row_to_advice(row)
    timeline = row_to_advice_timeline(row)
    conn.close()

    assert history.confidence == 0
    assert history.support == 0
    assert timeline.confidence is None
    assert timeline.support is None
    assert timeline.snapshot_contract_version == "legacy"
    assert timeline.conclusion_basis == "legacy_unknown"
    assert timeline.rule_version == "unknown"
    assert timeline.model_version == "unknown"
    assert timeline.data_quality_source is None
