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
from app.services.choice_quota import public_quotas, quota_query_args, sanitized_quota_response
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
        self.required_paid_functions: set[str] = set()
        self._remaining_paid_requests: dict[str, str] | None = None
        self._last_quota_refresh: float | None = None

    def refresh_quota(self) -> None:
        # Account queries have their own rate limit. The durable reservation ledger
        # still guards every data request while a recent quota snapshot is reused.
        if self._last_quota_refresh is not None and time.monotonic() - self._last_quota_refresh < 30:
            return
        response = self.client.request("datastatistics", quota_query_args(market_now().date()))
        self.budget.update(response, today=market_now().date())
        self._last_quota_refresh = time.monotonic()
        encoded = canonical_json_bytes({"queried_at": now_text(), "result": sanitized_quota_response(self.budget.quotas)})
        exclusive_atomic_publish(self.dataset.directory / "account" / f"{sha256_hex(encoded)}.json", encoded, max_bytes=1024 * 1024)

    def fetch(self, descriptor: dict[str, Any], units: int) -> list[Record]:
        self.stage = descriptor["kind"]
        payload = self.dataset.cached(descriptor)
        if payload is None:
            required = (set(self._remaining_paid_requests.values())
                        if self._remaining_paid_requests is not None else self.required_paid_functions)
            if required:
                self.budget.require_current(required)
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
            if self._remaining_paid_requests is not None:
                self._remaining_paid_requests.pop(self.dataset.key(descriptor), None)
        else:
            self.cached += 1
        records = normalize(payload)
        self.dataset.project(payload, records)
        self.progress({"stage": self.stage, "requests_this_run": self.calls, "cached_requests": self.cached, "records": len(records)})
        return records

    def run(self) -> dict[str, Any]:
        plan = self.dataset.plan
        self.required_paid_functions = {"EM_CSD", "EM_CSS"}
        if plan["event_symbols"]:
            self.required_paid_functions.add("EM_CTR")
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
        paid_work = [*self._metadata(symbols, dates), *self._dividends(symbols, plan),
                     *self._daily(symbols, sessions), *self._events(plan)]
        paid = {"csd": "EM_CSD", "css": "EM_CSS", "ctr": "EM_CTR"}
        self._remaining_paid_requests = {
            self.dataset.key(descriptor): paid[descriptor["method"]]
            for descriptor, _ in paid_work if self.dataset.cached(descriptor) is None
        }
        for descriptor, units in paid_work:
            self.fetch(descriptor, units)
        self.refresh_quota()
        summary = self.dataset.verify(normalize)
        summary.update({
            "status": "complete_for_declared_research_scope", "generated_at": now_text(),
            "requests_this_run": self.calls, "cached_requests": self.cached,
            "selected_symbols": symbols, "calendar_session_count": len(sessions),
            "monthly_universe_snapshot_count": len(dates), "raw_replay_verified": True,
            "limitations": plan["limitations"], "quota_snapshot": public_quotas(self.budget.quotas),
        })
        encoded = canonical_json_bytes(summary)
        path = self.dataset.directory / f"summary-{sha256_hex(encoded)}.json"
        exclusive_atomic_publish(path, encoded, max_bytes=1024 * 1024)
        summary["summary_path"] = str(path)
        return summary

    def _metadata(self, symbols: list[str], dates: list[str]) -> list[tuple[dict[str, Any], int]]:
        return [(request("metadata", "css", [",".join(symbols), ",".join(METADATA_FIELDS),
                f"TradeDate={day},EndDate={day},AdjustFlag=1,{OPTIONS}"], as_of=day, symbols=symbols, fields=METADATA_FIELDS),
                len(symbols) * len(METADATA_FIELDS)) for day in dates]

    def _dividends(self, symbols: list[str], plan: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
        return [(request("dividend_snapshot", "css", [",".join(symbols), ",".join(DIVIDEND_FIELDS),
                f"ReportDate={report},AssignFeature=4,YesNo=1,{OPTIONS}"], as_of=report, symbols=symbols, fields=DIVIDEND_FIELDS),
                len(symbols) * len(DIVIDEND_FIELDS)) for report in report_dates(plan["start_date"], plan["end_date"])]

    def _daily(self, symbols: list[str], sessions: list[str]) -> list[tuple[dict[str, Any], int]]:
        work = []
        for offset in range(0, len(symbols), 20):
            batch = symbols[offset:offset + 20]
            work.append((request("daily", "csd", [",".join(batch), ",".join(DAILY_FIELDS), sessions[0], sessions[-1],
                f"Period=1,AdjustFlag=1,FillData=0,Order=1,{OPTIONS}"], symbols=batch, fields=DAILY_FIELDS, sessions=sessions),
                len(batch) * len(sessions) * len(DAILY_FIELDS)))
        return work

    def _events(self, plan: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
        return [(request("dividend_event", "ctr", ["DividendImplementationInfo", ",".join(EVENT_FIELDS),
                f'secucode={symbol},StartDate={plan["start_date"]},EndDate={plan["end_date"]},DateType=1,{OPTIONS}'],
                as_of=plan["end_date"], symbols=[symbol], fields=EVENT_FIELDS), 1)
                for symbol in plan["event_symbols"]]


def failure_summary(dataset: ChoiceDataset, collector: ChoiceCollector, error: str) -> dict[str, Any]:
    summary = dataset.summary()
    summary.update({"status": "incomplete_resumable", "stage": collector.stage, "error": error,
                    "requests_this_run": collector.calls, "cached_requests": collector.cached, "generated_at": now_text()})
    encoded = canonical_json_bytes(summary)
    exclusive_atomic_publish(dataset.directory / f"progress-{sha256_hex(encoded)}.json", encoded, max_bytes=1024 * 1024)
    return summary
