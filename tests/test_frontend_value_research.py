from __future__ import annotations

from pathlib import Path
import subprocess
import textwrap

ROOT = Path(__file__).resolve().parents[1]


def _node(source: str) -> None:
    result = subprocess.run(["node", "--input-type=module", "-e", textwrap.dedent(source)], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_value_summary_is_optional_and_follows_the_selected_financial_period() -> None:
    _node("""
      import assert from 'node:assert/strict';
      import { renderFuyaoStock } from './static/js/fuyao-stock-view.js';
      import { renderFuyaoValueResearch } from './static/js/fuyao-value-view.js';
      import { valueStock } from './tests/e2e/value-research-fixtures.mjs';
      const target={innerHTML:''}, record=valueStock();
      renderFuyaoStock(target,record);
      assert.ok(target.innerHTML.includes('合成中报来源'));
      assert.ok(!target.innerHTML.includes('合成年报来源'));
      renderFuyaoStock(target,record,{period:'2025-12-31|annual'});
      for (const text of ['价值研究摘要','合成年报来源','2026-04-01T09:00:00+08:00',
        '年度净利润为正且经营现金流为负','2026-09-11T15:30:00+08:00','所有者权益合计']) assert.ok(target.innerHTML.includes(text),text);
      assert.ok(!target.innerHTML.includes('合成中报来源'));
      const missing=renderFuyaoValueResearch(record.value_research,'2024-12-31|annual');
      assert.ok(missing.includes('所选报告期的财务观察不足') && missing.includes('下一步核实'));
      assert.ok(!missing.includes('合成中报来源') && !missing.includes('合成年报来源'));
      delete record.value_research;
      renderFuyaoStock(target,record);
      assert.ok(!target.innerHTML.includes('data-fuyao-value-research'));
      assert.equal(renderFuyaoValueResearch(null,''),'');
    """)


def test_value_summary_shows_coverage_ratio_limits_and_verification_actions_without_new_score() -> None:
    _node("""
      import assert from 'node:assert/strict';
      import { renderFuyaoValueResearch } from './static/js/fuyao-value-view.js';
      import { valueResearch } from './tests/e2e/value-research-fixtures.mjs';
      const report=valueResearch();
      let html=renderFuyaoValueResearch(report,'2025-12-31|annual');
      for (const text of ['已准入评分输入覆盖 2/2','5%','50%','负值也计入','不是置信度',
        '不代表分红现金或预期回报','不代表清算价值','下一步核实','单位：待核实',
        '不另生成评分或目标价','不参与任何排名']) assert.ok(html.includes(text),text);
      assert.ok(!html.includes('/100'));
      report.valuation.available_inputs=1; report.valuation.coverage='partial';
      report.valuation.earnings_yield_pct=null; report.valuation.earnings_yield_reason='PE TTM 为负，倒数不展示。';
      report.valuation.book_to_price_pct=null;
      html=renderFuyaoValueResearch(report,'2026-06-30|quarterly');
      for (const text of ['已准入评分输入覆盖 1/2','PE TTM 为负','暂不可用','所有者权益合计为零','单季和累计口径未确认']) {
        assert.ok(html.includes(text),text);
      }
      assert.ok(!html.includes('年度净利润为正且经营现金流为负'));
      assert.ok(!html.includes('0%') && !html.includes('负，倒数不展示。%'));
    """)


def test_value_summary_escapes_all_dynamic_text_and_never_formats_invalid_ratios() -> None:
    _node("""
      import assert from 'node:assert/strict';
      import { renderFuyaoValueResearch } from './static/js/fuyao-value-view.js';
      import { valueResearch } from './tests/e2e/value-research-fixtures.mjs';
      const report=valueResearch(), x='<img src=x onerror=alert(1)>';
      report.valuation.source=x; report.valuation.earnings_yield_reason=x;
      report.valuation.checks[0]={...report.valuation.checks[0],key:x,label:x,summary:x,action:x,unit:x};
      report.periods[1].source=x; report.periods[1].summary=x; report.limitations=[x];
      for(const value of [Infinity,NaN,0,-1,'5']) {
        report.valuation.earnings_yield_pct=value; report.valuation.book_to_price_pct=value;
        const html=renderFuyaoValueResearch(report,'2025-12-31|annual');
        assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(!html.includes('<img') && !html.includes('Infinity%') && !html.includes('NaN%'));
        assert.ok(html.includes('暂不可用') && !html.includes('-1%') && !html.includes('0%'));
      }
    """)


def test_value_summary_contract_rejects_identity_scope_and_ambiguous_periods() -> None:
    _node("""
      import assert from 'node:assert/strict';
      import { verifiedStockObservation } from './static/js/fuyao-contracts.js';
      import { valueStock } from './tests/e2e/value-research-fixtures.mjs';
      const record=valueStock();
      assert.equal(verifiedStockObservation(record,record.symbol),record);
      const invalid=[{schema_version:'other'},{symbol:'000001.SZ'},{metric_scope:'formal_health'},
        {ranking_effect:'market_rank'},{point_in_time:true},{evaluated_at:'bad-date'},{periods:null},
        {periods:[record.value_research.periods[0],record.value_research.periods[0]]},{limitations:[1]}];
      for(const change of invalid) assert.throws(()=>verifiedStockObservation({...record,
        value_research:{...record.value_research,...change}},record.symbol));
      for(const value of [false,0,'bad']) assert.throws(()=>verifiedStockObservation({...record,value_research:value},record.symbol));
      assert.doesNotThrow(()=>verifiedStockObservation({...record,value_research:null},record.symbol));
    """)


def test_value_summary_contract_rejects_invalid_coverage_ratios_and_check_facts() -> None:
    _node("""
      import assert from 'node:assert/strict';
      import { verifiedStockObservation } from './static/js/fuyao-contracts.js';
      import { valueStock } from './tests/e2e/value-research-fixtures.mjs';
      const record=valueStock(), report=record.value_research;
      const invalid=[{available_inputs:3},{available_inputs:-1},{available_inputs:1.5},{required_inputs:3},
        {coverage:'partial'},{earnings_yield_pct:Infinity},{book_to_price_pct:NaN},{earnings_yield_pct:0},
        {book_to_price_pct:-2},{earnings_yield_pct:'5'},{source:null},{fetched_at:2},
        {available_inputs:0,coverage:'unavailable'},{checks:null},{checks:[report.valuation.checks[0]]},
        {checks:[report.valuation.checks[0],report.valuation.checks[0]]}];
      for(const change of invalid) assert.throws(()=>verifiedStockObservation({...record,
        value_research:{...report,valuation:{...report.valuation,...change}}},record.symbol));
      for(const change of [{value:Infinity},{value:'2'},{unit:2},{action:''},{status:'healthy'}]) {
        const checks=[{...report.valuation.checks[0],...change}];
        assert.throws(()=>verifiedStockObservation({...record,value_research:{...report,
          valuation:{...report.valuation,checks}}},record.symbol));
      }
      for(const change of [{value:-2},{value:0},{value:null}]) {
        const checks=[{...report.valuation.checks[0],...change},report.valuation.checks[1]];
        assert.throws(()=>verifiedStockObservation({...record,value_research:{...report,
          valuation:{...report.valuation,checks}}},record.symbol));
      }
      for(const change of [{period_type:'monthly'},{period_end:'bad'},{observation_available:null},
        {fetched_at:2},{checks:[{...report.periods[0].checks[0],value:NaN}]}]) {
        assert.throws(()=>verifiedStockObservation({...record,value_research:{...report,
          periods:[{...report.periods[0],...change}]}},record.symbol));
      }
      const unavailable={...report.valuation,available_inputs:0,coverage:'unavailable',fetched_at:null,
        earnings_yield_pct:null,book_to_price_pct:null,
        checks:report.valuation.checks.map(check=>({...check,status:'unavailable',value:null}))};
      assert.doesNotThrow(()=>verifiedStockObservation({...record,value_research:{...report,valuation:unavailable}},record.symbol));
      unavailable.fetched_at='无法解析的来源时间';
      assert.doesNotThrow(()=>verifiedStockObservation({...record,value_research:{...report,valuation:unavailable}},record.symbol));
    """)
