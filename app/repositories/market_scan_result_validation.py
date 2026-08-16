from __future__ import annotations

import math
import json
import re
import sqlite3

from app.market_scan_repository_contracts import (
    FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION,
    MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS,
    MARKET_SCAN_SKIP_EVIDENCE_KEY,
    MarketScanScoreReplay,
    is_current_market_scan_score_spec,
    verify_score_details,
    verify_market_scan_skip_evidence,
)
from app.models.market_scan import (
    MARKET_SCAN_DEGRADATION_REASONS,
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MARKET_SCAN_METADATA_DEGRADATION_REASONS,
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanResultWrite,
)
from app.utils.market_time import market_datetime_epoch


def validate_result_write(result: MarketScanResultWrite) -> None:
    if result.status == "pending":
        raise ValueError("待处理状态不是有效的扫描计算结果")
    score_values = (
        result.score,
        result.trend_score,
        result.leader_score,
        result.data_quality_score,
    )
    _require_finite_values(
        result,
        (
            *score_values,
            result.raw_score,
            result.price,
            result.change_pct,
            result.turnover_rate,
            result.volume_ratio,
            result.amount,
            *result.metrics.values(),
        ),
    )
    _require_valid_scores(result, score_values)
    if result.raw_score is not None and not 0 <= result.raw_score <= 100:
        raise ValueError(f"扫描原始评分超出 0-100：{result.symbol}")
    _require_valid_status_fields(result, score_values, raw_score=result.raw_score)
    _require_valid_degradation_fields(result)
    _require_json_score_details(result)


def validate_production_result_write(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    conn: sqlite3.Connection,
) -> None:
    rule_version = str(run["rule_version"] or "")
    if not _is_current_production_run(run, rule_version=rule_version):
        return
    if result.status == "skipped":
        if str(run["mode"] or "") == "official":
            _require_production_skip_evidence(result, run, conn)
        return
    if result.status != "success":
        return
    _require_production_outer_fields(result)
    _require_production_time_contract(result, run)
    if not is_current_market_scan_score_spec(
        result.score_details.get("score_spec"),
        result.score_details.get("score_spec_hash"),
    ):
        raise ValueError(f"生产扫描结果必须使用当前注册的 v5 评分规范：{result.symbol}")
    _require_run_score_contract(result, run, conn)
    replay = verify_score_details(
        result.score_details,
        expected_leader_score=result.leader_score,
        expected_final_score=result.score,
    )
    _require_production_replay_identity(result, replay, rule_version=rule_version)
    _require_production_replay_inputs(result, replay)


def validate_persisted_production_skips(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
) -> int:
    rule_version = str(run["rule_version"] or "")
    if (
        not _is_current_production_run(run, rule_version=rule_version)
        or str(run["mode"] or "") != "official"
    ):
        return 0
    rows = conn.execute(
        """
        SELECT symbol, reason, data_date, quote_timestamp, quote_observed_at,
               quote_source, kline_source, adjustment_mode, metrics_json
        FROM market_scan_result
        WHERE run_id = ? AND status = 'skipped'
        ORDER BY symbol ASC
        """,
        (run["id"],),
    ).fetchall()
    for row in rows:
        details = _persisted_score_details(row["metrics_json"])
        result = MarketScanResultWrite(
            symbol=str(row["symbol"]),
            status="skipped",
            reason=row["reason"],
            data_date=row["data_date"],
            quote_timestamp=row["quote_timestamp"],
            quote_observed_at=row["quote_observed_at"],
            quote_source=row["quote_source"],
            kline_source=row["kline_source"],
            adjustment_mode=row["adjustment_mode"],
            score_details=details,
        )
        _require_production_skip_evidence(result, run, conn)
    return len(rows)


def _persisted_score_details(value: object) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError) as exc:
        raise ValueError("生产扫描跳过结果负载不是有效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("_schema") != "market-scan-result-payload-v2":
        raise ValueError("生产扫描跳过结果负载合同无效")
    details = payload.get("score_details")
    if not isinstance(details, dict):
        raise ValueError("生产扫描跳过结果缺少结构化证据")
    return details


def _require_run_score_contract(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    conn: sqlite3.Connection,
) -> None:
    contract = _required_run_score_contract(result, run, conn)
    score_spec = result.score_details.get("score_spec")
    score_rule = score_spec.get("rule_version") if isinstance(score_spec, dict) else None
    if (
        score_rule != contract["production_score_rule_version"]
        or result.score_details.get("score_spec_hash")
        != contract["production_score_spec_hash"]
    ):
        raise ValueError(f"生产扫描结果与批次评分合同不一致：{result.symbol}")


def _required_run_score_contract(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    conn: sqlite3.Connection,
) -> sqlite3.Row:
    contract = conn.execute(
        """
        SELECT contract_json, production_score_rule_version, production_score_spec_hash
        FROM market_scan_rule_contract
        WHERE rule_version = ?
        """,
        (run["rule_version"],),
    ).fetchone()
    if contract is None:
        raise ValueError(f"生产扫描批次缺少封存的评分合同：{result.symbol}")
    return contract


def _require_production_skip_evidence(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    conn: sqlite3.Connection,
) -> None:
    _required_run_score_contract(result, run, conn)
    if str(run["stock_pool_source"] or "") != "provider-full-pool":
        raise ValueError(f"生产扫描跳过结果缺少新鲜权威股票池：{result.symbol}")
    evidence = result.score_details.get(MARKET_SCAN_SKIP_EVIDENCE_KEY)
    seed = _result_seed_contract(conn, run, result.symbol)
    min_history_rows, new_stock_days = _run_skip_thresholds(result, run, conn)
    if not verify_market_scan_skip_evidence(
        evidence,
        expected_symbol=result.symbol,
        expected_code=seed[0],
        expected_market=seed[1],
        expected_name=seed[2],
        expected_metadata_source=seed[3],
        expected_is_new=seed[4],
        expected_list_date=seed[5],
        expected_mode=run["mode"],
        expected_rule_version=run["rule_version"],
        expected_as_of=run["as_of"],
        expected_data_date=run["data_date"],
        expected_quote_date=run["quote_date"],
        expected_min_history_rows=min_history_rows,
        expected_new_stock_days=new_stock_days,
        expected_reason=result.reason,
        expected_quote_timestamp=result.quote_timestamp,
        expected_quote_observed_at=result.quote_observed_at,
        expected_quote_source=result.quote_source,
        expected_kline_source=result.kline_source,
        expected_adjustment_mode=result.adjustment_mode,
    ):
        raise ValueError(f"生产扫描跳过结果缺少可信会话缺口证据：{result.symbol}")


def _result_seed_contract(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    symbol: str,
) -> tuple[object, object, object, object, bool, object]:
    row = conn.execute(
        """
        SELECT code, market, name, metadata_source, is_new, list_date
        FROM market_scan_result
        WHERE run_id = ? AND symbol = ?
        """,
        (run["id"], symbol),
    ).fetchone()
    if row is None:
        raise ValueError(f"生产扫描跳过结果不属于批次股票池：{symbol}")
    return (
        row["code"],
        row["market"],
        row["name"],
        row["metadata_source"],
        bool(row["is_new"]),
        row["list_date"],
    )


def _run_skip_thresholds(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    conn: sqlite3.Connection,
) -> tuple[int, int]:
    contract = _required_run_score_contract(result, run, conn)
    try:
        payload = json.loads(str(contract["contract_json"]))
        history = payload["history"]
        universe = payload["universe"]
        min_history_rows = history["min_history_rows"]
        new_stock_days = universe["new_stock_days"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"生产扫描批次规则合同缺少跳过阈值：{result.symbol}") from exc
    if (
        isinstance(min_history_rows, bool)
        or not isinstance(min_history_rows, int)
        or min_history_rows < 61
        or isinstance(new_stock_days, bool)
        or not isinstance(new_stock_days, int)
        or new_stock_days <= 0
    ):
        raise ValueError(f"生产扫描批次跳过阈值无效：{result.symbol}")
    return min_history_rows, new_stock_days


def _is_current_production_run(
    run: sqlite3.Row,
    *,
    rule_version: str,
) -> bool:
    return (
        re.fullmatch(r"full-market-scan-v6:[0-9a-f]{64}", rule_version) is not None
        and str(run["scope"] or "")
        in {MARKET_SCAN_FULL_MARKET_SCOPE, MARKET_SCAN_TOP100_REFRESH_SCOPE}
    )


def _require_production_replay_identity(
    result: MarketScanResultWrite,
    replay: MarketScanScoreReplay,
    *,
    rule_version: str,
) -> None:
    if replay.score_spec_schema_version != FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION:
        raise ValueError(f"生产扫描结果必须使用当前评分规范：{result.symbol}")
    if result.raw_score is None or not _same_numeric(result.raw_score, replay.raw_score):
        raise ValueError(f"扫描 outer raw_score 与评分重放不一致：{result.symbol}")
    if replay.tie_break_values.get("symbol") != result.symbol:
        raise ValueError(f"扫描 outer symbol 与评分明细不一致：{result.symbol}")
    if result.score_details.get("run_rule_version") != rule_version:
        raise ValueError(f"扫描评分明细与批次规则版本不一致：{result.symbol}")
    if result.score_details.get("semantics") != MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS:
        raise ValueError(f"生产评分缺少明确成本、基准与可执行语义：{result.symbol}")


def _require_production_replay_inputs(
    result: MarketScanResultWrite,
    replay: MarketScanScoreReplay,
) -> None:
    expected_inputs = {
        "trend_score": result.trend_score,
        "change_pct": result.change_pct,
        "volume_ratio": result.volume_ratio,
        "amount": result.amount,
        "turnover_rate": result.turnover_rate,
        "data_quality_score": result.data_quality_score,
    }
    if any(
        value is None or not _same_numeric(replay.inputs.get(name), value)
        for name, value in expected_inputs.items()
    ):
        raise ValueError(f"扫描 outer fields 与评分输入不一致：{result.symbol}")


def _require_production_outer_fields(result: MarketScanResultWrite) -> None:
    values = {
        "change_pct": result.change_pct,
        "turnover_rate": result.turnover_rate,
        "volume_ratio": result.volume_ratio,
        "amount": result.amount,
    }
    if any(value is None for value in values.values()):
        raise ValueError(f"生产扫描成功结果缺少排名输入：{result.symbol}")
    assert result.turnover_rate is not None
    assert result.volume_ratio is not None
    assert result.amount is not None
    if result.turnover_rate < 0 or result.volume_ratio <= 0 or result.amount <= 0:
        raise ValueError(f"生产扫描成功结果排名输入超出有效范围：{result.symbol}")


def _require_production_time_contract(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
) -> None:
    decision_epoch = market_datetime_epoch(run["as_of"])
    event_epoch = market_datetime_epoch(result.quote_timestamp)
    observed_epoch = market_datetime_epoch(result.quote_observed_at)
    available_epoch = market_datetime_epoch(run["quote_capture_finished_at"])
    updated_epoch = market_datetime_epoch(run["updated_at"])
    if (
        decision_epoch is None
        or event_epoch is None
        or observed_epoch is None
        or available_epoch is None
        or updated_epoch is None
        or event_epoch > observed_epoch
        or observed_epoch > decision_epoch
        or decision_epoch > available_epoch
        or decision_epoch > updated_epoch
    ):
        raise ValueError(f"生产扫描结果报价事件/观测/决策/可用时点顺序无效：{result.symbol}")


def _same_numeric(left: object, right: object) -> bool:
    if (
        isinstance(left, bool)
        or isinstance(right, bool)
        or not isinstance(left, int | float)
        or not isinstance(right, int | float)
    ):
        return False
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-8)


def _require_finite_values(
    result: MarketScanResultWrite,
    values: tuple[int | float | None, ...],
) -> None:
    if any(value is not None and not math.isfinite(float(value)) for value in values):
        raise ValueError(f"扫描结果包含非有限数值：{result.symbol}")


def _require_valid_scores(result: MarketScanResultWrite, values: tuple[int | None, ...]) -> None:
    if any(value is not None and not 0 <= int(value) <= 100 for value in values):
        raise ValueError(f"扫描评分超出 0-100：{result.symbol}")


def _require_valid_status_fields(
    result: MarketScanResultWrite,
    scores: tuple[int | None, ...],
    *,
    raw_score: float | None,
) -> None:
    if result.status == "success":
        _require_success_fields(result, scores, raw_score=raw_score)
        return
    if any(value is not None for value in (*scores, raw_score)):
        raise ValueError(f"非成功扫描结果不得携带评分：{result.symbol}")
    if result.status == "missing" and not str(result.error or "").strip():
        raise ValueError(f"缺失扫描结果必须记录错误原因：{result.symbol}")
    if result.status == "skipped" and not str(result.reason or "").strip():
        raise ValueError(f"跳过扫描结果必须记录跳过原因：{result.symbol}")


def _require_success_fields(
    result: MarketScanResultWrite,
    scores: tuple[int | None, ...],
    *,
    raw_score: float | None,
) -> None:
    if any(value is None for value in scores) or not result.data_date:
        raise ValueError(f"成功扫描结果缺少评分或数据日期：{result.symbol}")
    if result.price is None or result.price <= 0:
        raise ValueError(f"成功扫描结果缺少有效价格：{result.symbol}")
    provenance = (result.quote_timestamp, result.quote_source, result.kline_source, result.reason)
    if not all(str(value or "").strip() for value in provenance):
        raise ValueError(f"成功扫描结果缺少数据来源或评分依据：{result.symbol}")
    if result.adjustment_mode != "qfq":
        raise ValueError(f"成功扫描结果不是前复权数据：{result.symbol}")
    if not result.metrics:
        raise ValueError(f"成功扫描结果缺少指标快照：{result.symbol}")


def _require_valid_degradation_fields(result: MarketScanResultWrite) -> None:
    reasons = tuple(reason.strip() for reason in result.degradation_reasons)
    if any(not reason for reason in reasons) or len(reasons) != len(set(reasons)):
        raise ValueError(f"扫描结果降级原因无效或重复：{result.symbol}")
    reason_set = set(reasons)
    metadata_reasons = reason_set & MARKET_SCAN_METADATA_DEGRADATION_REASONS
    flags_match = (
        result.quote_fallback_used == ("quote_fallback" in reason_set)
        and result.kline_fallback_used == ("kline_fallback" in reason_set)
        and result.metadata_degraded == bool(metadata_reasons)
    )
    legacy_mixed_with_specific = "metadata_incomplete" in metadata_reasons and len(metadata_reasons) > 1
    if reason_set - MARKET_SCAN_DEGRADATION_REASONS or not flags_match or legacy_mixed_with_specific:
        raise ValueError(f"扫描结果降级标记与原因不一致：{result.symbol}")


def _require_json_score_details(result: MarketScanResultWrite) -> None:
    try:
        _require_json_value(result.score_details)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"扫描评分明细不是有效 JSON：{result.symbol}") from exc


def _require_json_value(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON 数值必须有限")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _require_json_value(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON 对象键必须是字符串")
        for item in value.values():
            _require_json_value(item)
        return
    raise TypeError(f"不支持的 JSON 类型：{type(value).__name__}")


__all__ = [
    "validate_persisted_production_skips",
    "validate_production_result_write",
    "validate_result_write",
]
