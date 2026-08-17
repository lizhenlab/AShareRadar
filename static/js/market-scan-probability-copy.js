export function probabilitySnapshotCopy(artifact) {
  if (artifact.availability === "probability_artifact_source_unbound") {
    return {
      title: "上涨概率研究 · 源绑定无效",
      description: "已有概率产物未精确绑定本次源归档，系统已忽略该产物；概率、区间与选股筛选保持为空或关闭。",
    };
  }
  if (artifact.status === "calibrated_shadow") {
    return {
      title: "上涨概率研究 · 冻结 Shadow 证据",
      description: "趋势强度是序数状态分；以下概率来自该批次持久化的样本外校准证据，不参与生产排序。",
    };
  }
  if (artifact.status === "not_generated") {
    if (artifact.availability === "ineligible_run_contract") {
      return {
        title: "上涨概率研究 · 未进入归档",
        description: "当前来源批次不符合概率研究归档合同；仅已发布的盘后正式全市场原发布封印批次可进入归档，概率、区间与选股筛选保持为空或关闭。",
      };
    }
    if (artifact.availability === "source_capture_pending") {
      return {
        title: "上涨概率研究 · 正在归档样本",
        description: "当前批次已进入真实点时源样本归档；概率、区间与选股筛选继续保持为空或关闭。",
      };
    }
    if (artifact.availability === "source_scan_action_ineligible") {
      return {
        title: "上涨概率研究 · 未进入归档",
        description: "当前批次评分分布未通过动作门禁，因此未进入研究归档；不展示概率，也不开放选股筛选。",
      };
    }
    return {
      title: "上涨概率研究 · 尚未生成",
      description: "当前批次尚未生成上涨概率研究证据，不展示概率或群体校准调整区间，也不影响生产排序。",
    };
  }
  if (artifact.fit_status === "sampled_oos_assessment" || artifact.pipeline_stage === "sampled_fit_assessed") {
    return {
      title: "上涨概率研究 · 有界样本评估完成",
      description: "已完成可重放的有界样本评估，但它不满足全市场基准与 Top100 契约；逐股概率、群体校准调整区间和选股筛选继续保持为空或关闭。",
    };
  }
  const counts = objectValue(artifact.counts);
  const archivedSessions = countNumber(counts.archived_independent_session_count);
  const sourceOnly = (archivedSessions !== null && archivedSessions > 0)
    || artifact.limitations?.includes?.("live_point_in_time_source_archived");
  return sourceOnly
    ? {
        title: "上涨概率研究 · 点时样本积累中",
        description: "当前仅归档了真实点时源样本，尚未形成成熟标签和样本外校准概率；概率与群体校准调整区间保持为空。",
      }
    : {
        title: "上涨概率研究 · 样本不足",
        description: "研究证据已生成，但尚未通过独立日期、标签覆盖或校准门槛；概率与群体校准调整区间保持为空。",
      };
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function countNumber(value) {
  const number = Number(value);
  return value === null || value === undefined || value === "" || !Number.isInteger(number) || number < 0 ? null : number;
}
