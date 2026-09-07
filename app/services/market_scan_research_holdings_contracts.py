"""Digest-bound, self-asserted PIT classifications; never official authority."""

from __future__ import annotations

from datetime import date, datetime, time
import re
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex


HOLDINGS_CLASSIFICATION_VERSION = "market-scan-research-holdings-classifications-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def require_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid sha256 digest")
    return value


def require_date(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("date must be canonical ISO YYYY-MM-DD")
    return value


def classification_time(value: str) -> datetime:
    if "T" not in value:
        raise ValueError("as_of must contain a timezone-aware ISO timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return parsed.astimezone(SHANGHAI)


def session_close(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(require_date(value)), time(15), SHANGHAI)


def finite_json_bytes(value: object) -> bytes:
    try:
        return canonical_json_bytes(value)
    except ArtifactIOError as error:
        raise ValueError("input must be finite canonical JSON") from error


class HoldingsClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    symbol: str
    as_of: str
    effective_from: str
    effective_through: str
    source_id: str
    classification_version: str
    industry: str | None
    market: Literal["SH", "SZ", "BJ"] | None
    board: str | None
    row_digest: str

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value) is None:
            raise ValueError("invalid classification symbol")
        return value

    @field_validator("source_id", "classification_version", "industry", "board")
    @classmethod
    def nonempty_label(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("classification labels must be nonempty and trimmed")
        return value

    @model_validator(mode="after")
    def valid_identity(self) -> Self:
        classification_time(self.as_of)
        require_date(self.effective_from)
        require_date(self.effective_through)
        if self.effective_from > self.effective_through:
            raise ValueError("invalid classification effective interval")
        if self.market is not None and self.market != self.symbol[-2:]:
            raise ValueError("classification market conflicts with symbol")
        expected = sha256_hex(finite_json_bytes(self.model_dump(exclude={"row_digest"})))
        if require_digest(self.row_digest) != expected:
            raise ValueError("classification row digest mismatch")
        return self


class HoldingsClassifications(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["market-scan-research-holdings-classifications-v1"]
    portfolio_input_digest: str
    portfolio_result_digest: str
    rows: tuple[HoldingsClassification, ...]
    classification_digest: str

    @model_validator(mode="after")
    def valid_package(self) -> Self:
        require_digest(self.portfolio_input_digest)
        require_digest(self.portfolio_result_digest)
        expected = sha256_hex(finite_json_bytes(self.model_dump(mode="json", exclude={"classification_digest"})))
        if require_digest(self.classification_digest) != expected:
            raise ValueError("classification package digest mismatch")
        by_identity: dict[tuple[str, datetime], list[HoldingsClassification]] = {}
        for row in self.rows:
            key = row.symbol, classification_time(row.as_of)
            by_identity.setdefault(key, []).append(row)
        for rows in by_identity.values():
            ordered = sorted(rows, key=lambda row: row.effective_from)
            if any(left.effective_through >= right.effective_from for left, right in zip(ordered, ordered[1:], strict=False)):
                raise ValueError("ambiguous or duplicate classification interval at the same as_of")
        return self


def admit_classifications(
    payload: object, *, expected_digest: str, portfolio_input_digest: str, portfolio_result_digest: str,
) -> HoldingsClassifications:
    result = HoldingsClassifications.model_validate_json(finite_json_bytes(payload), strict=True)
    if result.classification_digest != require_digest(expected_digest):
        raise ValueError("classification pinned digest mismatch")
    if (result.portfolio_input_digest, result.portfolio_result_digest) != (portfolio_input_digest, portfolio_result_digest):
        raise ValueError("classification portfolio binding mismatch")
    return result


def select_classification(
    rows: tuple[HoldingsClassification, ...], symbol: str, session_date: str,
) -> HoldingsClassification | None:
    cutoff = session_close(session_date)
    eligible = [row for row in rows if row.symbol == symbol
                and row.effective_from <= session_date <= row.effective_through
                and classification_time(row.as_of) <= cutoff]
    return max(eligible, key=lambda row: classification_time(row.as_of)) if eligible else None


def index_classifications(evidence: HoldingsClassifications) -> dict[str, tuple[HoldingsClassification, ...]]:
    grouped: dict[str, list[HoldingsClassification]] = {}
    for row in evidence.rows:
        grouped.setdefault(row.symbol, []).append(row)
    return {symbol: tuple(rows) for symbol, rows in grouped.items()}
