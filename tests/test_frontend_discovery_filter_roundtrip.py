from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_saved_screen_restores_every_market_without_collapsing_multiple_selection() -> None:
    _run_node(r'''
import assert from "node:assert/strict";
import { applyDiscoveryPresetFields, buildDiscoveryPresetDefinition } from "./static/js/market-scan-filters.js";
const options = ["SH", "SZ", "BJ"].map(value => ({value, selected: false}));
const market = {
  options,
  get selectedOptions() { return options.filter(option => option.selected); },
  get value() { return this.selectedOptions[0]?.value || ""; },
  set value(value) { options.forEach(option => { option.selected = option.value === value; }); },
};
const field = () => ({value: ""});
const elements = {market, status: field(), industry: field(), isSt: field(), isNew: field(),
  scoreMin: field(), scoreMax: field(), sort: field(), order: field(), table: {dataset: {}},
  columnViews: ["overview", "risk"].map(value => ({value, checked: false}))};
const preset = {name: "沪深质量", criteria: {market: ["SH", "SZ"], is_st: false, score: {min: 70, max: 95}},
  sort: [{field: "score", order: "desc"}], column_view: "risk"};
applyDiscoveryPresetFields(preset, elements);
assert.deepEqual(buildDiscoveryPresetDefinition(preset.name, elements), preset);
applyDiscoveryPresetFields({...preset, criteria: {}}, elements);
assert.deepEqual(market.selectedOptions, []);
assert.deepEqual(buildDiscoveryPresetDefinition(preset.name, elements).criteria, {});
''')


def test_saved_screen_does_not_offer_lossy_edits_for_unrepresented_bounds_or_flags() -> None:
    _run_node(r'''
import assert from "node:assert/strict";
import { isDiscoveryPresetUiRepresentable } from "./static/js/market-scan-filters.js";
const preset = criteria => ({criteria, sort: [{field: "rank", order: "asc"}], column_view: "overview"});
for (const criteria of [
  {confidence: {max: 90}}, {confidence: {min: 60, max: 90}},
  {risk: {min: 20}}, {risk: {min: 20, max: 80}},
  {tradability: {max: 85}}, {tradability: {min: 50, max: 85}},
  {is_st: "false"}, {is_new: 1}, {market: ["NASDAQ"]},
]) assert.equal(isDiscoveryPresetUiRepresentable(preset(criteria)), false, JSON.stringify(criteria));
assert.equal(isDiscoveryPresetUiRepresentable(preset({
  confidence: {min: 60, max: null}, risk: {min: null, max: 80},
  tradability: {min: 50}, is_st: false, is_new: true, market: ["SH", "SZ", "BJ"],
})), true);
''')


def _run_node(script: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
