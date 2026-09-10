"""Unadjusted Fuyao research archives, never a production price cache."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FuyaoDumpFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["daily.parquet", "actions.parquet"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    rows: int = Field(ge=0)


class FuyaoDumpManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fuyao-market-dumps-v1"] = "fuyao-market-dumps-v1"
    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["full", "incremental"]
    source: Literal["fuyao"] = "fuyao"
    adjusted: Literal["none"] = "none"
    point_in_time_verified: Literal[False] = False
    production_cache_updated: Literal[False] = False
    observed_at: str
    previous_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    first_date: str
    last_date: str
    symbols: int = Field(gt=0)
    trading_dates: list[str]
    files: list[FuyaoDumpFile]
    source_sha256: dict[str, str]
    revised_daily_rows: int = Field(default=0, ge=0)
    duplicate_daily_rows: int = Field(default=0, ge=0)
    calendar_source: str
    notes: list[str] = Field(default_factory=list)
