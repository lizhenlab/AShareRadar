"""Current-watchlist overlay; never part of a frozen publication or saved rule."""
from __future__ import annotations

from typing import Literal, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MarketScanSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    universe: Literal["all", "watchlist"] = "all"
    watchlist_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.universe == "all" and self.watchlist_digest is not None:
            raise ValueError("全市场范围不能绑定自选成员摘要")
        return self


class MarketScanSelectionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    universe: Literal["watchlist"] = "watchlist"
    watchlist_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_count: int = Field(ge=0)
