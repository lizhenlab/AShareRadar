import { escapeHtml } from "./dom.js";
import { sourceValue } from "./fuyao-contracts.js";

const e = escapeHtml;

export function renderFuyaoMarket(target, data, filter = "") {
  if (!target) return;
  if (!data) { target.innerHTML = "<p class='fuyao-note'>尚未读取本地市场研究记录。</p>"; return; }
  const opened = new Set(Array.from(target.querySelectorAll?.("details[data-fuyao-section][open]") || [], (node) => node.dataset.fuyaoSection));
  target.innerHTML = `${sectorView(data.sectors, filter)}${sentimentView(data.sentiment, filter)}${historyView(data.history)}`;
  for (const node of target.querySelectorAll?.("details[data-fuyao-section]") || []) node.open = opened.has(node.dataset.fuyaoSection);
}

function sectorView(observation, filter) {
  if (!observation?.payload) return "<details class='fuyao-details' data-fuyao-section='sectors'><summary>板块数据 · 尚无缓存</summary><p>同步后可查看行业、概念和指定板块当前成员。</p></details>";
  const payload = observation.payload;
  const rows = filteredRows(payload.rows, filter);
  const body = rows.slice(0, 100).map((row) => `<tr><th>${e(row.name)}</th><td>${e(row.symbol)}</td>
    <td>${row.category === "industry" ? "行业" : "概念"}</td><td>${e(sourceValue(row.change_pct, "%"))}</td></tr>`).join("");
  const members = Object.entries(payload.members || {}).map(([symbol, items]) => `<details data-fuyao-section="members:${e(symbol)}"><summary>${e(symbol)} · ${items.length} 个当前成员</summary>
    <p class="fuyao-member-list">${items.slice(0, 200).map(e).join("、")}${items.length > 200 ? "（展示前 200 项）" : ""}</p></details>`).join("");
  return `<details class="fuyao-details" data-fuyao-section="sectors"><summary>板块 · ${e(payload.catalog_count ?? payload.rows?.length ?? 0)} 项</summary>
    <p>${e(observation.source)} · 获取 ${e(observation.fetched_at)}；${e(payload.membership_basis || "当前成员，非历史名单")}</p>
    ${table(["名称", "代码", "类别", "涨跌幅"], body)}<p class="fuyao-note">匹配 ${rows.length} 项，展示前 100 项。可用上方关键词筛选。</p>${members}</details>`;
}

function sentimentView(observation, filter) {
  if (!observation?.payload) return "<details class='fuyao-details' data-fuyao-section='sentiment'><summary>市场情绪 · 尚无缓存</summary><p>同步后可查看涨跌停、炸板、龙虎榜及指定股票的异动解释。</p></details>";
  const payload = observation.payload;
  const labels = { "limit-up-pool": "涨停", "limit-down-pool": "跌停", "limit-break-pool": "炸板" };
  const pools = Object.entries(labels).map(([key, name]) => poolView(name, payload.pools?.[key], filter)).join("");
  const lhb = filteredRows(payload.dragon_tiger, filter).slice(0, 100).map((row) => `<tr><th>${e(row.name || row.symbol)}</th><td>${e(row.symbol)}</td>
    <td>${e(sourceValue(row.net_value))}</td><td>${e(row.range_days ?? "未返回")}</td></tr>`).join("");
  const reasons = filteredRows(payload.anomalies, filter).slice(0, 100).map((row) => `<p><strong>${e(row.symbol)} ${e(row.tag || "")}</strong> ${e(row.text)}</p>`).join("");
  return `<details class="fuyao-details" data-fuyao-section="sentiment"><summary>市场情绪 · ${e(payload.trade_date || "日期未返回")}</summary>
    <p>${e(observation.source)} · 获取 ${e(observation.fetched_at)}。${e(payload.scope || "")}</p>${pools}
    <details data-fuyao-section="dragon-tiger"><summary>龙虎榜 · ${payload.dragon_tiger?.length ?? 0} 项</summary>${table(["名称", "代码", "净额原值（单位待核实）", "统计天数"], lhb)}</details>
    <details data-fuyao-section="anomalies"><summary>异动解释 · 提供者观点</summary>${reasons || "<p>本次筛选没有异动解释，不表示没有风险。</p>"}</details>
    <p class="fuyao-note">每表展示筛选后的前 100 项；异动解释不是已核实公告事实，缺席不等于零风险。</p></details>`;
}

function poolView(name, input, filter) {
  const all = Array.isArray(input) ? input : [];
  const rows = filteredRows(all, filter).slice(0, 100).map((row) => `<tr><th>${e(row.name || row.symbol)}</th><td>${e(row.symbol)}</td>
    <td>${e(sourceValue(row.change_pct, "%"))}</td><td>${e(row.reason || "未返回")}</td></tr>`).join("");
  return `<details data-fuyao-section="pool:${e(name)}"><summary>${e(name)} · ${Array.isArray(input) ? all.length : "未返回"} 项</summary>${table(["名称", "代码", "涨跌幅", "提供者解释"], rows)}</details>`;
}

function historyView(manifest) {
  if (!manifest) return "<details class='fuyao-details' data-fuyao-section='history'><summary>历史研究数据 · 尚无已发布版本</summary><p>完整历史与增量同步将在后台校验后发布独立研究文件。</p></details>";
  return `<details class="fuyao-details" data-fuyao-section="history"><summary>历史研究数据 · ${e(manifest.first_date)} 至 ${e(manifest.last_date)}</summary>
    <p>${e(manifest.symbols)} 只股票 · ${manifest.trading_dates?.length ?? 0} 个交易日 · ${e(manifest.observed_at)}</p>
    <p class="fuyao-version">版本 ${e(manifest.version)}</p><p>未复权研究数据；没有更新正式行情缓存，也不证明历史当时可知。</p>
    <p>本次历史修订 ${e(manifest.revised_daily_rows ?? 0)} 行；重复 ${e(manifest.duplicate_daily_rows ?? 0)} 行。</p>
    ${(manifest.notes || []).map((note) => `<p>${e(note)}</p>`).join("")}</details>`;
}

function table(headers, body) {
  return `<div class="fuyao-table-scroll"><table><thead><tr>${headers.map((label) => `<th>${e(label)}</th>`).join("")}</tr></thead>
    <tbody>${body || `<tr><td colspan="${headers.length}">没有匹配记录</td></tr>`}</tbody></table></div>`;
}

function filteredRows(input, filter) {
  const rows = Array.isArray(input) ? input : [];
  const query = String(filter).trim().toLowerCase();
  return rows.filter((row) => !query || `${row.symbol || ""} ${row.name || ""} ${row.tag || ""}`.toLowerCase().includes(query));
}
