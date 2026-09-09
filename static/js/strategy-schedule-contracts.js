import { validateSchedule } from "./strategy-lab-contracts.js";

export function validateManagedSchedule(value, strategyId) {
  const schedule = validateSchedule(value);
  if (schedule.strategy_id !== strategyId) throw new Error("定时任务与当前策略不一致");
  if (!["daily_after_close", "trading_day_intraday"].includes(schedule.cadence)
    || schedule.mode !== (schedule.cadence === "daily_after_close" ? "official" : "intraday")) {
    throw new Error("定时任务执行口径无效");
  }
  if (!Number.isFinite(schedule.notional_cash_cny) || schedule.notional_cash_cny < 10000) {
    throw new Error("定时任务名义资金无效");
  }
  for (const field of ["last_execution_id", "last_market_scan_run_id"]) {
    if (schedule[field] != null && (!Number.isSafeInteger(schedule[field]) || schedule[field] < 1)) {
      throw new Error("定时任务最近执行身份无效");
    }
  }
  return schedule;
}

export function validateSchedulePage(value, strategyId, page, pageSize) {
  if (!value || !Array.isArray(value.items) || value.page !== page || value.page_size !== pageSize
    || !Number.isSafeInteger(value.total) || value.total < 0
    || value.page_count !== Math.ceil(value.total / pageSize) || value.items.length > pageSize) {
    throw new Error("定时任务分页响应无效");
  }
  const items = value.items.map(item => validateManagedSchedule(item, strategyId));
  if (new Set(items.map(item => item.schedule_id)).size !== items.length) throw new Error("定时任务列表包含重复身份");
  return { ...value, items };
}

export function validateScheduleConfirmation(value, original, enabled) {
  const schedule = validateManagedSchedule(value, original.strategy_id);
  if (schedule.schedule_id !== original.schedule_id || schedule.enabled !== enabled
    || schedule.strategy_version !== original.strategy_version
    || schedule.strategy_fingerprint !== original.strategy_fingerprint) {
    throw new Error("定时任务启停回执与提交目标不一致");
  }
  return schedule;
}
