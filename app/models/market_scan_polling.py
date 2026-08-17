"""Non-authorizing identities for lightweight market-scan UI polling."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.market_scan import MarketScanMode


MARKET_SCAN_POLLING_IDENTITY_SCHEMA_VERSION: Final[
    Literal["market-scan-polling-identity-v1"]
] = "market-scan-polling-identity-v1"
MARKET_SCAN_POLLING_IDENTITY_AUTHORIZATION: Final[
    Literal["change_detection_only"]
] = "change_detection_only"


class MarketScanPollingRunToken(BaseModel):
    """Opaque change token plus a diagnostic id that never selects a trusted run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: int | None = Field(default=None, ge=1)
    token: str = Field(pattern=r"^[0-9a-f]{64}$")


class MarketScanPollingIdentity(BaseModel):
    """Cheap request identity that can never authorize results or actions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["market-scan-polling-identity-v1"] = (
        MARKET_SCAN_POLLING_IDENTITY_SCHEMA_VERSION
    )
    authorization: Literal["change_detection_only"] = (
        MARKET_SCAN_POLLING_IDENTITY_AUTHORIZATION
    )
    request_mode: MarketScanMode
    latest: MarketScanPollingRunToken
    latest_published: MarketScanPollingRunToken
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def reject_duplicate_run_with_distinct_tokens(self) -> "MarketScanPollingIdentity":
        latest_id = self.latest.run_id
        published_id = self.latest_published.run_id
        if latest_id is None and published_id is not None:
            raise ValueError("已发布轮询批次不能脱离全局最近批次")
        if latest_id is not None and published_id is not None and published_id > latest_id:
            raise ValueError("已发布轮询批次不能晚于全局最近批次")
        if latest_id == published_id and self.latest.token != self.latest_published.token:
            raise ValueError("同一批次的轮询 token 必须一致")
        return self


__all__ = [
    "MARKET_SCAN_POLLING_IDENTITY_AUTHORIZATION",
    "MARKET_SCAN_POLLING_IDENTITY_SCHEMA_VERSION",
    "MarketScanPollingIdentity",
    "MarketScanPollingRunToken",
]
