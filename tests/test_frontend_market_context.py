from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = """
import assert from 'node:assert/strict';
import { renderInsights } from './static/js/workbench.js';
import { renderFactorLab } from './static/js/research-factor-diagnostics.js';
const elements = new Map();
globalThis.document = { getElementById(id) {
  if (!elements.has(id)) elements.set(id, { innerHTML: '' });
  return elements.get(id);
} };
const html = id => elements.get(id).innerHTML;
function observation(name, change_pct) {
  return {name, symbol: name === '沪深300' ? '000300.SH' : '行业01', change_pct,
    event_at: '2026-09-14T10:00:00+08:00', observed_at: '2026-09-14T10:00:01+08:00', source: '本地行情'};
}
function context() {
  return {base_score: 70, reliability_score: 80, score: 58, before_gates_score: 65,
    pre_reliability_score: 60, market_multiplier: 0.75, industry_multiplier: 0.9,
    market: observation('沪深300', -3), industry: observation('银行', -2),
    stock_excess_pct: 1, industry_excess_pct: 1, relative_weight: 0.15,
    unavailable_reasons: [], note: '当日相对强弱修正，参数尚未经样本外收益验证。'};
}
function renderContext(value) {
  renderInsights({overview: {total_score: value.score, market_context_score: value}});
  return html('insightOverview');
}
"""


def _node(source: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SETUP + textwrap.dedent(source)],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_market_context_exposes_actual_score_chain_and_relative_weakness_limits() -> None:
    _node("""
      const result = renderContext(context());
      for (const text of ['方向基础 70.0 → 相对强弱修正 65.0 → 市场与行业约束 60.0 → 可靠性约束 58',
        '数据可靠性 80/100，只约束信号强度', '大盘：沪深300 000300.SH · -3.00%',
        '主行业：银行 行业01 · -2.00%', '个股相对行业：+1.00 个百分点',
        '行业相对大盘：+1.00 个百分点', '相对抗跌不等于上涨', '当前修正不用于历史概率',
        '行情时间：2026-09-14T10:00:00+08:00', '观测时间：2026-09-14T10:00:01+08:00',
        '大盘正向强度系数 0.75', '行业正向强度系数 0.90']) assert.ok(result.includes(text), text);
      assert.ok(!result.includes('胜率') && !result.includes('更准'));
      assert.ok(result.includes('<details><summary>评分口径与数据来源</summary>'));
    """)


def test_market_context_preserves_zero_and_negative_percentage_point_differences() -> None:
    _node("""
      const record = context();
      Object.assign(record, {base_score: 0, reliability_score: 0, score: 0,
        before_gates_score: 0, pre_reliability_score: 0, stock_excess_pct: 0, industry_excess_pct: -1.25});
      record.market.change_pct = 0; record.industry.change_pct = -0.5;
      const result = renderContext(record);
      for (const text of ['方向基础 0.0', '可靠性约束 0', '数据可靠性 0/100',
        '大盘：沪深300 000300.SH · 0.00%', '个股相对行业：0.00 个百分点',
        '行业相对大盘：-1.25 个百分点']) assert.ok(result.includes(text), text);
      assert.ok(!result.includes('未参与') && !result.includes('+0.00'));
    """)


def test_market_context_missing_and_nonfinite_values_never_become_neutral_evidence() -> None:
    _node("""
      for (const invalid of [null, undefined, NaN, Infinity, -Infinity, '', '0', false]) {
        const record = context();
        Object.assign(record, {base_score: invalid, score: invalid,
          stock_excess_pct: invalid, industry_excess_pct: invalid});
        record.market.change_pct = invalid; record.industry = null;
        record.unavailable_reasons = ['主行业数据已过时，未参与修正'];
        const result = renderContext(record);
        for (const text of ['大盘：未参与（缺少有效观测）', '主行业：未参与（缺少有效观测）',
          '个股相对行业：未参与', '行业相对大盘：未参与', '主行业数据已过时', '方向基础 --']) {
          assert.ok(result.includes(text), text);
        }
        const panel = result.slice(result.indexOf('<div data-market-context-score>'));
        for (const text of ['NaN', 'Infinity', '0.00%', '大盘来源：']) assert.ok(!panel.includes(text), text);
      }
      renderInsights({overview: {total_score: 60}});
      assert.ok(!html('insightOverview').includes('data-market-context-score'));
    """)


def test_market_context_escapes_identity_times_sources_and_unavailable_reasons() -> None:
    _node(r"""
      const record = context(), unsafe = '<img src=x onerror=alert(1)>';
      for (const item of [record.market, record.industry]) {
        for (const key of ['name', 'symbol', 'source', 'event_at', 'observed_at']) item[key] = unsafe;
      }
      record.note = unsafe; record.unavailable_reasons = [unsafe];
      const result = renderContext(record);
      assert.ok(!result.includes('<img'));
      assert.equal((result.match(/&lt;img src=x onerror=alert\(1\)&gt;/g) || []).length, 12);
    """)


def test_composite_factor_keeps_observed_value_without_weight_or_historical_probability() -> None:
    _node("""
      renderFactorLab({factors: [{name: '龙头<script>', aggregation_role: 'composite',
        participates_in_current_score: false, data_nature: 'derived', score: 0, weight: 1,
        value: '相对表现<script>', evidence: ['已有因子证据<script>'], percentile: 99,
        calibration: {sample_count: 999, win_rate: 99, avg_forward_5d_return: 20},
        calibration_buckets: [{name: '历史桶', sample_count: 999, avg_forward_5d_return: 20}]}]});
      const result = html('factorLab');
      for (const text of ['0 · 复合观察，不重复计分', '相对表现&lt;script&gt;',
        '已有因子证据&lt;script&gt;', '龙头&lt;script&gt;为复合观察，不重复计分']) {
        assert.ok(result.includes(text), text);
      }
      for (const text of ['当前不计分/不可用', '当前观测值不可用', '胜率', '历史分位',
        '历史桶', '999', '权重 1', '<script>']) assert.ok(!result.includes(text), text);
    """)


def test_composite_missing_evidence_and_independent_unavailable_factors_stay_distinct() -> None:
    _node("""
      renderFactorLab({factors: [
        {name: '复合缺失', aggregation_role: 'composite', participates_in_current_score: false,
          data_nature: 'unavailable', score: 88, value: '兼容占位', evidence: ['不可用证据']},
        {name: '估值缺失', aggregation_role: 'independent', participates_in_current_score: false,
          data_nature: 'unavailable', score: 55, value: '占位估值'},
      ]});
      const result = html('factorLab');
      assert.ok(result.includes('复合观察，不重复计分') && result.includes('当前观测证据不可用'));
      assert.ok(result.includes('当前评分口径：估值缺失当前不计分/不可用'));
      for (const text of ['兼容占位', '不可用证据', '占位估值', '88 ·']) assert.ok(!result.includes(text), text);
    """)


def test_directional_basis_explains_fixed_evidence_budget_and_actual_risk_deduction() -> None:
    _node("""
      renderInsights({overview: {total_score: 58, directional_evidence_score: 72.5,
        risk_penalty: 2.5, directional_score: 70, evidence_coverage_pct: 75,
        market_context_score: context()}});
      const result = html('insightOverview');
      for (const text of ['独立证据分 72.5 → 风险扣分 2.5 → 方向基分 70.0',
        '方向证据覆盖 75.0%', '缺失份额不转移', '覆盖度不是上涨概率']) {
        assert.ok(result.includes(text), text);
      }
      assert.ok(result.indexOf('data-directional-score-basis') < result.indexOf('data-market-context-score'));
      renderInsights({overview: {total_score: 50, directional_evidence_score: 0,
        risk_penalty: 0, directional_score: 0, evidence_coverage_pct: 0}});
      for (const text of ['独立证据分 0.0', '风险扣分 0.0', '方向基分 0.0', '方向证据覆盖 0.0%']) {
        assert.ok(html('insightOverview').includes(text), text);
      }
    """)


def test_directional_basis_rejects_dirty_values_and_preserves_legacy_absence() -> None:
    _node("""
      for (const invalid of [NaN, Infinity, -Infinity, false, '', '0', -1, 101, '<img src=x>']) {
        renderInsights({overview: {total_score: 50, directional_evidence_score: invalid,
          risk_penalty: invalid, directional_score: invalid, evidence_coverage_pct: invalid}});
        const result = html('insightOverview');
        assert.ok(result.includes('独立证据分 -- → 风险扣分 -- → 方向基分 --'));
        assert.ok(result.includes('方向证据覆盖 --%'));
        for (const text of ['NaN', 'Infinity', '<img', '0.0%']) assert.ok(!result.includes(text), text);
      }
      for (const value of [undefined, null]) {
        renderInsights({overview: {total_score: 60, directional_evidence_score: value,
          risk_penalty: value, evidence_coverage_pct: value, market_context_score: context()}});
        assert.ok(!html('insightOverview').includes('data-directional-score-basis'));
        assert.ok(html('insightOverview').includes('data-market-context-score'));
      }
    """)


def test_relative_adjustment_shows_signed_actual_points_without_inventing_legacy_mix() -> None:
    _node("""
      for (const [value, expected] of [[0, '0.00'], [1.25, '+1.25'], [-2.5, '-2.50']]) {
        const record = {...context(), rule_version: 'current-market-context.v2', relative_adjustment: value};
        assert.ok(renderContext(record).includes(`相对强弱实际调整 ${expected} 分`));
      }
      for (const value of [NaN, Infinity, '2', '<img src=x>', false]) {
        const result = renderContext({...context(), relative_adjustment: value});
        assert.ok(result.includes('相对强弱实际调整 -- 分'));
        assert.ok(!result.includes('<img') && !result.includes('NaN') && !result.includes('Infinity'));
      }
      for (const value of [undefined, null]) {
        const record = {...context(), rule_version: 'current-market-context.v1',
          relative_adjustment: value, relative_weight: 0.2};
        const result = renderContext(record);
        assert.ok(!result.includes('相对强弱实际调整'));
        assert.ok(!result.includes('20%') && !result.includes('混合'));
      }
    """)


def test_risk_factor_is_an_observed_constraint_with_escaped_evidence() -> None:
    _node("""
      renderInsights({overview: {factors: [
        {name: '风险面<script>', aggregation_role: 'risk_constraint', score: 40, level: '偏弱',
          score_available: true, participates_in_total_score: true, data_nature: 'derived',
          summary: '风险判断<script>', evidence: ['已核验卖压<script>']},
      ]}});
      const result = html('factorList');
      for (const text of ['风险约束：仅按风险扣分，不作为独立方向证据', '40 · 偏弱',
        '风险面&lt;script&gt;', '风险判断&lt;script&gt;', '已核验卖压&lt;script&gt;']) {
        assert.ok(result.includes(text), text);
      }
      assert.ok(!result.includes('不可用') && !result.includes('<script>'));
      renderInsights({overview: {factors: [{name: '旧风险面', score: 50, evidence: []}]}});
      assert.ok(!html('factorList').includes('风险约束'));
    """)
