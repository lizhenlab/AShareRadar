from __future__ import annotations

import pytest

from test_frontend_review_scan import _run_node


PRELUDE = r'''
  const elements = new Map([
    "reviewPlanList", "reviewPlanLoadMore", "reviewAdviceId", "reviewPlanSubmit", "reviewPlanCancel",
    "reviewHypothesis", "reviewTrigger", "reviewInvalidation", "reviewTarget", "reviewStop", "reviewHorizon",
  ].map(id => [id, { value: "", innerHTML: "", disabled: false, focus() {} }]));
  elements.set("reviewPlanFeedback", { textContent: "", dataset: {}, hidden: true });
  globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
  const api = await import("./static/js/advice-reviews.js");
  const state = { symbol: "600519.SH", adviceReviewDetails: [] };
  function snapshot(id = 3, overrides = {}) {
    return { id, symbol: state.symbol, price: 100, market_time: "2026-07-16 10:00:00",
      summary: `默认假设${id}`, kline_adjustment_mode: "qfq", kline_anchor_date: "2026-07-15",
      kline_anchor_close: 100, kline_data_version: "fixture.v1", kline_contract_version: "fixture.v1",
      snapshot_contract_version: "fixture.v1", rule_version: "fixture.v1", ...overrides };
  }
  function draft(text = "用户最新草稿") {
    for (const id of ["reviewHypothesis", "reviewTrigger", "reviewInvalidation"]) elements.get(id).value = text;
    elements.get("reviewTarget").value = "120";
    elements.get("reviewStop").value = "90";
    elements.get("reviewHorizon").value = "35";
  }
  function values() {
    return ["reviewHypothesis", "reviewTrigger", "reviewInvalidation", "reviewTarget", "reviewStop", "reviewHorizon"]
      .map(id => String(elements.get(id).value));
  }
  function assert(condition, message) { if (!condition) throw new Error(message); }
  function jsonResponse(payload, status = 200) {
    return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
  }
  api.syncAdviceReviewSnapshots(state, [snapshot(), snapshot(4, { price: 200 })], null);
'''


@pytest.mark.parametrize("append", [False, True])
def test_review_list_read_preserves_latest_draft_at_response_time(append: bool) -> None:
    _run_node(PRELUDE + f"const append = {str(append).lower()};" + r'''
      state.adviceReviewHasMore = true;
      state.adviceReviewDetails = Array.from({ length: 20 }, (_, index) => ({
        plan: completeReviewPlan({ id: index + 10, advice_id: index + 100 }), latest_evaluation: null,
      }));
      let resolveResponse;
      let requestUrl;
      globalThis.fetch = url => {
        requestUrl = String(url);
        return new Promise(resolve => { resolveResponse = resolve; });
      };
      draft("请求前草稿");
      const pending = append ? api.loadMoreAdviceReviews(state) : api.loadAdviceReviews(state);
      await Promise.resolve();
      draft();
      const expected = values();
      resolveResponse(jsonResponse([]));
      assert(await pending, "list did not finish");
      assert(!append || requestUrl.endsWith("&offset=20"), "did not exercise a real next page");
      assert(JSON.stringify(values()) === JSON.stringify(expected), "list response erased the latest draft");
      assert(elements.get("reviewAdviceId").value === "3", "list changed the selected snapshot");
    ''')


def test_review_snapshot_sync_preserves_same_snapshot_draft_and_intentional_blanks() -> None:
    _run_node(PRELUDE + r'''
      draft();
      elements.get("reviewHypothesis").value = "";
      elements.get("reviewTarget").value = "";
      const expected = values();
      api.syncAdviceReviewSnapshots(state, [snapshot(5), snapshot(3, { summary: "后台改写默认文案" })], null);
      assert(JSON.stringify(values()) === JSON.stringify(expected), "snapshot sync erased edits or intentional blanks");
      assert(elements.get("reviewAdviceId").value === "3", "new snapshot stole the selection");
    ''')


def test_review_explicit_snapshot_change_resets_draft_and_then_preserves_new_edits() -> None:
    _run_node(PRELUDE + r'''
      draft();
      elements.get("reviewAdviceId").value = "4";
      assert(api.selectAdviceReviewSnapshot(state), "explicit switch was rejected");
      assert(elements.get("reviewHypothesis").value === "默认假设4", "explicit switch did not reset text");
      assert(Number(elements.get("reviewTarget").value) === 210, "explicit switch did not reset prices");
      draft("新快照草稿");
      const expected = values();
      api.syncAdviceReviewSnapshots(state, [snapshot(), snapshot(4, { price: 200 })], null);
      assert(JSON.stringify(values()) === JSON.stringify(expected), "new snapshot draft was not retained");
    ''')


def test_review_switching_stock_never_attaches_old_draft_to_same_numeric_snapshot_id() -> None:
    _run_node(PRELUDE + r'''
      draft();
      state.symbol = "000001.SZ";
      api.syncAdviceReviewSnapshots(state, [snapshot(3, { price: 200, summary: "另一股票的快照" })], null);
      assert(elements.get("reviewHypothesis").value === "另一股票的快照", "old stock draft crossed its owner");
      assert(Number(elements.get("reviewTarget").value) === 210, "old stock prices crossed their owner");
    ''')


def test_review_cancel_edit_resets_to_selected_snapshot_defaults() -> None:
    _run_node(PRELUDE + r'''
      state.adviceReviewDetails = [{ plan: completeReviewPlan({ advice_id: 9 }), latest_evaluation: null }];
      api.beginAdviceReviewEdit(state, 7);
      draft("未保存的编辑");
      api.cancelAdviceReviewEdit(state);
      assert(state.adviceReviewEditingPlanId === null, "cancel kept the editing owner");
      assert(elements.get("reviewHypothesis").value === "默认假设3", "cancel retained the abandoned edit");
      assert(Number(elements.get("reviewTarget").value) === 105, "cancel retained edited prices");
    ''')


@pytest.mark.parametrize("success", [False, True])
def test_review_submit_has_explicit_draft_reset_boundary(success: bool) -> None:
    _run_node(PRELUDE + f"const success = {str(success).lower()};" + r'''
      draft();
      let releaseRead;
      let releaseWrite;
      globalThis.fetch = (url, options = {}) => options.method === "POST"
        ? new Promise(resolve => { releaseWrite = resolve; })
        : new Promise(resolve => { releaseRead = resolve; });
      const expected = values();
      const pending = api.submitAdviceReviewPlan(state).catch(error => error);
      await Promise.resolve();
      releaseWrite(success ? jsonResponse(completeReviewPlan()) : jsonResponse({ detail: "拒绝保存" }, 409));
      for (let index = 0; index < 20 && success && !releaseRead; index += 1) await new Promise(resolve => setTimeout(resolve, 0));
      if (success) {
        assert(elements.get("reviewHypothesis").value === "默认假设3", "successful write did not reset before readback");
        draft("保存后继续输入的新草稿");
        releaseRead(jsonResponse([]));
      }
      const result = await pending;
      if (success) {
        assert(result.id === 7, "successful write failed");
        assert(elements.get("reviewHypothesis").value === "保存后继续输入的新草稿", "late readback erased post-save edits");
      } else {
        assert(result instanceof Error, "failed write was accepted");
        assert(JSON.stringify(values()) === JSON.stringify(expected), "failed write erased the draft");
      }
    ''')
