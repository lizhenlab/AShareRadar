"""Typed contracts for individual-stock short-horizon probability research."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import math
import re
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.clock import utc_now
from app.utils.exchange_calendar_contract import is_bundled_exchange_session


IndividualProbabilityStatus = Literal[
    "not_generated",
    "insufficient_data",
    "calibrated_shadow",
]
MINIMUM_SELECTION_FOLDS = 2
REQUIRED_OFFICIAL_PIT_SESSIONS = 288
MINIMUM_SELECTION_SESSIONS = {2: 284, 3: 286, 4: 288}
MINIMUM_TEST_SESSIONS_PER_FOLD = 60
MINIMUM_CALIBRATION_BIN_SESSIONS = 20
DAILY_EVIDENCE_PUBLISH_TIME = time(15, 15)
REPORT_FUTURE_SKEW = timedelta(minutes=5)
ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REGISTERED_TARGET_VERSION = "individual-upside-net-return-label-v1"
REGISTERED_COST_PROFILE = "base-a0441d84df44"
REGISTERED_FEATURE_VERSION = "historical-replay-common-ohlcv-v1"
REGISTERED_MODEL_VERSION = "shadow-up-probability-logit-l2-v2-convergence-required"


class IndividualProbabilityCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    observation_count: int = Field(default=0, ge=0)
    eligible_observation_count: int = Field(default=0, ge=0)
    independent_session_count: int = Field(default=0, ge=0)
    out_of_sample_observation_count: int = Field(default=0, ge=0)
    out_of_sample_session_count: int = Field(default=0, ge=0)
    evaluated_fold_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_count_order(self) -> IndividualProbabilityCounts:
        if self.eligible_observation_count > self.observation_count:
            raise ValueError("eligible observations 不能超过全部 observations")
        if self.out_of_sample_observation_count > self.eligible_observation_count:
            raise ValueError("OOS observations 不能超过 eligible observations")
        if self.out_of_sample_session_count > self.independent_session_count:
            raise ValueError("OOS sessions 不能超过 independent sessions")
        if self.independent_session_count > self.eligible_observation_count:
            raise ValueError("independent sessions 不能超过 eligible observations")
        if self.out_of_sample_session_count > self.out_of_sample_observation_count:
            raise ValueError("OOS sessions 不能超过 OOS observations")
        return self


class IndividualProbabilityInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lower: float = Field(ge=0, le=1)
    upper: float = Field(ge=0, le=1)
    level: float = Field(default=0.95, ge=0, le=1)

    @model_validator(mode="after")
    def validate_order(self) -> IndividualProbabilityInterval:
        if self.level != 0.95:
            raise ValueError("个股上涨概率 CI level 固定为 0.95")
        if self.lower > self.upper:
            raise ValueError("个股上涨概率 CI 上下界颠倒")
        return self


class IndividualProbabilityMetrics(BaseModel):
    """Historical out-of-sample diagnostics, never a current-stock estimate."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    brier_score: float | None = Field(default=None, ge=0, le=1)
    reference_brier_score: float | None = Field(default=None, ge=0, le=1)
    brier_skill_score: float | None = None
    ece: float | None = Field(default=None, ge=0, le=1)
    auc: float | None = Field(default=None, ge=0, le=1)
    actual_positive_rate: float | None = Field(default=None, ge=0, le=1)
    actual_positive_rate_ci_95: IndividualProbabilityInterval | None = None
    bin_monotonic: bool | None = None
    highest_bin_above_base_rate: bool | None = None
    selection_gate_version: Literal["market-scan-probability-selection-gates-v1"] | None = None
    calibration_bin_count: int | None = Field(default=None, ge=0)
    minimum_calibration_bin_session_count: int | None = Field(default=None, ge=0)
    all_folds_positive_brier_skill: bool | None = None

    @model_validator(mode="after")
    def validate_rate_interval(self) -> IndividualProbabilityMetrics:
        interval = self.actual_positive_rate_ci_95
        if interval is not None and self.actual_positive_rate is not None:
            if not interval.lower <= self.actual_positive_rate <= interval.upper:
                raise ValueError("历史 OOS 正例率 CI 必须覆盖 actual_positive_rate")
        _validate_brier_identity(self)
        return self


class IndividualProbabilityTargetContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal["individual-upside-net-return-label-v1"]
    signal_cutoff: Literal["completed_session_D_close"]
    entry: Literal["D_plus_1_official_daily_open_proxy_no_shift"]
    exits: dict[str, str]
    target: Literal["round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy"]
    cost_profile: Literal["base-a0441d84df44"]
    execution_notional: float = Field(gt=0)
    feature_version: Literal["historical-replay-common-ohlcv-v1"]
    point_in_time_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_exits(self) -> IndividualProbabilityTargetContract:
        expected = {
            "D+2": "D_plus_2_close_holding_session_1",
            "D+3": "D_plus_3_close_holding_session_2",
            "D+4": "D_plus_4_close_holding_session_3",
        }
        if self.exits != expected:
            raise ValueError("个股上涨概率 exits 契约必须固定为 D+2/D+3/D+4")
        if self.execution_notional != 100000.0:
            raise ValueError("个股上涨概率执行名义本金必须固定为 100000")
        return self


class IndividualUpsideHorizon(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    display_day: Literal[2, 3, 4]
    holding_sessions: Literal[1, 2, 3]
    status: IndividualProbabilityStatus
    probability: float | None = Field(default=None, ge=0, le=1)
    confidence_interval: IndividualProbabilityInterval | None = None
    base_rate: float | None = Field(default=None, ge=0, le=1)
    counts: IndividualProbabilityCounts
    calibration_metrics: IndividualProbabilityMetrics | None = None
    training_cutoff: str | None = None
    model_version: Literal["shadow-up-probability-logit-l2-v2-convergence-required"] | None = None
    feature_version: Literal["historical-replay-common-ohlcv-v1"]
    evidence_digest: str | None = None
    gate_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_probability_state(self) -> IndividualUpsideHorizon:
        if self.display_day != self.holding_sessions + 1:
            raise ValueError("D+k 展示日必须与持有期 k-1 一致")
        if self.status != "calibrated_shadow":
            if self.probability is not None or self.confidence_interval is not None:
                raise ValueError("证据未过门禁时 probability 和 CI 必须为 null")
            return self
        if self.probability is None or self.confidence_interval is None:
            raise ValueError("calibrated_shadow 必须同时提供 probability 和 CI")
        _validate_calibrated_counts(self)
        _validate_calibrated_evidence(self)
        _validate_calibrated_metrics(self.calibration_metrics)
        if self.gate_reasons:
            raise ValueError("calibrated_shadow 不能携带任何阻断或限制原因")
        lower, upper = self.confidence_interval.lower, self.confidence_interval.upper
        if not lower <= self.probability <= upper:
            raise ValueError("个股上涨概率 CI 必须覆盖 probability 且位于 [0,1]")
        return self


class IndividualProbabilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    assessment_digest: str | None = None
    history_manifest_digest: str | None = None
    history_database_sha256: str | None = None
    official_pit_session_count: int = Field(default=0, ge=0)
    required_official_pit_session_count: Literal[288]
    historical_replay_session_count: int = Field(default=0, ge=0)
    historical_replay_official: bool = False
    selection_qualified: bool = False


class IndividualUpsideProbabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal["individual-upside-probability-v1"] = (
        "individual-upside-probability-v1"
    )
    symbol: str
    signal_date: str | None = None
    generated_at: str
    status: IndividualProbabilityStatus
    target_contract: IndividualProbabilityTargetContract
    horizons: list[IndividualUpsideHorizon]
    evidence: IndividualProbabilityEvidence
    limitations: list[str] = Field(default_factory=list)
    production_effect: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_report_state(self) -> IndividualUpsideProbabilityReport:
        _validate_report_identity_and_time(self)
        if self.status == "calibrated_shadow":
            _validate_calibrated_report(self)
        elif self.evidence.selection_qualified:
            raise ValueError("未校准报告不能声明 selection_qualified")
        elif any(item.status == "calibrated_shadow" for item in self.horizons):
            raise ValueError("子周期不能绕过报告级 selection 门禁展示概率")
        return self


def _validate_report_identity_and_time(report: IndividualUpsideProbabilityReport) -> None:
    pairs = [(item.display_day, item.holding_sessions) for item in report.horizons]
    if pairs != [(2, 1), (3, 2), (4, 3)]:
        raise ValueError("个股上涨概率必须按 D+2/D+3/D+4 各返回一次")
    generated = _aware_report_time(report.generated_at)
    official_count = report.evidence.official_pit_session_count
    if (official_count == 0) != (report.signal_date is None):
        raise ValueError("signal_date 必须且只能来自正式 PIT 证据")
    if report.signal_date is not None:
        _validate_signal_maturity(report.signal_date, generated)
        _validate_training_cutoffs(report, report.signal_date)


def _validate_calibrated_report(report: IndividualUpsideProbabilityReport) -> None:
    evidence = report.evidence
    if not evidence.selection_qualified:
        raise ValueError("calibrated_shadow 必须通过 selection 门禁")
    if evidence.official_pit_session_count < REQUIRED_OFFICIAL_PIT_SESSIONS:
        raise ValueError("calibrated_shadow 的正式 PIT 日数未达到注册门槛")
    digests = (evidence.assessment_digest, evidence.history_manifest_digest, evidence.history_database_sha256)
    if any(value is None or SHA256_PATTERN.fullmatch(value) is None for value in digests):
        raise ValueError("calibrated_shadow 的 assessment/history/database 摘要证据不完整")
    if not evidence.historical_replay_official or evidence.historical_replay_session_count < REQUIRED_OFFICIAL_PIT_SESSIONS:
        raise ValueError("calibrated_shadow 必须绑定达到注册日数的正式历史回放证据")
    if not any(item.status == "calibrated_shadow" for item in report.horizons):
        raise ValueError("报告 calibrated_shadow 时至少一个独立周期须可用")


def _aware_report_time(value: str) -> datetime:
    try:
        generated = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("个股上涨概率 generated_at 必须是含时区的有效时间") from exc
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("个股上涨概率 generated_at 必须含时区")
    if generated.astimezone(UTC) > utc_now().astimezone(UTC) + REPORT_FUTURE_SKEW:
        raise ValueError("个股上涨概率 generated_at 不能晚于当前时间")
    return generated


def _validate_signal_maturity(signal_date: str, generated: datetime) -> None:
    try:
        signal = date.fromisoformat(signal_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("个股上涨概率 signal_date 必须是 ISO 日期") from exc
    if signal.isoformat() != signal_date:
        raise ValueError("个股上涨概率 signal_date 必须是 ISO 日期")
    if not is_bundled_exchange_session(signal):
        raise ValueError("个股上涨概率 signal_date 必须是可信交易所交易日")
    maturity = datetime.combine(signal, DAILY_EVIDENCE_PUBLISH_TIME, tzinfo=ASHARE_TIMEZONE)
    if generated < maturity:
        raise ValueError("正式 PIT 证据必须在信号日 15:15 后成熟")


def _validate_training_cutoffs(report: IndividualUpsideProbabilityReport, signal_date: str) -> None:
    signal = date.fromisoformat(signal_date)
    for horizon in report.horizons:
        if horizon.status != "calibrated_shadow":
            continue
        try:
            cutoff = date.fromisoformat(horizon.training_cutoff or "")
        except ValueError as exc:
            raise ValueError("calibrated_shadow training_cutoff 必须是 ISO 日期") from exc
        if (
            cutoff.isoformat() != horizon.training_cutoff
            or cutoff >= signal
            or not is_bundled_exchange_session(cutoff)
        ):
            raise ValueError("calibrated_shadow training_cutoff 必须是早于 signal_date 的可信交易日")


def _validate_brier_identity(metrics: IndividualProbabilityMetrics) -> None:
    values = (
        metrics.brier_score,
        metrics.reference_brier_score,
        metrics.brier_skill_score,
    )
    if any(value is None for value in values):
        return
    brier, reference, skill = (float(value) for value in values if value is not None)
    if reference <= 0 or not math.isclose(
        skill,
        1.0 - brier / reference,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        raise ValueError("Brier skill 必须由 Brier/reference Brier 确定性重建")


def _validate_calibrated_counts(horizon: IndividualUpsideHorizon) -> None:
    counts = horizon.counts
    folds = counts.evaluated_fold_count
    required_sessions = MINIMUM_SELECTION_SESSIONS[horizon.display_day]
    required_sessions += max(0, folds - MINIMUM_SELECTION_FOLDS) * MINIMUM_TEST_SESSIONS_PER_FOLD
    if (
        folds < MINIMUM_SELECTION_FOLDS
        or counts.independent_session_count < required_sessions
        or counts.out_of_sample_observation_count <= 0
        or counts.out_of_sample_session_count < folds * MINIMUM_TEST_SESSIONS_PER_FOLD
    ):
        raise ValueError("calibrated_shadow 未达到注册的独立交易日、OOS 样本与完整 folds 门槛")


def _validate_calibrated_evidence(horizon: IndividualUpsideHorizon) -> None:
    if (
        horizon.base_rate is None
        or horizon.calibration_metrics is None
        or not horizon.training_cutoff
        or not horizon.model_version
        or not horizon.evidence_digest
        or SHA256_PATTERN.fullmatch(horizon.evidence_digest) is None
    ):
        raise ValueError("calibrated_shadow 的 OOS 模型、校准与摘要证据不完整")


def _validate_calibrated_metrics(metrics: IndividualProbabilityMetrics | None) -> None:
    if metrics is None:
        raise ValueError("calibrated_shadow 的 OOS 校准摘要缺失")
    if (
        metrics.brier_score is None
        or metrics.reference_brier_score is None
        or metrics.reference_brier_score <= 0
        or metrics.brier_skill_score is None
        or metrics.brier_skill_score <= 0
        or metrics.actual_positive_rate is None
        or metrics.actual_positive_rate_ci_95 is None
        or metrics.bin_monotonic is not True
        or metrics.highest_bin_above_base_rate is not True
        or metrics.selection_gate_version != "market-scan-probability-selection-gates-v1"
        or (metrics.calibration_bin_count or 0) < 2
        or (metrics.minimum_calibration_bin_session_count or 0)
        < MINIMUM_CALIBRATION_BIN_SESSIONS
        or metrics.all_folds_positive_brier_skill is not True
    ):
        raise ValueError("calibrated_shadow 的 OOS 校准摘要未证明 selection 门禁通过")


__all__ = [
    "IndividualProbabilityCounts",
    "IndividualProbabilityEvidence",
    "IndividualProbabilityInterval",
    "IndividualProbabilityMetrics",
    "IndividualProbabilityStatus",
    "IndividualProbabilityTargetContract",
    "IndividualUpsideHorizon",
    "IndividualUpsideProbabilityReport",
]
