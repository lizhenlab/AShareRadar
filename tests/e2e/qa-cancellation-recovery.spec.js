import { expect, test } from "@playwright/test";
import { mockApi } from "./frontend-flow-api-fixtures.mjs";

for (const requestedSymbol of ["600519", "000001"]) {
  test(`failed ${requestedSymbol} refresh releases retained question controls and the next answer succeeds`, async ({ page }) => {
    const questions = [];
    let failWorkbench = false;
    let releaseOldAnswer;
    const oldAnswerReady = new Promise(resolve => { releaseOldAnswer = resolve; });
    await mockApi(page, {
      async api(url, request) {
        if (url.pathname === "/api/stock/workbench" && failWorkbench) {
          return { payload: { detail: "隔离测试工作台暂不可用" }, status: 503 };
        }
        if (url.pathname !== "/api/stock/ask") return null;
        questions.push(request.postDataJSON());
        const first = questions.length === 1;
        if (first) await oldAnswerReady;
        return { payload: answerPayload(request.postDataJSON(), first ? "过期的第一问" : "恢复后的当前回答") };
      },
    });
    await page.goto("/");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    await page.locator("#workspace-tab-qa").click();
    const input = page.locator("#aiQuestionInput");
    const form = page.locator("#aiQuestionForm");
    const button = form.locator("button[type=submit]");
    await input.fill("风险在哪里？");
    await page.evaluate(() => { window.originalQuestionForm = document.getElementById("aiQuestionForm"); });
    await button.click();
    await expect.poll(() => questions.length).toBe(1);
    await expect(form).toHaveAttribute("aria-busy", "true");
    await expect(button).toBeDisabled();

    failWorkbench = true;
    if (!(await page.locator("#symbolInput").isVisible())) await page.locator("#queryPanelToggle").click();
    await page.locator("#symbolInput").fill(requestedSymbol);
    await page.locator("#searchForm button").click();
    await expect(page.locator("#dataStatus")).toContainText("仍显示贵州茅台");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    expect(await page.evaluate(() => window.originalQuestionForm === document.getElementById("aiQuestionForm"))).toBe(true);
    await expect(form).toHaveAttribute("aria-busy", "false");
    await expect(button).toBeEnabled();
    await expect(button).toHaveText("问一下");
    await expect(input).toHaveValue("风险在哪里？");

    releaseOldAnswer();
    await button.click();
    await expect(page.locator("#aiDashboard .ai-card-wide")).toContainText("恢复后的当前回答");
    await expect(page.locator("#aiDashboard")).not.toContainText("过期的第一问");
    await expect(form).toHaveAttribute("aria-busy", "false");
    await expect(button).toBeEnabled();
    expect(questions).toEqual([
      { symbol: "600519", question: "风险在哪里？" },
      { symbol: "600519.SH", question: "风险在哪里？" },
    ]);
  });
}

function answerPayload(question, answer) {
  return {
    ...question, updated_at: "2026-09-09 10:00:00", topic: "风险", conclusion: "按已有来源回答",
    answer, confidence: 70, answer_source: "规则问诊", llm_used: false,
    llm_status: "未配置大模型API", evidence: [], actions: [], invalidations: [], related_questions: [],
  };
}
