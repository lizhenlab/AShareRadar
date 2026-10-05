"""Independent frozen-template net scenarios; never a continuous portfolio NAV."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
import re
from typing import cast

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_strategy_templates import MarketStrategyTemplate
from app.models.paper_trading import PaperCostOverrides, PaperCostProfile
from app.models.strategy_lab import StrategyExecutionPolicy, StrategySpecInput
from app.services.market_scan_official_execution import VerifiedOfficialExecutionSession
from app.services.market_scan_research_portfolio_admission import admit_research_market_data
from app.services.paper_trading_costs import resolve_cost_profile
from app.services.strategy_compiler import strategy_spec_fingerprint
from app.services.strategy_template_net_execution import FrozenNetScenarioContext, evaluate_frozen_net_scenario
from app.services.strategy_template_tracking_metrics import (
    StrategyTrackingPosition, StrategyTrackingSession, validate_strategy_tracking_inputs,
)
from app.services.trading_calendar import (
    ASHARE_TIMEZONE, TradingCalendarCoverageError, is_trading_day, latest_expected_daily_kline_date, next_trade_dates,
)
from app.utils.clock import utc_now


NET_RETURN_SCHEMA_VERSION = "strategy-template-net-returns-v1"
_STRESS_RULE = "commission-rate-and-minimum-times-1.5;buy-and-sell-slippage-plus-10bps;tax-and-transfer-unchanged"


def evaluate_strategy_template_net_returns(
    sessions: Sequence[StrategyTrackingSession], templates: Sequence[Mapping[str, object]], *, as_of: datetime,
    horizon: int, notional_cash_cny: float,
    official_sessions: Sequence[VerifiedOfficialExecutionSession] = (),
) -> dict[str, object]:
    """Consume frozen selections and strict-loader tokens without fetching data."""
    completed = _validate_inputs(sessions, templates, as_of, horizon, notional_cash_cny)
    specs, cost_specs, profiles = _template_costs(templates)
    market = admit_research_market_data(official_sessions, ())
    future = _unavailable_artifact_dates(official_sessions, as_of)
    context = FrozenNetScenarioContext(market.rows, future, (), completed, notional_cash_cny,
                                      next(iter(profiles.values()))[0], 0.0, bool(official_sessions))
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for session in sorted(sessions, key=lambda item: (item.signal_date, item.run_id)):
        key = (session.rule_version, session.score_spec_hash)
        grouped[key].append(_session_report(session, horizon, specs, profiles, context))
    manifest = {"artifact_digests": list(market.source_artifact_digests),
                "raw_file_set_digests": sorted(item.raw_file_set_digest for item in official_sessions)}
    return {
        "schema_version": NET_RETURN_SCHEMA_VERSION, "as_of": as_of.isoformat(), "as_of_completed_date": completed,
        "horizon_sessions": horizon, "notional_cash_cny": notional_cash_cny,
        "entry_policy": "D+1-open", "exit_policy": "D+H+1-close", "return_unit": "decimal_fraction",
        "quantity_policy": "frozen-cash-weights;recalculate-lots-at-official-unadjusted-entry-open;no-signal-share-count-reuse",
        "maximum_exact_quantity": 2 ** 53 - 1,
        "continuous_portfolio_simulation": False, "promotion_eligible": False, "provider_calls": 0,
        "execution_evidence": {"provenance_status": market.provenance_status, **manifest,
                               "manifest_digest": sha256_hex(canonical_json_bytes(manifest))},
        "cost_specs": cost_specs,
        "cohorts": [{"rule_version": rule, "score_spec_hash": digest, "sessions": rows}
                    for (rule, digest), rows in sorted(grouped.items())],
        "limitations": ["independent_batches_do_not_share_or_compound_capital", "frozen_target_weights_are_cash_budgets_including_entry_costs",
                        "daily_execution_states_do_not_prove_order_book_fills", "unfilled_entry_is_cash_without_replacement",
                        "unknown_path_or_blocked_exit_invalidates_entire_batch", "close_marks_are_not_intraday_maximum_drawdown",
                        "cost_stress_changes_share_quantities_within_the_same_frozen_budgets"],
    }


def _validate_inputs(
    sessions: Sequence[StrategyTrackingSession], templates: Sequence[Mapping[str, object]],
    as_of: datetime, horizon: int, notional: float,
) -> str:
    validate_strategy_tracking_inputs(sessions, templates, horizon, notional)
    if len(sessions) > 200 or len(templates) != 3:
        raise ValueError("net comparison exceeds the fixed bounded experiment")
    if as_of.tzinfo is None or as_of.utcoffset() is None or as_of > utc_now():
        raise ValueError("net comparison observation requires a nonfuture timezone-aware time")
    if Decimal(str(notional)) != Decimal(str(notional)).quantize(Decimal("0.01")):
        raise ValueError("net comparison notional must be an exact cent amount")
    completed = latest_expected_daily_kline_date(as_of, allow_auto_refresh=False).isoformat()
    for session in sessions:
        _validate_frozen_session(session, as_of, completed)
    return completed


def _validate_frozen_session(session: StrategyTrackingSession, as_of: datetime, completed: str) -> None:
    if isinstance(session.run_id, bool) or not isinstance(session.run_id, int) or session.run_id <= 0:
        raise ValueError("invalid frozen run identity")
    signal = date.fromisoformat(session.signal_date)
    if signal.isoformat() != session.signal_date or session.signal_date > completed or not is_trading_day(signal, allow_auto_refresh=False):
        raise ValueError("signal is not a completed canonical date")
    published = datetime.fromisoformat(session.published_at)
    if published.tzinfo is None or published.utcoffset() is None or published > as_of:
        raise ValueError("publication must have been observed by report time")
    next_session = next_trade_dates(signal, 1, allow_auto_refresh=False)[0]
    if not datetime.combine(signal, time(15), ASHARE_TIMEZONE) <= published < datetime.combine(next_session, time(9, 30), ASHARE_TIMEZONE):
        raise ValueError("frozen selection was not published before the next opening")
    for selection in session.selections:
        _validate_frozen_budget(selection.status, selection.positions)


def _validate_frozen_budget(status: str, positions: Sequence[StrategyTrackingPosition]) -> None:
    if status == "no_trade" and positions:
        raise ValueError("no-trade selection cannot contain target positions")
    if sum(Decimal(str(item.target_weight)) for item in positions) > 1:
        raise ValueError("target budgets cannot exceed capital")
    if any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item.symbol) for item in positions):
        raise ValueError("invalid frozen symbol identity")


def _template_costs(
    templates: Sequence[Mapping[str, object]],
) -> tuple[dict[str, StrategySpecInput], list[dict[str, object]], dict[str, tuple[PaperCostProfile, PaperCostProfile]]]:
    specs, profiles = {}, {}
    metadata = []
    for item in templates:
        template = MarketStrategyTemplate.model_validate({key: value for key, value in item.items() if key != "strategy_fingerprint"})
        if template.strategy_spec is None or template.availability != "available_for_draft":
            raise ValueError("net comparison requires loadable frozen template specifications")
        spec = template.strategy_spec
        fingerprint = strategy_spec_fingerprint(spec)
        if item.get("strategy_fingerprint", fingerprint) != fingerprint:
            raise ValueError("template strategy fingerprint mismatch")
        base, stress = _cost_profiles(spec.execution_policy)
        specs[template.template_id], profiles[template.template_id] = spec, (base, stress)
        metadata.append({"template_id": template.template_id, "strategy_fingerprint": fingerprint,
                         "execution_policy": spec.execution_policy.model_dump(mode="json"),
                         "base": base.model_dump(mode="json"), "stress": stress.model_dump(mode="json"), "stress_rule": _STRESS_RULE})
    return specs, metadata, profiles


def _cost_profiles(policy: StrategyExecutionPolicy) -> tuple[PaperCostProfile, PaperCostProfile]:
    overrides = PaperCostOverrides(
        commission_rate_pct=policy.commission_rate * 100, minimum_commission=policy.minimum_commission_cny,
        stamp_duty_sell_pct=policy.sell_stamp_duty_rate * 100, transfer_fee_pct=policy.transfer_fee_rate * 100,
        slippage_buy_pct=policy.buy_slippage_bps / 100, slippage_sell_pct=policy.sell_slippage_bps / 100,
    )
    base = resolve_cost_profile(policy.cost_profile, overrides)
    payload = base.model_dump(mode="json")
    payload.update(commission_rate_pct=base.commission_rate_pct * 1.5, minimum_commission=base.minimum_commission * 1.5,
                   slippage_buy_pct=base.slippage_buy_pct + .1, slippage_sell_pct=base.slippage_sell_pct + .1,
                   name="冻结模板成本压力", note=_STRESS_RULE)
    payload["profile_id"] = "template-stress-" + sha256_hex(canonical_json_bytes(payload))[:12]
    return base, PaperCostProfile.model_validate(payload)


def _unavailable_artifact_dates(sessions: Sequence[VerifiedOfficialExecutionSession], as_of: datetime) -> frozenset[str]:
    unavailable = set()
    for session in sessions:
        artifact = session.artifact
        times = [cast(str, artifact["generated_at"])]
        for receipt in cast(list[dict[str, object]], artifact["receipts"]):
            times.extend(cast(str, receipt[key]) for key in ("available_at", "acquired_at"))
        times.extend(cast(str, row["observed_at"]) for row in cast(list[dict[str, object]], artifact["rows"]))
        if any(datetime.fromisoformat(value) > as_of for value in times):
            unavailable.add(session.session_date)
    return frozenset(unavailable)


def _session_report(
    session: StrategyTrackingSession, horizon: int, specs: Mapping[str, StrategySpecInput],
    profiles: Mapping[str, tuple[PaperCostProfile, PaperCostProfile]], context: FrozenNetScenarioContext,
) -> dict[str, object]:
    try:
        days = (session.signal_date, *(day.isoformat() for day in next_trade_dates(
            date.fromisoformat(session.signal_date), horizon + 1, allow_auto_refresh=False)))
    except TradingCalendarCoverageError:
        days = ()
    rows = []
    for selection in session.selections:
        base, stress = profiles[selection.template_id]
        capacity = specs[selection.template_id].portfolio_constraints.max_notional_share_of_daily_amount
        settings = replace(context, days=days, profile=base, max_participation=capacity)
        regular = evaluate_frozen_net_scenario(selection, settings)
        stressed = evaluate_frozen_net_scenario(selection, replace(settings, profile=stress))
        reasons = sorted(set(cast(dict[str, int], regular["reason_counts"])) | set(cast(dict[str, int], stressed["reason_counts"])))
        rows.append({"template_id": selection.template_id, "status": _paired_status(regular, stressed),
                     "reason_codes": reasons, "net_return": regular["net_return"], "stress_net_return": stressed["net_return"],
                     "base": regular, "stress": stressed})
    return {"run_id": session.run_id, "signal_date": session.signal_date, "entry_date": days[1] if days else None,
            "exit_date": days[-1] if days else None, "snapshot_digest": session.snapshot_digest, "selections": rows}


def _paired_status(base: Mapping[str, object], stress: Mapping[str, object]) -> str:
    for status in ("blocked", "pending", "unavailable"):
        if status in {base["status"], stress["status"]}:
            return status
    return "available"
