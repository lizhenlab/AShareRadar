import { impactEvaluation, impactRows, routeImpactFixture } from "./market-scan-condition-impact-fixtures.mjs";

export async function selectionFixture(page) {
  const base = await routeImpactFixture(page, { candidateCount: 220 });
  const prototype = impactRows(base.run)[0];
  const rows = Array.from({ length: 220 }, (_, index) => {
    const code = String(600000 + index);
    return { ...structuredClone(prototype), symbol: `${code}.SH`, code, rank: index + 1, name: `范围候选${index + 1}` };
  });
  const state = { members: rows.slice(110).map(row => row.symbol), digest: "a".repeat(64), reads: [], evaluations: [], exports: [], applications: [], failRefresh: false, hold: false, release: null };
  await page.route(/\/api\/(market-scans\/42\/(results|export\.xlsx|screen\/evaluate)|discovery\/presets\/7\/apply)(\?|$)/, async route => {
    const request = route.request(), url = new URL(request.url());
    const body = request.method() === "POST" ? request.postDataJSON() : Object.fromEntries(url.searchParams);
    if (body.universe === "watchlist" && body.watchlist_digest && body.watchlist_digest !== state.digest) {
      await json(route, { detail: "自选成员已变化或未绑定，请点击刷新自选范围后重试" }, 409); return;
    }
    if (state.failRefresh && body.universe === "watchlist" && !body.watchlist_digest) {
      await json(route, { detail: "测试范围读取暂不可用" }, 503); return;
    }
    const selected = body.universe === "watchlist" ? rows.filter(row => state.members.includes(row.symbol)) : rows;
    const scope = body.universe === "watchlist" ? { universe: "watchlist", watchlist_digest: state.digest, member_count: state.members.length } : null;
    const current = { ...body };
    if (url.pathname.endsWith("/export.xlsx")) {
      state.exports.push(current);
      await route.fulfill({ contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers: { "content-disposition": 'attachment; filename="selection.xlsx"' }, body: "fixture-xlsx" }); return;
    }
    if (url.pathname.endsWith("/screen/evaluate")) {
      state.evaluations.push(current);
      await json(route, { ...impactEvaluation(base.run, body, selected), selection_scope: scope }); return;
    }
    const pageNumber = Number(body.page || 1), size = Number(body.page_size || 100);
    const items = selected.slice((pageNumber - 1) * size, pageNumber * size);
    const pagination = { items, total: selected.length, page: pageNumber, page_size: size, page_count: Math.ceil(selected.length / size), selection_scope: scope };
    let payload = { ...pagination, run: base.run, probability_research: null };
    if (url.pathname.endsWith("/apply")) {
      state.applications.push(current);
      const preset = { id: 7, name: "高置信方案", revision: 1, criteria: { confidence: { min: 80 }, risk: { max: 50 } }, sort: [{ field: "score", order: "desc" }], column_view: "research" };
      payload = { ...pagination, preset, run_id: base.run.id, rule_version: base.run.rule_version,
        items: items.map((row, index) => ({ ...row, position: (pageNumber - 1) * size + index + 1, source_rank: row.rank, quality: row.data_quality_score, trend: row.trend_score, change: row.change_pct, turnover: row.turnover_rate })) };
    } else state.reads.push(current);
    if (state.hold && scope) { state.hold = false; await new Promise(resolve => { state.release = resolve; }); }
    await json(route, payload);
  });
  return state;
}

function json(route, payload, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
}
