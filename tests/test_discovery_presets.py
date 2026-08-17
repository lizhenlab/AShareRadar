from __future__ import annotations

import hashlib
import sqlite3

from pydantic import ValidationError
import pytest

from app.db.schema import initialize_schema
from app.db.schema_definitions import SCHEMA_SQL
from app.db.market_scan_action_source import MarketScanActionSourceError
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    seal_market_scan_snapshot,
)
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.models.discovery import (
    DiscoveryCriteria,
    DiscoveryLeaderboardItem,
    DiscoveryLeaderboardPage,
    DiscoveryPresetArchive,
    DiscoveryPresetCreate,
    DiscoveryPresetPage,
    DiscoveryPresetRename,
    DiscoveryPresetUpdate,
    DiscoveryResearchQueueRequest,
    DiscoveryResearchQueueItem,
    DiscoveryResearchQueueResponse,
)
from app.repositories.discovery import DiscoveryRepository
from app.repositories.discovery_sql import canonical_json
from app.services.discovery import DiscoveryConflictError, DiscoveryImportError, DiscoveryService
from app.utils.errors import NotFoundError
from tests.market_scan_test_support import (
    action_pass_publication_diagnostics,
    distribution_degraded_publication_diagnostics,
)


_LEGACY_DISCOVERY_SCHEMA_SQL = """
CREATE TABLE discovery_preset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    criteria_json TEXT NOT NULL CHECK (json_valid(criteria_json)),
    sort_json TEXT NOT NULL CHECK (json_valid(sort_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE discovery_research_queue_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    source_run_id INTEGER NOT NULL CHECK (source_run_id > 0),
    source_preset_id INTEGER NOT NULL CHECK (source_preset_id > 0),
    source_preset_revision INTEGER NOT NULL CHECK (source_preset_revision > 0),
    source_preset_name TEXT NOT NULL,
    preset_schema_version INTEGER NOT NULL CHECK (preset_schema_version = 1),
    preset_snapshot_json TEXT NOT NULL CHECK (json_valid(preset_snapshot_json)),
    enqueued_at TEXT NOT NULL,
    FOREIGN KEY (symbol) REFERENCES watchlist(symbol) ON DELETE CASCADE,
    UNIQUE (symbol, source_run_id, source_preset_id, source_preset_revision)
);
"""


def test_preset_crud_and_application_are_persisted_and_paginated(tmp_path) -> None:
    path, service = _service(tmp_path)
    run_id = _seed_run(
        path,
        rule_version="discovery-v1",
        rows=[
            _result("600001.SH", rank=1, market="SH", score=92, quality=90, amount=1_000_000_000),
            _result("000002.SZ", rank=2, market="SZ", score=88, quality=80, amount=2_000_000_000),
            _result("920003.BJ", rank=3, market="BJ", score=96, quality=95, amount=3_000_000_000),
            _result("600004.SH", rank=4, market="SH", score=91, quality=90, is_st=True),
            _result("600005.SH", rank=5, market="SH", score=90, quality=90, industry="白酒"),
        ],
    )

    created = service.create_preset(_preset_payload())
    page = service.list_presets(page=1, page_size=20)
    result = service.apply_preset(created.id, run_id=run_id, page=2, page_size=1)

    assert created.revision == 1
    assert page.total == 1
    assert page.items == [created]
    assert result.total == 2
    assert result.page_count == 2
    assert [item.symbol for item in result.items] == ["000002.SZ"]
    assert result.items[0].position == 2
    assert result.preset.id == created.id
    assert result.preset.revision == 1
    assert result.run_id == run_id
    assert result.rule_version == "discovery-v1"

    updated = service.update_preset(
        created.id,
        DiscoveryPresetUpdate(
            name="高质量半导体增强版",
            criteria={"market": ["BJ"], "score": {"min": 95}},
            sort=[{"field": "amount", "order": "desc"}],
            expected_revision=1,
        ),
    )
    assert updated.revision == 2
    assert updated.criteria.market == ["BJ"]
    assert updated.criteria.score is not None and updated.criteria.score.min == 95
    assert updated.sort[0].field == "amount"

    renamed = service.rename_preset(
        created.id,
        DiscoveryPresetRename(name="高质量半导体", expected_revision=2),
    )
    assert renamed.name == "高质量半导体"
    assert renamed.revision == 3

    with pytest.raises(DiscoveryConflictError, match="修订"):
        service.rename_preset(
            created.id,
            DiscoveryPresetRename(name="过期写入", expected_revision=2),
        )
    with pytest.raises(DiscoveryConflictError, match="修订"):
        service.delete_preset(created.id, expected_revision=2)

    service.delete_preset(created.id, expected_revision=3)
    with pytest.raises(NotFoundError, match="筛选方案不存在"):
        service.get_preset(created.id)


def test_preset_names_are_unique_case_insensitively(tmp_path) -> None:
    _path, service = _service(tmp_path)
    service.create_preset(_preset_payload(name="Alpha"))

    with pytest.raises(DiscoveryConflictError, match="名称已存在"):
        service.create_preset(_preset_payload(name="alpha"))


def test_partial_scan_cannot_authorize_discovery_or_research_queue(tmp_path) -> None:
    path, service = _service(tmp_path)
    run_id = _seed_run(
        path,
        rule_version="discovery-partial-v1",
        rows=[_result("600001.SH", rank=1, market="SH", score=92, quality=90)],
        scope="TOP100",
    )
    preset = service.create_preset(_preset_payload())

    with pytest.raises(MarketScanSnapshotSealError, match="完整全市场"):
        service.apply_preset(preset.id, run_id=run_id, page=1, page_size=20)
    with pytest.raises(MarketScanSnapshotSealError, match="完整全市场"):
        service.enqueue_research(
            preset.id,
            DiscoveryResearchQueueRequest(
                run_id=run_id,
                expected_preset_revision=preset.revision,
                symbols=["600001.SH"],
            ),
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM discovery_research_queue_source"
        ).fetchone()[0] == 0


def test_distribution_degraded_scan_is_browsable_but_cannot_enqueue_research(
    tmp_path,
) -> None:
    path, service = _service(tmp_path)
    run_id = _seed_run(
        path,
        rule_version="discovery-distribution-degraded-v1",
        rows=[_result("600001.SH", rank=1, market="SH", score=92, quality=90)],
        action_eligible=False,
    )
    preset = service.create_preset(_preset_payload())

    page = service.apply_preset(preset.id, run_id=run_id, page=1, page_size=20)
    assert [item.symbol for item in page.items] == ["600001.SH"]
    with pytest.raises(MarketScanActionSourceError, match="评分分布门禁"):
        service.enqueue_research(
            preset.id,
            DiscoveryResearchQueueRequest(
                run_id=run_id,
                expected_preset_revision=preset.revision,
                symbols=["600001.SH"],
            ),
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM discovery_research_queue_source"
        ).fetchone()[0] == 0


def test_discovery_response_pages_positions_and_queue_counts_fail_closed(tmp_path) -> None:
    _path, service = _service(tmp_path)
    preset = service.create_preset(_preset_payload())
    with pytest.raises(ValidationError, match="当前分页"):
        DiscoveryPresetPage(
            items=[preset],
            total=2,
            page=1,
            page_size=2,
            page_count=1,
        )

    first = DiscoveryLeaderboardItem(
        position=1,
        source_rank=1,
        symbol="600001.SH",
        code="600001",
        market="SH",
        name="样本一",
        is_st=False,
        is_new=False,
        quality=90,
        trend=85,
        change=2,
        turnover=1,
        amount=100_000_000,
        score=90,
        raw_score=90.1,
    )
    second = first.model_copy(
        update={"position": 3, "source_rank": 2, "symbol": "600002.SH", "code": "600002", "name": "样本二"}
    )
    with pytest.raises(ValidationError, match="position"):
        DiscoveryLeaderboardPage(
            preset=preset,
            run_id=1,
            rule_version="v1",
            items=[first, second],
            total=2,
            page=1,
            page_size=2,
            page_count=1,
        )

    queued = DiscoveryResearchQueueItem(
        symbol="600001.SH",
        source_run_id=1,
        source_preset_id=preset.id,
        source_preset_revision=preset.revision,
        source_preset_name=preset.name,
        enqueued_at="2026-08-13T10:00:00Z",
        added=True,
    )
    with pytest.raises(ValidationError, match="计数"):
        DiscoveryResearchQueueResponse(items=[queued], added_count=0, existing_count=1)


def test_preset_industry_filter_matches_ordinary_leaderboard_semantics_and_escapes_like(tmp_path) -> None:
    path, service = _service(tmp_path)
    run_id = _seed_run(
        path,
        rule_version="discovery-v1",
        rows=[
            _result("600001.SH", rank=1, market="SH", score=92, quality=90, industry="功率半导体"),
            _result("000002.SZ", rank=2, market="SZ", score=90, quality=90, industry="银行"),
        ],
    )
    partial = service.create_preset(
        _preset_payload(name="行业部分匹配").model_copy(
            update={"criteria": DiscoveryCriteria(industry=["半导体"])}
        )
    )
    escaped = service.create_preset(
        _preset_payload(name="转义通配符").model_copy(
            update={"criteria": DiscoveryCriteria(industry=["%"])}
        )
    )

    assert [item.symbol for item in service.apply_preset(partial.id, run_id=run_id, page=1, page_size=20).items] == [
        "600001.SH"
    ]
    assert service.apply_preset(escaped.id, run_id=run_id, page=1, page_size=20).items == []


@pytest.mark.parametrize(
    "criteria",
    [
        {"keyword": "600519\n贵州茅台"},
        {"is_st": "false"},
        {"market": ["SH", "HK"]},
        {"market": ["SH", "SH"]},
        {"industry": []},
        {"industry": ["x" * 81]},
        {"quality": {"min": -1}},
        {"quality": {"min": 90, "max": 80}},
        {"change": {"min": float("nan")}},
        {"turnover": {"min": -0.01}},
        {"amount": {"max": 10_000_000_000_000_001}},
    ],
)
def test_criteria_reject_unknown_unsafe_or_out_of_range_values(criteria: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DiscoveryCriteria.model_validate(criteria)


@pytest.mark.parametrize(
    "sort",
    [
        [{"field": "unknown", "order": "asc"}],
        [{"field": "score", "order": "sideways"}],
        [{"field": "score", "order": "desc"}, {"field": "score", "order": "asc"}],
        [],
        [
            {"field": "score", "order": "desc"},
            {"field": "amount", "order": "desc"},
            {"field": "quality", "order": "desc"},
            {"field": "trend", "order": "desc"},
        ],
    ],
)
def test_sort_contract_is_whitelisted_bounded_and_unambiguous(sort: list[dict[str, str]]) -> None:
    payload = _preset_payload().model_dump()
    payload["sort"] = sort

    with pytest.raises(ValidationError):
        DiscoveryPresetCreate.model_validate(payload)


def test_export_import_is_versioned_checksummed_and_rejects_tampering(tmp_path) -> None:
    _source_path, source = _service(tmp_path / "source")
    _target_path, target = _service(tmp_path / "target")
    preset = source.create_preset(_preset_payload(name="可移植方案"))

    archive = source.export_preset(preset.id)
    imported = target.import_preset(archive)

    assert archive.format == "ashare-radar.discovery-preset"
    assert archive.schema_version == 2
    assert archive.checksum_algorithm == "sha256"
    assert len(archive.checksum) == 64
    assert imported.name == "可移植方案"
    assert imported.criteria == preset.criteria
    assert imported.sort == preset.sort

    tampered_data = archive.model_dump(mode="json")
    tampered_data["preset"]["criteria"]["score"]["min"] = 1
    tampered = DiscoveryPresetArchive.model_validate(tampered_data)
    with pytest.raises(DiscoveryImportError, match="校验和"):
        _service(tmp_path / "tampered")[1].import_preset(tampered)

    unsupported_data = archive.model_dump(mode="json")
    unsupported_data["schema_version"] = 3
    unsupported = DiscoveryPresetArchive.model_validate(unsupported_data)
    with pytest.raises(DiscoveryImportError, match="版本"):
        _service(tmp_path / "unsupported")[1].import_preset(unsupported)


def test_legacy_v1_sqlite_presets_migrate_without_losing_queue_provenance(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(_LEGACY_DISCOVERY_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO watchlist (symbol, code, market, name, created_at, updated_at)
            VALUES ('600001.SH', '600001', 'SH', '旧方案样本', '2026-08-12T00:00:00Z', '2026-08-12T00:00:00Z')
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO discovery_preset (
                name, schema_version, revision, criteria_json, sort_json, created_at, updated_at
            ) VALUES ('旧版方案', 1, 3, ?, ?, '2026-08-12T00:00:00Z', '2026-08-12T00:00:00Z')
            """,
            ('{"market":["SH"]}', '[{"field":"rank","order":"asc"}]'),
        )
        preset_id = int(cursor.lastrowid or 0)
        conn.execute(
            """
            INSERT INTO discovery_research_queue_source (
                symbol, source_run_id, source_preset_id, source_preset_revision,
                source_preset_name, preset_schema_version, preset_snapshot_json, enqueued_at
            ) VALUES ('600001.SH', 9, ?, 3, '旧版方案', 1, '{}', '2026-08-12T00:00:00Z')
            """,
            (preset_id,),
        )
        initialize_schema(conn)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(discovery_preset)")}
        alert_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'discovery_screen_alert_event'"
        ).fetchone()
        alert_foreign_keys = {
            str(row[2]) for row in conn.execute("PRAGMA foreign_key_list(discovery_screen_alert_event)")
        }
        queued = conn.execute(
            "SELECT preset_schema_version, source_preset_revision FROM discovery_research_queue_source"
        ).fetchone()

    migrated = DiscoveryRepository(path).preset(preset_id)
    assert "column_view" in columns
    assert alert_table == (1,)
    assert alert_foreign_keys == {"discovery_preset", "market_scan_run"}
    assert migrated.schema_version == 1
    assert migrated.column_view == "overview"
    assert migrated.revision == 3
    assert migrated.criteria.market == ["SH"]
    assert queued == (1, 3)


def test_legacy_v1_archive_checksum_remains_importable(tmp_path) -> None:
    preset_payload = _preset_payload(name="旧版归档").model_dump(mode="json")
    preset_payload.pop("column_view")
    for field in ("confidence", "risk", "tradability", "keyword"):
        preset_payload["criteria"].pop(field)
    checksum_payload = canonical_json(
        {
            "format": "ashare-radar.discovery-preset",
            "schema_version": 1,
            "preset": preset_payload,
        }
    )
    archive = DiscoveryPresetArchive.model_validate(
        {
            "format": "ashare-radar.discovery-preset",
            "schema_version": 1,
            "checksum_algorithm": "sha256",
            "checksum": hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest(),
            "exported_at": "2026-08-12T08:00:00+08:00",
            "preset": preset_payload,
        }
    )

    imported = _service(tmp_path / "legacy-archive")[1].import_preset(archive)

    assert imported.schema_version == 2
    assert imported.name == "旧版归档"
    assert imported.column_view == "overview"

    injected = archive.model_copy(
        update={
            "preset": archive.preset.model_copy(
                update={"column_view": "research"}
            )
        }
    )
    with pytest.raises(DiscoveryImportError, match="v2 字段"):
        _service(tmp_path / "legacy-injected")[1].import_preset(injected)


def test_enqueue_research_is_atomic_idempotent_and_keeps_source_snapshot(tmp_path) -> None:
    path, service = _service(tmp_path)
    run_id = _seed_run(
        path,
        rule_version="discovery-v1",
        rows=[
            _result("600001.SH", rank=1, market="SH", score=92, quality=90),
            _result("600002.SH", rank=2, market="SH", score=70, quality=50),
        ],
    )
    preset = service.create_preset(_preset_payload(name="队列来源"))
    request = DiscoveryResearchQueueRequest(
        run_id=run_id,
        expected_preset_revision=1,
        symbols=["600001.SH"],
    )

    first = service.enqueue_research(preset.id, request)
    second = service.enqueue_research(preset.id, request)

    assert first.added_count == 1
    assert first.existing_count == 0
    assert first.items[0].source_run_id == run_id
    assert first.items[0].source_preset_id == preset.id
    assert first.items[0].source_preset_revision == 1
    assert first.items[0].source_preset_name == "队列来源"
    assert second.added_count == 0
    assert second.existing_count == 1

    with sqlite3.connect(path) as conn:
        queue_row = conn.execute(
            "SELECT research_status FROM watchlist WHERE symbol = ?",
            ("600001.SH",),
        ).fetchone()
        source_row = conn.execute(
            """
            SELECT source_run_id, source_preset_id, source_preset_revision,
                   source_preset_name, preset_snapshot_json
            FROM discovery_research_queue_source
            WHERE symbol = ?
            """,
            ("600001.SH",),
        ).fetchone()
    assert queue_row == ("to_research",)
    assert source_row[:4] == (run_id, preset.id, 1, "队列来源")
    assert '"score"' in source_row[4]

    with pytest.raises(ValueError, match="不属于当前榜单"):
        service.enqueue_research(
            preset.id,
            request.model_copy(update={"symbols": ["600002.SH"]}),
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE symbol = ?",
            ("600002.SH",),
        ).fetchone()[0] == 0

    service.rename_preset(
        preset.id,
        DiscoveryPresetRename(name="队列来源-已重命名", expected_revision=1),
    )
    with sqlite3.connect(path) as conn:
        preserved = conn.execute(
            """
            SELECT source_run_id, source_preset_id, source_preset_name, preset_snapshot_json
            FROM discovery_research_queue_source
            """
        ).fetchone()
    assert preserved[:3] == (run_id, preset.id, "队列来源")
    assert '"name":"队列来源"' in preserved[3]
    assert "已重命名" not in preserved[3]

    service.delete_preset(preset.id, expected_revision=2)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM market_scan_run WHERE id = ?", (run_id,))
        after_cleanup = conn.execute(
            """
            SELECT source_run_id, source_preset_id, source_preset_name, preset_snapshot_json
            FROM discovery_research_queue_source
            """
        ).fetchone()
    assert after_cleanup == preserved


def test_queue_source_keeps_run_and_preset_as_detached_audit_values(tmp_path) -> None:
    path, _service_instance = _service(tmp_path)
    timestamp = "2026-07-28T01:00:00.000000Z"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        foreign_tables = {
            row[2]
            for row in conn.execute("PRAGMA foreign_key_list(discovery_research_queue_source)")
        }
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, research_status, created_at, updated_at
            ) VALUES ('600519.SH', '600519', 'SH', '贵州茅台', 'to_research', ?, ?)
            """,
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO discovery_research_queue_source (
                symbol, source_run_id, source_preset_id, source_preset_revision,
                source_preset_name, preset_schema_version, preset_snapshot_json,
                enqueued_at
            ) VALUES ('600519.SH', 424242, 31337, 4, '已删除方案', 1, '{}', ?)
            """,
            (timestamp,),
        )

    assert foreign_tables == {"watchlist"}
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT source_run_id, source_preset_id FROM discovery_research_queue_source"
        ).fetchone() == (424242, 31337)


def test_enqueue_preserves_holding_research_and_user_managed_metadata(tmp_path) -> None:
    path, service = _service(tmp_path)
    run_id = _seed_run(
        path,
        rule_version="discovery-v1",
        rows=[_result("600001.SH", rank=1, market="SH", score=92, quality=90)],
    )
    preset = service.create_preset(_preset_payload())
    created_at = "2026-07-01T01:00:00.000000Z"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, note, group_name, pinned,
                research_status, priority, next_review_date,
                created_at, updated_at
            ) VALUES (
                '600001.SH', '600001', 'SH', '原名称', '用户笔记', '持仓组', 1,
                'holding_research', 'high', '2026-08-01', ?, ?
            )
            """,
            (created_at, created_at),
        )

    service.enqueue_research(
        preset.id,
        DiscoveryResearchQueueRequest(
            run_id=run_id,
            expected_preset_revision=1,
            symbols=["600001.SH"],
        ),
    )

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT research_status, note, group_name, pinned, priority,
                   next_review_date, created_at
            FROM watchlist WHERE symbol = '600001.SH'
            """
        ).fetchone()
    assert row == (
        "holding_research",
        "用户笔记",
        "持仓组",
        1,
        "high",
        "2026-08-01",
        created_at,
    )


@pytest.mark.parametrize("existing_status", ["watching", "excluded"])
def test_enqueue_promotes_weaker_existing_states_to_research(tmp_path, existing_status: str) -> None:
    path, service = _service(tmp_path)
    run_id = _seed_run(
        path,
        rule_version="discovery-v1",
        rows=[_result("600001.SH", rank=1, market="SH", score=92, quality=90)],
    )
    preset = service.create_preset(_preset_payload())
    timestamp = "2026-07-01T01:00:00.000000Z"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, research_status, created_at, updated_at
            ) VALUES ('600001.SH', '600001', 'SH', '样本', ?, ?, ?)
            """,
            (existing_status, timestamp, timestamp),
        )

    service.enqueue_research(
        preset.id,
        DiscoveryResearchQueueRequest(
            run_id=run_id,
            expected_preset_revision=1,
            symbols=["600001.SH"],
        ),
    )

    with sqlite3.connect(path) as conn:
        status = conn.execute(
            "SELECT research_status FROM watchlist WHERE symbol = '600001.SH'"
        ).fetchone()[0]
    assert status == "to_research"


def _service(root) -> tuple[object, DiscoveryService]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "runtime.sqlite3"
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    return path, DiscoveryService(DiscoveryRepository(path))


def _preset_payload(name: str = "半导体强势股") -> DiscoveryPresetCreate:
    return DiscoveryPresetCreate.model_validate(
        {
            "name": name,
            "criteria": {
                "market": ["SH", "SZ"],
                "industry": ["半导体"],
                "is_st": False,
                "quality": {"min": 80},
                "score": {"min": 85},
            },
            "sort": [
                {"field": "score", "order": "desc"},
                {"field": "amount", "order": "desc"},
            ],
        }
    )


def _seed_run(
    path,
    *,
    rule_version: str,
    rows: list[dict[str, object]],
    scope: str = MARKET_SCAN_FULL_MARKET_SCOPE,
    action_eligible: bool = True,
) -> int:
    timestamp = "2026-07-28T01:00:00.000000Z"
    with sqlite3.connect(path) as conn:
        run_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, rule_version, as_of, data_date, scope,
                publication_diagnostics_json, created_at, updated_at, finished_at
            ) VALUES ('success', 'manual', ?, '2026-07-28 09:00:00', '2026-07-28',
                      ?, ?, ?, ?, ?)
            """,
            (
                rule_version,
                scope,
                (
                    action_pass_publication_diagnostics()
                    if action_eligible
                    else distribution_degraded_publication_diagnostics()
                ).model_dump_json(),
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        assert run_id is not None
        for row in rows:
            conn.execute(
                """
                INSERT INTO market_scan_result (
                    run_id, symbol, code, market, name, industry, is_st, is_new,
                    status, rank, score, raw_score, trend_score, data_quality_score,
                    change_pct, turnover_rate, amount, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["symbol"],
                    str(row["symbol"]).split(".")[0],
                    row["market"],
                    row["name"],
                    row["industry"],
                    int(bool(row["is_st"])),
                    int(bool(row["is_new"])),
                    row["rank"],
                    row["score"],
                    float(row["score"]) + 0.1,
                    row["trend"],
                    row["quality"],
                    row["change"],
                    row["turnover"],
                    row["amount"],
                    timestamp,
                ),
            )
        seal_market_scan_snapshot(conn, int(run_id), sealed_at=timestamp)
    return int(run_id)


def _result(
    symbol: str,
    *,
    rank: int,
    market: str,
    score: int,
    quality: int,
    amount: float = 500_000_000,
    industry: str = "半导体",
    is_st: bool = False,
    is_new: bool = False,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "market": market,
        "name": f"样本{symbol[:6]}",
        "industry": industry,
        "is_st": is_st,
        "is_new": is_new,
        "rank": rank,
        "score": score,
        "trend": score - 5,
        "quality": quality,
        "change": 3.5,
        "turnover": 4.2,
        "amount": amount,
    }
