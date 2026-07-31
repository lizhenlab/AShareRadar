"""Versioned and inspectable paper-trading cost assumptions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from app.models.paper_trading import (
    CostProfileName,
    PaperCostOverrides,
    PaperCostProfile,
)


PAPER_COST_PROFILE_VERSION = "cn-a-share-cost-model.v2"
STAMP_DUTY_EFFECTIVE_FROM = "2023-08-28"
STAMP_DUTY_SOURCE_URL = "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html"
MODEL_ASSUMPTION_SOURCE_URL = "local://ashare-radar/paper-trading-cost-assumptions"

_PROFILE_VALUES: dict[CostProfileName, dict[str, float | str]] = {
    "base": {
        "name": "基础成本",
        "commission_rate_pct": 0.025,
        "minimum_commission": 5.0,
        "stamp_duty_sell_pct": 0.05,
        "transfer_fee_pct": 0.001,
        "slippage_buy_pct": 0.02,
        "slippage_sell_pct": 0.02,
    },
    "conservative": {
        "name": "保守成本",
        "commission_rate_pct": 0.03,
        "minimum_commission": 5.0,
        "stamp_duty_sell_pct": 0.05,
        "transfer_fee_pct": 0.001,
        "slippage_buy_pct": 0.05,
        "slippage_sell_pct": 0.05,
    },
    "stress": {
        "name": "压力成本",
        "commission_rate_pct": 0.05,
        "minimum_commission": 5.0,
        "stamp_duty_sell_pct": 0.05,
        "transfer_fee_pct": 0.001,
        "slippage_buy_pct": 0.15,
        "slippage_sell_pct": 0.15,
    },
}


@dataclass(frozen=True)
class PaperTradeCosts:
    commission: float
    stamp_duty: float
    transfer_fee: float
    slippage: float

    @property
    def total(self) -> float:
        return round(self.commission + self.stamp_duty + self.transfer_fee + self.slippage, 2)


def available_cost_profiles() -> list[PaperCostProfile]:
    return [resolve_cost_profile(name) for name in ("base", "conservative", "stress")]


def resolve_cost_profile(
    name: CostProfileName,
    overrides: PaperCostOverrides | None = None,
) -> PaperCostProfile:
    values = dict(_PROFILE_VALUES[name])
    if overrides is not None:
        for field, value in overrides.model_dump().items():
            if value is not None:
                values[field] = float(value)
    fingerprint_payload = {"base": name, "version": PAPER_COST_PROFILE_VERSION, **values}
    digest = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return PaperCostProfile(
        profile_id=f"{name}-{digest}",
        name=str(values["name"]) if overrides is None else f"{values['name']}（自定义）",
        version=PAPER_COST_PROFILE_VERSION,
        effective_from=STAMP_DUTY_EFFECTIVE_FROM,
        commission_rate_pct=float(values["commission_rate_pct"]),
        minimum_commission=float(values["minimum_commission"]),
        stamp_duty_sell_pct=float(values["stamp_duty_sell_pct"]),
        transfer_fee_pct=float(values["transfer_fee_pct"]),
        slippage_buy_pct=float(values["slippage_buy_pct"]),
        slippage_sell_pct=float(values["slippage_sell_pct"]),
        source_urls=[STAMP_DUTY_SOURCE_URL, MODEL_ASSUMPTION_SOURCE_URL],
        note=(
            "印花税按公开政策生效日建模；佣金、最低佣金、过户费与滑点是可配置研究假设，"
            "不代表任何用户的真实券商费率。"
        ),
    )


def trade_costs(
    profile: PaperCostProfile,
    *,
    side: str,
    gross_amount: float,
) -> PaperTradeCosts:
    if gross_amount <= 0:
        return PaperTradeCosts(0, 0, 0, 0)
    commission = max(profile.minimum_commission, gross_amount * profile.commission_rate_pct / 100)
    stamp_duty = gross_amount * profile.stamp_duty_sell_pct / 100 if side == "sell" else 0
    transfer_fee = gross_amount * profile.transfer_fee_pct / 100
    slippage_pct = profile.slippage_buy_pct if side == "buy" else profile.slippage_sell_pct
    slippage = gross_amount * slippage_pct / 100
    return PaperTradeCosts(
        commission=round(commission, 2),
        stamp_duty=round(stamp_duty, 2),
        transfer_fee=round(transfer_fee, 2),
        slippage=round(slippage, 2),
    )


__all__ = [
    "PAPER_COST_PROFILE_VERSION",
    "PaperTradeCosts",
    "available_cost_profiles",
    "resolve_cost_profile",
    "trade_costs",
]
