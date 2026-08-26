"""Bounded Choice research collection; never writes runtime or formal evidence stores."""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol

from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.services.choice_research import (
    DAILY_FIELDS, DIVIDEND_FIELDS, EVENT_FIELDS, METADATA_FIELDS, OPTIONS,
    choose_symbols, normalize, report_dates, request, snapshot_dates,
)
from app.services.choice_research_store import ChoiceBudget, ChoiceDataset, Record, now_text
from app.services.choice_sdk import ChoiceError
from app.utils.clock import market_now


class ResearchClient(Protocol):
    def request(self, method: str, args: list[str]) -> dict[str, Any]: ...


class ChoiceCollector:
    def __init__(self, dataset: ChoiceDataset, client: ResearchClient, budget: ChoiceBudget, *,
                 max_requests: int = 100, progress: Callable[[dict[str, Any]], None] | None = None) -> None:
        if not 1 <= max_requests <= 1000:
            raise ChoiceError("max_requests must be between 1 and 1000")
        self.dataset, self.client, self.budget = dataset, client, budget
        self.max_requests = max_requests
        self.progress = progress or (lambda _: None)
        self.calls = self.cached = 0
        self.stage = "account"
        self._last_quota_refresh: float | None = None

    def refresh_quota(self) -> None:
        # Account queries have their own rate limit. The durable reservation ledger
        # still guards every data request while a recent quota snapshot is reused.
        if self._last_quota_refresh is not None and time.monotonic() - self._last_quota_refresh < 30:
            return
        today = market_now().date().isoformat()
        response = self.client.request("datastatistics", ["", "", f"StartDate={today},EndDate={today},Ispandas=0"])
        self.budget.update(response)
        self._last_quota_refresh = time.monotonic()
        encoded = canonical_json_bytes({"queried_at": now_text(), "result": response})
        exclusive_atomic_publish(self.dataset.directory / "account" / f"{sha256_hex(encoded)}.json", encoded, max_bytes=1024 * 1024)

    def fetch(self, descriptor: dict[str, Any], units: int) -> list[Record]:
        self.stage = descriptor["kind"]
        payload = self.dataset.cached(descriptor)
        if payload is None:
            if self.calls >= self.max_requests:
                raise ChoiceError("request-count pause; rerun the same plan to resume")
            if self.calls and self.calls % 10 == 0:
                self.refresh_quota()
            self.budget.reserve(descriptor["method"], units)
            self.calls += 1
            result = self.client.request(descriptor["method"], descriptor["args"])
            # Archive before normalization: malformed responses remain auditable,
            # but never become successful checkpoints or synthetic valid bars.
            payload = self.dataset.archive(descriptor, result)
        else:
            self.cached += 1
        records = normalize(payload)
        self.dataset.project(payload, records)
        self.progress({"stage": self.stage, "requests_this_run": self.calls, "cached_requests": self.cached, "records": len(records)})
        return records

    def run(self) -> dict[str, Any]:
        plan = self.dataset.plan
        self.refresh_quota()
        calendar = self.fetch(request("calendar", "tradedates", [plan["start_date"], plan["end_date"], f"Market=CNSESH,{OPTIONS}"]), 1)
        sessions = [row[2] for row in calendar]
        dates = snapshot_dates(sessions)
        universe: set[str] = set()
        for day in dates:
            records = self.fetch(request("universe", "sector", ["001071", day, OPTIONS], as_of=day), 1)
            universe.update(row[1] for row in records)
        symbols = choose_symbols(universe, plan)
        encoded = canonical_json_bytes({"symbols": symbols, "sessions": sessions, "snapshot_dates": dates, "selection": plan["selection"]})
        exclusive_atomic_publish(self.dataset.directory / "selection.json", encoded, max_bytes=1024 * 1024)
        self._metadata(symbols, dates)
        self._dividends(symbols, plan)
        self._daily(symbols, sessions)
        self._events(plan)
        self.refresh_quota()
        summary = self.dataset.verify(normalize)
        summary.update({
            "status": "complete_for_declared_research_scope", "generated_at": now_text(),
            "requests_this_run": self.calls, "cached_requests": self.cached,
            "selected_symbols": symbols, "calendar_session_count": len(sessions),
            "monthly_universe_snapshot_count": len(dates), "raw_replay_verified": True,
            "limitations": plan["limitations"], "quota_snapshot": self.budget.quotas,
        })
        encoded = canonical_json_bytes(summary)
        path = self.dataset.directory / f"summary-{sha256_hex(encoded)}.json"
        exclusive_atomic_publish(path, encoded, max_bytes=1024 * 1024)
        summary["summary_path"] = str(path)
        return summary

    def _metadata(self, symbols: list[str], dates: list[str]) -> None:
        for day in dates:
            self.fetch(request("metadata", "css", [",".join(symbols), ",".join(METADATA_FIELDS),
                f"TradeDate={day},EndDate={day},AdjustFlag=1,{OPTIONS}"], as_of=day, symbols=symbols, fields=METADATA_FIELDS),
                len(symbols) * len(METADATA_FIELDS))

    def _dividends(self, symbols: list[str], plan: dict[str, Any]) -> None:
        for report in report_dates(plan["start_date"], plan["end_date"]):
            self.fetch(request("dividend_snapshot", "css", [",".join(symbols), ",".join(DIVIDEND_FIELDS),
                f"ReportDate={report},AssignFeature=4,YesNo=1,{OPTIONS}"], as_of=report, symbols=symbols, fields=DIVIDEND_FIELDS),
                len(symbols) * len(DIVIDEND_FIELDS))

    def _daily(self, symbols: list[str], sessions: list[str]) -> None:
        for offset in range(0, len(symbols), 20):
            batch = symbols[offset:offset + 20]
            self.fetch(request("daily", "csd", [",".join(batch), ",".join(DAILY_FIELDS), sessions[0], sessions[-1],
                f"Period=1,AdjustFlag=1,FillData=0,Order=1,{OPTIONS}"], symbols=batch, fields=DAILY_FIELDS, sessions=sessions),
                len(batch) * len(sessions) * len(DAILY_FIELDS))

    def _events(self, plan: dict[str, Any]) -> None:
        for symbol in plan["event_symbols"]:
            self.fetch(request("dividend_event", "ctr", ["DividendImplementationInfo", ",".join(EVENT_FIELDS),
                f'secucode={symbol},StartDate={plan["start_date"]},EndDate={plan["end_date"]},DateType=1,{OPTIONS}'],
                as_of=plan["end_date"], symbols=[symbol], fields=EVENT_FIELDS), 1)


def failure_summary(dataset: ChoiceDataset, collector: ChoiceCollector, error: str) -> dict[str, Any]:
    summary = dataset.summary()
    summary.update({"status": "incomplete_resumable", "stage": collector.stage, "error": error,
                    "requests_this_run": collector.calls, "cached_requests": collector.cached, "generated_at": now_text()})
    encoded = canonical_json_bytes(summary)
    exclusive_atomic_publish(dataset.directory / f"progress-{sha256_hex(encoded)}.json", encoded, max_bytes=1024 * 1024)
    return summary
