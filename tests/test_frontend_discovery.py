from __future__ import annotations

import json
from pathlib import Path
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def test_discovery_controls_are_wired_into_the_existing_market_scan_surface() -> None:
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert '"/static/js/discovery.js"' in index
    for element_id in (
        "discoveryPresetControls",
        "discoveryPresetSelect",
        "discoveryPresetName",
        "discoveryPresetSave",
        "discoveryPresetApply",
        "discoveryPresetRename",
        "discoveryPresetDelete",
        "discoveryPresetFeedback",
        "discoveryRankSummary",
    ):
        assert f'id="{element_id}"' in index
    assert 'from "./js/discovery.js"' in app
    assert "createDiscoveryController" in app


def test_discovery_payload_uses_only_existing_supported_filter_fields() -> None:
    output = _run_node(
        r'''
          import {
            buildDiscoveryPresetDefinition,
            normalizeDiscoveryLeaderboard,
            rankChangeLabel,
          } from "./static/js/discovery.js";

          const value = (value) => ({ value });
          const elements = {
            market: value("SH"),
            industry: value("白酒"),
            isSt: value("false"),
            isNew: value("true"),
            quality: value("88"),
            status: value("missing"),
            keyword: value("600519"),
            sort: value("trend_score"),
            order: value("desc"),
          };
          const definition = buildDiscoveryPresetDefinition("高质量白酒", elements);
          const normalized = normalizeDiscoveryLeaderboard({
            preset: {
              id: 7,
              name: "高质量白酒",
              revision: 3,
              criteria: {},
              sort: [{ field: "score", order: "desc" }],
            },
            run_id: 42,
            rule_version: "leader-v2",
            items: [{
              position: 1,
              source_rank: 4,
              symbol: "600519.SH",
              code: "600519",
              market: "SH",
              name: "贵州茅台",
              industry: "白酒",
              is_st: false,
              is_new: false,
              quality: 93,
              trend: 81,
              change: 2.5,
              turnover: 1.2,
              amount: 123000000,
              score: 90,
            }],
            total: 1,
            page: 1,
            page_size: 100,
            page_count: 1,
          });
          console.log(JSON.stringify({
            definition,
            item: normalized.items[0],
            labels: [
              rankChangeLabel({ movement: "up", rank_delta: 3 }),
              rankChangeLabel({ movement: "down", rank_delta: -2 }),
              rankChangeLabel({ movement: "unchanged", rank_delta: 0 }),
              rankChangeLabel({ movement: "new", rank_delta: null }),
              rankChangeLabel({ movement: "exit", rank_delta: null }),
            ],
          }));
        '''
    )
    payload = json.loads(output)

    assert payload["definition"] == {
        "name": "高质量白酒",
        "criteria": {
            "market": ["SH"],
            "industry": ["白酒"],
            "is_st": False,
            "is_new": True,
            "quality": {"min": 88},
        },
        "sort": [{"field": "trend", "order": "desc"}],
    }
    assert "status" not in payload["definition"]["criteria"]
    assert "keyword" not in payload["definition"]["criteria"]
    assert payload["item"] | {
        "run_id": 42,
        "status": "success",
        "rank": 1,
        "source_rank": 4,
        "trend_score": 81,
        "change_pct": 2.5,
        "turnover_rate": 1.2,
        "data_quality_score": 93,
    } == payload["item"]
    assert payload["labels"] == ["上升 3", "下降 2", "持平", "新进", "离榜"]


def _run_node(source: str) -> str:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", textwrap.dedent(source)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
