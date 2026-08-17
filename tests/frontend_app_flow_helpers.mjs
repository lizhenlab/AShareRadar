export async function createAppHarness(options = {}) {
  const harness = installAppDom(options);
  const { __appTest } = await import("../static/app.js");
  return { ...harness, __appTest };
}

export function marketScanPollingIdentity(latest, latestPublished, mode = "official", databaseRevision = "db-1") {
  const slot = (_kind, run) => ({
    run_id: run?.id ?? null,
    token: testDigest(`${databaseRevision}|${JSON.stringify(pollingHeader(run))}`),
  });
  const latestToken = slot("latest", latest);
  const publishedToken = slot(`latest-published:${mode}`, latestPublished);
  return {
    schema_version: "market-scan-polling-identity-v1",
    authorization: "change_detection_only",
    request_mode: mode,
    latest: latestToken,
    latest_published: publishedToken,
    fingerprint: testDigest(`${databaseRevision}|${mode}|${latestToken.token}|${publishedToken.token}`),
  };
}

function pollingHeader(run) {
  if (!run) return null;
  const header = Object.fromEntries([
    "id", "status", "mode", "scope", "trigger", "rule_version", "data_date", "quote_date",
    "updated_at", "finished_at", "snapshot_digest", "snapshot_seal_origin", "snapshot_sealed_at",
  ].map((field) => [field, run[field] ?? null]));
  if (["queued", "running", "cancelling"].includes(header.status)) header.updated_at = null;
  return header;
}

function testDigest(value) {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (const character of value) {
    first = Math.imul(first ^ character.codePointAt(0), 0x01000193) >>> 0;
    second = Math.imul(second ^ first, 0x85ebca6b) >>> 0;
  }
  return `${first.toString(16).padStart(8, "0")}${second.toString(16).padStart(8, "0")}`.repeat(4);
}

export function installAppDom({ canvasContext } = {}) {
  const elements = new Map();
  const streams = [];

  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, createElement(id, element, canvasContext));
    }
    return elements.get(id);
  }

  globalThis.__ASHARE_RADAR_DISABLE_AUTOLOAD__ = true;
  globalThis.window = globalThis;
  globalThis.window.addEventListener = () => {};
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};
  globalThis.requestAnimationFrame = (callback) => callback();
  globalThis.document = {
    hidden: false,
    body: element("body"),
    getElementById: element,
    querySelector(selector) {
      if (selector === ".workspace-tabs") return element("workspaceTabs");
      if (selector === ".monitor-actions") return element("monitorActions");
      return element(selector);
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
  };
  globalThis.EventSource = class {
    constructor(url) {
      this.url = url;
      this.closed = false;
      this.listeners = {};
      streams.push(this);
    }

    addEventListener(name, handler) {
      this.listeners[name] = handler;
    }

    close() {
      this.closed = true;
    }
  };

  return { elements, element, streams, waitFor, jsonResponse, legacyWorkbenchFixture, legacyWorkbenchResponse };
}

function createElement(id, element, canvasContext) {
  return {
    id,
    value: "",
    innerHTML: "",
    textContent: "",
    className: "",
    dataset: {},
    disabled: false,
    width: 920,
    height: 300,
    clientWidth: 920,
    clientHeight: 300,
    classList: classList(),
    addEventListener(type, handler) {
      this.listeners = this.listeners || {};
      this.listeners[type] = handler;
    },
    querySelector() {
      return element(`${id}-button`);
    },
    querySelectorAll() {
      return [];
    },
    closest(selector) {
      return selector === ".metric-card" ? { classList: classList() } : null;
    },
    getContext() {
      return canvasContext === null ? null : canvasContext || { clearRect() {} };
    },
  };
}

function classList() {
  const values = new Set();
  return {
    add(value) {
      values.add(value);
    },
    remove(value) {
      values.delete(value);
    },
    toggle(value, active) {
      if (active) values.add(value);
      else values.delete(value);
    },
    contains(value) {
      return values.has(value);
    },
  };
}

export async function waitFor(condition, label) {
  for (let index = 0; index < 20; index += 1) {
    if (condition()) return;
    await Promise.resolve();
  }
  throw new Error(`timed out waiting for ${label}`);
}

export function jsonResponse(payload) {
  return {
    ok: true,
    async json() {
      return payload;
    },
  };
}

export function legacyWorkbenchResponse(payload) {
  return jsonResponse(legacyWorkbenchFixture(payload));
}

export function legacyWorkbenchFixture(payload) {
  if (!payload?.analysis?.quote || payload.schema_version === "stock-workbench-v2") return payload;
  const quote = payload.analysis.quote;
  const symbol = `${quote.code}.${String(quote.market || "").toUpperCase()}`;
  const quoteTime = String(quote.timestamp || "");
  const signalDate = quoteTime.slice(0, 10);
  const klines = Array.isArray(payload.analysis.klines) ? payload.analysis.klines : [];
  const owned = (value = {}) => ({ ...(value && typeof value === "object" ? value : {}), symbol });
  const researchOwned = (value = {}) => ({ ...owned(value), updated_at: quoteTime });
  const insights = payload.insights && typeof payload.insights === "object" ? payload.insights : {};
  const analysis = { ...payload.analysis, klines };
  if (analysis.stock_profile !== null && analysis.stock_profile !== undefined) {
    analysis.stock_profile = owned(analysis.stock_profile);
  }
  if (analysis.review !== null && analysis.review !== undefined) analysis.review = owned(analysis.review);
  return {
    ...payload,
    schema_version: "stock-workbench-v2",
    symbol,
    generated_at: quoteTime,
    context_generated_at: quoteTime,
    research_mode: "interactive_shadow",
    production_effect: "none",
    diagnosis_production_effect: "none",
    research_cohort: {
      requested_symbol: symbol,
      observed_symbol: symbol,
      mode: "interactive_shadow",
      decision_time: quoteTime,
      quote_event_time: quoteTime,
      signal_date: signalDate,
      daily_bar_cutoff: klines.at(-1)?.date || signalDate,
      production_effect: "none",
      advice_persistence: "disabled",
    },
    analysis,
    insights: {
      ...insights,
      overview: researchOwned(insights.overview),
      fund_flow: researchOwned(insights.fund_flow),
      order_pressure: researchOwned(insights.order_pressure),
      events: researchOwned(insights.events),
      financial_health: researchOwned(insights.financial_health),
      valuation: researchOwned(insights.valuation),
      lhb: researchOwned(insights.lhb),
      abnormal_events: researchOwned(insights.abnormal_events),
      rule_matches: researchOwned(insights.rule_matches),
      strategy_cards: Array.isArray(insights.strategy_cards)
        ? insights.strategy_cards.map(researchOwned)
        : [],
    },
    feature_snapshot: researchOwned(payload.feature_snapshot),
    factor_lab: researchOwned(payload.factor_lab),
    market_regime: researchOwned(payload.market_regime),
    signal_validation: researchOwned(payload.signal_validation),
    risk_reward: researchOwned(payload.risk_reward),
    timeframe_alignment: researchOwned(payload.timeframe_alignment),
    alpha_evidence: researchOwned(payload.alpha_evidence),
    diagnosis: researchOwned(payload.diagnosis),
    evidence_chain: researchOwned(payload.evidence_chain),
    qa_report: researchOwned(payload.qa_report),
    event_digest: researchOwned(payload.event_digest),
    peer_comparison: researchOwned(payload.peer_comparison),
    t_strategy: researchOwned(payload.t_strategy),
    risk_radar: researchOwned(payload.risk_radar),
    chip_analysis: researchOwned(payload.chip_analysis),
    leadership: researchOwned(payload.leadership),
    theme_context: researchOwned(payload.theme_context),
    replay: researchOwned(payload.replay),
    chart_marks: owned(payload.chart_marks),
    alert_rules: (payload.alert_rules || []).map(owned),
    alert_events: (payload.alert_events || []).map(owned),
    notes: (payload.notes || []).map(owned),
  };
}
