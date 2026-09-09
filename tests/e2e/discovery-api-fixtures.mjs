export function discoveryArchive(definition) {
  return {
    format: "ashare-radar.discovery-preset",
    schema_version: 2,
    checksum_algorithm: "sha256",
    checksum: "a".repeat(64),
    exported_at: "2026-07-28T12:00:00Z",
    preset: {
      name: definition.name,
      criteria: definition.criteria,
      sort: definition.sort,
      column_view: definition.column_view || "overview",
    },
  };
}

export function discoveryPreset(payload, revision) {
  return {
    ...payload,
    id: 7,
    schema_version: 2,
    column_view: payload.column_view || "overview",
    revision,
    created_at: "2026-07-28T10:00:00Z",
    updated_at: "2026-07-28T10:00:00Z",
  };
}

export function discoveryLeaderboard(preset, payload) {
  return {
    preset,
    run_id: payload.run_id,
    rule_version: "leader-v2",
    items: [
      {
        position: 1, source_rank: 1, symbol: "600519.SH", code: "600519", market: "SH",
        name: "贵州茅台", industry: "白酒", is_st: false, is_new: false,
        quality: 96, trend: 91, change: 2.4, turnover: 1.2, amount: 1800000000, score: 95, raw_score: 94.8,
      },
      {
        position: 2, source_rank: 4, symbol: "600809.SH", code: "600809", market: "SH",
        name: "山西汾酒", industry: "白酒", is_st: false, is_new: false,
        quality: 93, trend: 89, change: 1.8, turnover: 0.9, amount: 920000000, score: 92, raw_score: 91.8,
      },
    ],
    total: 2,
    page: payload.page,
    page_size: payload.page_size,
    page_count: 1,
  };
}

export function discoveryRankChanges() {
  return {
    current_run_id: 42,
    previous_run_id: 41,
    current_rule_version: "leader-v2",
    previous_rule_version: "leader-v2",
    comparable: true,
    reason: null,
    items: [
      { symbol: "600519.SH", code: "600519", market: "SH", name: "贵州茅台", previous_rank: 2, current_rank: 1, rank_delta: 1, movement: "up" },
      { symbol: "600809.SH", code: "600809", market: "SH", name: "山西汾酒", previous_rank: null, current_rank: 4, rank_delta: null, movement: "new" },
      { symbol: "000001.SZ", code: "000001", market: "SZ", name: "平安银行", previous_rank: 3, current_rank: null, rank_delta: null, movement: "exit" },
    ],
    total: 3,
    page: 1,
    page_size: 200,
    page_count: 1,
  };
}
