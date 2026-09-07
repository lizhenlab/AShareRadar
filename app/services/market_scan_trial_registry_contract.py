"""Strict frozen experiment contracts; hashes prove integrity, not authorship."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import math
import re
from typing import cast

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, sha256_hex


TRIAL_CONTRACT_VERSION = "market-scan-trial-contract-v1"
TRIAL_PRIMARY_OBJECTIVE = {"top_n": 100, "horizon": 5, "target": "mean_daily_net_excess_return"}
TRIAL_INCREMENTAL_PRIMARY_OBJECTIVE = {
    "schema_version": "market-scan-primary-incremental-objective-v1",
    "top_n": 100, "horizon": 5, "target": "mean_daily_net_return_improvement", "reference": "production_v5-top100",
}
TRIAL_INCREMENTAL_BENCHMARK_POLICY = {"allocation": "same-capital-production-v5-top100"}
TRIAL_HOLDING_OFFSET = 6
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CONTRACT_KEYS = frozenset({
    "registration_kind", "primary_objective", "trials", "input_manifest",
    "cost_policy", "capital_policy", "shared_account_policy", "benchmark_policy", "calendar", "exploration_cutoff",
})
_TRIAL_KEYS = frozenset({"trial_id", "candidate_id", "score_specification", "score_spec_hash", "parameters"})
_CALENDAR_KEYS = frozenset({"trading_dates", "train_signal_dates", "calibration_signal_dates", "test_signal_dates"})
_SHARED_POLICY_KEYS = frozenset({
    "allocation", "reinvest", "entry", "scheduled_exit", "blocked_exit", "duplicate_symbol", "unfilled_entry",
})


class TrialRegistryError(ValueError):
    """The registry is incomplete, inconsistent, unsafe, or unverifiable."""


def trial_registry_digest(value: object) -> str:
    """Hash the exact finite JSON value, including all parameters and identities."""
    return sha256_hex(canonical_json_bytes(value))


def registry_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TrialRegistryError(f"{name}: expected JSON object")
    return cast(dict[str, object], value)


def registry_keys(value: Mapping[str, object], keys: frozenset[str], name: str) -> None:
    if set(value) != keys:
        raise TrialRegistryError(f"{name}: unexpected or missing fields")


def registry_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise TrialRegistryError(f"{name}: invalid identifier")
    return value


def registry_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise TrialRegistryError(f"{name}: invalid SHA-256")
    return value


def registry_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise TrialRegistryError(f"{name}: expected ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise TrialRegistryError(f"{name}: invalid date") from exc
    if parsed.isoformat() != value:
        raise TrialRegistryError(f"{name}: noncanonical date")
    return parsed


def registry_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise TrialRegistryError(f"{name}: expected aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TrialRegistryError(f"{name}: invalid timestamp") from exc
    if parsed.utcoffset() is None:
        raise TrialRegistryError(f"{name}: timezone required")
    return parsed


def validate_trial_contract(value: Mapping[str, object]) -> dict[str, object]:
    """Copy and validate the complete declared search family and split contract."""
    contract = registry_object(decode_json_bytes(canonical_json_bytes(dict(value))), "contract")
    registry_keys(contract, _CONTRACT_KEYS, "contract")
    if contract["registration_kind"] not in ("prospective", "retrospective"):
        raise TrialRegistryError("registration_kind: expected prospective or retrospective")
    if canonical_json_bytes(contract["primary_objective"]) not in (
        canonical_json_bytes(TRIAL_PRIMARY_OBJECTIVE), canonical_json_bytes(TRIAL_INCREMENTAL_PRIMARY_OBJECTIVE),
    ):
        raise TrialRegistryError("primary_objective: must exactly match a supported Top100 H5 objective contract")
    _validate_trials(contract["trials"])
    manifest = registry_object(contract["input_manifest"], "input_manifest")
    registry_keys(manifest, frozenset({"universe_digest", "run_manifest_digest", "implementation_digest"}), "input_manifest")
    for name, digest in manifest.items():
        registry_hash(digest, name)
    for name in ("cost_policy", "capital_policy", "shared_account_policy", "benchmark_policy"):
        if not registry_object(contract[name], name):
            raise TrialRegistryError(f"{name}: full policy must be frozen")
    _validate_policies(contract)
    _validate_calendar(contract)
    return contract


def contract_trials(contract: Mapping[str, object]) -> dict[str, dict[str, object]]:
    trials = cast(list[dict[str, object]], contract["trials"])
    return {cast(str, trial["trial_id"]): trial for trial in trials}


def contract_test_start(contract: Mapping[str, object]) -> date:
    calendar = cast(dict[str, list[str]], contract["calendar"])
    return registry_date(calendar["test_signal_dates"][0], "test_signal_dates")


def _validate_trials(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise TrialRegistryError("trials: nonempty complete expected trial set required")
    trial_ids: set[str] = set()
    candidate_specs: dict[str, str] = {}
    for raw in value:
        trial = registry_object(raw, "trial")
        registry_keys(trial, _TRIAL_KEYS, "trial")
        trial_id = registry_identifier(trial["trial_id"], "trial_id")
        candidate_id = registry_identifier(trial["candidate_id"], "candidate_id")
        if trial_id in trial_ids:
            raise TrialRegistryError("trial_id: duplicate")
        trial_ids.add(trial_id)
        specification = registry_object(trial["score_specification"], "score_specification")
        if not specification or trial["score_spec_hash"] != trial_registry_digest(specification):
            raise TrialRegistryError("score_spec_hash: mismatch or empty specification")
        registry_object(trial["parameters"], "parameters")
        candidate_digest = trial_registry_digest({"specification": specification, "parameters": trial["parameters"]})
        if candidate_id in candidate_specs and candidate_specs[candidate_id] != candidate_digest:
            raise TrialRegistryError("candidate_id: different parameters require a distinct candidate identity")
        candidate_specs[candidate_id] = candidate_digest


def _dates(value: object, name: str) -> list[date]:
    if not isinstance(value, list) or not value:
        raise TrialRegistryError(f"{name}: nonempty date list required")
    dates = [registry_date(item, name) for item in value]
    if dates != sorted(set(dates)):
        raise TrialRegistryError(f"{name}: dates must be unique and ordered")
    return dates


def _validate_policies(contract: Mapping[str, object]) -> None:
    capital = registry_object(contract["capital_policy"], "capital_policy")
    registry_keys(capital, frozenset({"initial_cash", "currency"}), "capital_policy")
    if capital["currency"] != "CNY" or not _positive_number(capital["initial_cash"]):
        raise TrialRegistryError("capital_policy: positive finite CNY capital required")
    cost = registry_object(contract["cost_policy"], "cost_policy")
    registry_keys(cost, frozenset({"profile", "max_participation_rate"}), "cost_policy")
    if not isinstance(cost["profile"], str) or not cost["profile"].strip():
        raise TrialRegistryError("cost_policy: named cost profile required")
    rate = cost["max_participation_rate"]
    if not _positive_number(rate) or cast(float, rate) > 1:
        raise TrialRegistryError("cost_policy: participation rate must be in (0,1]")
    shared = registry_object(contract["shared_account_policy"], "shared_account_policy")
    registry_keys(shared, _SHARED_POLICY_KEYS, "shared_account_policy")
    benchmark = registry_object(contract["benchmark_policy"], "benchmark_policy")
    registry_keys(benchmark, frozenset({"allocation"}), "benchmark_policy")
    if any(not isinstance(value, str) or not value.strip() for value in [*shared.values(), *benchmark.values()]):
        raise TrialRegistryError("account policies: explicit nonempty rule definitions required")
    if contract["primary_objective"] == TRIAL_INCREMENTAL_PRIMARY_OBJECTIVE and benchmark != TRIAL_INCREMENTAL_BENCHMARK_POLICY:
        raise TrialRegistryError("benchmark_policy: incremental objective requires the frozen production v5 Top100 account")


def _positive_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(cast(float, value)) and cast(float, value) > 0
    except OverflowError:
        return False


def _validate_calendar(contract: Mapping[str, object]) -> None:
    calendar = registry_object(contract["calendar"], "calendar")
    registry_keys(calendar, _CALENDAR_KEYS, "calendar")
    trading_dates = _dates(calendar["trading_dates"], "trading_dates")
    positions = {day: index for index, day in enumerate(trading_dates)}
    partitions = [_dates(calendar[name], name) for name in (
        "train_signal_dates", "calibration_signal_dates", "test_signal_dates",
    )]
    all_signals = [day for partition in partitions for day in partition]
    if len(all_signals) != len(set(all_signals)):
        raise TrialRegistryError("calendar: one signal date cannot cross partitions")
    if any(day not in positions for day in all_signals):
        raise TrialRegistryError("calendar: signal date missing from trading calendar")
    if any(positions[day] + TRIAL_HOLDING_OFFSET >= len(trading_dates) for day in all_signals):
        raise TrialRegistryError("calendar: H+1 label target missing")
    for previous, following in zip(partitions, partitions[1:], strict=False):
        if positions[previous[-1]] + TRIAL_HOLDING_OFFSET >= positions[following[0]]:
            raise TrialRegistryError("calendar: H+1 purge required between partitions")
    _validate_exploration_cutoff(contract, trading_dates, positions, partitions)


def _validate_exploration_cutoff(
    contract: Mapping[str, object], trading_dates: list[date], positions: dict[date, int], partitions: list[list[date]],
) -> None:
    cutoff = registry_date(contract["exploration_cutoff"], "exploration_cutoff")
    calibration_end = trading_dates[positions[partitions[1][-1]] + TRIAL_HOLDING_OFFSET]
    if cutoff < calibration_end or cutoff >= partitions[2][0]:
        raise TrialRegistryError("exploration_cutoff: must follow calibration labels and precede test")
