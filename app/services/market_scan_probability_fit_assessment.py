"""Bounded, fail-closed Shadow fit assessment orchestration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
import gzip
from io import BytesIO
from pathlib import Path
import re
from typing import cast

from app.artifacts.io import (
    ArtifactIOError,
    canonical_json_text,
    content_addressed_filename,
    decode_json_bytes,
    exclusive_atomic_publish,
    read_regular_file,
    sha256_hex,
)
from app.services.market_scan_probability import stable_probability_hash
from app.services.market_scan_probability_outcomes import probability_research_rows_from_outcome_artifacts
from app.services.market_scan_probability_research import ProbabilityResearchRow, build_probability_research


PROBABILITY_FIT_ASSESSMENT_SCHEMA_VERSION = "market-scan-probability-fit-assessment-v1"
PROBABILITY_FIT_ASSESSMENT_RELATIVE_PATH = "research/market_scan_probability_fit"
PROBABILITY_FIT_SAMPLES_PER_SESSION = 90
PROBABILITY_FIT_MAX_ROWS = 30_000
PROBABILITY_FIT_MAX_SESSIONS = 300
PROBABILITY_FIT_MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
PROBABILITY_FIT_MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_FIT_FILENAME = re.compile(r"market-scan-probability-fit-through-run-(\d+)-([0-9a-f]{64})\.json\.gz")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_HORIZONS = (1, 5, 20)
_TARGETS = ("net_excess_positive", "absolute_net_positive")
_PAYLOAD_KEYS = {
    "contract_version", "cohort", "through_run_id", "training_cutoff", "input_pair_digest",
    "corpus_chain_digest", "members", "sampling", "sampled_row_count", "research_digest",
    "compact_research_digest",
    "fit_status", "fit_replay_verified", "fit_selection_qualified",
    "fit_selection_qualification", "horizons", "records_included", "projection_status",
    "production_ranking_effect", "limitations",
}
_MEMBER_KEYS = {
    "run_id", "session_date", "source_filename", "outcome_filename", "source_content_digest",
    "outcome_content_digest", "sampled_symbol_digest", "sampled_row_count",
}
_SAMPLING_KEYS = {
    "method", "maximum_rows_per_session", "maximum_total_rows", "maximum_rolling_sessions",
    "total_canonical_session_count", "excluded_older_session_count", "window_start_session",
    "window_end_session",
}
_HORIZON_EVIDENCE_KEYS = {
    "status", "fit_status", "selection_qualified", "selection_qualification", "counts",
    "training_cutoff", "evidence_digest", "input_digest", "deterministic_replay_verified",
    "promotion_gates", "limitations", "compact_evidence_digest",
}


def build_bounded_probability_fit_assessment(
    source_paths: Sequence[str | Path],
    outcome_paths: Sequence[str | Path],
    *,
    generated_at: str,
    bootstrap_samples: int = 1_000,
) -> dict[str, object]:
    """Stream source/outcome pairs into a deterministic bounded fit corpus."""
    if len(source_paths) != len(outcome_paths):
        raise ValueError("上涨概率 fit source/outcome 数量不一致")
    total_session_count = len(source_paths)
    if total_session_count > PROBABILITY_FIT_MAX_SESSIONS:
        start = len(source_paths) - PROBABILITY_FIT_MAX_SESSIONS
        source_paths, outcome_paths = source_paths[start:], outcome_paths[start:]
    rows: list[ProbabilityResearchRow] = []
    members: list[dict[str, object]] = []
    for source_path, outcome_path in zip(source_paths, outcome_paths, strict=True):
        session_rows = probability_research_rows_from_outcome_artifacts((source_path,), (outcome_path,))
        selected = _balanced_session_sample(session_rows, PROBABILITY_FIT_SAMPLES_PER_SESSION)
        if len(rows) + len(selected) > PROBABILITY_FIT_MAX_ROWS:
            raise ValueError("上涨概率 fit corpus 超过有界内存预算")
        rows.extend(selected)
        members.append(_member_identity(source_path, outcome_path, selected))
    research = build_probability_research(
        rows,
        generated_at=generated_at,
        bootstrap_samples=bootstrap_samples,
        include_records=False,
    )
    payload = _assessment_payload(
        research,
        members,
        rows,
        total_session_count=total_session_count,
    )
    digest = sha256_hex(canonical_json_text(payload))
    return {
        "schema_version": PROBABILITY_FIT_ASSESSMENT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": payload,
        "integrity": {
            "algorithm": "sha256",
            "scope": "payload",
            "integrity_digest": digest,
            "notice": "integrity_digest_not_a_signature",
        },
    }


def probability_fit_corpus_ready(outcome_manifests: Sequence[object]) -> bool:
    """Require the conservative 260-session floor for every horizon."""
    if len(outcome_manifests) < 260:
        return False
    available = {str(horizon): 0 for horizon in _HORIZONS}
    for manifest in outcome_manifests:
        quality = getattr(manifest, "horizon_quality", None)
        if not isinstance(quality, Mapping):
            return False
        for horizon in _HORIZONS:
            horizon_quality = quality.get(str(horizon))
            if isinstance(horizon_quality, Mapping) and horizon_quality.get("available_for_study") is True:
                available[str(horizon)] += 1
    return all(count >= 260 for count in available.values())


def publish_probability_fit_assessment(
    directory: str | Path,
    assessment: Mapping[str, object],
) -> dict[str, object]:
    """Publish compact fit evidence without per-stock records."""
    verified = verify_probability_fit_assessment(assessment)
    payload = _mapping(verified["payload"], "payload")
    integrity = _mapping(verified["integrity"], "integrity")
    target = Path(directory).expanduser().absolute() / content_addressed_filename(
        "market-scan-probability-fit-through-run",
        (int(cast(int, payload["through_run_id"])),),
        str(integrity["integrity_digest"]),
        ".json.gz",
    )
    encoded = gzip.compress(canonical_json_text(verified).encode(), compresslevel=9, mtime=0)
    exclusive_atomic_publish(target, encoded, max_bytes=PROBABILITY_FIT_MAX_COMPRESSED_BYTES)
    return {
        "path": str(target),
        "digest": integrity["integrity_digest"],
        "through_run_id": payload["through_run_id"],
        "fit_status": payload["fit_status"],
        "fit_replay_verified": payload["fit_replay_verified"],
    }


def verify_probability_fit_assessment(assessment: Mapping[str, object]) -> dict[str, object]:
    normalized = deepcopy(dict(assessment))
    _require_exact_keys(normalized, {"schema_version", "generated_at", "payload", "integrity"}, "root")
    if normalized.get("schema_version") != PROBABILITY_FIT_ASSESSMENT_SCHEMA_VERSION:
        raise ValueError("上涨概率 fit assessment schema 不受支持")
    _aware_timestamp(normalized.get("generated_at"), "generated_at")
    payload = _mapping(normalized.get("payload"), "payload")
    integrity = _mapping(normalized.get("integrity"), "integrity")
    _verify_fit_payload(payload)
    _verify_integrity_contract(integrity)
    if sha256_hex(canonical_json_text(payload)) != integrity.get("integrity_digest"):
        raise ValueError("上涨概率 fit assessment digest 不一致")
    return normalized


def _verify_fit_payload(payload: Mapping[str, object]) -> None:
    _require_exact_keys(payload, _PAYLOAD_KEYS, "payload")
    if payload["contract_version"] != "bounded-balanced-session-fit-v1":
        raise ValueError("上涨概率 fit assessment contract 不受支持")
    _verify_cohort(payload["cohort"])
    members = _verify_members(payload["members"])
    _verify_member_digests(payload, members)
    _verify_sampling(payload, members)
    fitted, replay = _verify_horizons(payload["horizons"])
    _verify_compact_research_digest(payload)
    expected_status = "sampled_oos_assessment" if fitted else "not_fitted"
    if payload["fit_status"] != expected_status or payload["fit_replay_verified"] is not replay:
        raise ValueError("上涨概率 fit assessment 状态与 horizon 证据不一致")
    _verify_fail_closed_contract(payload, replay)


def _verify_member_digests(
    payload: Mapping[str, object], members: Sequence[Mapping[str, object]],
) -> None:
    _positive_int(payload["through_run_id"], "through_run_id")
    _iso_date(payload["training_cutoff"], "training_cutoff")
    _digest(payload["input_pair_digest"], "input_pair_digest")
    _digest(payload["corpus_chain_digest"], "corpus_chain_digest")
    pairs = [(item["source_content_digest"], item["outcome_content_digest"]) for item in members]
    if payload["input_pair_digest"] != stable_probability_hash(pairs):
        raise ValueError("上涨概率 fit assessment input pair digest 不一致")
    if payload["corpus_chain_digest"] != stable_probability_hash(list(members)):
        raise ValueError("上涨概率 fit assessment corpus chain digest 不一致")
    if payload["through_run_id"] != members[-1]["run_id"]:
        raise ValueError("上涨概率 fit assessment through run 不一致")
    if payload["training_cutoff"] != members[-1]["session_date"]:
        raise ValueError("上涨概率 fit assessment training cutoff 不一致")


def _verify_members(value: object) -> tuple[Mapping[str, object], ...]:
    members = tuple(_mapping(item, "members[]") for item in _sequence(value, "members"))
    if not members or len(members) > PROBABILITY_FIT_MAX_SESSIONS:
        raise ValueError("上涨概率 fit assessment members 数量无效")
    for member in members:
        _verify_member(member)
    order = [(str(item["session_date"]), int(cast(int, item["run_id"]))) for item in members]
    if order != sorted(order) or len({item[0] for item in order}) != len(order):
        raise ValueError("上涨概率 fit assessment members 未按独立交易日排序")
    return members


def _verify_member(member: Mapping[str, object]) -> None:
    _require_exact_keys(member, _MEMBER_KEYS, "members[]")
    _positive_int(member["run_id"], "members[].run_id")
    _iso_date(member["session_date"], "members[].session_date")
    count = _positive_int(member["sampled_row_count"], "members[].sampled_row_count")
    if count > PROBABILITY_FIT_SAMPLES_PER_SESSION:
        raise ValueError("上涨概率 fit assessment 单日样本超限")
    for name in ("source_content_digest", "outcome_content_digest", "sampled_symbol_digest"):
        _digest(member[name], f"members[].{name}")
    for name, digest_name in (
        ("source_filename", "source_content_digest"), ("outcome_filename", "outcome_content_digest"),
    ):
        filename = member[name]
        if not isinstance(filename, str) or str(member[digest_name]) not in filename:
            raise ValueError(f"上涨概率 fit assessment members[].{name} 无效")


def _verify_sampling(payload: Mapping[str, object], members: Sequence[Mapping[str, object]]) -> None:
    sampling = _mapping(payload["sampling"], "sampling")
    _require_exact_keys(sampling, _SAMPLING_KEYS, "sampling")
    expected = {
        "method": "deterministic_sha256_market_balanced_per_session",
        "maximum_rows_per_session": PROBABILITY_FIT_SAMPLES_PER_SESSION,
        "maximum_total_rows": PROBABILITY_FIT_MAX_ROWS,
        "maximum_rolling_sessions": PROBABILITY_FIT_MAX_SESSIONS,
    }
    if any(sampling[name] != value for name, value in expected.items()):
        raise ValueError("上涨概率 fit assessment sampling contract 无效")
    total = _positive_int(sampling["total_canonical_session_count"], "sampling.total")
    excluded = _nonnegative_int(sampling["excluded_older_session_count"], "sampling.excluded")
    sampled = sum(int(cast(int, item["sampled_row_count"])) for item in members)
    sampled_count = _positive_int(payload["sampled_row_count"], "sampled_row_count")
    if total - len(members) != excluded or sampled_count != sampled:
        raise ValueError("上涨概率 fit assessment sampling 计数不一致")
    if sampled > PROBABILITY_FIT_MAX_ROWS:
        raise ValueError("上涨概率 fit assessment 超过内存预算")
    if sampling["window_start_session"] != members[0]["session_date"] or sampling[
        "window_end_session"
    ] != members[-1]["session_date"]:
        raise ValueError("上涨概率 fit assessment sampling window 不一致")


def _verify_horizons(value: object) -> tuple[bool, bool]:
    horizons = _mapping(value, "horizons")
    _require_exact_keys(horizons, {str(item) for item in _HORIZONS}, "horizons")
    evidence: list[Mapping[str, object]] = []
    for horizon in _HORIZONS:
        targets = _mapping(horizons[str(horizon)], f"horizons.{horizon}")
        _require_exact_keys(targets, set(_TARGETS), f"horizons.{horizon}")
        for target in _TARGETS:
            item = _mapping(targets[target], f"horizons.{horizon}.{target}")
            _verify_horizon_evidence(item)
            evidence.append(item)
    return any(item["fit_status"] == "fitted_oos" for item in evidence), all(
        item["deterministic_replay_verified"] is True for item in evidence
    )


def _verify_horizon_evidence(evidence: Mapping[str, object]) -> None:
    _require_exact_keys(evidence, _HORIZON_EVIDENCE_KEYS, "horizon evidence")
    compact_identity = dict(evidence)
    claimed_digest = compact_identity.pop("compact_evidence_digest")
    _digest(claimed_digest, "horizon evidence.compact_evidence_digest")
    if claimed_digest != stable_probability_hash(compact_identity):
        raise ValueError("上涨概率 fit assessment horizon 紧凑证据摘要不一致")
    if evidence["status"] not in {"insufficient_data", "calibrated_shadow"}:
        raise ValueError("上涨概率 fit assessment horizon status 无效")
    if evidence["fit_status"] not in {"fitted_oos", "not_fitted"}:
        raise ValueError("上涨概率 fit assessment horizon fit_status 无效")
    if evidence["deterministic_replay_verified"] is not True:
        raise ValueError("上涨概率 fit assessment horizon 未通过确定性回放")
    if evidence["selection_qualified"] is not False or evidence[
        "selection_qualification"
    ] != _fit_selection_contract(True):
        raise ValueError("上涨概率有界 fit assessment horizon 不得取得选股资格")
    _mapping(evidence["counts"], "horizon evidence.counts")
    _mapping(evidence["promotion_gates"], "horizon evidence.promotion_gates")
    _sequence(evidence["limitations"], "horizon evidence.limitations")
    _digest(evidence["evidence_digest"], "horizon evidence.evidence_digest")
    _digest(evidence["input_digest"], "horizon evidence.input_digest")
    cutoff = evidence["training_cutoff"]
    if cutoff is not None:
        _iso_date(cutoff, "horizon evidence.training_cutoff")


def _verify_fail_closed_contract(payload: Mapping[str, object], replay: bool) -> None:
    qualification = _mapping(payload["fit_selection_qualification"], "fit selection")
    expected_limitations = [
        "individual_probability_projection_not_published",
        "selection_filter_fail_closed",
        "bounded_sample_benchmark_not_full_market_contract_selection_forbidden",
    ]
    valid = (
        replay is True
        and payload["projection_status"] == "projection_pending"
        and payload["records_included"] is False
        and payload["production_ranking_effect"] == "none"
        and payload["fit_selection_qualified"] is False
        and qualification == _fit_selection_contract(True)
        and payload["limitations"] == expected_limitations
    )
    if not valid:
        raise ValueError("上涨概率有界 fit assessment 生产隔离或选股门禁无效")
    _digest(payload["research_digest"], "research_digest")


def _verify_compact_research_digest(payload: Mapping[str, object]) -> None:
    claimed = payload["compact_research_digest"]
    _digest(claimed, "compact_research_digest")
    if claimed != stable_probability_hash(_compact_research_identity(payload)):
        raise ValueError("上涨概率 fit assessment 紧凑研究摘要不一致")


def _verify_cohort(value: object) -> None:
    cohort = _mapping(value, "cohort")
    _require_exact_keys(cohort, {"mode", "scope", "rule_version"}, "cohort")
    if any(not isinstance(cohort[name], str) or not str(cohort[name]).strip() for name in cohort):
        raise ValueError("上涨概率 fit assessment cohort 无效")


def _verify_integrity_contract(integrity: Mapping[str, object]) -> None:
    _require_exact_keys(integrity, {"algorithm", "scope", "integrity_digest", "notice"}, "integrity")
    if (
        integrity["algorithm"] != "sha256"
        or integrity["scope"] != "payload"
        or integrity["notice"] != "integrity_digest_not_a_signature"
    ):
        raise ValueError("上涨概率 fit assessment integrity contract 无效")
    _digest(integrity["integrity_digest"], "integrity.integrity_digest")


def load_probability_fit_assessment(path: str | Path) -> dict[str, object]:
    source = Path(path).expanduser().absolute()
    try:
        encoded = read_regular_file(source, max_bytes=PROBABILITY_FIT_MAX_COMPRESSED_BYTES)
        with gzip.GzipFile(fileobj=BytesIO(encoded), mode="rb") as stream:
            raw = stream.read(PROBABILITY_FIT_MAX_UNCOMPRESSED_BYTES + 1)
        if len(raw) > PROBABILITY_FIT_MAX_UNCOMPRESSED_BYTES:
            raise ValueError("上涨概率 fit assessment 解压后超过大小上限")
        decoded = decode_json_bytes(raw)
    except (ArtifactIOError, gzip.BadGzipFile, OSError, EOFError) as exc:
        raise ValueError("上涨概率 fit assessment 无法读取") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("上涨概率 fit assessment 顶层必须是 object")
    verified = verify_probability_fit_assessment(cast(Mapping[str, object], decoded))
    _verify_fit_filename(source, verified)
    canonical = gzip.compress(canonical_json_text(verified).encode(), compresslevel=9, mtime=0)
    if encoded != canonical:
        raise ValueError("上涨概率 fit assessment 不是规范确定性gzip")
    return verified


def _verify_fit_filename(path: Path, assessment: Mapping[str, object]) -> None:
    matched = _FIT_FILENAME.fullmatch(path.name)
    payload = _mapping(assessment["payload"], "payload")
    integrity = _mapping(assessment["integrity"], "integrity")
    if matched is None or int(matched.group(1)) != payload["through_run_id"] or matched.group(2) != integrity["integrity_digest"]:
        raise ValueError("上涨概率 fit assessment 文件名与内容地址冲突")


def _balanced_session_sample(
    rows: Sequence[ProbabilityResearchRow],
    limit: int,
) -> tuple[ProbabilityResearchRow, ...]:
    markets: dict[str, list[ProbabilityResearchRow]] = defaultdict(list)
    for row in rows:
        markets[_market_bucket(row.symbol)].append(row)
    selected: list[ProbabilityResearchRow] = []
    quota = max(1, limit // 3)
    for market in ("SH", "SZ", "BJ"):
        ranked = sorted(markets[market], key=_sample_order)
        selected.extend(ranked[:quota])
    if len(selected) < limit:
        occupied = {(row.run_id, row.symbol) for row in selected}
        remainder = sorted((row for row in rows if (row.run_id, row.symbol) not in occupied), key=_sample_order)
        selected.extend(remainder[: limit - len(selected)])
    return tuple(sorted(selected, key=lambda row: row.symbol))


def _sample_order(row: ProbabilityResearchRow) -> tuple[str, str]:
    digest = stable_probability_hash({"run_id": row.run_id, "symbol": row.symbol, "session": row.session_date})
    return digest, row.symbol


def _market_bucket(symbol: str) -> str:
    if symbol.endswith(".BJ"):
        return "BJ"
    return "SH" if symbol.endswith(".SH") else "SZ"


def _member_identity(
    source_path: str | Path,
    outcome_path: str | Path,
    rows: Sequence[ProbabilityResearchRow],
) -> dict[str, object]:
    if not rows:
        raise ValueError("上涨概率 fit session 没有可归档样本")
    return {
        "run_id": rows[0].run_id,
        "session_date": rows[0].session_date,
        "source_filename": Path(source_path).name,
        "outcome_filename": Path(outcome_path).name,
        "source_content_digest": _filename_digest(Path(source_path)),
        "outcome_content_digest": _filename_digest(Path(outcome_path)),
        "sampled_symbol_digest": stable_probability_hash([row.symbol for row in rows]),
        "sampled_row_count": len(rows),
    }


def _filename_digest(path: Path) -> str:
    values = [part for part in path.name.replace(".json.gz", "").split("-") if len(part) == 64]
    if len(values) != 1 or any(character not in "0123456789abcdef" for character in values[0]):
        raise ValueError("上涨概率 fit corpus 文件名缺少内容 digest")
    return values[0]


def _assessment_payload(
    research: Mapping[str, object],
    members: Sequence[Mapping[str, object]],
    rows: Sequence[ProbabilityResearchRow],
    *,
    total_session_count: int,
) -> dict[str, object]:
    cohorts = cast(list[Mapping[str, object]], research["cohorts"])
    if len(cohorts) != 1:
        raise ValueError("上涨概率 fit assessment 必须严格隔离单一 cohort")
    horizons = _mapping(research["horizons"], "research.horizons")
    compact = _compact_horizons(horizons)
    replay, fitted = _fit_replay_state(compact)
    payload: dict[str, object] = {
        "contract_version": "bounded-balanced-session-fit-v1",
        "cohort": deepcopy(cohorts[0]["cohort_contract"]),
        "through_run_id": members[-1]["run_id"],
        "training_cutoff": members[-1]["session_date"],
        "input_pair_digest": stable_probability_hash(
            [(member["source_content_digest"], member["outcome_content_digest"]) for member in members]
        ),
        "corpus_chain_digest": stable_probability_hash(list(members)),
        "members": [dict(value) for value in members],
        "sampling": _sampling_contract(rows, members, total_session_count),
        "sampled_row_count": len(rows),
        "research_digest": research["research_digest"],
        "fit_status": "sampled_oos_assessment" if fitted else "not_fitted",
        "fit_replay_verified": replay,
        "fit_selection_qualified": False,
        "fit_selection_qualification": _fit_selection_contract(replay),
        "horizons": compact,
        "records_included": False,
        "projection_status": "projection_pending",
        "production_ranking_effect": "none",
        "limitations": [
            "individual_probability_projection_not_published",
            "selection_filter_fail_closed",
            "bounded_sample_benchmark_not_full_market_contract_selection_forbidden",
        ],
    }
    payload["compact_research_digest"] = stable_probability_hash(
        _compact_research_identity(payload)
    )
    return payload


def _fit_replay_state(horizons: Mapping[str, object]) -> tuple[bool, bool]:
    evidence = (
        _mapping(_mapping(horizons[str(horizon)], "horizon")[target], "evidence")
        for horizon in _HORIZONS
        for target in _TARGETS
    )
    values = tuple(evidence)
    replay = all(value.get("deterministic_replay_verified") is True for value in values)
    fitted = any(value.get("fit_status") == "fitted_oos" for value in values)
    return replay, fitted


def _sampling_contract(
    rows: Sequence[ProbabilityResearchRow],
    members: Sequence[Mapping[str, object]],
    total_session_count: int,
) -> dict[str, object]:
    return {
        "method": "deterministic_sha256_market_balanced_per_session",
        "maximum_rows_per_session": PROBABILITY_FIT_SAMPLES_PER_SESSION,
        "maximum_total_rows": PROBABILITY_FIT_MAX_ROWS,
        "maximum_rolling_sessions": PROBABILITY_FIT_MAX_SESSIONS,
        "total_canonical_session_count": total_session_count,
        "excluded_older_session_count": max(0, total_session_count - len(members)),
        "window_start_session": min(row.session_date for row in rows),
        "window_end_session": max(row.session_date for row in rows),
    }


def _fit_selection_contract(replay: bool) -> dict[str, object]:
    return {
        "passed": False,
        "gates": {
            "full_market_benchmark_contract": False,
            "full_market_top100_contract": False,
            "deterministic_sample_replay": replay,
        },
        "reason": "sampled_market_benchmark_not_full_market_contract",
    }


def _compact_horizons(horizons: Mapping[str, object]) -> dict[str, object]:
    return {
        str(horizon): {
            target: _compact_horizon_evidence(
                _mapping(_mapping(horizons[str(horizon)], "horizon")[target], "evidence")
            )
            for target in _TARGETS
        }
        for horizon in _HORIZONS
    }


def _compact_horizon_evidence(evidence: Mapping[str, object]) -> dict[str, object]:
    replay = evidence.get("deterministic_replay_verified") is True
    compact: dict[str, object] = {
        name: deepcopy(evidence.get(name))
        for name in _HORIZON_EVIDENCE_KEYS
        if name != "compact_evidence_digest"
    }
    compact["selection_qualified"] = False
    compact["selection_qualification"] = _fit_selection_contract(replay)
    compact["compact_evidence_digest"] = stable_probability_hash(compact)
    return compact


def _compact_research_identity(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        name: deepcopy(payload[name])
        for name in (
            "cohort", "through_run_id", "training_cutoff", "input_pair_digest",
            "corpus_chain_digest", "members", "sampling", "sampled_row_count",
            "research_digest", "fit_status", "fit_replay_verified", "horizons",
        )
    }


def _require_exact_keys(value: Mapping[str, object], expected: set[str], path: str) -> None:
    if set(value) != expected:
        raise ValueError(f"上涨概率 fit assessment {path} 字段不完整")


def _sequence(value: object, path: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"上涨概率 fit assessment {path} 必须是 array")
    return cast(Sequence[object], value)


def _positive_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"上涨概率 fit assessment {path} 必须是正整数")
    return value


def _nonnegative_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"上涨概率 fit assessment {path} 必须是非负整数")
    return value


def _digest(value: object, path: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"上涨概率 fit assessment {path} 必须是 SHA-256")
    return value


def _iso_date(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"上涨概率 fit assessment {path} 必须是日期")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"上涨概率 fit assessment {path} 日期无效") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"上涨概率 fit assessment {path} 日期不规范")
    return value


def _aware_timestamp(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"上涨概率 fit assessment {path} 必须是时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"上涨概率 fit assessment {path} 时间无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"上涨概率 fit assessment {path} 必须带时区")
    return value


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"上涨概率 fit assessment {path} 必须是 object")
    return cast(Mapping[str, object], value)


__all__ = [
    "PROBABILITY_FIT_ASSESSMENT_RELATIVE_PATH",
    "PROBABILITY_FIT_ASSESSMENT_SCHEMA_VERSION",
    "PROBABILITY_FIT_MAX_ROWS",
    "PROBABILITY_FIT_MAX_UNCOMPRESSED_BYTES",
    "PROBABILITY_FIT_MAX_SESSIONS",
    "PROBABILITY_FIT_SAMPLES_PER_SESSION",
    "build_bounded_probability_fit_assessment",
    "load_probability_fit_assessment",
    "probability_fit_corpus_ready",
    "publish_probability_fit_assessment",
    "verify_probability_fit_assessment",
]
