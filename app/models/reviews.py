"""Structured advice review plans, evaluations, and bounded scan contracts."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.market import KlineAdjustmentMode
from app.utils.market_time import market_local_naive


PositiveFiniteFloat = Annotated[FiniteFloat, Field(gt=0)]
OptionalSha256 = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64}|legacy-unverified)$")]
AdviceReviewStatus = Literal["pending", "insufficient", "evaluated"]
AdviceReviewConclusion = Literal[
    "pending",
    "insufficient_data",
    "target_hit",
    "stop_hit",
    "target_stop_ambiguous",
    "horizon_gain",
    "horizon_loss",
    "horizon_flat",
]
AdviceEvidenceDirection = Literal["positive", "negative", "neutral"]
AdviceEvidenceNature = Literal["observed", "derived", "estimated", "unavailable"]
AdviceEvidenceValue = str | int | float | bool | None
AdviceReviewTriggerBasis = Literal["daily_high_gte_target_price"]
AdviceReviewInvalidationBasis = Literal["daily_low_lte_stop_price"]
AdviceReviewExecutableBasis = AdviceReviewTriggerBasis | AdviceReviewInvalidationBasis
DEFAULT_ADVICE_REVIEW_TRIGGER_BASIS: AdviceReviewTriggerBasis = "daily_high_gte_target_price"
DEFAULT_ADVICE_REVIEW_INVALIDATION_BASIS: AdviceReviewInvalidationBasis = "daily_low_lte_stop_price"
ResearchQueueRefreshStatus = Literal["saved", "unchanged", "skipped", "failed"]
ResearchQueueRefreshReason = Literal[
    "not_after_close",
    "non_official_research_mode",
    "stale_data_date",
    "low_data_quality",
    "invalid_rule_contract",
    "already_current",
    "analysis_failed",
]
AdviceReviewBatchItemStatus = Literal["evaluated", "insufficient", "pending", "failed"]
WatchlistScanUniverse = Literal["watchlist", "symbols"]
WatchlistScanCondition = Literal[
    "close_above_ma20",
    "close_below_ma20",
    "breakout_20d_high",
    "volume_surge_5d",
]


class ReviewInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdviceEvidenceRef(ReviewInputModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: AdviceEvidenceValue = None
    direction: AdviceEvidenceDirection
    data_date: str = Field(min_length=10, max_length=10)
    nature: AdviceEvidenceNature
    rule_version: str = Field(min_length=1, max_length=80)

    @field_validator("data_date")
    @classmethod
    def validate_data_date(cls, value: str) -> str:
        return _strict_iso_date_text(value, "证据 data_date 必须是 YYYY-MM-DD")

    @field_validator("rule_version")
    @classmethod
    def clean_rule_version(cls, value: str) -> str:
        return _required_text(value)

    @model_validator(mode="after")
    def validate_value(self) -> AdviceEvidenceRef:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("证据 value 必须是有效值")
        return self


AdviceEvidenceRefValue = AdviceEvidenceRef | str


class AdviceReviewPlanInput(ReviewInputModel):
    advice_id: int = Field(gt=0)
    symbol: str = Field(min_length=1, max_length=20)
    hypothesis: str = Field(min_length=1, max_length=1000)
    trigger_condition: str = Field(min_length=1, max_length=1000)
    invalidation_condition: str = Field(min_length=1, max_length=1000)
    trigger_basis: AdviceReviewTriggerBasis = DEFAULT_ADVICE_REVIEW_TRIGGER_BASIS
    invalidation_basis: AdviceReviewInvalidationBasis = DEFAULT_ADVICE_REVIEW_INVALIDATION_BASIS
    target_price: PositiveFiniteFloat
    stop_price: PositiveFiniteFloat
    horizon_days: int = Field(ge=1, le=60)
    evidence_refs: list[AdviceEvidenceRefValue] = Field(default_factory=list, max_length=50)

    @field_validator("hypothesis", "trigger_condition", "invalidation_condition")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("evidence_refs")
    @classmethod
    def clean_evidence_refs(cls, value: list[AdviceEvidenceRefValue]) -> list[AdviceEvidenceRefValue]:
        return _clean_evidence_refs(value)

    @model_validator(mode="after")
    def validate_price_order(self) -> AdviceReviewPlanInput:
        if self.target_price <= self.stop_price:
            raise ValueError("目标价必须高于止损价")
        return self


class AdviceReviewPlanUpdate(ReviewInputModel):
    expected_revision: int = Field(ge=1)
    hypothesis: str | None = Field(default=None, min_length=1, max_length=1000)
    trigger_condition: str | None = Field(default=None, min_length=1, max_length=1000)
    invalidation_condition: str | None = Field(default=None, min_length=1, max_length=1000)
    target_price: PositiveFiniteFloat | None = None
    stop_price: PositiveFiniteFloat | None = None
    horizon_days: int | None = Field(default=None, ge=1, le=60)
    evidence_refs: list[AdviceEvidenceRefValue] | None = Field(default=None, max_length=50)

    @field_validator("hypothesis", "trigger_condition", "invalidation_condition")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        return _required_text(value) if value is not None else None

    @field_validator("evidence_refs")
    @classmethod
    def clean_optional_evidence_refs(
        cls,
        value: list[AdviceEvidenceRefValue] | None,
    ) -> list[AdviceEvidenceRefValue] | None:
        return _clean_evidence_refs(value) if value is not None else None


class AdviceSnapshotRef(BaseModel):
    advice_id: int
    symbol: str
    market_time: str
    price: float
    adjustment_mode: KlineAdjustmentMode = "unknown"
    anchor_date: str | None = None
    anchor_close: float | None = None
    data_version: str = "unknown"
    contract_version: str = "unknown"
    advice_contract_version: str = "legacy"
    rule_version: str = "unknown"
    action: str | None = None
    trend_score: int | None = None
    risk_level: str | None = None
    support: float | None = None
    resistance: float | None = None
    data_quality_score: int | None = None


class AdviceReviewPlan(BaseModel):
    id: int
    advice_id: int
    symbol: str
    snapshot_market_time: str
    snapshot_price: float
    snapshot_adjustment_mode: KlineAdjustmentMode = "unknown"
    snapshot_anchor_date: str | None = None
    snapshot_anchor_close: FiniteFloat | None = None
    snapshot_data_version: str = "unknown"
    snapshot_contract_version: str = "unknown"
    hypothesis: str
    trigger_condition: str
    invalidation_condition: str
    trigger_basis: AdviceReviewTriggerBasis = DEFAULT_ADVICE_REVIEW_TRIGGER_BASIS
    invalidation_basis: AdviceReviewInvalidationBasis = DEFAULT_ADVICE_REVIEW_INVALIDATION_BASIS
    target_price: float
    stop_price: float
    horizon_days: int
    evidence_refs: list[AdviceEvidenceRefValue] = Field(default_factory=list)
    revision: int = Field(ge=1)
    plan_payload_digest: OptionalSha256 = "legacy-unverified"
    created_at: str
    updated_at: str


class AdviceReviewEvaluationRequest(ReviewInputModel):
    as_of: datetime | None = None
    expected_revision: int = Field(ge=1)


class AdviceReviewBatchEvaluationRequest(ReviewInputModel):
    as_of: datetime | None = None


class AdviceReviewOutcomeEvidence(BaseModel):
    id: Literal["trigger", "invalidation"]
    basis: AdviceReviewExecutableBasis
    met: bool
    price: float | None = None
    data_date: str | None = None
    nature: AdviceEvidenceNature
    rule_version: str


class AdviceReviewEvaluationDraft(BaseModel):
    plan_id: int
    plan_revision: int
    advice_id: int
    symbol: str
    snapshot_market_time: str
    as_of: str
    evaluated_at: str
    status: AdviceReviewStatus
    conclusion: AdviceReviewConclusion
    rule_version: str
    trigger_basis: AdviceReviewTriggerBasis = DEFAULT_ADVICE_REVIEW_TRIGGER_BASIS
    invalidation_basis: AdviceReviewInvalidationBasis = DEFAULT_ADVICE_REVIEW_INVALIDATION_BASIS
    snapshot_adjustment_mode: KlineAdjustmentMode = "unknown"
    snapshot_anchor_date: str | None = None
    snapshot_anchor_close: float | None = None
    snapshot_data_version: str = "unknown"
    snapshot_contract_version: str = "unknown"
    evaluation_adjustment_mode: KlineAdjustmentMode = "unknown"
    evaluation_data_version: str = "unknown"
    evaluation_contract_version: str = "unknown"
    anchor_evaluation_close: FiniteFloat | None = None
    price_scale_factor: FiniteFloat | None = None
    normalized_entry_price: FiniteFloat | None = None
    normalized_target_price: FiniteFloat | None = None
    normalized_stop_price: FiniteFloat | None = None
    entry_price: PositiveFiniteFloat
    target_price: PositiveFiniteFloat
    stop_price: PositiveFiniteFloat
    horizon_days: int = Field(ge=1, le=60)
    visible_bar_count: int = Field(ge=0)
    visible_start_date: str | None = None
    visible_end_date: str | None = None
    available_forward_days: int = Field(ge=0)
    forward_start_date: str | None = None
    forward_end_date: str | None = None
    return_pct: FiniteFloat | None = None
    max_favorable_excursion_pct: FiniteFloat | None = None
    max_adverse_excursion_pct: FiniteFloat | None = None
    target_hit: bool = False
    target_hit_date: str | None = None
    stop_hit: bool = False
    stop_hit_date: str | None = None
    attempt: int = Field(default=1, ge=1)
    plan_payload_digest: OptionalSha256 = "legacy-unverified"
    input_digest: OptionalSha256 = "legacy-unverified"
    result_digest: OptionalSha256 = "legacy-unverified"
    evidence_contract_version: Literal[
        "advice-review-evidence.v2",
        "advice-review-evidence.v1",
        "legacy-unverified",
    ] = "advice-review-evidence.v2"
    source_window_digest: OptionalSha256
    source_session_count: int = Field(ge=0)
    expected_session_count: int = Field(ge=0)
    observation_basis: Literal["gross_close_and_barrier_observation"] = (
        "gross_close_and_barrier_observation"
    )
    trigger_evidence: AdviceReviewOutcomeEvidence | None = None
    invalidation_evidence: AdviceReviewOutcomeEvidence | None = None

    @model_validator(mode="after")
    def populate_outcome_evidence(self) -> AdviceReviewEvaluationDraft:
        _validate_evaluation_contract(self)
        if self.trigger_evidence is None:
            self.trigger_evidence = _review_outcome_evidence(self, trigger=True)
        if self.invalidation_evidence is None:
            self.invalidation_evidence = _review_outcome_evidence(self, trigger=False)
        return self


class AdviceReviewEvaluation(AdviceReviewEvaluationDraft):
    id: int


class AdviceReviewDetail(BaseModel):
    plan: AdviceReviewPlan
    latest_evaluation: AdviceReviewEvaluation | None = None


class AdviceReviewDueItem(AdviceReviewDetail):
    due_date: str
    overdue_trading_days: int = Field(default=0, ge=0)


class ResearchQueueRefreshItem(BaseModel):
    symbol: str
    status: ResearchQueueRefreshStatus
    reason_code: ResearchQueueRefreshReason | None = None
    advice_id: int | None = None
    data_date: str | None = None
    message: str | None = None


class ResearchQueueRefreshSummary(BaseModel):
    started_at: str
    data_date: str | None = None
    active_count: int = Field(default=0, ge=0)
    selected_count: int = Field(default=0, ge=0)
    saved_count: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    deferred: bool = False
    reason_code: ResearchQueueRefreshReason | None = None
    items: list[ResearchQueueRefreshItem] = Field(default_factory=list)


class AdviceReviewBatchItem(BaseModel):
    plan_id: int
    symbol: str
    status: AdviceReviewBatchItemStatus
    evaluation_id: int | None = None
    conclusion: AdviceReviewConclusion | None = None
    message: str | None = None


class AdviceReviewBatchSummary(BaseModel):
    as_of: str
    candidate_count: int = Field(default=0, ge=0)
    attempted_count: int = Field(default=0, ge=0)
    evaluated_count: int = Field(default=0, ge=0)
    insufficient_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    items: list[AdviceReviewBatchItem] = Field(default_factory=list)


class AdviceReviewSummary(BaseModel):
    generated_at: str
    total_plan_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    insufficient_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    favorable_count: int = Field(ge=0)
    unfavorable_count: int = Field(ge=0)
    ambiguous_count: int = Field(ge=0)
    target_hit_count: int = Field(ge=0)
    stop_hit_count: int = Field(ge=0)
    favorable_rate_pct: float | None = None
    average_return_pct: float | None = None
    average_mfe_pct: float | None = None
    average_mae_pct: float | None = None
    conclusion_counts: dict[str, int] = Field(default_factory=dict)


class WatchlistScanRequest(ReviewInputModel):
    universe: WatchlistScanUniverse = "watchlist"
    symbols: list[str] = Field(default_factory=list, max_length=50)
    conditions: list[WatchlistScanCondition] = Field(min_length=1, max_length=4)
    rule_version: Literal["watchlist-scan-v1"] = "watchlist-scan-v1"
    as_of: datetime | None = None


class WatchlistScanItem(BaseModel):
    symbol: str
    data_date: str
    matched: bool
    condition_results: dict[str, bool]
    matched_conditions: list[WatchlistScanCondition]
    metrics: dict[str, float]


class WatchlistScanMissing(BaseModel):
    symbol: str
    reason: str


class WatchlistScanResponse(BaseModel):
    universe: list[str]
    success: list[WatchlistScanItem]
    missing: list[WatchlistScanMissing]
    as_of: str
    rule_version: Literal["watchlist-scan-v1"] = "watchlist-scan-v1"
    conditions: list[WatchlistScanCondition]


class WatchlistScanRecord(WatchlistScanResponse):
    id: int
    universe_kind: WatchlistScanUniverse
    created_at: str


class WatchlistScanHistoryItem(BaseModel):
    id: int
    universe_kind: WatchlistScanUniverse
    as_of: str
    rule_version: str
    conditions: list[WatchlistScanCondition]
    universe_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    created_at: str


def _required_text(value: str) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise ValueError("内容不能为空")
    return cleaned


def _clean_evidence_refs(values: list[AdviceEvidenceRefValue]) -> list[AdviceEvidenceRefValue]:
    cleaned: list[AdviceEvidenceRefValue] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, AdviceEvidenceRef):
            key = value.model_dump_json()
            item: AdviceEvidenceRefValue = value
        else:
            text = " ".join(value.split()).strip()
            if not text:
                raise ValueError("证据引用不能为空")
            if len(text) > 240:
                raise ValueError("单条证据引用不能超过240个字符")
            key = text
            item = text
        if key not in seen:
            cleaned.append(item)
            seen.add(key)
    return cleaned


def structured_advice_evidence_refs(snapshot: object) -> list[AdviceEvidenceRef]:
    data_date = _snapshot_data_date(snapshot)
    rule_version = str(
        getattr(snapshot, "rule_version", None)
        or getattr(snapshot, "snapshot_rule_version", None)
        or "unknown"
    ).strip()
    if data_date is None or not rule_version or rule_version in {"unknown", "legacy"}:
        return []
    action = getattr(snapshot, "action", None)
    trend_score = getattr(snapshot, "trend_score", None)
    risk_level = getattr(snapshot, "risk_level", None)
    quality_score = getattr(snapshot, "data_quality_score", None)
    candidates = (
        ("action", action, _text_direction(action), "derived"),
        ("price", getattr(snapshot, "price", None), "neutral", "observed"),
        ("trend_score", trend_score, _score_direction(trend_score, positive=60, negative=40), "derived"),
        ("risk_level", risk_level, _risk_direction(risk_level), "derived"),
        ("support", getattr(snapshot, "support", None), "positive", "derived"),
        ("resistance", getattr(snapshot, "resistance", None), "neutral", "derived"),
        (
            "data_quality_score",
            quality_score,
            _score_direction(quality_score, positive=60, negative=50),
            "derived",
        ),
    )
    return [
        AdviceEvidenceRef(
            id=evidence_id,
            value=value,
            direction=direction,
            data_date=data_date,
            nature=nature,
            rule_version=rule_version,
        )
        for evidence_id, value, direction, nature in candidates
        if value is not None and (not isinstance(value, str) or value.strip())
    ]


def _snapshot_data_date(snapshot: object) -> str | None:
    anchor_date = getattr(snapshot, "anchor_date", None) or getattr(snapshot, "kline_anchor_date", None)
    if anchor_date:
        try:
            return _strict_iso_date_text(str(anchor_date), "")
        except ValueError:
            pass
    market_time = str(getattr(snapshot, "market_time", None) or "").strip()
    try:
        return _strict_iso_date_text(market_time[:10], "")
    except ValueError:
        return None


def _strict_iso_date_text(value: str, message: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(message or "日期无效") from exc
    if parsed.isoformat() != text:
        raise ValueError(message or "日期无效")
    return text


def _strict_datetime_text(value: str, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 必须是有效日期时间") from exc
    if "T" not in text and " " not in text:
        raise ValueError(f"{field} 必须包含日期和时间")
    return parsed


def _validate_evaluation_contract(value: AdviceReviewEvaluationDraft) -> None:
    snapshot_at = market_local_naive(_strict_datetime_text(value.snapshot_market_time, "snapshot_market_time"))
    as_of = market_local_naive(_strict_datetime_text(value.as_of, "as_of"))
    evaluated_at = market_local_naive(_strict_datetime_text(value.evaluated_at, "evaluated_at"))
    if as_of < snapshot_at:
        raise ValueError("as_of 不能早于 snapshot_market_time")
    if as_of > evaluated_at:
        raise ValueError("as_of 不能晚于 evaluated_at")
    if not str(value.rule_version or "").strip():
        raise ValueError("rule_version 不能为空")
    _validate_status_and_conclusion(value)
    _validate_evaluation_window(value)
    _validate_evaluation_digests(value)


def _validate_status_and_conclusion(value: AdviceReviewEvaluationDraft) -> None:
    expected_conclusions = {
        "pending": {"pending"},
        "insufficient": {"insufficient_data"},
        "evaluated": {
            "target_hit",
            "stop_hit",
            "target_stop_ambiguous",
            "horizon_gain",
            "horizon_loss",
            "horizon_flat",
        },
    }
    if value.conclusion not in expected_conclusions[value.status]:
        raise ValueError("status 与 conclusion 不一致")
    _validate_hit_state(value)
    _validate_evaluation_metric_state(value)
    _validate_horizon_return_direction(value)


def _validate_hit_state(value: AdviceReviewEvaluationDraft) -> None:
    if value.target_hit != bool(value.target_hit_date):
        raise ValueError("target_hit 与 target_hit_date 不一致")
    if value.stop_hit != bool(value.stop_hit_date):
        raise ValueError("stop_hit 与 stop_hit_date 不一致")
    _validate_barrier_conclusion(value)


def _validate_barrier_conclusion(value: AdviceReviewEvaluationDraft) -> None:
    if value.conclusion == "target_hit" and (not value.target_hit or value.stop_hit):
        raise ValueError("target_hit 结论与价格屏障命中不一致")
    if value.conclusion == "stop_hit" and (not value.stop_hit or value.target_hit):
        raise ValueError("stop_hit 结论与价格屏障命中不一致")
    if value.conclusion == "target_stop_ambiguous" and not (value.target_hit and value.stop_hit):
        raise ValueError("歧义结论必须同时包含目标和止损命中")
    if value.conclusion.startswith("horizon_") and (value.target_hit or value.stop_hit):
        raise ValueError("周期结论不能同时声明价格屏障命中")


def _validate_evaluation_metric_state(value: AdviceReviewEvaluationDraft) -> None:
    metrics = (
        value.return_pct,
        value.max_favorable_excursion_pct,
        value.max_adverse_excursion_pct,
    )
    if value.status != "evaluated" and (
        any(metric is not None for metric in metrics) or value.target_hit or value.stop_hit
    ):
        raise ValueError("未完成复盘不能携带收益或命中结果")
    if value.status == "evaluated" and (
        value.available_forward_days <= 0 or any(metric is None for metric in metrics)
    ):
        raise ValueError("正式复盘必须包含前向会话和完整指标")


def _validate_horizon_return_direction(value: AdviceReviewEvaluationDraft) -> None:
    if value.status != "evaluated" or value.return_pct is None:
        return
    invalid_direction = (
        (value.conclusion == "horizon_gain" and value.return_pct <= 0)
        or (value.conclusion == "horizon_loss" and value.return_pct >= 0)
        or (value.conclusion == "horizon_flat" and value.return_pct != 0)
    )
    if invalid_direction:
        raise ValueError(f"{value.conclusion} 与价格变化方向不一致")


def _validate_evaluation_window(value: AdviceReviewEvaluationDraft) -> None:
    visible_start = _optional_date_text(value.visible_start_date, "visible_start_date")
    visible_end = _optional_date_text(value.visible_end_date, "visible_end_date")
    forward_start = _optional_date_text(value.forward_start_date, "forward_start_date")
    forward_end = _optional_date_text(value.forward_end_date, "forward_end_date")
    _validate_window_bounds(value.visible_bar_count, visible_start, visible_end, "可见")
    _validate_window_bounds(value.available_forward_days, forward_start, forward_end, "前向")
    snapshot_date = _strict_iso_date_text(value.snapshot_market_time[:10], "snapshot date 无效")
    if visible_end is not None and visible_end.isoformat() > snapshot_date:
        raise ValueError("可见窗口不能晚于快照日期")
    if forward_start is not None and forward_start.isoformat() <= snapshot_date:
        raise ValueError("前向窗口必须晚于快照日期")
    _validate_hit_dates(value, forward_start, forward_end)


def _validate_window_bounds(count: int, start: date | None, end: date | None, label: str) -> None:
    if count == 0 and (start is not None or end is not None):
        raise ValueError(f"空{label}窗口不能包含日期")
    if count > 0 and (start is None or end is None or start > end):
        raise ValueError(f"{label}窗口日期不完整或顺序无效")


def _validate_hit_dates(
    value: AdviceReviewEvaluationDraft,
    forward_start: date | None,
    forward_end: date | None,
) -> None:
    for hit_date, field in (
        (value.target_hit_date, "target_hit_date"),
        (value.stop_hit_date, "stop_hit_date"),
    ):
        hit = _optional_date_text(hit_date, field)
        if hit is not None and (
            forward_start is None or forward_end is None or not forward_start <= hit <= forward_end
        ):
            raise ValueError(f"{field} 必须位于前向窗口")


def _optional_date_text(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(_strict_iso_date_text(value, f"{field} 必须是 YYYY-MM-DD"))


def _validate_evaluation_digests(value: AdviceReviewEvaluationDraft) -> None:
    for field in ("plan_payload_digest", "input_digest", "result_digest"):
        digest = str(getattr(value, field) or "")
        if digest != "legacy-unverified" and (
            len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"{field} 必须是 SHA-256")
    digest = str(value.source_window_digest or "")
    if digest != "legacy-unverified" and (
        len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("source_window_digest 必须是 SHA-256")
    if value.available_forward_days > value.source_session_count:
        raise ValueError("可用前向会话不能超过来源会话数")


def _score_direction(value: object, *, positive: int, negative: int) -> AdviceEvidenceDirection:
    if isinstance(value, bool):
        return "neutral"
    if not isinstance(value, str | bytes | bytearray | int | float):
        return "neutral"
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "neutral"
    if not math.isfinite(score):
        return "neutral"
    if score >= positive:
        return "positive"
    if score < negative:
        return "negative"
    return "neutral"


def _text_direction(value: object) -> AdviceEvidenceDirection:
    text = str(value or "").strip().casefold()
    if any(token in text for token in ("buy", "add", "hold", "买", "加仓", "持有", "积极")):
        return "positive"
    if any(token in text for token in ("sell", "reduce", "avoid", "卖", "减仓", "回避", "风险")):
        return "negative"
    return "neutral"


def _risk_direction(value: object) -> AdviceEvidenceDirection:
    text = str(value or "").strip().casefold()
    if any(token in text for token in ("low", "controlled", "低", "可控")):
        return "positive"
    if any(token in text for token in ("high", "severe", "高", "较高", "严重")):
        return "negative"
    return "neutral"


def _review_outcome_evidence(
    evaluation: AdviceReviewEvaluationDraft,
    *,
    trigger: bool,
) -> AdviceReviewOutcomeEvidence:
    met = evaluation.target_hit if trigger else evaluation.stop_hit
    price = evaluation.normalized_target_price if trigger else evaluation.normalized_stop_price
    hit_date = evaluation.target_hit_date if trigger else evaluation.stop_hit_date
    has_observed_window = evaluation.available_forward_days > 0 and price is not None
    return AdviceReviewOutcomeEvidence(
        id="trigger" if trigger else "invalidation",
        basis=evaluation.trigger_basis if trigger else evaluation.invalidation_basis,
        met=met,
        price=price,
        data_date=hit_date or evaluation.forward_end_date,
        nature="observed" if has_observed_window else "unavailable",
        rule_version=evaluation.rule_version,
    )


__all__ = [
    "AdviceReviewConclusion",
    "AdviceReviewExecutableBasis",
    "AdviceReviewInvalidationBasis",
    "AdviceReviewTriggerBasis",
    "AdviceEvidenceDirection",
    "AdviceEvidenceNature",
    "AdviceEvidenceRef",
    "AdviceReviewDetail",
    "AdviceReviewDueItem",
    "AdviceReviewBatchItem",
    "AdviceReviewBatchSummary",
    "AdviceReviewEvaluation",
    "AdviceReviewBatchEvaluationRequest",
    "AdviceReviewEvaluationDraft",
    "AdviceReviewEvaluationRequest",
    "AdviceReviewOutcomeEvidence",
    "AdviceReviewPlan",
    "AdviceReviewPlanInput",
    "AdviceReviewPlanUpdate",
    "AdviceReviewStatus",
    "AdviceReviewSummary",
    "AdviceSnapshotRef",
    "DEFAULT_ADVICE_REVIEW_INVALIDATION_BASIS",
    "DEFAULT_ADVICE_REVIEW_TRIGGER_BASIS",
    "ResearchQueueRefreshItem",
    "ResearchQueueRefreshSummary",
    "WatchlistScanCondition",
    "WatchlistScanItem",
    "WatchlistScanMissing",
    "WatchlistScanHistoryItem",
    "WatchlistScanRecord",
    "WatchlistScanRequest",
    "WatchlistScanResponse",
    "WatchlistScanUniverse",
    "structured_advice_evidence_refs",
]
