"""Personal, explicitly non-authorizing historical H5 estimator.

An offline build verifies the old replay, then fits a fresh train/calibration
split. It never relabels the old study as qualified or uses its final OOS model
as a production deployment. Runtime only reads the compact local artifact.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class ExperimentalEstimator(_StrictModel):
    schema_version: Literal["personal-experimental-h5-model-v1"] = "personal-experimental-h5-model-v1"
    generated_at: str
    experimental: Literal[True] = True
    filter_qualified: Literal[False] = False
    production_ranking_effect: Literal["none"] = "none"
    horizon: Literal[5] = 5
    target: Literal["net_return_positive"] = "net_return_positive"
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


def _partition_experimental_samples(samples: list[ProbabilitySample], targets: dict[str, str]) -> tuple[
    list[ProbabilitySample], list[ProbabilitySample], str, str,
]:
    dates = sorted({sample.session_date for sample in samples})
    if len(dates) < 166:
        raise ExperimentalProbabilityUnavailable("实验模型仍需至少 120 个训练日、6 日隔离和 40 个独立校准日")
    calibration_dates = set(dates[-40:])
    calibration_start = dates[-40]
    training_dates = set(dates[:-46])
    train = [sample for sample in samples if sample.session_date in training_dates and targets[sample.sample_id] < calibration_start]
    calibration = [sample for sample in samples if sample.session_date in calibration_dates]
    if len({sample.session_date for sample in train}) < 120 or any({sample.target for sample in group} != {0, 1} for group in (train, calibration)):
        raise ExperimentalProbabilityUnavailable("实验模型训练/校准样本不足或没有正负两类标签")
    return train, calibration, calibration_start, dates[-1]


def fit_experimental_estimator(payload: dict[str, Any], *, source_filename: str,
                               source_sha256: str, source_integrity_digest: str) -> ExperimentalEstimator:
    """Called only by the offline builder after the entire source is verified."""
    samples, targets, symbols = _experimental_samples(payload)
    train, calibration, calibration_start, calibration_end = _partition_experimental_samples(samples, targets)
    config = ProbabilityConfig(horizon=5, target="net_return_positive")
    model = fit_probability_logistic_model(train, MODEL_FEATURE_NAMES, config)
    raw = [probability_model_probability(model, sample.features) for sample in calibration]
    labels = [int(sample.target) for sample in calibration if sample.target is not None]
    calibrator = fit_probability_platt_calibrator(raw, labels, config)
    latest_label = max(targets[sample.sample_id] for sample in calibration)
    evaluation = payload["probability_fit"]["horizons"]["5"]
    return ExperimentalEstimator(
        generated_at=market_now().isoformat(), source_filename=source_filename,
        source_sha256=source_sha256, source_integrity_digest=source_integrity_digest,
        training_symbols=sorted(symbols), train_session_count=len({sample.session_date for sample in train}),
        calibration_session_count=40, train_record_count=len(train), calibration_record_count=len(calibration),
        train_signal_end=max(sample.session_date for sample in train), train_label_end=max(targets[sample.sample_id] for sample in train),
        calibration_start=calibration_start, calibration_end=calibration_end, latest_label_date=latest_label,
        expires_after_signal_date=(date.fromisoformat(latest_label) + timedelta(days=MAX_SIGNAL_AGE_DAYS)).isoformat(),
        base_rate=sum(labels) / len(labels), model=ExperimentalLogit.model_validate(model),
        calibrator=ExperimentalCalibrator.model_validate(calibrator), cost_contract=payload["cost_contract"],
        historical_recipe_evaluation={key: evaluation.get(key) for key in ("status", "counts", "calibration_metrics", "limitations")},
        limitations=[*payload["limitations"], "personal_experimental_not_formal_authority",
                     "historical_oos_evaluates_recipe_not_this_refitted_estimator",
                     "current_predictions_use_reconstructed_cache_not_original_pit",
                     "new_universe_generalization_unverified"],
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


def load_experimental_model(directory: Path) -> tuple[ExperimentalEstimator, str]:
    if not path_has_only_trusted_aliases(directory):
        raise ExperimentalProbabilityUnavailable("个人实验模型目录包含不可信路径")
    candidates = sorted(directory.glob("experimental-h5-*.json"))
    if not candidates:
        raise ExperimentalProbabilityUnavailable("个人实验模型尚未生成，请先运行 tools/build_experimental_probability.py")
    if len(candidates) > 32:
        raise ExperimentalProbabilityUnavailable("个人实验模型目录过大，请明确保留的版本")
    loaded: list[tuple[ExperimentalEstimator, str]] = []
    try:
        for path in candidates:
            value = decode_json_bytes(read_regular_file(path, max_bytes=MODEL_MAX_BYTES))
            if not isinstance(value, dict) or set(value) != {"payload", "sha256"}:
                raise ValueError("experimental artifact envelope mismatch")
            digest = sha256_hex(canonical_json_bytes(value["payload"]))
            if digest != value["sha256"] or path.name != f"experimental-h5-{digest}.json":
                raise ValueError("experimental model digest mismatch")
            estimator = ExperimentalEstimator.model_validate(value["payload"])
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
