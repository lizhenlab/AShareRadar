from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.services.market_strategy_templates import market_strategy_template_catalog


ROOT = Path(__file__).resolve().parents[1]


def _run_validator(payload: dict[str, object], expression: str = "catalog.templates.length") -> str:
    script = f"""
import {{ validateStrategyTemplateCatalog }} from "./static/js/strategy-template-catalog.js";
const payload = JSON.parse(process.argv[1]);
try {{
  const catalog = validateStrategyTemplateCatalog(payload);
  console.log(JSON.stringify({{ ok: true, value: {expression} }}));
}} catch (error) {{
  console.log(JSON.stringify({{ ok: false, message: error.message }}));
}}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(payload, ensure_ascii=False)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def test_frontend_validator_accepts_frozen_backend_catalog() -> None:
    payload = market_strategy_template_catalog().model_dump(mode="json")
    result = json.loads(_run_validator(payload, "catalog.catalog_digest"))

    assert result == {"ok": True, "value": payload["catalog_digest"]}
    assert len(payload["templates"]) == 15
    assert sum(item["availability"] == "available_for_draft" for item in payload["templates"]) == 8
    assert sum(item["availability"] == "shadow_only" for item in payload["templates"]) == 2
    assert sum(item["availability"] == "unavailable" for item in payload["templates"]) == 5


def test_frontend_validator_rejects_unknown_status_extra_field_and_state_mismatch() -> None:
    base = market_strategy_template_catalog().model_dump(mode="json")
    mutations = []

    unknown_status = json.loads(json.dumps(base))
    unknown_status["templates"][0]["efficacy_status"] = "passed"
    mutations.append(unknown_status)

    extra_field = json.loads(json.dumps(base))
    extra_field["templates"][0]["probability"] = 0.75
    mutations.append(extra_field)

    state_mismatch = json.loads(json.dumps(base))
    ready = next(item for item in state_mismatch["templates"] if item["availability"] == "available_for_draft")
    ready["strategy_spec"] = None
    mutations.append(state_mismatch)

    malformed_digest = json.loads(json.dumps(base))
    malformed_digest["catalog_digest"] = "NOT-A-SHA256"
    mutations.append(malformed_digest)

    duplicate_id = json.loads(json.dumps(base))
    duplicate_id["templates"][1]["template_id"] = duplicate_id["templates"][0]["template_id"]
    duplicate_id["templates"][1]["version"] = 2
    mutations.append(duplicate_id)

    named_profile_override = json.loads(json.dumps(base))
    ready = next(item for item in named_profile_override["templates"] if item["availability"] == "available_for_draft")
    ready["strategy_spec"]["profile"] = "balanced"
    ready["strategy_spec"]["objectives"]["alpha_1d"] = 1.0
    mutations.append(named_profile_override)

    for payload in mutations:
        result = json.loads(_run_validator(payload))
        assert result["ok"] is False


def test_template_card_escapes_server_text_and_keeps_non_ready_actions_disabled() -> None:
    payload = market_strategy_template_catalog().model_dump(mode="json")
    unavailable = next(item for item in payload["templates"] if item["availability"] == "unavailable")
    unavailable["objective"] = '<img src=x onerror="alert(1)">'
    script = """
import { strategyTemplateCardHtml } from "./static/js/strategy-template-catalog.js";
const item = JSON.parse(process.argv[1]);
console.log(strategyTemplateCardHtml(item));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(unavailable, ensure_ascii=False)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "&lt;img" in completed.stdout
    assert "<img" not in completed.stdout
    assert "字段不足，暂不可载入" in completed.stdout
    assert " disabled" in completed.stdout
    assert "研究适用环境" in completed.stdout and "假设未匹配" in completed.stdout
    assert "收益有效性不可用" in completed.stdout


def test_catalog_metadata_cannot_claim_live_samples_or_drop_source_contracts() -> None:
    base = market_strategy_template_catalog().model_dump(mode="json")
    mutations = [
        {**base, "schema_version": "full-market-strategy-template-catalog-v1"},
        {**base, "official_session_count": 2},
        {**base, "evidence_status": "validated"},
        {**base, "source_contracts": {}},
        {**base, "source_contracts": {**base["source_contracts"], "feature_windows": ""}},
    ]
    for payload in mutations:
        assert json.loads(_run_validator(payload))["ok"] is False


def test_new_quant_strategy_filters_survive_frontend_validation_unchanged() -> None:
    payload = market_strategy_template_catalog().model_dump(mode="json")
    for template_id in ("medium_momentum", "low_volatility_trend"):
        expected = next(item for item in payload["templates"] if item["template_id"] == template_id)
        expression = f'catalog.templates.find(item => item.template_id === "{template_id}").strategy_spec'
        result = json.loads(_run_validator(payload, expression))
        assert result == {"ok": True, "value": expected["strategy_spec"]}
        assert expected["strategy_spec"]["evidence_policy"]["require_verified_point_in_time_evidence"]
