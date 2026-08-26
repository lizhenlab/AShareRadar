"""Pinned, raw-file-backed intake for licensed official execution sessions.

The public market-data providers used by the application are useful operational
inputs, but they are not exchange-authoritative point-in-time evidence.  This
module provides the deliberately separate intake boundary for licensed SSE,
SZSE, BSE, or licensed-redistributor files.

Authority is never inferred from a field inside an uploaded artifact.  A source
must first appear in an operator-pinned registry whose digest is supplied out of
band.  Every session receipt is then checked against that registry and against
the exact bytes of its raw delivery file.  Only the opaque objects returned by
the strict loaders below may be used by downstream joint-execution research.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Literal, Self, cast, overload
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, read_regular_file, sha256_hex


OFFICIAL_EXECUTION_REGISTRY_SCHEMA_VERSION = "official-execution-source-registry-v1"
OFFICIAL_EXECUTION_SESSION_SCHEMA_VERSION = "official-execution-session-artifact-v1"
OFFICIAL_EXECUTION_SESSION_CONTRACT_VERSION = (
    "licensed-official-unadjusted-ohlcv-state-corporate-action-v1"
)
OFFICIAL_EXECUTION_INTEGRITY_NOTICE = (
    "content_address_plus_operator_pinned_registry_and_raw_file_verification"
)
OFFICIAL_EXECUTION_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
OFFICIAL_EXECUTION_MAX_SESSION_BYTES = 256 * 1024 * 1024

ExchangeMarket = Literal["SH", "SZ", "BJ"]
OfficialAuthorityBasis = Literal[
    "exchange_direct_subscription",
    "exchange_licensed_redistributor",
]
OfficialDeliveryChannel = Literal["https", "sftp", "rsync", "private_api"]
OfficialExchangeSessionState = Literal["trading", "suspended", "not_listed", "delisted"]
OfficialExecutionState = Literal[
    "executable",
    "locked_limit",
    "suspended",
    "rule_ineligible",
    "capacity_exceeded",
]

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_VERIFIED_REGISTRY_SEAL = object()
_VERIFIED_SESSION_SEAL = object()
_BOUND_SESSION_SEAL = object()


class OfficialExecutionIntakeError(ValueError):
    """Raised when licensed official evidence cannot be verified exactly."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class OfficialExecutionSourceRegistration(_StrictModel):
    source_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    dataset_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    provider_legal_name: str = Field(min_length=1)
    markets: list[ExchangeMarket] = Field(min_length=1)
    authority_basis: OfficialAuthorityBasis
    delivery_channel: OfficialDeliveryChannel
    source_base_uri: str = Field(min_length=1)
    license_reference: str = Field(min_length=1)
    license_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: str
    valid_through: str | None = None
    research_use_authorized: Literal[True] = True
    registration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_registration(self) -> Self:
        start = _iso_date(self.valid_from, "registration.valid_from")
        end = (
            _iso_date(self.valid_through, "registration.valid_through")
            if self.valid_through is not None
            else None
        )
        if end is not None and end < start:
            raise ValueError("official source license validity is inverted")
        if self.markets != sorted(set(self.markets)):
            raise ValueError("official source markets must be sorted and unique")
        _source_uri(self.source_base_uri, self.delivery_channel, "registration.source_base_uri")
        if self.registration_digest != official_execution_content_digest(self, "registration_digest"):
            raise ValueError("official source registration digest mismatch")
        return self


class OfficialExecutionSourceRegistry(_StrictModel):
    schema_version: Literal["official-execution-source-registry-v1"] = (
        "official-execution-source-registry-v1"
    )
    registered_at: str
    registrations: list[OfficialExecutionSourceRegistration] = Field(min_length=1)
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        _aware_timestamp(self.registered_at, "registry.registered_at")
        identities = [(item.source_id, item.dataset_id) for item in self.registrations]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("official source registrations must be canonical and unique")
        if self.registry_digest != official_execution_content_digest(self, "registry_digest"):
            raise ValueError("official source registry digest mismatch")
        return self


class OfficialExecutionRawFileReceipt(_StrictModel):
    source_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    dataset_id: str = Field(min_length=1)
    market: ExchangeMarket
    session_date: str
    relative_path: str = Field(min_length=1)
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_uri: str = Field(min_length=1)
    available_at: str
    acquired_at: str
    parser_version: str = Field(min_length=1)
    license_reference: str = Field(min_length=1)
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        session = _iso_date(self.session_date, "receipt.session_date")
        available = _aware_timestamp(self.available_at, "receipt.available_at")
        acquired = _aware_timestamp(self.acquired_at, "receipt.acquired_at")
        _relative_path(self.relative_path)
        _any_source_uri(self.source_uri, "receipt.source_uri")
        if acquired < available:
            raise ValueError("official raw file cannot be acquired before it is available")
        local_available = available.astimezone(_SHANGHAI)
        if local_available.date() < session or (
            local_available.date() == session and local_available.time() <= time(15, 0)
        ):
            raise ValueError("official daily file must be available after its session close")
        if self.receipt_digest != official_execution_content_digest(self, "receipt_digest"):
            raise ValueError("official raw file receipt digest mismatch")
        return self


class OfficialExecutionInstrumentRules(_StrictModel):
    effective_date: str
    board: Literal["main", "chinext", "star", "beijing", "other"]
    is_st: bool
    listing_status: Literal["listed", "delisting_period", "not_listed", "delisted"]
    board_rule_id: str = Field(min_length=1)
    st_rule_id: str = Field(min_length=1)
    delisting_rule_id: str = Field(min_length=1)
    minimum_buy_quantity: int = Field(gt=0)
    buy_quantity_step: int = Field(gt=0)
    sell_quantity_step: int = Field(gt=0)
    price_limit_pct: float = Field(gt=0, le=1)
    ruleset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        _iso_date(self.effective_date, "rules.effective_date")
        if self.ruleset_digest != official_execution_content_digest(
            self,
            "ruleset_digest",
        ):
            raise ValueError("official instrument ruleset digest mismatch")
        return self


class OfficialExecutionCorporateActionReference(_StrictModel):
    status: Literal["none", "effective_event"]
    event_id: str | None = None
    previous_close: float = Field(gt=0)
    reference_price: float = Field(gt=0)
    reference_price_rule_id: str = Field(min_length=1)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if (self.status == "effective_event") is not bool(self.event_id):
            raise ValueError("corporate-action event identity is inconsistent")
        if self.evidence_digest != official_execution_content_digest(self, "evidence_digest"):
            raise ValueError("corporate-action reference digest mismatch")
        return self


class OfficialExecutionDailyBar(_StrictModel):
    adjustment_mode: Literal["none"] = "none"
    open: float | None = Field(default=None, gt=0)
    high: float | None = Field(default=None, gt=0)
    low: float | None = Field(default=None, gt=0)
    close: float | None = Field(default=None, gt=0)
    volume: float | None = Field(default=None, ge=0)
    amount: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_prices(self) -> Self:
        prices = (self.open, self.high, self.low, self.close)
        if all(value is None for value in (*prices, self.volume, self.amount)):
            return self
        if any(value is None for value in (*prices, self.volume, self.amount)):
            raise ValueError("official daily bar must be wholly present or wholly absent")
        open_price, high, low, close = cast(tuple[float, float, float, float], prices)
        if low > high or not all(low <= value <= high for value in (open_price, close)):
            raise ValueError("official daily bar OHLC bounds are inconsistent")
        return self


class OfficialExecutionSessionRow(_StrictModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    code: str = Field(pattern=r"^\d{6}$")
    market: ExchangeMarket
    session_date: str
    observed_at: str
    source_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_record_id: str = Field(min_length=1)
    exchange_session_state: OfficialExchangeSessionState
    entry_execution_state: OfficialExecutionState
    entry_reason_code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    exit_execution_state: OfficialExecutionState
    exit_reason_code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    instrument_rules: OfficialExecutionInstrumentRules
    corporate_action: OfficialExecutionCorporateActionReference
    bar: OfficialExecutionDailyBar
    trading_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_row(self) -> Self:
        session = _iso_date(self.session_date, "row.session_date")
        observed = _aware_timestamp(self.observed_at, "row.observed_at").astimezone(_SHANGHAI)
        if self.symbol != f"{self.code}.{self.market}":
            raise ValueError("official execution symbol/code/market mismatch")
        if self.instrument_rules.effective_date != self.session_date:
            raise ValueError("official execution rules are not effective on session date")
        if observed.date() < session or (
            observed.date() == session and observed.time() <= time(15, 0)
        ):
            raise ValueError("official session row must be observed after close")
        _validate_execution_state_shape(self)
        if self.trading_state_digest != _trading_state_digest(self):
            raise ValueError("official trading-state digest mismatch")
        if self.row_digest != official_execution_content_digest(self, "row_digest"):
            raise ValueError("official session row digest mismatch")
        return self


class OfficialExecutionSessionArtifact(_StrictModel):
    schema_version: Literal["official-execution-session-artifact-v1"] = (
        "official-execution-session-artifact-v1"
    )
    contract_version: Literal[
        "licensed-official-unadjusted-ohlcv-state-corporate-action-v1"
    ] = "licensed-official-unadjusted-ohlcv-state-corporate-action-v1"
    session_date: str
    generated_at: str
    source_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipts: list[OfficialExecutionRawFileReceipt] = Field(min_length=1)
    expected_market_counts: dict[ExchangeMarket, int]
    expected_row_count: int = Field(gt=0)
    rows: list[OfficialExecutionSessionRow] = Field(min_length=1)
    receipt_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    universe_membership_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    integrity_notice: Literal[
        "content_address_plus_operator_pinned_registry_and_raw_file_verification"
    ] = "content_address_plus_operator_pinned_registry_and_raw_file_verification"
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        _iso_date(self.session_date, "artifact.session_date")
        generated = _aware_timestamp(self.generated_at, "artifact.generated_at")
        _validate_artifact_counts(self)
        receipts = _validate_artifact_receipts(self)
        _validate_artifact_row_bindings(self, receipts, generated)
        if self.receipt_set_digest != _digest([item.receipt_digest for item in self.receipts]):
            raise ValueError("official session receipt-set digest mismatch")
        if self.universe_membership_digest != _digest([item.symbol for item in self.rows]):
            raise ValueError("official session universe membership digest mismatch")
        if self.artifact_digest != official_execution_content_digest(self, "artifact_digest"):
            raise ValueError("official session artifact digest mismatch")
        return self


def _validate_artifact_counts(artifact: OfficialExecutionSessionArtifact) -> None:
    if set(artifact.expected_market_counts) != {"SH", "SZ", "BJ"} or any(
        isinstance(value, bool) or value < 0 for value in artifact.expected_market_counts.values()
    ):
        raise ValueError("official session expected market counts are incomplete")
    if sum(artifact.expected_market_counts.values()) != artifact.expected_row_count:
        raise ValueError("official session expected market counts do not conserve")
    symbols = [item.symbol for item in artifact.rows]
    if symbols != sorted(symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("official session rows must use unique canonical symbols")
    if len(artifact.rows) != artifact.expected_row_count:
        raise ValueError("official session row count is incomplete")
    counts = Counter(item.market for item in artifact.rows)
    markets: tuple[ExchangeMarket, ...] = ("SH", "SZ", "BJ")
    if any(counts[market] != artifact.expected_market_counts[market] for market in markets):
        raise ValueError("official session per-market row counts mismatch")


def _validate_artifact_receipts(
    artifact: OfficialExecutionSessionArtifact,
) -> dict[str, OfficialExecutionRawFileReceipt]:
    receipt_digests = [item.receipt_digest for item in artifact.receipts]
    if receipt_digests != sorted(receipt_digests) or len(set(receipt_digests)) != len(
        receipt_digests
    ):
        raise ValueError("official session receipts must be canonical and unique")
    return {item.receipt_digest: item for item in artifact.receipts}


def _validate_artifact_row_bindings(
    artifact: OfficialExecutionSessionArtifact,
    receipts: Mapping[str, OfficialExecutionRawFileReceipt],
    generated: datetime,
) -> None:
    for row in artifact.rows:
        receipt = receipts.get(row.receipt_digest)
        if receipt is None or (
            row.session_date != artifact.session_date
            or receipt.session_date != artifact.session_date
            or row.market != receipt.market
            or row.source_id != receipt.source_id
            or row.dataset_id != receipt.dataset_id
        ):
            raise ValueError("official session row is not bound to its raw receipt")
        if _aware_timestamp(row.observed_at, "row.observed_at") > generated:
            raise ValueError("official session artifact predates a row observation")


class VerifiedOfficialExecutionSourceRegistry(Mapping[str, object]):
    """Opaque registry accepted only after exact out-of-band digest pinning."""

    __slots__ = ("_encoded", "registry_digest")

    def __init__(self, encoded: str, registry_digest: str, *, _seal: object | None = None) -> None:
        if _seal is not _VERIFIED_REGISTRY_SEAL:
            raise TypeError("official source registry token can only be created by the strict loader")
        self._encoded = encoded
        self.registry_digest = registry_digest

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._encoded)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified official registry payload is invalid")
        return cast(dict[str, object], value)

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


class VerifiedOfficialExecutionSession(Sequence[Mapping[str, object]]):
    """Opaque session whose registry and every raw delivery file were verified."""

    __slots__ = ("_encoded", "artifact_digest", "session_date", "raw_file_set_digest")

    def __init__(
        self,
        encoded: str,
        *,
        artifact_digest: str,
        session_date: str,
        raw_file_set_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_SESSION_SEAL:
            raise TypeError("official execution session token can only be created by the strict loader")
        self._encoded = encoded
        self.artifact_digest = artifact_digest
        self.session_date = session_date
        self.raw_file_set_digest = raw_file_set_digest

    @property
    def artifact(self) -> dict[str, object]:
        value = json.loads(self._encoded)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified official session payload is invalid")
        return cast(dict[str, object], value)

    @property
    def rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], item) for item in cast(list[object], self.artifact["rows"])]

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(
        self, index: int | slice
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        return self.rows[index]

    def __len__(self) -> int:
        return len(self.rows)


class BoundOfficialExecutionDecisionSession(Sequence[Mapping[str, object]]):
    """Exact source-decision projection from a verified official session."""

    __slots__ = (
        "_encoded_rows",
        "session_date",
        "source_snapshot_digest",
        "decision_membership_digest",
        "official_session_artifact_digest",
        "official_raw_file_set_digest",
    )

    def __init__(
        self,
        encoded_rows: str,
        *,
        session_date: str,
        source_snapshot_digest: str,
        decision_membership_digest: str,
        official_session_artifact_digest: str,
        official_raw_file_set_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _BOUND_SESSION_SEAL:
            raise TypeError("bound official session can only be created by strict decision binding")
        self._encoded_rows = encoded_rows
        self.session_date = session_date
        self.source_snapshot_digest = source_snapshot_digest
        self.decision_membership_digest = decision_membership_digest
        self.official_session_artifact_digest = official_session_artifact_digest
        self.official_raw_file_set_digest = official_raw_file_set_digest

    @property
    def rows(self) -> list[dict[str, object]]:
        value = json.loads(self._encoded_rows)
        if not isinstance(value, list):  # pragma: no cover - sealed invariant
            raise TypeError("bound official execution rows are invalid")
        return [cast(dict[str, object], item) for item in value]

    def row_by_symbol(self) -> dict[str, Mapping[str, object]]:
        return {str(item["symbol"]): item for item in self.rows}

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(
        self, index: int | slice
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        return self.rows[index]

    def __len__(self) -> int:
        return len(self.rows)


def official_execution_content_digest(
    value: BaseModel | Mapping[str, object],
    digest_field: str,
) -> str:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("official execution evidence must be a model or mapping")
    payload.pop(digest_field, None)
    return sha256_hex(canonical_json_bytes(payload))


def seal_official_execution_source_registration(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Normalize one registration; this does not create authority."""

    payload = deepcopy(dict(value))
    payload.pop("registration_digest", None)
    payload["registration_digest"] = official_execution_content_digest(
        payload, "registration_digest"
    )
    return OfficialExecutionSourceRegistration.model_validate(payload).model_dump(mode="json")


def seal_official_execution_source_registry(
    registrations: Sequence[Mapping[str, object]],
    *,
    registered_at: str,
) -> dict[str, object]:
    """Build a canonical registry candidate for out-of-band operator pinning."""

    normalized = [seal_official_execution_source_registration(item) for item in registrations]
    normalized.sort(key=lambda item: (str(item["source_id"]), str(item["dataset_id"])))
    payload: dict[str, object] = {
        "schema_version": OFFICIAL_EXECUTION_REGISTRY_SCHEMA_VERSION,
        "registered_at": registered_at,
        "registrations": normalized,
    }
    payload["registry_digest"] = official_execution_content_digest(payload, "registry_digest")
    return OfficialExecutionSourceRegistry.model_validate(payload).model_dump(mode="json")


def seal_official_execution_raw_file_receipt(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Normalize one raw-delivery receipt; raw bytes are checked only at load."""

    payload = deepcopy(dict(value))
    payload.pop("receipt_digest", None)
    payload["receipt_digest"] = official_execution_content_digest(payload, "receipt_digest")
    return OfficialExecutionRawFileReceipt.model_validate(payload).model_dump(mode="json")


def seal_official_execution_corporate_action_reference(
    value: Mapping[str, object],
) -> dict[str, object]:
    payload = deepcopy(dict(value))
    payload.pop("evidence_digest", None)
    payload["evidence_digest"] = official_execution_content_digest(payload, "evidence_digest")
    return OfficialExecutionCorporateActionReference.model_validate(payload).model_dump(mode="json")


def seal_official_execution_instrument_rules(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Content-bind the complete effective rule projection for one symbol-day."""

    payload = deepcopy(dict(value))
    payload.pop("ruleset_digest", None)
    payload["ruleset_digest"] = official_execution_content_digest(
        payload,
        "ruleset_digest",
    )
    return OfficialExecutionInstrumentRules.model_validate(payload).model_dump(mode="json")


def seal_official_execution_session_row(value: Mapping[str, object]) -> dict[str, object]:
    """Build the two nested row digests without granting official authority."""

    payload = deepcopy(dict(value))
    rules_payload = seal_official_execution_instrument_rules(
        cast(Mapping[str, object], payload.get("instrument_rules"))
    )
    rules = OfficialExecutionInstrumentRules.model_validate(rules_payload)
    payload["instrument_rules"] = rules_payload
    corporate = cast(Mapping[str, object], payload.get("corporate_action"))
    corporate_payload = seal_official_execution_corporate_action_reference(corporate)
    corporate_model = OfficialExecutionCorporateActionReference.model_validate(corporate_payload)
    payload["corporate_action"] = corporate_payload
    bar = OfficialExecutionDailyBar.model_validate(payload.get("bar"))
    payload["bar"] = bar.model_dump(mode="json")
    shell = OfficialExecutionSessionRow.model_construct(
        **{
            **payload,
            "instrument_rules": rules,
            "corporate_action": corporate_model,
            "bar": bar,
            "trading_state_digest": "0" * 64,
            "row_digest": "0" * 64,
        }
    )
    payload["trading_state_digest"] = _trading_state_digest(shell)
    payload["row_digest"] = official_execution_content_digest(payload, "row_digest")
    return OfficialExecutionSessionRow.model_validate(payload).model_dump(mode="json")


def seal_official_execution_session_artifact(
    *,
    session_date: str,
    generated_at: str,
    source_registry_digest: str,
    receipts: Sequence[Mapping[str, object]],
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build a canonical normalized session candidate for strict raw-file load."""

    normalized_receipts = [seal_official_execution_raw_file_receipt(item) for item in receipts]
    normalized_receipts.sort(key=lambda item: str(item["receipt_digest"]))
    normalized_rows = [seal_official_execution_session_row(item) for item in rows]
    normalized_rows.sort(key=lambda item: str(item["symbol"]))
    counts = Counter(str(item["market"]) for item in normalized_rows)
    payload: dict[str, object] = {
        "schema_version": OFFICIAL_EXECUTION_SESSION_SCHEMA_VERSION,
        "contract_version": OFFICIAL_EXECUTION_SESSION_CONTRACT_VERSION,
        "session_date": session_date,
        "generated_at": generated_at,
        "source_registry_digest": source_registry_digest,
        "receipts": normalized_receipts,
        "expected_market_counts": {
            "SH": counts["SH"],
            "SZ": counts["SZ"],
            "BJ": counts["BJ"],
        },
        "expected_row_count": len(normalized_rows),
        "rows": normalized_rows,
        "receipt_set_digest": _digest(
            [str(item["receipt_digest"]) for item in normalized_receipts]
        ),
        "universe_membership_digest": _digest(
            [str(item["symbol"]) for item in normalized_rows]
        ),
        "integrity_notice": OFFICIAL_EXECUTION_INTEGRITY_NOTICE,
    }
    payload["artifact_digest"] = official_execution_content_digest(payload, "artifact_digest")
    return OfficialExecutionSessionArtifact.model_validate(payload).model_dump(mode="json")


def load_verified_official_execution_source_registry(
    path: str | Path,
    *,
    expected_registry_digest: str,
) -> VerifiedOfficialExecutionSourceRegistry:
    """Load a registry only when its digest matches an out-of-band pin."""

    if not _is_sha256(expected_registry_digest):
        raise OfficialExecutionIntakeError("official source registry digest pin is missing or invalid")
    value = _load_json(path, OFFICIAL_EXECUTION_MAX_REGISTRY_BYTES, "official source registry")
    try:
        registry = OfficialExecutionSourceRegistry.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise OfficialExecutionIntakeError("official source registry failed strict verification") from exc
    if registry.registry_digest != expected_registry_digest:
        raise OfficialExecutionIntakeError("official source registry does not match the operator pin")
    encoded = canonical_json_bytes(registry.model_dump(mode="json")).decode("utf-8")
    return VerifiedOfficialExecutionSourceRegistry(
        encoded,
        registry.registry_digest,
        _seal=_VERIFIED_REGISTRY_SEAL,
    )


def load_verified_official_execution_session(
    path: str | Path,
    *,
    registry: VerifiedOfficialExecutionSourceRegistry,
    raw_file_root: str | Path,
) -> VerifiedOfficialExecutionSession:
    """Verify the normalized artifact, pinned registrations, and raw bytes."""

    if not isinstance(registry, VerifiedOfficialExecutionSourceRegistry):
        raise OfficialExecutionIntakeError("official execution intake requires a pinned registry token")
    value = _load_json(path, OFFICIAL_EXECUTION_MAX_SESSION_BYTES, "official execution session")
    try:
        artifact = OfficialExecutionSessionArtifact.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise OfficialExecutionIntakeError("official execution session failed strict verification") from exc
    if artifact.source_registry_digest != registry.registry_digest:
        raise OfficialExecutionIntakeError("official execution session references a different registry")
    registrations = _registration_index(registry)
    verified_raw: list[dict[str, object]] = []
    for receipt in artifact.receipts:
        registration = registrations.get((receipt.source_id, receipt.dataset_id))
        if registration is None:
            raise OfficialExecutionIntakeError("official execution receipt uses an unregistered source")
        _verify_receipt_registration(receipt, registration)
        raw_path = _trusted_raw_path(raw_file_root, receipt.relative_path)
        size, digest = _stable_file_sha256(raw_path)
        if size != receipt.byte_size or digest != receipt.sha256:
            raise OfficialExecutionIntakeError(
                f"official raw file bytes do not match receipt: {receipt.relative_path}"
            )
        verified_raw.append(
            {
                "receipt_digest": receipt.receipt_digest,
                "byte_size": size,
                "sha256": digest,
            }
        )
    raw_set_digest = _digest(sorted(verified_raw, key=lambda item: str(item["receipt_digest"])))
    encoded = canonical_json_bytes(artifact.model_dump(mode="json")).decode("utf-8")
    return VerifiedOfficialExecutionSession(
        encoded,
        artifact_digest=artifact.artifact_digest,
        session_date=artifact.session_date,
        raw_file_set_digest=raw_set_digest,
        _seal=_VERIFIED_SESSION_SEAL,
    )


def bind_official_execution_session_to_decisions(
    session: VerifiedOfficialExecutionSession,
    *,
    expected_symbols: Sequence[str],
    source_snapshot_digest: str,
) -> BoundOfficialExecutionDecisionSession:
    """Require complete exact coverage of a frozen source decision population."""

    if not isinstance(session, VerifiedOfficialExecutionSession):
        raise OfficialExecutionIntakeError("decision binding requires a verified official session")
    if not _is_sha256(source_snapshot_digest):
        raise OfficialExecutionIntakeError("decision binding source snapshot digest is invalid")
    symbols = list(expected_symbols)
    if (
        not symbols
        or symbols != sorted(symbols)
        or len(set(symbols)) != len(symbols)
        or any(not _valid_symbol(item) for item in symbols)
    ):
        raise OfficialExecutionIntakeError("expected decision symbols must be canonical and unique")
    rows = session.rows
    by_symbol = {str(row["symbol"]): row for row in rows}
    missing = [symbol for symbol in symbols if symbol not in by_symbol]
    if missing:
        preview = ", ".join(missing[:5])
        raise OfficialExecutionIntakeError(
            f"official execution session does not cover the frozen decision set: {preview}"
        )
    selected = [by_symbol[symbol] for symbol in symbols]
    encoded = canonical_json_bytes(selected).decode("utf-8")
    return BoundOfficialExecutionDecisionSession(
        encoded,
        session_date=session.session_date,
        source_snapshot_digest=source_snapshot_digest,
        decision_membership_digest=_digest(symbols),
        official_session_artifact_digest=session.artifact_digest,
        official_raw_file_set_digest=session.raw_file_set_digest,
        _seal=_BOUND_SESSION_SEAL,
    )


def _validate_execution_state_shape(row: OfficialExecutionSessionRow) -> None:
    if row.exchange_session_state == "trading":
        _validate_trading_execution_state(row)
        return
    _validate_nontrading_execution_state(row)


def _validate_trading_execution_state(row: OfficialExecutionSessionRow) -> None:
    if all(value is None for value in row.bar.model_dump(mode="json").values() if value != "none"):
        raise ValueError("trading official session requires an unadjusted bar")
    values = (
        row.bar.open,
        row.bar.high,
        row.bar.low,
        row.bar.close,
        row.bar.volume,
        row.bar.amount,
    )
    if any(value is None for value in values):
        raise ValueError("trading official session requires complete OHLCV/amount")
    if cast(float, row.bar.volume) <= 0 or cast(float, row.bar.amount) <= 0:
        raise ValueError("trading official session requires positive volume and amount")
    incompatible = {"suspended", "rule_ineligible"}
    if row.entry_execution_state in incompatible or row.exit_execution_state in incompatible:
        raise ValueError("trading session has an incompatible execution state")


def _validate_nontrading_execution_state(row: OfficialExecutionSessionRow) -> None:
    if any(
        value is not None
        for value in (
            row.bar.open,
            row.bar.high,
            row.bar.low,
            row.bar.close,
            row.bar.volume,
            row.bar.amount,
        )
    ):
        raise ValueError("non-trading official session must not synthesize a bar")
    if row.exchange_session_state == "suspended":
        if row.entry_execution_state != "suspended" or row.exit_execution_state != "suspended":
            raise ValueError("suspended session must mark both execution sides suspended")
    elif (
        row.entry_execution_state != "rule_ineligible"
        or row.exit_execution_state != "rule_ineligible"
    ):
        raise ValueError("not-listed or delisted session must be rule-ineligible")


def _trading_state_digest(row: OfficialExecutionSessionRow) -> str:
    return _digest(
        {
            "symbol": row.symbol,
            "session_date": row.session_date,
            "exchange_session_state": row.exchange_session_state,
            "entry_execution_state": row.entry_execution_state,
            "entry_reason_code": row.entry_reason_code,
            "exit_execution_state": row.exit_execution_state,
            "exit_reason_code": row.exit_reason_code,
            "instrument_rules": row.instrument_rules.model_dump(mode="json"),
            "corporate_action": row.corporate_action.model_dump(mode="json"),
            "bar": row.bar.model_dump(mode="json"),
            "receipt_digest": row.receipt_digest,
            "source_record_id": row.source_record_id,
        }
    )


def _registration_index(
    registry: VerifiedOfficialExecutionSourceRegistry,
) -> dict[tuple[str, str], OfficialExecutionSourceRegistration]:
    values = cast(list[object], registry.payload["registrations"])
    registrations = [OfficialExecutionSourceRegistration.model_validate(item) for item in values]
    return {(item.source_id, item.dataset_id): item for item in registrations}


def _verify_receipt_registration(
    receipt: OfficialExecutionRawFileReceipt,
    registration: OfficialExecutionSourceRegistration,
) -> None:
    session = date.fromisoformat(receipt.session_date)
    valid_from = date.fromisoformat(registration.valid_from)
    valid_through = (
        date.fromisoformat(registration.valid_through)
        if registration.valid_through is not None
        else None
    )
    if (
        receipt.market not in registration.markets
        or receipt.license_reference != registration.license_reference
        or session < valid_from
        or (valid_through is not None and session > valid_through)
        or not _uri_within_registration(receipt.source_uri, registration.source_base_uri)
    ):
        raise OfficialExecutionIntakeError("official raw receipt conflicts with its pinned registration")


def _uri_within_registration(value: str, base: str) -> bool:
    parsed, registered = urlparse(value), urlparse(base)
    if (parsed.scheme, parsed.netloc) != (registered.scheme, registered.netloc):
        return False
    base_path = registered.path.rstrip("/") + "/"
    return parsed.path == registered.path.rstrip("/") or parsed.path.startswith(base_path)


def _load_json(path: str | Path, max_bytes: int, label: str) -> Mapping[str, object]:
    try:
        value = decode_json_bytes(read_regular_file(path, max_bytes=max_bytes))
    except Exception as exc:
        raise OfficialExecutionIntakeError(f"{label} cannot be read safely") from exc
    if not isinstance(value, Mapping):
        raise OfficialExecutionIntakeError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _trusted_raw_path(root: str | Path, relative_path: str) -> Path:
    parts = _relative_path(relative_path).parts
    base = Path(root).expanduser().absolute()
    try:
        base_stat = base.lstat()
    except OSError as exc:
        raise OfficialExecutionIntakeError("official raw-file root is unavailable") from exc
    if not stat.S_ISDIR(base_stat.st_mode) or stat.S_ISLNK(base_stat.st_mode):
        raise OfficialExecutionIntakeError("official raw-file root must be a real directory")
    current = base
    for index, part in enumerate(parts):
        current = current / part
        try:
            facts = current.lstat()
        except OSError as exc:
            raise OfficialExecutionIntakeError(f"official raw file path is unavailable: {relative_path}") from exc
        if stat.S_ISLNK(facts.st_mode):
            raise OfficialExecutionIntakeError("official raw file path must not contain symlinks")
        if index < len(parts) - 1 and not stat.S_ISDIR(facts.st_mode):
            raise OfficialExecutionIntakeError("official raw file parent is not a directory")
        if index == len(parts) - 1 and not stat.S_ISREG(facts.st_mode):
            raise OfficialExecutionIntakeError("official raw file is not regular")
    return current


def _stable_file_sha256(path: Path) -> tuple[int, str]:
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OfficialExecutionIntakeError("official raw file is not regular")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OfficialExecutionIntakeError:
        raise
    except OSError as exc:
        raise OfficialExecutionIntakeError("official raw file cannot be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise OfficialExecutionIntakeError("official raw file changed during verification")
    return before.st_size, digest.hexdigest()


def _source_uri(value: str, channel: OfficialDeliveryChannel, label: str) -> None:
    parsed = urlparse(value)
    expected_scheme = "https" if channel in {"https", "private_api"} else channel
    if parsed.scheme != expected_scheme or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{label} is not a supported credential-free source URI")


def _any_source_uri(value: str, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "sftp", "rsync"} or not parsed.netloc:
        raise ValueError(f"{label} is not a supported source URI")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not persist credentials")


def _relative_path(value: str) -> PurePosixPath:
    if "\\" in value:
        raise ValueError("official raw file path must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("official raw file path must be a safe relative path")
    return path


def _iso_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _aware_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed


def _valid_symbol(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 9
        and value[:6].isdigit()
        and value[6:] in {".SH", ".SZ", ".BJ"}
    )


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _digest(value: object) -> str:
    return sha256_hex(canonical_json_bytes(value))


__all__ = [
    "BoundOfficialExecutionDecisionSession",
    "OFFICIAL_EXECUTION_INTEGRITY_NOTICE",
    "OFFICIAL_EXECUTION_REGISTRY_SCHEMA_VERSION",
    "OFFICIAL_EXECUTION_SESSION_CONTRACT_VERSION",
    "OFFICIAL_EXECUTION_SESSION_SCHEMA_VERSION",
    "OfficialExecutionCorporateActionReference",
    "OfficialExecutionDailyBar",
    "OfficialExecutionInstrumentRules",
    "OfficialExecutionIntakeError",
    "OfficialExecutionRawFileReceipt",
    "OfficialExecutionSessionArtifact",
    "OfficialExecutionSessionRow",
    "OfficialExecutionSourceRegistration",
    "OfficialExecutionSourceRegistry",
    "VerifiedOfficialExecutionSession",
    "VerifiedOfficialExecutionSourceRegistry",
    "bind_official_execution_session_to_decisions",
    "load_verified_official_execution_session",
    "load_verified_official_execution_source_registry",
    "official_execution_content_digest",
    "seal_official_execution_corporate_action_reference",
    "seal_official_execution_instrument_rules",
    "seal_official_execution_raw_file_receipt",
    "seal_official_execution_session_artifact",
    "seal_official_execution_session_row",
    "seal_official_execution_source_registration",
    "seal_official_execution_source_registry",
]
