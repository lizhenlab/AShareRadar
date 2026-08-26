"""Personal, explicitly non-authorizing historical probability estimators.

An offline build verifies the old replay, then fits a fresh train/calibration
split. It never relabels the old study as qualified or uses its final OOS model
as a production deployment. Runtime only reads the compact local artifact.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, read_regular_file, sha256_hex
from app.services.market_scan_probability import (
    PROBABILITY_CALIBRATOR_VERSION, PROBABILITY_MODEL_VERSION, ProbabilityConfig, ProbabilitySample,
    fit_probability_logistic_model, fit_probability_platt_calibrator,
    probability_model_probability, probability_platt_probability,
)
from app.services.market_scan_probability_replay import (
    HISTORICAL_REPLAY_FEATURE_NAMES, HISTORICAL_REPLAY_FEATURE_VERSION,
    verify_historical_replay_artifact,
)
from app.utils.clock import market_now


MODEL_SCHEMA = "personal-experimental-h5-model-v1"
MODEL_DIRECTORY = Path("research/personal_experimental_probability")
MODEL_MAX_BYTES = 256 * 1024
MAX_SIGNAL_AGE_DAYS = 90
MODEL_FEATURE_NAMES = tuple(sorted(HISTORICAL_REPLAY_FEATURE_NAMES))
WARNING = (
    "个人实验，未通过正式生产验证；历史抽样与当前全市场分布可能不同。"
    "概率仅表示历史日线模型下扣除假定成本后收益为正，不代表可成交概率或保证收益；"
    "未完整建模停复牌、涨跌停、容量和公司行动，不改变正式排名。"
)
ExperimentalPredictionKind = Literal["net_h5", "close_d1", "close_d2", "close_d5"]
DIRECTION_OFFSETS: dict[str, Literal[1, 2, 5]] = {"close_d1": 1, "close_d2": 2, "close_d5": 5}
DIRECTION_SCHEMA = "personal-experimental-close-direction-model-v1"
DIRECTION_WARNING = (
    "个人实验，未经过独立样本外验证；预测固定交易日收盘价相对D日收盘价上涨，"
    "不是买入后扣费盈利或可成交概率。历史抽样、复权版本与全市场泛化局限仍存在，不改变正式排名。"
)


def experimental_definition(kind: str) -> dict[str, Any]:
    if kind == "net_h5":
        return {"horizon": 5, "target": "net_return_positive", "schema": MODEL_SCHEMA,
                "file_prefix": "experimental-h5", "target_offset": 6, "reference": "next_session_open",
                "label": "H5 扣费盈利", "warning": WARNING}
    if kind not in DIRECTION_OFFSETS:
        raise ExperimentalProbabilityUnavailable("实验目标仅支持 close_d1、close_d2、close_d5、net_h5")
    offset = DIRECTION_OFFSETS[kind]
    return {"horizon": offset, "target": "close_return_positive", "schema": DIRECTION_SCHEMA,
            "file_prefix": f"experimental-close-d{offset}", "target_offset": offset,
            "reference": "signal_day_qfq_close", "label": f"D+{offset} 收盘上涨", "warning": DIRECTION_WARNING}


class ExperimentalProbabilityUnavailable(ValueError):
    """The personal model or its input is not usable; never fall back to a guess."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ExperimentalLogit(_StrictModel):
    version: str
    feature_names: list[str]
    means: list[float] = Field(min_length=11, max_length=11)
    scales: list[float] = Field(min_length=11, max_length=11)
    coefficients: list[float] = Field(min_length=11, max_length=11)
    intercept: float
    l2_strength: float = Field(ge=1, le=1)
    iterations: int = Field(ge=1, le=100)
    converged: Literal[True]

    @model_validator(mode="after")
    def validate_contract(self) -> ExperimentalLogit:
        if self.version != PROBABILITY_MODEL_VERSION or self.feature_names != list(MODEL_FEATURE_NAMES):
            raise ValueError("experimental model feature/version mismatch")
        if any(value <= 0 for value in self.scales):
            raise ValueError("experimental model scales must be positive")
        return self


class ExperimentalCalibrator(_StrictModel):
    version: str
    intercept: float
    slope: float
    iterations: int = Field(ge=1, le=100)
    converged: Literal[True]
    fit_partition: Literal["calibration_only"]

    @model_validator(mode="after")
    def validate_version(self) -> ExperimentalCalibrator:
        if self.version != PROBABILITY_CALIBRATOR_VERSION:
            raise ValueError("experimental calibrator version mismatch")
        return self


class DirectionEvidence(_StrictModel):
    label_version: Literal["close-to-close-fixed-session-v1"] = "close-to-close-fixed-session-v1"
    reference: Literal["signal_day_qfq_close"] = "signal_day_qfq_close"
    target_session_offset: Literal[1, 2, 5]
    comparison: Literal["target_close_gt_signal_close"] = "target_close_gt_signal_close"
    adjustment_mode: Literal["qfq"] = "qfq"
    fees_included: Literal[False] = False
    execution_modelled: Literal[False] = False
    history_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    samples_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_start: str
    source_end: str
    sample_count: int = Field(gt=0, le=100_000)
    excluded: dict[str, int] = Field(default_factory=dict)

    @field_validator("target_session_offset", mode="before")
    @classmethod
    def validate_offset_type(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("close-direction offset must be an integer")
        return value

    @model_validator(mode="after")
    def validate_source(self) -> DirectionEvidence:
        start, end = date.fromisoformat(self.source_start), date.fromisoformat(self.source_end)
        if start.isoformat() != self.source_start or end.isoformat() != self.source_end or start > end:
            raise ValueError("close-direction source dates invalid")
        if any(count < 0 for count in self.excluded.values()):
            raise ValueError("close-direction exclusion counts must be nonnegative")
        return self


class ExperimentalEstimator(_StrictModel):
    schema_version: Literal["personal-experimental-h5-model-v1", "personal-experimental-close-direction-model-v1"] = "personal-experimental-h5-model-v1"
    generated_at: str
    experimental: Literal[True] = True
    filter_qualified: Literal[False] = False
    production_ranking_effect: Literal["none"] = "none"
    horizon: Literal[1, 2, 5] = 5
    target: Literal["net_return_positive", "close_return_positive"] = "net_return_positive"
    direction_evidence: DirectionEvidence | None = None
    feature_version: str = HISTORICAL_REPLAY_FEATURE_VERSION
    source_filename: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_integrity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_symbols: list[str] = Field(min_length=1)
    train_session_count: int = Field(ge=120)
    calibration_session_count: int = Field(ge=40)
    train_record_count: int = Field(gt=0)
    calibration_record_count: int = Field(gt=0)
    train_signal_end: str
    train_label_end: str
    calibration_start: str
    calibration_end: str
    latest_label_date: str
    expires_after_signal_date: str
    base_rate: float = Field(gt=0, lt=1)
    model: ExperimentalLogit
    calibrator: ExperimentalCalibrator
    historical_recipe_evaluation: dict[str, Any]
    cost_contract: dict[str, Any]
    limitations: list[str]

    @field_validator("horizon", mode="before")
    @classmethod
    def validate_horizon_type(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("experimental horizon must be an integer")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> ExperimentalEstimator:
        if self.schema_version == MODEL_SCHEMA:
            if self.horizon != 5 or self.target != "net_return_positive" or self.direction_evidence is not None:
                raise ValueError("H5 model cannot carry close-direction labels")
            return self
        evidence = self.direction_evidence
        if self.target != "close_return_positive" or evidence is None or self.horizon != evidence.target_session_offset:
            raise ValueError("close-direction model target/offset mismatch")
        if self.cost_contract or self.historical_recipe_evaluation != {"status": "not_evaluated", "target": self.target, "horizon": self.horizon}:
            raise ValueError("close-direction model cannot borrow net-return costs or OOS evidence")
        if not evidence.source_start <= self.train_signal_end < self.latest_label_date <= evidence.source_end:
            raise ValueError("close-direction labels outside source dates")
        if evidence.history_manifest_digest != self.source_integrity_digest:
            raise ValueError("close-direction source manifest mismatch")
        if evidence.sample_count < self.train_record_count + self.calibration_record_count:
            raise ValueError("close-direction fit counts exceed source samples")
        return self

    @model_validator(mode="after")
    def validate_dates(self) -> ExperimentalEstimator:
        generated = datetime.fromisoformat(self.generated_at)
        if generated.tzinfo is None or generated.utcoffset() is None:
            raise ValueError("experimental build timestamp must include timezone")
        values = [date.fromisoformat(value) for value in (
            self.train_signal_end, self.train_label_end, self.calibration_start,
            self.calibration_end, self.latest_label_date, self.expires_after_signal_date,
        )]
        if not values[0] < values[1] < values[2] <= values[3] < values[4] <= generated.date():
            raise ValueError("experimental train/calibration label dates overlap or are future")
        if values[5] != values[4] + timedelta(days=MAX_SIGNAL_AGE_DAYS):
            raise ValueError("experimental model freshness must bind last label date")
        if self.feature_version != HISTORICAL_REPLAY_FEATURE_VERSION:
            raise ValueError("experimental feature version mismatch")
        return self


def _experimental_samples(payload: dict[str, Any]) -> tuple[list[ProbabilitySample], dict[str, str], set[str]]:
    samples: list[ProbabilitySample] = []
    targets: dict[str, str] = {}
    symbols: set[str] = set()
    for row in payload["records"]:
        outcome = next(item for item in row["outcomes"] if item["horizon"] == 5)
        if outcome["status"] != "modelled":
            continue
        sample = ProbabilitySample(
            sample_id=row["sample_id"], session_date=row["signal_date"],
            features=dict(zip(HISTORICAL_REPLAY_FEATURE_NAMES, row["feature_values"], strict=True)),
            target=int(outcome["net_return"] > 0), net_return=outcome["net_return"],
        )
        samples.append(sample)
        targets[sample.sample_id] = outcome["target_session_date"]
        symbols.add(row["symbol"])
    return samples, targets, symbols


def _partition_experimental_samples(samples: list[ProbabilitySample], targets: dict[str, str], purge_sessions: int) -> tuple[
    list[ProbabilitySample], list[ProbabilitySample], str, str,
]:
    dates = sorted({sample.session_date for sample in samples})
    if len(dates) < 160 + purge_sessions:
        raise ExperimentalProbabilityUnavailable(f"实验模型仍需至少 120 个训练日、{purge_sessions} 日隔离和 40 个独立校准日")
    calibration_dates = set(dates[-40:])
    calibration_start = dates[-40]
    training_dates = set(dates[:-(40 + purge_sessions)])
    train = [sample for sample in samples if sample.session_date in training_dates and targets[sample.sample_id] < calibration_start]
    calibration = [sample for sample in samples if sample.session_date in calibration_dates]
    if len({sample.session_date for sample in train}) < 120 or any({sample.target for sample in group} != {0, 1} for group in (train, calibration)):
        raise ExperimentalProbabilityUnavailable("实验模型训练/校准样本不足或没有正负两类标签")
    return train, calibration, calibration_start, dates[-1]


def fit_experimental_estimator(payload: dict[str, Any], *, source_filename: str,
                               source_sha256: str, source_integrity_digest: str) -> ExperimentalEstimator:
    """Called only by the offline builder after the entire source is verified."""
    samples, targets, symbols = _experimental_samples(payload)
    parameters = fit_experimental_sample_parameters(samples, targets, horizon=5, purge_sessions=6)
    evaluation = payload["probability_fit"]["horizons"]["5"]
    return ExperimentalEstimator(
        **parameters, source_filename=source_filename, source_sha256=source_sha256,
        source_integrity_digest=source_integrity_digest, training_symbols=sorted(symbols), cost_contract=payload["cost_contract"],
        historical_recipe_evaluation={key: evaluation.get(key) for key in ("status", "counts", "calibration_metrics", "limitations")},
        limitations=[*payload["limitations"], "personal_experimental_not_formal_authority",
                     "historical_oos_evaluates_recipe_not_this_refitted_estimator",
                     "current_predictions_use_reconstructed_cache_not_original_pit",
                     "new_universe_generalization_unverified"],
    )


def fit_experimental_sample_parameters(samples: list[ProbabilitySample], targets: dict[str, str], *,
                                       horizon: int, purge_sessions: int) -> dict[str, Any]:
    """Shared numeric fitting only; callers bind their distinct label semantics."""
    if type(horizon) is not int or type(purge_sessions) is not int or horizon not in {1, 2, 5} or purge_sessions < horizon:
        raise ExperimentalProbabilityUnavailable("实验周期或训练隔离无效")
    train, calibration, calibration_start, calibration_end = _partition_experimental_samples(samples, targets, purge_sessions)
    config = ProbabilityConfig(horizon=horizon, target="net_return_positive")
    model = fit_probability_logistic_model(train, MODEL_FEATURE_NAMES, config)
    raw = [probability_model_probability(model, sample.features) for sample in calibration]
    labels = [int(sample.target) for sample in calibration if sample.target is not None]
    calibrator = fit_probability_platt_calibrator(raw, labels, config)
    latest_label = max(targets[sample.sample_id] for sample in calibration)
    return dict(
        generated_at=market_now().isoformat(), train_session_count=len({sample.session_date for sample in train}),
        calibration_session_count=40, train_record_count=len(train), calibration_record_count=len(calibration),
        train_signal_end=max(sample.session_date for sample in train), train_label_end=max(targets[sample.sample_id] for sample in train),
        calibration_start=calibration_start, calibration_end=calibration_end, latest_label_date=latest_label,
        expires_after_signal_date=(date.fromisoformat(latest_label) + timedelta(days=MAX_SIGNAL_AGE_DAYS)).isoformat(),
        base_rate=sum(labels) / len(labels), model=ExperimentalLogit.model_validate(model),
        calibrator=ExperimentalCalibrator.model_validate(calibrator),
    )


def build_experimental_model(source: Path, directory: Path) -> Path:
    raw = read_regular_file(source, max_bytes=256 * 1024 * 1024)
    decoded = decode_json_bytes(raw)
    if not isinstance(decoded, dict):
        raise ExperimentalProbabilityUnavailable("历史模型源不是 JSON object")
    verified = verify_historical_replay_artifact(decoded)
    estimator = fit_experimental_estimator(
        cast(dict[str, Any], verified["payload"]), source_filename=source.name, source_sha256=sha256_hex(raw),
        source_integrity_digest=cast(dict[str, Any], verified["integrity"])["integrity_digest"],
    )
    payload = estimator.model_dump()
    digest = sha256_hex(canonical_json_bytes(payload))
    target = directory / f"experimental-h5-{digest}.json"
    exclusive_atomic_publish(target, canonical_json_bytes({"payload": payload, "sha256": digest}), max_bytes=MODEL_MAX_BYTES)
    return target


def load_experimental_model(directory: Path, *, prediction_kind: ExperimentalPredictionKind = "net_h5") -> tuple[ExperimentalEstimator, str]:
    definition = experimental_definition(prediction_kind)
    prefix = definition["file_prefix"]
    if not path_has_only_trusted_aliases(directory):
        raise ExperimentalProbabilityUnavailable("个人实验模型目录包含不可信路径")
    candidates = sorted(directory.glob(f"{prefix}-*.json"))
    if not candidates:
        raise ExperimentalProbabilityUnavailable(f"{definition['label']}模型尚未生成，请先运行 tools/build_experimental_probability.py")
    if len(candidates) > 32:
        raise ExperimentalProbabilityUnavailable("个人实验模型目录过大，请明确保留的版本")
    loaded: list[tuple[ExperimentalEstimator, str]] = []
    try:
        for path in candidates:
            value = decode_json_bytes(read_regular_file(path, max_bytes=MODEL_MAX_BYTES))
            if not isinstance(value, dict) or set(value) != {"payload", "sha256"}:
                raise ValueError("experimental artifact envelope mismatch")
            digest = sha256_hex(canonical_json_bytes(value["payload"]))
            if digest != value["sha256"] or path.name != f"{prefix}-{digest}.json":
                raise ValueError("experimental model digest mismatch")
            estimator = ExperimentalEstimator.model_validate(value["payload"])
            if (estimator.schema_version, estimator.target, estimator.horizon) != (definition["schema"], definition["target"], definition["horizon"]):
                raise ValueError("experimental model kind mismatch")
            if datetime.fromisoformat(estimator.generated_at) > market_now():
                raise ValueError("experimental model is from the future")
            loaded.append((estimator, digest))
    except (ArtifactIOError, OSError, ValueError, ValidationError) as exc:
        raise ExperimentalProbabilityUnavailable("个人实验模型校验失败，未使用旧缓存或猜测概率") from exc
    return max(loaded, key=lambda item: (item[0].latest_label_date, item[0].generated_at, item[1]))


def experimental_probability(estimator: ExperimentalEstimator, values: tuple[float, ...]) -> float:
    if len(values) != len(HISTORICAL_REPLAY_FEATURE_NAMES):
        raise ExperimentalProbabilityUnavailable("实验特征数量不匹配")
    if any(not math.isfinite(value) for value in values):
        raise ExperimentalProbabilityUnavailable("实验特征必须为有限数值")
    features = dict(zip(HISTORICAL_REPLAY_FEATURE_NAMES, values, strict=True))
    ordered_values = [features[name] for name in MODEL_FEATURE_NAMES]
    if any(abs((value - mean) / scale) > 8 for value, mean, scale in zip(ordered_values, estimator.model.means, estimator.model.scales, strict=True)):
        raise ExperimentalProbabilityUnavailable("feature_out_of_distribution")
    raw = probability_model_probability(estimator.model.model_dump(), features)
    return probability_platt_probability(estimator.calibrator.model_dump(), raw)
