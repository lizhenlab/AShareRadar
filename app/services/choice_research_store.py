"""Resumable research-only raw archive, SQLite projection and conservative budgets."""

from __future__ import annotations

from contextlib import AbstractContextManager
import fcntl
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, read_regular_file, sha256_hex
from app.services.choice_sdk import ChoiceError, safe_directory
from app.utils.clock import market_now


MAX_RAW_BYTES = 32 * 1024 * 1024
SCHEMA_VERSION = "choice-research-dataset-v1"
Record = tuple[str, str, str, dict[str, Any]]


def now_text() -> str:
    return market_now().isoformat(timespec="seconds")


def encoded_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _connect(path: Path, *, version: int) -> sqlite3.Connection:
    if path.is_symlink():
        raise ChoiceError("refusing symlink database")
    existed = path.exists()
    db = sqlite3.connect(path)
    actual = db.execute("PRAGMA user_version").fetchone()[0]
    if existed and actual != version:
        db.close()
        raise ChoiceError("refusing to modify an existing non-Choice database")
    db.execute(f"PRAGMA user_version={version}")
    db.execute("PRAGMA foreign_keys=ON")
    return db


class ChoiceBudget(AbstractContextManager["ChoiceBudget"]):
    """Shared process lease and durable reservations, including failed/uncertain calls.

    CSD/CSS reserve returned cells conservatively, not claimed vendor billing units.
    Server remaining minus ALL local reservations intentionally double-counts settled
    usage rather than trusting potentially delayed statistics. Missing quotas fail
    closed, except bounded calendar/sector discovery (not advertised as unlimited).
    """

    def __init__(self, directory: Path, *, csd_limit: int = 450000, css_limit: int = 200000, ctr_limit: int = 3,
                 sector_limit: int = 60) -> None:
        self.directory = safe_directory(directory)
        self.limits = {"EM_CSD": csd_limit, "EM_CSS": css_limit, "EM_CTR": ctr_limit, "sector": sector_limit, "tradedates": 6}
        if any(value < 0 for value in self.limits.values()):
            raise ChoiceError("negative Choice budget")
        if sector_limit > 600:
            raise ChoiceError("universe discovery must be bounded to at most 600 weekly requests")
        self.db: sqlite3.Connection | None = None
        self.fd: int | None = None
        self.quotas: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> ChoiceBudget:
        self.fd = os.open(self.directory / "session.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        ready = False
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = _connect(self.directory / "budget.sqlite3", version=7411)
            self.db.execute("CREATE TABLE IF NOT EXISTS reservations (period TEXT, function TEXT, units INTEGER, created_at TEXT)")
            self.db.commit()
            ready = True
        except Exception:
            raise ChoiceError("another Choice collector is running or the shared control directory is invalid") from None
        finally:
            if not ready:
                self.__exit__()
        return self

    def update(self, response: dict[str, Any]) -> None:
        columns = response["indicators"]
        quotas: dict[str, dict[str, Any]] = {}
        for values in response["data"].values():
            row = dict(zip(columns, values, strict=True))
            function = row["FUNCENAME"]
            if function == "EM_CTR" and row["SECUTYPE"] != "分红送转":
                continue
            if function in quotas:
                raise ChoiceError("multiple quota packages need explicit accounting before collection")
            quotas[function] = row
        self.quotas = quotas

    def reserve(self, method: str, units: int) -> None:
        assert self.db is not None
        function = {"csd": "EM_CSD", "css": "EM_CSS", "ctr": "EM_CTR"}.get(method, method)
        today = market_now().date()
        period = today.strftime("%G-W%V")
        quota = self.quotas.get(function)
        if quota:
            if not str(quota["STARTDATE"]) <= today.isoformat() <= str(quota["ENDDATE"]):
                raise ChoiceError("quota period is stale; refresh account statistics")
            if str(quota.get("EFFECTIVEDATE", ""))[:10] < today.isoformat():
                raise ChoiceError("Choice package has expired")
            period = f'{quota["STARTDATE"]}/{quota["ENDDATE"]}'
        elif function not in {"sector", "tradedates"}:
            raise ChoiceError(f"missing confirmed quota for {function}; collection stopped")
        used = self.db.execute("SELECT COALESCE(SUM(units),0) FROM reservations WHERE period=? AND function=?", (period, function)).fetchone()[0]
        limit = self.limits[function]
        remaining = min(limit, int(quota["AVAILABEDATA"])) if quota else limit
        if units <= 0 or units + used > remaining:
            raise ChoiceError(f"budget pause: {function}, estimated_reserved={used}, next={units}, safe_limit={remaining}; completed requests are resumable")
        with self.db:
            self.db.execute("INSERT INTO reservations VALUES (?,?,?,?)", (period, function, units, now_text()))

    def __exit__(self, *_: object) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ChoiceDataset(AbstractContextManager["ChoiceDataset"]):
    @classmethod
    def open_readonly(cls, directory: Path) -> ChoiceDataset:
        """Open an existing archive without creating files or changing its schema."""
        if not path_has_only_trusted_aliases(directory / "choice_research.sqlite3"):
            raise ChoiceError("refusing symlink dataset")
        plan = decode_json_bytes(read_regular_file(directory / "plan.json", max_bytes=1024 * 1024))
        if not isinstance(plan, dict):
            raise ChoiceError("invalid Choice plan")
        dataset = object.__new__(cls)
        dataset.directory, dataset.plan = directory.resolve(), plan
        uri = (dataset.directory / "choice_research.sqlite3").as_uri() + "?mode=ro"
        dataset.db = sqlite3.connect(uri, uri=True)
        dataset.db.execute("PRAGMA query_only=ON")
        if dataset.db.execute("PRAGMA user_version").fetchone()[0] != 7412:
            dataset.db.close()
            raise ChoiceError("not a Choice research database")
        return dataset

    def __init__(self, directory: Path, plan: dict[str, Any]) -> None:
        self.directory = safe_directory(directory)
        self.plan = plan
        exclusive_atomic_publish(self.directory / "plan.json", canonical_json_bytes(plan), max_bytes=1024 * 1024)
        self.db = _connect(self.directory / "choice_research.sqlite3", version=7412)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                request_key TEXT PRIMARY KEY, descriptor_json TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL, captured_at TEXT NOT NULL,
                records_digest TEXT NOT NULL, record_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                request_key TEXT NOT NULL REFERENCES requests(request_key),
                ordinal INTEGER NOT NULL, kind TEXT NOT NULL, symbol TEXT NOT NULL,
                as_of TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(request_key, ordinal)
            );
            CREATE INDEX IF NOT EXISTS choice_kind_symbol_date ON records(kind,symbol,as_of);
            CREATE VIEW IF NOT EXISTS daily_bars AS
                SELECT symbol,as_of AS session_date,
                    json_extract(payload_json,'$.OPEN') AS open,
                    json_extract(payload_json,'$.HIGH') AS high,
                    json_extract(payload_json,'$.LOW') AS low,
                    json_extract(payload_json,'$.CLOSE') AS close,
                    json_extract(payload_json,'$.VOLUME') AS volume_shares,
                    json_extract(payload_json,'$.AMOUNT') AS amount_yuan,
                    json_extract(payload_json,'$.TRADESTATUS') AS trade_status,
                    json_extract(payload_json,'$.ISSTSTOCK') AS is_st,
                    json_extract(payload_json,'$.ISXSTSTOCK') AS is_star_st,
                    json_extract(payload_json,'$.quality') AS quality,
                    payload_json,request_key FROM records WHERE kind='daily';
            CREATE VIEW IF NOT EXISTS universe_membership AS
                SELECT symbol,as_of AS snapshot_date,payload_json,request_key FROM records WHERE kind='universe';
            CREATE VIEW IF NOT EXISTS metadata_snapshots AS
                SELECT symbol,as_of AS snapshot_date,
                    json_extract(payload_json,'$.HISNAME') AS historical_name,
                    json_extract(payload_json,'$.TRADESTATUS') AS trade_status,
                    json_extract(payload_json,'$.SUSPENDREASON') AS suspend_reason,
                    json_extract(payload_json,'$.PRECLOSEEXCH') AS previous_close_reference,
                    json_extract(payload_json,'$.LIMITUPPRICE') AS limit_up_price,
                    json_extract(payload_json,'$.LIMITDOWNPRICE') AS limit_down_price,
                    payload_json,request_key FROM records WHERE kind='metadata';
            CREATE VIEW IF NOT EXISTS dividend_reports AS
                SELECT symbol,as_of AS report_date,payload_json,request_key FROM records WHERE kind='dividend_snapshot';
            CREATE VIEW IF NOT EXISTS dividend_events AS
                SELECT symbol,as_of AS ex_date,payload_json,request_key FROM records WHERE kind='dividend_event';
            CREATE VIEW IF NOT EXISTS execution_references AS
                SELECT symbol,as_of AS session_date,
                    json_extract(payload_json,'$.PRECLOSEEXCH') AS previous_close_reference,
                    json_extract(payload_json,'$.LIMITUPPRICE') AS limit_up_price,
                    json_extract(payload_json,'$.LIMITDOWNPRICE') AS limit_down_price,
                    payload_json,request_key FROM records WHERE kind='execution_reference';
            CREATE VIEW IF NOT EXISTS suspension_details AS
                SELECT symbol,as_of AS session_date,payload_json,request_key FROM records WHERE kind='suspension_detail';
        """)
        self.db.commit()

    def __enter__(self) -> ChoiceDataset:
        return self

    @staticmethod
    def key(descriptor: dict[str, Any]) -> str:
        return sha256_hex(canonical_json_bytes(descriptor))

    def cached(self, descriptor: dict[str, Any]) -> dict[str, Any] | None:
        key = self.key(descriptor)
        path = self.directory / "raw" / f"{key}.json"
        exists_in_db = self.db.execute("SELECT raw_sha256 FROM requests WHERE request_key=?", (key,)).fetchone()
        if not path.exists():
            if exists_in_db:
                raise ChoiceError("completed Choice request is missing its raw archive")
            return None
        raw = read_regular_file(path, max_bytes=MAX_RAW_BYTES)
        value: Any = decode_json_bytes(raw)
        payload = value["payload"]
        if value["sha256"] != sha256_hex(canonical_json_bytes(payload)) or payload["request"] != descriptor:
            raise ChoiceError("Choice raw archive digest/request mismatch")
        if exists_in_db and exists_in_db[0] != sha256_hex(raw):
            raise ChoiceError("Choice raw archive changed after checkpoint")
        return payload

    def archive(self, descriptor: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        payload = {"request": descriptor, "captured_at": now_text(), "source": "Choice EMQuantAPI", "result": result}
        raw = canonical_json_bytes({"payload": payload, "sha256": sha256_hex(canonical_json_bytes(payload))})
        exclusive_atomic_publish(self.directory / "raw" / f"{self.key(descriptor)}.json", raw, max_bytes=MAX_RAW_BYTES)
        return payload

    def project(self, payload: dict[str, Any], records: list[Record]) -> None:
        descriptor = payload["request"]
        key = self.key(descriptor)
        existing = self.db.execute("SELECT records_digest,record_count FROM requests WHERE request_key=?", (key,)).fetchone()
        digest = sha256_hex(canonical_json_bytes([list(record) for record in records]))
        if existing:
            rows = self.db.execute("SELECT kind,symbol,as_of,payload_json FROM records WHERE request_key=? ORDER BY ordinal", (key,)).fetchall()
            actual = [[kind, symbol, as_of, decode_json_bytes(value.encode())] for kind, symbol, as_of, value in rows]
            if existing[0] != digest or existing[1] != len(records) or sha256_hex(canonical_json_bytes(actual)) != digest:
                raise ChoiceError("Choice normalized records differ from raw replay")
            return
        raw = read_regular_file(self.directory / "raw" / f"{key}.json", max_bytes=MAX_RAW_BYTES)
        with self.db:
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?)", (
                key, encoded_text(descriptor), sha256_hex(raw), payload["captured_at"], digest, len(records),
            ))
            self.db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?)", [
                (key, i, kind, symbol, as_of, encoded_text(record))
                for i, (kind, symbol, as_of, record) in enumerate(records)
            ])

    def verify(self, normalize: Callable[[dict[str, Any]], list[Record]]) -> dict[str, Any]:
        for (descriptor_json,) in self.db.execute("SELECT descriptor_json FROM requests ORDER BY request_key").fetchall():
            descriptor = decode_json_bytes(descriptor_json.encode())
            if not isinstance(descriptor, dict):
                raise ChoiceError("invalid Choice request descriptor")
            payload = self.cached(descriptor)
            assert payload is not None
            self.project(payload, normalize(payload))
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ChoiceError("Choice dataset SQLite check failed")
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ChoiceError("Choice records have missing request provenance")
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "official": False, "formal_equivalent_pit": False,
            "filter_qualified": False, "production_ranking_effect": "none",
            "database": str(self.directory / "choice_research.sqlite3"),
            "requests_completed": self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
            "record_counts": dict(self.db.execute("SELECT kind,COUNT(*) FROM records GROUP BY kind")),
            "daily_market_counts": dict(self.db.execute("SELECT substr(symbol,-2),COUNT(DISTINCT symbol) FROM daily_bars GROUP BY 1")),
            "daily_quality_counts": dict(self.db.execute("SELECT quality,COUNT(*) FROM daily_bars GROUP BY quality")),
            "daily_date_range": list(self.db.execute("SELECT MIN(session_date),MAX(session_date) FROM daily_bars").fetchone()),
        }

    def __exit__(self, *_: object) -> None:
        self.db.close()
