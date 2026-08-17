"""Immutable artifacts for fixed-session future-range research.

The SHA-256 value is an integrity digest rather than a digital signature.
This module never opens or mutates SQLite; callers pass the database path only
so an artifact can never accidentally replace it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
from pathlib import Path
from typing import cast

from app.artifacts.io import (
    ArtifactCanonicalJsonError,
    ArtifactContentConflictError,
    ArtifactDuplicateKeyError,
    ArtifactIOError,
    ArtifactNonFiniteConstantError,
    ArtifactPublishConflictError,
    canonical_json_text,
    content_addressed_filename,
    decode_json_bytes,
    read_regular_file,
    sha256_hex,
)
from app.db.market_scan_artifact_lease import (
    MarketScanArtifactLeaseError,
    publish_market_scan_artifact,
    require_project_managed_artifact_database,
    verified_market_scan_artifact_publication,
)

exclusive_atomic_publish = publish_market_scan_artifact


FUTURE_RANGE_ARTIFACT_SCHEMA_VERSION = "market-scan-future-range-artifact-v1"
FUTURE_RANGE_REPORT_CONTRACT_VERSION = "market-scan-future-range-report-v1"
FUTURE_RANGE_ARTIFACT_DIGEST_ALGORITHM = "sha256"
FUTURE_RANGE_ARTIFACT_DIGEST_SCOPE = "payload"
FUTURE_RANGE_ARTIFACT_INTEGRITY_NOTICE = "integrity_digest_not_a_signature"
FUTURE_RANGE_ARTIFACT_MAX_BYTES = 128 * 1024 * 1024
FUTURE_RANGE_MANAGED_DIRECTORY = Path("research/market_scan_future_range")

_TOP_LEVEL_KEYS = frozenset({"schema_version", "generated_at", "payload", "integrity"})
_INTEGRITY_KEYS = frozenset({"algorithm", "scope", "integrity_digest", "notice"})
_PAYLOAD_KEYS = frozenset(
    {
        "report_contract_version",
        "status",
        "generated_at",
        "run",
        "config",
        "source",
        "records",
        "groups",
        "rank_ic",
        "monotonicity",
        "probability_context",
        "limitations",
    }
)


class FutureRangeArtifactError(ValueError):
    """Raised when a future-range artifact is malformed or unsafe."""


def canonical_future_range_artifact_json(value: object) -> str:
    """Return canonical, finite JSON suitable for hashing and persistence."""
    normalized = _json_value(value, "JSON")
    try:
        return canonical_json_text(normalized)
    except ArtifactCanonicalJsonError as exc:  # pragma: no cover - defensive
        raise FutureRangeArtifactError("未来区间 artifact 不是有限 JSON") from exc


def future_range_payload_integrity_digest(payload: Mapping[str, object]) -> str:
    canonical = canonical_future_range_artifact_json(payload)
    return sha256_hex(canonical)


def build_future_range_artifact(
    payload: Mapping[str, object],
    *,
    generated_at: str,
) -> dict[str, object]:
    """Build a verified schema-v1 wrapper around one self-contained run report."""
    normalized = _validate_payload(payload, generated_at=generated_at)
    artifact: dict[str, object] = {
        "schema_version": FUTURE_RANGE_ARTIFACT_SCHEMA_VERSION,
        "generated_at": _required_text(generated_at, "generated_at"),
        "payload": normalized,
        "integrity": {
            "algorithm": FUTURE_RANGE_ARTIFACT_DIGEST_ALGORITHM,
            "scope": FUTURE_RANGE_ARTIFACT_DIGEST_SCOPE,
            "integrity_digest": future_range_payload_integrity_digest(normalized),
            "notice": FUTURE_RANGE_ARTIFACT_INTEGRITY_NOTICE,
        },
    }
    return verify_future_range_artifact(artifact)


def verify_future_range_artifact(artifact: Mapping[str, object]) -> dict[str, object]:
    """Fail closed unless structure, versions, and payload SHA-256 agree."""
    normalized = cast(dict[str, object], _json_value(artifact, "artifact"))
    _require_exact_keys(normalized, _TOP_LEVEL_KEYS, "artifact")
    if normalized["schema_version"] != FUTURE_RANGE_ARTIFACT_SCHEMA_VERSION:
        raise FutureRangeArtifactError("未来区间 artifact schema_version 不受支持")
    generated_at = _required_text(normalized["generated_at"], "artifact.generated_at")
    payload = _required_mapping(normalized["payload"], "artifact.payload")
    integrity = _required_mapping(normalized["integrity"], "artifact.integrity")
    _require_exact_keys(integrity, _INTEGRITY_KEYS, "artifact.integrity")
    expected_integrity = {
        "algorithm": FUTURE_RANGE_ARTIFACT_DIGEST_ALGORITHM,
        "scope": FUTURE_RANGE_ARTIFACT_DIGEST_SCOPE,
        "notice": FUTURE_RANGE_ARTIFACT_INTEGRITY_NOTICE,
    }
    if any(integrity.get(key) != value for key, value in expected_integrity.items()):
        raise FutureRangeArtifactError("未来区间 artifact integrity contract 冲突")
    digest = _required_sha256(integrity.get("integrity_digest"), "integrity_digest")
    validated_payload = _validate_payload(payload, generated_at=generated_at)
    if digest != future_range_payload_integrity_digest(validated_payload):
        raise FutureRangeArtifactError("未来区间 artifact integrity digest 不一致")
    return {
        "schema_version": FUTURE_RANGE_ARTIFACT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": validated_payload,
        "integrity": dict(integrity),
    }


def replay_future_range_artifact(artifact: Mapping[str, object]) -> dict[str, object]:
    """Verify and return the self-contained report for offline/restart replay."""
    verified = verify_future_range_artifact(artifact)
    return deepcopy(cast(dict[str, object], verified["payload"]))


def write_future_range_artifact(
    path: str | Path,
    artifact: Mapping[str, object],
    *,
    database_path: str | Path,
) -> Path:
    """Atomically publish immutable content without replacing the database."""
    target = Path(path).expanduser().absolute()
    database = Path(database_path).expanduser().resolve()
    _reject_database_target(target, database)
    try:
        require_project_managed_artifact_database(target, database, FUTURE_RANGE_MANAGED_DIRECTORY)
    except MarketScanArtifactLeaseError as exc:
        raise FutureRangeArtifactError(str(exc)) from exc
    verified = verify_future_range_artifact(artifact)
    encoded = canonical_future_range_artifact_json(verified).encode("utf-8")
    payload = cast(Mapping[str, object], verified["payload"])
    run = cast(Mapping[str, object], payload["run"])
    try:
        with verified_market_scan_artifact_publication(
            database,
            target,
            (cast(int, run["run_id"]),),
            managed_directory=FUTURE_RANGE_MANAGED_DIRECTORY,
        ):
            exclusive_atomic_publish(
                target,
                encoded,
                max_bytes=FUTURE_RANGE_ARTIFACT_MAX_BYTES,
                before_publish=lambda: _reject_database_target(target, database),
            )
    except ArtifactContentConflictError as exc:
        raise FutureRangeArtifactError("未来区间 artifact 已存在且内容不同，拒绝覆盖") from exc
    except ArtifactPublishConflictError as exc:
        raise FutureRangeArtifactError("未来区间 artifact 并发发布冲突") from exc
    except ArtifactIOError as exc:
        raise FutureRangeArtifactError(f"未来区间 artifact 写入失败：{target}") from exc
    except FutureRangeArtifactError:
        raise
    except MarketScanArtifactLeaseError as exc:
        raise FutureRangeArtifactError("未来区间 artifact 来源批次已失效") from exc
    except OSError as exc:
        raise FutureRangeArtifactError(f"未来区间 artifact 写入失败：{target}") from exc
    return target


def load_future_range_artifact(path: str | Path) -> dict[str, object]:
    """Read and strictly verify an artifact, including duplicate-key rejection."""
    source = Path(path).expanduser().absolute()
    try:
        decoded = decode_json_bytes(read_regular_file(source, max_bytes=FUTURE_RANGE_ARTIFACT_MAX_BYTES))
    except ArtifactDuplicateKeyError as exc:
        raise FutureRangeArtifactError(f"未来区间 artifact 包含重复 JSON key：{exc.key}") from exc
    except ArtifactNonFiniteConstantError as exc:
        raise FutureRangeArtifactError(f"未来区间 artifact 包含非法常量：{exc.constant}") from exc
    except ArtifactIOError as exc:
        raise FutureRangeArtifactError(f"未来区间 artifact 读取失败：{source}") from exc
    if not isinstance(decoded, Mapping):
        raise FutureRangeArtifactError("未来区间 artifact 顶层必须是 JSON object")
    return verify_future_range_artifact(cast(Mapping[str, object], decoded))


def future_range_artifact_filename(run_id: int, artifact: Mapping[str, object]) -> str:
    """Return the content-addressed canonical filename for one run artifact."""
    if isinstance(run_id, bool) or run_id <= 0:
        raise FutureRangeArtifactError("run_id 必须是正整数")
    verified = verify_future_range_artifact(artifact)
    payload = cast(Mapping[str, object], verified["payload"])
    run = _required_mapping(payload["run"], "payload.run")
    if run.get("run_id") != run_id:
        raise FutureRangeArtifactError("文件 run_id 与 payload.run_id 不一致")
    integrity = cast(Mapping[str, object], verified["integrity"])
    return content_addressed_filename(
        "market-scan-future-range-run",
        (run_id,),
        cast(str, integrity["integrity_digest"]),
        ".json",
    )


def _validate_payload(payload: Mapping[str, object], *, generated_at: str) -> dict[str, object]:
    normalized = cast(dict[str, object], _json_value(payload, "payload"))
    _require_exact_keys(normalized, _PAYLOAD_KEYS, "payload")
    _validate_payload_identity(normalized, generated_at)
    _validate_payload_contracts(normalized)
    _validate_payload_collections(normalized)
    return normalized


def _validate_payload_identity(payload: Mapping[str, object], generated_at: str) -> None:
    if payload["report_contract_version"] != FUTURE_RANGE_REPORT_CONTRACT_VERSION:
        raise FutureRangeArtifactError("未来区间 report contract_version 不受支持")
    if payload["status"] not in {"ok", "insufficient_data"}:
        raise FutureRangeArtifactError("未来区间 payload.status 无效")
    if payload["generated_at"] != _required_text(generated_at, "generated_at"):
        raise FutureRangeArtifactError("未来区间 payload.generated_at 与 artifact 不一致")


def _validate_payload_contracts(payload: Mapping[str, object]) -> None:
    run = _required_mapping(payload["run"], "payload.run")
    if run.get("mode") != "official":
        raise FutureRangeArtifactError("未来区间研究仅接受 official 批次")
    if isinstance(run.get("run_id"), bool) or not isinstance(run.get("run_id"), int) or cast(int, run["run_id"]) <= 0:
        raise FutureRangeArtifactError("未来区间 payload.run_id 无效")
    if run.get("snapshot_digest") is not None:
        _required_sha256(run.get("snapshot_digest"), "payload.run.snapshot_digest")
    config = _required_mapping(payload["config"], "payload.config")
    if config.get("session_offsets") != [1, 2, 3]:
        raise FutureRangeArtifactError("未来区间 session_offsets 必须固定为 [1,2,3]")
    if config.get("center_proxy") != "HLC3_proxy_not_VWAP":
        raise FutureRangeArtifactError("未来区间中枢必须明确为 HLC3 proxy，不能伪称 VWAP")
    source = _required_mapping(payload["source"], "payload.source")
    if source.get("read_only") is not True or source.get("adjustment_mode") != "qfq":
        raise FutureRangeArtifactError("未来区间 source 必须是只读 qfq")


def _validate_payload_collections(payload: Mapping[str, object]) -> None:
    probability = _required_mapping(payload["probability_context"], "payload.probability_context")
    if probability.get("status") not in {"available", "not_available"}:
        raise FutureRangeArtifactError("未来区间 probability_context.status 无效")
    for name in ("records", "groups", "rank_ic", "monotonicity", "limitations"):
        if not isinstance(payload[name], list):
            raise FutureRangeArtifactError(f"未来区间 payload.{name} 必须是数组")
    _validate_records(payload)
    _validate_aggregate_contracts(payload)


def _validate_records(payload: Mapping[str, object]) -> None:
    run = _required_mapping(payload["run"], "payload.run")
    records = cast(list[object], payload["records"])
    symbols: set[str] = set()
    for raw in records:
        record = _required_mapping(raw, "payload.records[]")
        symbol = _required_text(record.get("symbol"), "record.symbol")
        if symbol in symbols:
            raise FutureRangeArtifactError(f"未来区间 record symbol 重复：{symbol}")
        symbols.add(symbol)
        if record.get("run_id") != run["run_id"] or record.get("quote_date") != run.get("quote_date"):
            raise FutureRangeArtifactError("未来区间 record 身份与 payload.run 冲突")
        d_bar = _validated_bar(record.get("d_bar"), "record.d_bar")
        source = _required_mapping(record.get("source_evidence"), "record.source_evidence")
        if source.get("status") != "verified":
            raise FutureRangeArtifactError("未来区间 record 缺少已验证点时证据")
        _required_sha256(source.get("payload_digest"), "record.source_evidence.payload_digest")
        _validate_record_probability(record.get("probability"))
        _validate_record_offsets(record.get("offsets"), d_bar)


def _validated_bar(value: object, label: str) -> dict[str, object]:
    bar = _required_mapping(value, label)
    numbers = {name: _finite_number(bar.get(name), f"{label}.{name}") for name in ("open", "high", "low", "close", "volume", "hlc3_proxy")}
    if min(numbers["open"], numbers["high"], numbers["low"], numbers["close"]) <= 0 or numbers["volume"] < 0:
        raise FutureRangeArtifactError(f"{label} 价格/成交量无效")
    if numbers["high"] < max(numbers["open"], numbers["close"], numbers["low"]) or numbers["low"] > min(numbers["open"], numbers["close"], numbers["high"]):
        raise FutureRangeArtifactError(f"{label} OHLC 关系无效")
    if not math.isclose(numbers["hlc3_proxy"], (numbers["high"] + numbers["low"] + numbers["close"]) / 3, rel_tol=0, abs_tol=1e-9):
        raise FutureRangeArtifactError(f"{label} HLC3 proxy 计算不一致")
    if bar.get("adjustment_mode") != "qfq" or bar.get("contract_version") != "daily-kline.v1":
        raise FutureRangeArtifactError(f"{label} qfq/contract 冲突")
    _required_text(bar.get("data_version"), f"{label}.data_version")
    _required_text(bar.get("date"), f"{label}.date")
    return bar


def _validate_record_probability(value: object) -> None:
    probability = _required_mapping(value, "record.probability")
    predictions = probability.get("predictions")
    if not isinstance(predictions, list):
        raise FutureRangeArtifactError("record.probability.predictions 必须是数组")
    if probability.get("status") == "not_available" and predictions:
        raise FutureRangeArtifactError("not_available 概率不能携带预测")
    if probability.get("status") not in {"not_available", "calibrated_shadow"}:
        raise FutureRangeArtifactError("record.probability.status 无效")
    if probability.get("status") == "calibrated_shadow" and not predictions:
        raise FutureRangeArtifactError("calibrated_shadow 概率预测不能为空")
    for raw in predictions:
        prediction = _required_mapping(raw, "record.probability.predictions[]")
        number = _finite_number(prediction.get("probability"), "prediction.probability")
        if not 0 <= number <= 1 or not prediction.get("source_artifact_digest"):
            raise FutureRangeArtifactError("record calibrated_shadow 概率或来源无效")


def _validate_record_offsets(value: object, d_bar: Mapping[str, object]) -> None:
    if not isinstance(value, list) or [item.get("session_offset") if isinstance(item, Mapping) else None for item in value] != [1, 2, 3]:
        raise FutureRangeArtifactError("record.offsets 必须严格覆盖 [1,2,3]")
    target_bars: list[Mapping[str, object] | None] = []
    for raw in value:
        offset = _required_mapping(raw, "record.offsets[]")
        target_bars.append(_validate_offset(offset, d_bar))
    for offset, target in zip(value, target_bars, strict=True):
        item = _required_mapping(offset, "record.offsets[]")
        _validate_d1_path(item, target, target_bars)
        _validate_execution(item.get("execution"), cast(int, item["session_offset"]), target_bars)


def _validate_offset(
    offset: Mapping[str, object],
    d_bar: Mapping[str, object],
) -> Mapping[str, object] | None:
    status = offset.get("fixed_session_status")
    if status not in {"available", "not_mature", "unavailable"}:
        raise FutureRangeArtifactError("record offset status 无效")
    if status != "available":
        if any(
            offset.get(name) is not None
            for name in (
                "target_bar",
                "target_bar_digest",
                "level_shift",
                "d_close_reference",
                "d1_open_reference",
                "interval_structure",
                "daily_bar_path_unknown",
            )
        ):
            raise FutureRangeArtifactError("不可用 offset 不能携带伪造 outcome")
        return None
    target = _validated_bar(offset.get("target_bar"), "record.offset.target_bar")
    if target.get("date") != offset.get("target_session_date") or offset.get("daily_bar_path_unknown") is not True:
        raise FutureRangeArtifactError("可用 offset 日期或日线路径声明无效")
    if offset.get("target_bar_digest") != _target_bar_digest(target):
        raise FutureRangeArtifactError("target_bar_digest 与 target bar 不一致")
    _validate_return_mapping(offset.get("level_shift"), target, d_bar, ("low", "hlc3_proxy", "high"), "level_shift")
    d_close = {name: d_bar["close"] for name in ("low", "hlc3_proxy", "high", "close")}
    _validate_return_mapping(offset.get("d_close_reference"), target, d_close, tuple(d_close), "d_close_reference")
    return target


def _validate_return_mapping(
    value: object,
    numerators: Mapping[str, object],
    denominators: Mapping[str, object],
    names: tuple[str, ...],
    label: str,
) -> None:
    returns = _required_mapping(value, label)
    for name in names:
        actual = _finite_number(returns.get(name), f"{label}.{name}")
        expected = float(cast(float, numerators[name])) / float(cast(float, denominators[name])) - 1
        if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9):
            raise FutureRangeArtifactError(f"{label}.{name} 不能由持久化OHLC重放")


def _validate_d1_path(
    offset: Mapping[str, object],
    target: Mapping[str, object] | None,
    target_bars: Sequence[Mapping[str, object] | None],
) -> None:
    if target is None:
        return
    reference = _required_mapping(offset.get("d1_open_reference"), "d1_open_reference")
    path = target_bars[: cast(int, offset["session_offset"])]
    if any(item is None for item in path):
        _validate_unavailable_d1_path(reference)
        return
    concrete = cast(list[Mapping[str, object]], path)
    _validate_available_d1_path(reference, target, concrete)


def _validate_unavailable_d1_path(reference: Mapping[str, object]) -> None:
    if reference.get("status") != "unavailable" or reference.get("cumulative_path") is not None:
        raise FutureRangeArtifactError("缺失固定交易日时 cumulative path 必须不可用")


def _validate_available_d1_path(
    reference: Mapping[str, object],
    target: Mapping[str, object],
    concrete: Sequence[Mapping[str, object]],
) -> None:
    entry = float(cast(float, concrete[0]["open"]))
    if reference.get("status") != "available" or not math.isclose(_finite_number(reference.get("entry_price"), "entry_price"), entry, rel_tol=0, abs_tol=1e-9):
        raise FutureRangeArtifactError("D+1 open reference 无效")
    if reference.get("entry_date") != concrete[0]["date"]:
        raise FutureRangeArtifactError("D+1 open reference entry_date 无效")
    _validate_return_mapping(
        reference.get("specified_day"),
        target,
        {name: entry for name in ("low", "hlc3_proxy", "high", "close")},
        ("low", "hlc3_proxy", "high", "close"),
        "specified_day",
    )
    cumulative = _required_mapping(reference.get("cumulative_path"), "cumulative_path")
    expected = {
        "mae": min(float(cast(float, item["low"])) for item in concrete) / entry - 1,
        "mfe": max(float(cast(float, item["high"])) for item in concrete) / entry - 1,
        "terminal_close_return": float(cast(float, target["close"])) / entry - 1,
    }
    if cumulative.get("daily_bar_path_unknown") is not True:
        raise FutureRangeArtifactError("cumulative path 必须声明日线内先后未知")
    if not _outcomes_match(cumulative, expected):
        raise FutureRangeArtifactError("cumulative MFE/MAE/终值不能由固定OHLC路径重放")


def _outcomes_match(actual: Mapping[str, object], expected: Mapping[str, float]) -> bool:
    return all(math.isclose(_finite_number(actual.get(name), name), value, rel_tol=0, abs_tol=1e-9) for name, value in expected.items())


def _validate_execution(
    value: object,
    session_offset: int,
    target_bars: Sequence[Mapping[str, object] | None],
) -> None:
    execution = _required_mapping(value, "offset.execution")
    status = execution.get("status")
    if status not in {"modelled", "unfilled", "data_unavailable"}:
        raise FutureRangeArtifactError("offset.execution.status 无效")
    if session_offset == 1 and (status != "data_unavailable" or execution.get("reason") != "A_share_T_plus_1_no_same_session_exit"):
        raise FutureRangeArtifactError("D+1 execution 必须明确受 A股T+1约束")
    if status != "modelled":
        _validate_unmodelled_execution(execution)
        return
    _validate_modelled_execution_identity(execution, session_offset, target_bars)
    gross = _finite_number(execution.get("gross_return"), "execution.gross_return")
    net = _finite_number(execution.get("net_return"), "execution.net_return")
    drag = _finite_number(execution.get("cost_drag"), "execution.cost_drag")
    if not math.isclose(gross - net, drag, rel_tol=0, abs_tol=1e-9):
        raise FutureRangeArtifactError("execution cost_drag 不能由 gross/net 重放")
    benchmark = execution.get("market_benchmark_net_return")
    excess = execution.get("net_excess_return")
    if benchmark is not None and not math.isclose(net - _finite_number(benchmark, "benchmark"), _finite_number(excess, "net_excess"), rel_tol=0, abs_tol=1e-9):
        raise FutureRangeArtifactError("execution net_excess 不能由 net/benchmark 重放")


def _validate_unmodelled_execution(execution: Mapping[str, object]) -> None:
    return_fields = (
        "gross_return",
        "net_return",
        "cost_drag",
        "market_benchmark_net_return",
        "net_excess_return",
        "entry_price",
        "exit_price",
    )
    if any(execution.get(name) is not None for name in return_fields):
        raise FutureRangeArtifactError("非 modelled execution 不能携带收益或成交价格")


def _validate_modelled_execution_identity(
    execution: Mapping[str, object],
    session_offset: int,
    target_bars: Sequence[Mapping[str, object] | None],
) -> None:
    entry = target_bars[0]
    target = target_bars[session_offset - 1]
    if entry is None or target is None:
        raise FutureRangeArtifactError("modelled execution 缺少固定交易日bar")
    entry_price = _finite_number(execution.get("entry_price"), "execution.entry_price")
    exit_price = _finite_number(execution.get("exit_price"), "execution.exit_price")
    if execution.get("entry_date") != entry["date"] or not math.isclose(entry_price, float(cast(float, entry["open"])), rel_tol=0, abs_tol=1e-9):
        raise FutureRangeArtifactError("execution entry 不能由 D+1 open 重放")
    if execution.get("exit_date") != target["date"] or not math.isclose(exit_price, float(cast(float, target["close"])), rel_tol=0, abs_tol=1e-9):
        raise FutureRangeArtifactError("execution exit 不能由固定目标日 close 重放")
    gross = _finite_number(execution.get("gross_return"), "execution.gross_return")
    if not math.isclose(gross, exit_price / entry_price - 1, rel_tol=0, abs_tol=1e-9):
        raise FutureRangeArtifactError("execution gross_return 不能由 entry/exit 重放")


def _validate_aggregate_contracts(payload: Mapping[str, object]) -> None:
    run = _required_mapping(payload["run"], "payload.run")
    expected = {name: run[name] for name in ("mode", "scope", "rule_version")}
    for collection in ("groups", "rank_ic", "monotonicity"):
        for raw in cast(list[object], payload[collection]):
            item = _required_mapping(raw, f"payload.{collection}[]")
            if item.get("cohort") != expected or item.get("status") not in {"ok", "insufficient_data"}:
                raise FutureRangeArtifactError(f"payload.{collection} cohort/status 冲突")


def _target_bar_digest(bar: Mapping[str, object]) -> str:
    try:
        return sha256_hex(canonical_json_text(bar))
    except ArtifactCanonicalJsonError as exc:  # payload validation should already reject this
        raise FutureRangeArtifactError("target bar 不是有限 JSON") from exc


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise FutureRangeArtifactError(f"{label} 必须是有限数值")
    return float(value)


def _json_value(value: object, label: str) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FutureRangeArtifactError(f"{label} 包含 NaN/Infinity")
        return value
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in output:
                raise FutureRangeArtifactError(f"{label} 包含无效或重复键")
            output[key] = _json_value(item, f"{label}.{key}")
        return output
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{label}[]") for item in value]
    raise FutureRangeArtifactError(f"{label} 包含非 JSON 类型")


def _required_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise FutureRangeArtifactError(f"{label} 必须是 object")
    return cast(dict[str, object], _json_value(value, label))


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FutureRangeArtifactError(f"{label} 必须是非空字符串")
    return value


def _required_sha256(value: object, label: str) -> str:
    text = _required_text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise FutureRangeArtifactError(f"{label} 必须是小写 SHA-256")
    return text


def _require_exact_keys(value: Mapping[str, object], keys: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != keys:
        raise FutureRangeArtifactError(f"{label} 字段不匹配；missing={sorted(keys - actual)} extra={sorted(actual - keys)}")


def _reject_database_target(target: Path, database: Path) -> None:
    if target == database:
        raise FutureRangeArtifactError("artifact 路径不能覆盖 SQLite 数据库")
    if target.exists() and database.exists():
        try:
            if target.samefile(database):
                raise FutureRangeArtifactError("artifact 路径不能硬链接到 SQLite 数据库")
        except OSError as exc:
            raise FutureRangeArtifactError("无法验证 artifact 与 SQLite 的文件身份") from exc


__all__ = [
    "FUTURE_RANGE_ARTIFACT_INTEGRITY_NOTICE",
    "FUTURE_RANGE_ARTIFACT_SCHEMA_VERSION",
    "FUTURE_RANGE_REPORT_CONTRACT_VERSION",
    "FutureRangeArtifactError",
    "build_future_range_artifact",
    "canonical_future_range_artifact_json",
    "future_range_artifact_filename",
    "future_range_payload_integrity_digest",
    "load_future_range_artifact",
    "replay_future_range_artifact",
    "verify_future_range_artifact",
    "write_future_range_artifact",
]
