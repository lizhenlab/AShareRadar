"""Research observations remain separate from executable market evidence."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


FuyaoJobKind = Literal["financials", "valuations", "sectors", "sentiment", "history_full", "history_incremental"]


class FuyaoObservation(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    id: int = 0
    capability: str
    symbol: str
    fetched_at: str
    digest: str
    payload: dict[str, Any]
    source: str = "同花顺扶摇"
    point_in_time: Literal[False] = False


class FuyaoJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: FuyaoJobKind
    symbols: list[str] = Field(default_factory=list, max_length=100)
    index_symbols: list[str] = Field(default_factory=list, max_length=20)
    period: Literal["annual", "quarterly"] = "annual"
    limit: int = Field(default=4, ge=1, le=20)
    report: str | None = Field(default=None, pattern=r"^20\d{2}-[1-4]$")


class FuyaoJobProgress(BaseModel):
    stage: str
    current: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: str | None = None


class FuyaoJob(BaseModel):
    id: str
    kind: FuyaoJobKind
    status: Literal["running", "cancelling", "completed", "degraded", "failed", "cancelled", "interrupted"]
    created_at: str
    finished_at: str | None = None
    completed: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    message: str = "等待开始"
    errors: list[str] = Field(default_factory=list)
    request: FuyaoJobRequest | None = None
    parent_job_id: str | None = None
    completed_symbols: list[str] = Field(default_factory=list)
    updated_at: str | None = None
    progress: FuyaoJobProgress | None = None
