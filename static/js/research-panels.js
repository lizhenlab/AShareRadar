import { $, escapeHtml } from "./dom.js";
import {
  renderAlphaEvidence,
  renderChipAnalysis,
  renderDiagnosis,
  renderFactorLab,
  renderFeatureSnapshot,
  renderLeadership,
  renderSignalValidation,
} from "./research-factor-diagnostics.js";
import {
  renderAiDashboard,
  renderReplay,
  renderThemeContext,
} from "./research-qa-reports.js";
import {
  renderMarketRegime,
  renderRiskReward,
  renderTimeframeAlignment,
} from "./research-risk-reward.js";
import { asObject } from "./research-render-utils.js";

export function renderResearch(workbench, state) {
  const data = asObject(workbench);
  renderResearchPanel("aiDashboard", "AI单股驾驶舱", () => renderAiDashboard(data, state));
  renderResearchPanel("featureSnapshot", "特征快照", () => renderFeatureSnapshot(data.feature_snapshot));
  renderResearchPanel("diagnosisPanel", "个股诊断", () => renderDiagnosis(data.diagnosis));
  renderResearchPanel("alphaEvidence", "Alpha证据链", () => renderAlphaEvidence(data.alpha_evidence));
  renderResearchPanel("marketRegime", "市场环境", () => renderMarketRegime(data.market_regime));
  renderResearchPanel("signalValidation", "信号验证", () => renderSignalValidation(data.signal_validation));
  renderResearchPanel("timeframeAlignment", "多周期一致性", () => renderTimeframeAlignment(data.timeframe_alignment));
  renderResearchPanel("riskReward", "风险收益", () => renderRiskReward(data.risk_reward));
  renderResearchPanel("factorLab", "因子实验室", () => renderFactorLab(data.factor_lab));
  renderResearchPanel("themePanel", "题材背景", () => renderThemeContext(data.theme_context));
  renderResearchPanel("chipPanel", "筹码分析", () => renderChipAnalysis(data.chip_analysis));
  renderResearchPanel("leadershipPanel", "龙头识别", () => renderLeadership(data.leadership));
  renderResearchPanel("replayPanel", "历史回放", () => renderReplay(data.replay));
}

function renderResearchPanel(elementId, title, render) {
  try {
    render();
  } catch (error) {
    const el = $(elementId);
    if (!el) return;
    el.innerHTML = `
      <div class="empty-state">
        <strong>${escapeHtml(title)}暂不可用</strong>
        <span>${escapeHtml(error && error.message ? error.message : "该模块数据格式异常，主分析不受影响。")}</span>
      </div>`;
  }
}
