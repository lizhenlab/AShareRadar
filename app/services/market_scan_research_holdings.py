"""Pure actual-holdings exposure audit, separate from account performance."""

from __future__ import annotations

from app.artifacts.io import sha256_hex
from app.services.market_scan_research_holdings_contracts import (
    HoldingsClassification, admit_classifications, finite_json_bytes, index_classifications, select_classification,
)
from app.services.market_scan_research_holdings_validation import admit_holdings_portfolio
from app.services.market_scan_research_portfolio_models import ResearchPortfolioDay, ResearchPortfolioPosition


HOLDINGS_AUDIT_VERSION = "market-scan-research-actual-holdings-audit-v1"
_DIMENSIONS = ("industry", "market", "board")


def audit_research_holdings_payload(
    portfolio_payload: object, classification_payload: object, *,
    expected_portfolio_digest: str, expected_classification_digest: str,
) -> dict[str, object]:
    """Validate ledger digest, accounting and PIT sidecar before aggregating."""
    result = admit_holdings_portfolio(portfolio_payload, expected_digest=expected_portfolio_digest)
    evidence = admit_classifications(
        classification_payload, expected_digest=expected_classification_digest,
        portfolio_input_digest=result.input_digest, portfolio_result_digest=result.result_digest,
    )
    indexed = index_classifications(evidence)
    report: dict[str, object] = {
        "schema_version": HOLDINGS_AUDIT_VERSION, "promotion_eligible": False,
        "portfolio_input_digest": result.input_digest, "portfolio_result_digest": result.result_digest,
        "classification_digest": evidence.classification_digest,
        "provenance": {"portfolio_source_status": result.provenance_status,
                       "portfolio_authority": "digest_bound_not_independently_replayed",
                       "classification_authority": "self_asserted_pit_metadata", "official_pit_verified": False},
        "policy": {"valuation": "existing_end_of_day_actual_positions", "classification_cutoff": "15:00:00 Asia/Shanghai",
                   "exposure_denominator": "complete_account_NAV", "hhi_denominator": "complete_equity_market_value",
                   "unknown_classifications": "separate_null_bucket;no_future_backfill", "allocation_changed": False},
        "days": [_audit_day(day, indexed) for day in result.days],
        "limitations": ["self-reported hashes bind bytes, not official PIT provenance",
                        "unknown industry bucket does not prove common economic exposure",
                        "missing account valuation leaves weights and concentration unavailable",
                        "descriptive exposure audit; no risk constraint or promotion authorization"],
    }
    report["audit_digest"] = sha256_hex(finite_json_bytes(report))
    return report


def _audit_day(day: ResearchPortfolioDay, indexed: dict[str, tuple[HoldingsClassification, ...]]) -> dict[str, object]:
    rows = [(position, select_classification(indexed.get(position.symbol, ()), position.symbol, day.session_date)) for position in day.positions]
    valued = sum(position.market_value is not None for position in day.positions)
    known_value = round(sum(position.market_value for position in day.positions if position.market_value is not None), 2)
    complete = day.nav is not None
    exposures = {dimension: _exposures(rows, dimension, day.nav) for dimension in _DIMENSIONS}
    return {
        "session_date": day.session_date, "cash": day.cash, "market_value": day.market_value, "nav": day.nav,
        "cash_weight": _weight(day.cash, day.nav), "known_market_value": known_value,
        "position_count": len(rows), "unvalued_position_count": len(rows) - valued,
        "account_valuation_complete": complete,
        "position_valuation_coverage": valued / len(rows) if rows else 1.0 if complete else None,
        "unresolved_reasons": list(day.unresolved_reasons),
        "classification_coverage": {dimension: _coverage(rows, dimension, day.market_value) for dimension in _DIMENSIONS},
        "positions": [_position_payload(position, classification, day) for position, classification in rows],
        "exposures": exposures, "concentration": _concentration(rows, day, exposures["industry"]),
    }


def _label(row: HoldingsClassification | None, dimension: str) -> str | None:
    if row is None:
        return None
    return {"industry": row.industry, "market": row.market, "board": row.board}[dimension]


def _weight(value: float, denominator: float | None) -> float | None:
    return value / denominator if denominator is not None and denominator > 0 else None


def _position_payload(
    position: ResearchPortfolioPosition, classification: HoldingsClassification | None, day: ResearchPortfolioDay,
) -> dict[str, object]:
    return {
        "symbol": position.symbol, "quantity": position.quantity, "entry_date": position.entry_date,
        "target_exit_date": position.target_exit_date, "exit_overdue": day.session_date >= position.target_exit_date,
        "market_value": position.market_value, "nav_weight": _weight(position.market_value, day.nav) if position.market_value is not None else None,
        "unresolved_reason": position.unresolved_reason,
        "classification_row_digest": classification.row_digest if classification else None,
        "classification": classification.model_dump(mode="json") if classification else None,
    }


def _exposures(
    rows: list[tuple[ResearchPortfolioPosition, HoldingsClassification | None]], dimension: str, nav: float | None,
) -> list[dict[str, object]]:
    groups: dict[str | None, list[ResearchPortfolioPosition]] = {}
    for position, classification in rows:
        groups.setdefault(_label(classification, dimension), []).append(position)
    result: list[dict[str, object]] = []
    for label in sorted(groups, key=lambda item: (item is None, item or "")):
        positions = groups[label]
        known = round(sum(item.market_value for item in positions if item.market_value is not None), 2)
        unvalued = sum(item.market_value is None for item in positions)
        result.append({"classification": label, "position_count": len(positions), "known_market_value": known,
                       "market_value": None if unvalued else known, "unvalued_position_count": unvalued,
                       "nav_weight": None if unvalued else _weight(known, nav)})
    return result


def _coverage(
    rows: list[tuple[ResearchPortfolioPosition, HoldingsClassification | None]], dimension: str, market_value: float | None,
) -> dict[str, object]:
    classified = [position for position, classification in rows if _label(classification, dimension) is not None]
    known = sum(position.market_value for position in classified if position.market_value is not None)
    return {
        "classified_position_count": len(classified), "unknown_position_count": len(rows) - len(classified),
        "position_coverage": len(classified) / len(rows) if rows else None,
        "market_value_coverage": _weight(known, market_value),
    }


def _concentration(
    rows: list[tuple[ResearchPortfolioPosition, HoldingsClassification | None]], day: ResearchPortfolioDay,
    industries: list[dict[str, object]],
) -> dict[str, float | None]:
    names = ("symbol_hhi_equity", "industry_hhi_equity", "industry_bucket_hhi_equity",
             "maximum_symbol_nav_weight", "maximum_industry_nav_weight")
    if day.nav is None or day.market_value is None:
        return dict.fromkeys(names)
    if not rows:
        return dict.fromkeys(names, 0.0)
    values = [position.market_value for position, _ in rows if position.market_value is not None]
    totals = _industry_totals(industries)
    gross = day.market_value
    classified = all(_label(classification, "industry") is not None for _, classification in rows)
    hhi = sum((value / gross) ** 2 for value in totals) if gross > 0 else None
    return {
        "symbol_hhi_equity": sum((value / gross) ** 2 for value in values) if gross > 0 else None,
        "industry_hhi_equity": hhi if classified else None, "industry_bucket_hhi_equity": hhi,
        "maximum_symbol_nav_weight": _weight(max(values), day.nav),
        "maximum_industry_nav_weight": _weight(max(totals), day.nav) if classified else None,
    }


def _industry_totals(industries: list[dict[str, object]]) -> list[float]:
    grouped = [item["market_value"] for item in industries]
    return [float(value) for value in grouped if isinstance(value, (int, float))]
