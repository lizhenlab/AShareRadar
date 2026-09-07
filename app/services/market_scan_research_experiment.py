"""A fixed research family, frozen signal inputs and replay-bound trial contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
import sys
from typing import cast

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.services.market_scan_official_execution import OfficialExecutionSessionRow, VerifiedOfficialExecutionSession
from app.services.market_scan_research_challengers import (
    RESEARCH_CHALLENGERS, FrozenResearchScore, ResearchChallenger, prepare_research_scores,
    research_challenger_spec, score_research_challenger,
)
from app.services.market_scan_research_portfolio_models import ResearchSignalBatch, ResearchSignalCandidate
from app.services.market_scan_trial_registry_contract import (
    TRIAL_INCREMENTAL_BENCHMARK_POLICY, TRIAL_INCREMENTAL_PRIMARY_OBJECTIVE, trial_registry_digest, validate_trial_contract,
)
from app.services.trading_calendar import next_trade_dates, trading_dates_between
from app.utils.clock import ASHARE_TIMEZONE


RESEARCH_OPTIMIZATION_CHALLENGERS = tuple(variant for variant in RESEARCH_CHALLENGERS if variant != "production_v5")


def research_optimization_test_contract() -> dict[str, object]:
    """Freeze the incremental hypothesis separately from market-relative diagnostics."""
    return {
        "schema_version": "candidate-versus-production-daily-net-increment-v1",
        "reference": TRIAL_INCREMENTAL_PRIMARY_OBJECTIVE["reference"], "target": TRIAL_INCREMENTAL_PRIMARY_OBJECTIVE["target"],
        "alternative": "greater", "family": list(RESEARCH_OPTIMIZATION_CHALLENGERS),
        "method": "benjamini-yekutieli-fdr", "alpha": .05,
        "block_length_sessions": 6, "minimum_session_count": 40, "bootstrap_samples": 1000,
        "bootstrap_method": "deterministic-circular-moving-block-bootstrap-under-null",
    }


@dataclass(frozen=True)
class FrozenResearchBatch:
    run_id: int
    signal_date: str
    source_digest: str
    scores: tuple[FrozenResearchScore, ...]


@dataclass(frozen=True)
class FrozenResearchDataset:
    batches: tuple[FrozenResearchBatch, ...]
    universe_digest: str
    run_manifest_digest: str


def prepare_research_dataset(snapshots: Iterable[Mapping[str, object]]) -> FrozenResearchDataset:
    """Replay raw PIT scores without inspecting any forward execution prices."""
    batches = []
    for snapshot in snapshots:
        batches.append(_prepare_research_batch(snapshot))
        del snapshot  # Release a large PIT document before requesting the next one.
    batches.sort(key=lambda batch: (batch.signal_date, batch.run_id))
    if not batches or len({batch.signal_date for batch in batches}) != len(batches) or len({batch.run_id for batch in batches}) != len(batches):
        raise ValueError("research requires one unique frozen run per signal date")
    universe = [{"date": batch.signal_date, "symbols": sorted(row.symbol for row in batch.scores)} for batch in batches]
    manifest = [{"run_id": batch.run_id, "date": batch.signal_date, "digest": batch.source_digest} for batch in batches]
    return FrozenResearchDataset(tuple(batches), trial_registry_digest(universe), trial_registry_digest(manifest))


def _prepare_research_batch(snapshot: Mapping[str, object]) -> FrozenResearchBatch:
    if set(snapshot) != {"run", "items"} or not isinstance(snapshot["items"], list):
        raise ValueError("snapshot requires exactly run and items")
    run = MarketScanRun.model_validate(snapshot["run"])
    items = [MarketScanResultItem.model_validate(item) for item in snapshot["items"]]
    validate_research_decision_time(run, items)
    _validate_research_membership(run, items)
    scores = prepare_research_scores([item for item in items if item.status == "success"], run)
    digest = trial_registry_digest({"run": run.model_dump(mode="json"), "items": [
        item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.symbol)
    ]})
    return FrozenResearchBatch(run.id, run.data_date, digest, scores)


def _validate_research_membership(run: MarketScanRun, items: Sequence[MarketScanResultItem]) -> None:
    if len(items) != run.total_count or len({item.symbol for item in items}) != len(items):
        raise ValueError("research snapshots require the entire unique frozen membership")
    if run.missing_count or any(item.status in {"pending", "missing"} for item in items):
        raise ValueError("missing frozen members cannot be silently removed from the research universe")
    if any(item.run_id != run.id for item in items):
        raise ValueError("frozen membership belongs to a different run")


def validate_research_decision_time(run: MarketScanRun, items: Sequence[MarketScanResultItem]) -> None:
    """Late/backfilled inputs cannot become historically executable decisions."""
    if run.status not in {"success", "degraded"} or run.snapshot_seal_origin != "publication":
        raise ValueError("research requires a completed publication, not running or backfilled ranks")
    entry_date = next_trade_dates(date.fromisoformat(run.data_date), 1)[0]
    deadline = datetime.combine(entry_date, time(9, 30), ASHARE_TIMEZONE)
    timestamps = [run.as_of, run.created_at, run.updated_at, run.finished_at, run.snapshot_sealed_at,
                  run.quote_capture_finished_at]
    timestamps.extend(value for item in items for value in (item.updated_at, item.quote_observed_at))
    if any(value is None or _research_timestamp(value) >= deadline for value in timestamps):
        raise ValueError("decision inputs and publication must be available before D+1 opening")


def _research_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=ASHARE_TIMEZONE) if parsed.tzinfo is None else parsed.astimezone(ASHARE_TIMEZONE)


def research_signal_batches(dataset: FrozenResearchDataset, variant: ResearchChallenger) -> tuple[ResearchSignalBatch, ...]:
    return tuple(ResearchSignalBatch(
        batch_id=str(batch.run_id), signal_date=batch.signal_date, source_digest=batch.source_digest,
        candidates=tuple(ResearchSignalCandidate(row.symbol, row.rank, row.source_digest)
                         for row in score_research_challenger(batch.scores, variant)),
    ) for batch in dataset.batches)


def frozen_research_dataset_digest(dataset: FrozenResearchDataset) -> str:
    """Rebind actual prepared values; never trust caller-carried digest properties."""
    return trial_registry_digest({
        "universe_digest": dataset.universe_digest, "raw_snapshot_manifest_digest": dataset.run_manifest_digest,
        "batches": [{"run_id": batch.run_id, "signal_date": batch.signal_date, "source_digest": batch.source_digest,
                     "scores": [asdict(row) for row in batch.scores]} for batch in dataset.batches],
    })


def research_implementation_digest() -> str:
    """Bind all application Python sources, dependency declarations and interpreter."""
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "app").rglob("*.py"))
    files.extend(path for name in ("pyproject.toml", "requirements.txt", "requirements-dev.txt") if (path := root / name).is_file())
    payload = {"python": sys.version, "sources": {str(path.relative_to(root)): sha256_hex(path.read_bytes()) for path in files}}
    return sha256_hex(canonical_json_bytes(payload))


def research_execution_manifest(
    official_sessions: Sequence[VerifiedOfficialExecutionSession] = (),
    synthetic_rows: Sequence[OfficialExecutionSessionRow] = (),
) -> str:
    if official_sessions and synthetic_rows:
        raise ValueError("cannot mix official and synthetic inputs")
    if any(not isinstance(session, VerifiedOfficialExecutionSession) for session in official_sessions):
        raise ValueError("official inputs require strict-loader tokens")
    return trial_registry_digest({
        "mode": "official" if official_sessions else "synthetic" if synthetic_rows else "unavailable",
        "official_sessions": sorted(session.artifact_digest for session in official_sessions),
        "synthetic_rows": sorted(row.row_digest for row in synthetic_rows),
    })


def build_research_trial_contract(
    dataset: FrozenResearchDataset, calendar: Mapping[str, object], *, execution_manifest_digest: str,
    exploration_cutoff: str, registration_kind: str = "retrospective", initial_cash: float = 1_000_000,
    cost_profile: str = "base", max_participation_rate: float = .01,
) -> dict[str, object]:
    if registration_kind == "prospective":
        raise ValueError("frozen-result research bundles support retrospective registration only; prospective plans need a separate future-input protocol")
    trials = [{"trial_id": variant, "candidate_id": variant, "score_specification": research_challenger_spec(variant),
               "score_spec_hash": trial_registry_digest(research_challenger_spec(variant)),
               "parameters": {"execution_manifest_digest": execution_manifest_digest,
                              "optimization_test": research_optimization_test_contract()}} for variant in RESEARCH_CHALLENGERS]
    contract = {
        "registration_kind": registration_kind, "primary_objective": dict(TRIAL_INCREMENTAL_PRIMARY_OBJECTIVE), "trials": trials,
        "input_manifest": {"universe_digest": dataset.universe_digest, "run_manifest_digest": frozen_research_dataset_digest(dataset),
                           "implementation_digest": research_implementation_digest()},
        "cost_policy": {"profile": cost_profile, "max_participation_rate": max_participation_rate},
        "capital_policy": {"initial_cash": initial_cash, "currency": "CNY"},
        "shared_account_policy": {
            "allocation": "rotating-horizon-plus-one-sleeves", "reinvest": "within-sleeve", "entry": "D+1-open",
            "scheduled_exit": "D+H+1-close", "blocked_exit": "retain-and-retry-next-session",
            "duplicate_symbol": "keep-existing-position-and-cash-slot", "unfilled_entry": "cash-no-replacement",
        },
        "benchmark_policy": dict(TRIAL_INCREMENTAL_BENCHMARK_POLICY),
        "calendar": dict(calendar), "exploration_cutoff": exploration_cutoff,
    }
    validated = validate_trial_contract(contract)
    dates = cast(list[str], calendar["trading_dates"])
    expected_dates = [day.isoformat() for day in trading_dates_between(date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]))]
    if dates != expected_dates:
        raise ValueError("calendar must match the complete trusted trading calendar")
    if cast(list[str], calendar["test_signal_dates"]) != [batch.signal_date for batch in dataset.batches]:
        raise ValueError("test dates must match every frozen research batch")
    return validated
