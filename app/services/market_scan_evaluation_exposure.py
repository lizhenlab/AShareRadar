from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
import math
import sqlite3
from statistics import fmean
from typing import Literal, Protocol


@dataclass(frozen=True)
class ExposureItem:
    symbol: str
    rank: int
    board: str
    industry: str
    amount: float
    turnover_rate: float | None


class ExposureSnapshot(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def rule_version(self) -> str: ...

    @property
    def quote_date(self) -> str: ...

    @property
    def exposures(self) -> tuple[ExposureItem, ...]: ...

    @property
    def regime(self) -> str: ...


class ExposureConfig(Protocol):
    @property
    def top_sizes(self) -> tuple[int, ...]: ...


def exposure_audit(
    snapshots: Sequence[ExposureSnapshot],
    config: ExposureConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for snapshot in snapshots:
        universe = snapshot.exposures
        if not universe:
            continue
        for top_n in config.top_sizes:
            selected = tuple(item for item in universe if item.rank <= top_n)
            records.append(
                {
                    "run_id": snapshot.id,
                    "rule_version": snapshot.rule_version,
                    "quote_date": snapshot.quote_date,
                    "top_n": top_n,
                    "sample_count": len(selected),
                    "universe_count": len(universe),
                    "board": group_exposure(selected, universe, "board"),
                    "industry": group_exposure(selected, universe, "industry"),
                    "liquidity": group_exposure(selected, universe, "liquidity"),
                    "average_amount": mean_optional(item.amount for item in selected),
                    "universe_average_amount": mean_optional(item.amount for item in universe),
                    "average_turnover_rate": mean_optional(item.turnover_rate for item in selected),
                    "universe_average_turnover_rate": mean_optional(
                        item.turnover_rate for item in universe
                    ),
                    "taxonomy_quality": industry_taxonomy_quality(universe),
                    "policy": "audit-only-no-naive-sector-quota",
                }
            )
    return records


def regime_overlay(snapshots: Sequence[ExposureSnapshot]) -> list[dict[str, object]]:
    policy = {
        "strong": (1.0, 45),
        "neutral": (0.8, 50),
        "weak": (0.5, 60),
        "unknown": (0.5, 60),
    }
    return [
        {
            "run_id": snapshot.id,
            "quote_date": snapshot.quote_date,
            "regime": snapshot.regime,
            "position_size_multiplier": policy.get(snapshot.regime, policy["unknown"])[0],
            "minimum_balanced_utility": policy.get(snapshot.regime, policy["unknown"])[1],
            "role": "admission-and-position-sizing-only-does-not-change-alpha-rank",
        }
        for snapshot in snapshots
    ]


def group_exposure(
    selected: Sequence[ExposureItem],
    universe: Sequence[ExposureItem],
    dimension: Literal["board", "industry", "liquidity"],
) -> list[dict[str, object]]:
    def label(item: ExposureItem) -> str:
        if dimension == "board":
            return item.board
        if dimension == "industry":
            return item.industry
        return liquidity_bucket(item.amount)

    selected_counts = Counter(label(item) for item in selected)
    universe_counts = Counter(label(item) for item in universe)
    records: list[dict[str, object]] = []
    for value in sorted(universe_counts):
        selected_share = selected_counts[value] / len(selected) if selected else 0.0
        universe_share = universe_counts[value] / len(universe) if universe else 0.0
        difference = selected_share - universe_share
        records.append(
            {
                "value": value,
                "selected_count": selected_counts[value],
                "universe_count": universe_counts[value],
                "selected_share": selected_share,
                "universe_share": universe_share,
                "share_difference": difference,
                "representation_ratio": selected_share / universe_share if universe_share > 0 else None,
                "alert": abs(difference) >= 0.05,
            }
        )
    return records


def exposure_item(row: sqlite3.Row, *, rank: int | None = None) -> ExposureItem:
    symbol = str(row["symbol"])
    return ExposureItem(
        symbol=symbol,
        rank=rank if rank is not None else int(row["rank"]),
        board=board(symbol, str(row["market"])),
        industry=normalize_industry(row["industry"]),
        amount=float(row["amount"] or 0),
        turnover_rate=float(row["turnover_rate"]) if row["turnover_rate"] is not None else None,
    )


def normalize_industry(value: object) -> str:
    normalized = "".join(str(value or "").split()).strip("-/、，")
    if not normalized:
        return "UNKNOWN"
    aliases = {
        "信息传输、软件和信息技术服务业": "信息技术",
        "软件和信息技术服务业": "信息技术",
        "信息传输软件和信息技术服务业": "信息技术",
    }
    return aliases.get(normalized, normalized)


def industry_taxonomy_quality(rows: Sequence[ExposureItem]) -> dict[str, object]:
    industries = [item.industry for item in rows]
    broad = {"制造业", "信息技术", "金融业", "建筑业", "采矿业", "房地产业", "UNKNOWN"}
    broad_count = sum(value in broad for value in industries)
    return {
        "unknown_count": sum(value == "UNKNOWN" for value in industries),
        "broad_category_count": broad_count,
        "mixed_granularity": 0 < broad_count < len(industries),
        "neutralization_ready": broad_count == 0 and all(value != "UNKNOWN" for value in industries),
    }


def mean_optional(values: Iterable[float | None]) -> float | None:
    materialized = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return fmean(materialized) if materialized else None


def market_regime(rows: Sequence[sqlite3.Row]) -> str:
    changes = [float(row["change_pct"]) for row in rows if row["change_pct"] is not None]
    average = fmean(changes) if changes else 0.0
    if average >= 1:
        return "strong"
    if average <= -1:
        return "weak"
    return "neutral"


def quality_bucket(value: object) -> str:
    if value is None:
        return "unknown"
    score = int(str(value))
    if score >= 90:
        return "high"
    if score >= 80:
        return "medium"
    return "low"


def liquidity_bucket(value: object) -> str:
    try:
        amount = float(str(value))
    except (TypeError, ValueError):
        return "low"
    if amount >= 1_000_000_000:
        return "high"
    if amount >= 100_000_000:
        return "medium"
    return "low"


def scan_time_bucket(value: object, mode: str) -> str:
    text = str(value or "").replace("T", " ").replace("Z", "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return "unknown"
    if mode == "preopen":
        return "preopen"
    if mode == "official" or parsed.time() >= datetime.strptime("15:15", "%H:%M").time():
        return "after_close"
    if parsed.time() < datetime.strptime("11:30", "%H:%M").time():
        return "morning"
    return "afternoon"


def board(symbol: str, market: str) -> str:
    code = symbol.split(".", 1)[0]
    if market == "BJ":
        return "BSE"
    if market == "SH" and code.startswith(("688", "689")):
        return "STAR"
    if market == "SZ" and code.startswith(("300", "301")):
        return "CHINEXT"
    return f"{market}_MAIN"


__all__ = [
    "ExposureConfig",
    "ExposureItem",
    "ExposureSnapshot",
    "board",
    "exposure_audit",
    "exposure_item",
    "group_exposure",
    "industry_taxonomy_quality",
    "liquidity_bucket",
    "market_regime",
    "mean_optional",
    "normalize_industry",
    "quality_bucket",
    "regime_overlay",
    "scan_time_bucket",
]
