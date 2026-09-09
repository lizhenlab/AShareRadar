"""A failed older-page read preserves the usable review list and its edit draft."""
from __future__ import annotations

import pytest

from tests.test_frontend_review_plan_navigation import SETUP
from tests.test_frontend_review_scan import _run_node


@pytest.mark.parametrize("status", [400, 503])
def test_review_older_page_failure_preserves_rows_draft_and_retry_cursor(status: int) -> None:
    _run_node(SETUP + f"const failureStatus = {status};" + r'''
      const state = { symbol: "600519.SH", adviceReviewSnapshots: [] };
      const requests = [];
      let failures = 1;
      globalThis.fetch = async (url) => {
        requests.push(String(url));
        if (!String(url).includes("offset=20")) return json(page(101, 20));
        if (failures-- > 0) return json({ detail: "后续页面读取失败" }, failureStatus);
        return json(page(121, 1));
      };
      assert(await reviews.loadAdviceReviews(state), "first page did not load");
      reviews.beginAdviceReviewEdit(state, 101);
      el("reviewHypothesis").value = "尚未提交的编辑";
      el("reviewTarget").value = "123";
      el("reviewPlanFeedback").textContent = "独立的保存提示";
      const html = el("reviewPlanList").innerHTML;
      const ids = JSON.stringify(state.adviceReviewPagePlanIds);
      assert(await reviews.loadMoreAdviceReviews(state) === false, "failed page claimed success");
      assert(el("reviewPlanList").innerHTML === html, "failed older page erased already visible plans");
      assert(state.adviceReviewNextOffset === 20 && JSON.stringify(state.adviceReviewPagePlanIds) === ids, "failed page advanced its cursor");
      assert(el("reviewHypothesis").value === "尚未提交的编辑" && el("reviewTarget").value === "123", "page failure erased the draft");
      assert(state.adviceReviewEditingPlanId === 101, "page failure lost editing identity");
      assert(!el("reviewPlanLoadMore").hidden, "failed page removed its retry entry");
      assert(!el("reviewPlanPageFeedback").hidden && el("reviewPlanPageFeedback").textContent.includes("重试"), "page failure has no local retry explanation");
      assert(el("reviewPlanFeedback").textContent === "独立的保存提示", "page failure overwrote form feedback");
      assert(await reviews.loadMoreAdviceReviews(state), "same-page retry did not succeed");
      assert(requests.slice(1).every(url => url.endsWith("&offset=20")), "retry skipped the failed page");
      assert(state.adviceReviewDetails.length === 21 && countPlan(101) === 1 && countPlan(121) === 1, "retry duplicated or lost plans");
      assert(state.adviceReviewNextOffset === 21 && !state.adviceReviewHasMore, "retry cursor is inconsistent");
      assert(el("reviewPlanPageFeedback").hidden && !el("reviewPlanPageFeedback").textContent, "successful retry retained the page error");
      assert(el("reviewHypothesis").value === "尚未提交的编辑" && el("reviewTarget").value === "123", "successful retry reset the edit draft");
    ''')


@pytest.mark.parametrize("kind", ["wrong_symbol", "invalid_payload"])
def test_review_invalid_older_page_keeps_previously_verified_records(kind: str) -> None:
    _run_node(SETUP + f'const failureKind = "{kind}";' + r'''
      const state = initialState();
      reviews.renderAdviceReviewDetails(state.adviceReviewDetails, state);
      const html = el("reviewPlanList").innerHTML;
      globalThis.fetch = async () => json(failureKind === "wrong_symbol" ? [detail(121, "000001.SZ")] : { items: [] });
      assert(await reviews.loadMoreAdviceReviews(state) === false, "invalid page claimed success");
      assert(el("reviewPlanList").innerHTML === html && countPlan(101) === 1, "invalid page hid the verified first page");
      assert(state.adviceReviewNextOffset === 20 && state.adviceReviewDetails.length === 20, "invalid page was merged");
      assert(el("reviewPlanPageFeedback").textContent.includes("重试"), "invalid page omitted local error state");
    ''')


def test_review_empty_retry_closes_pagination_without_emptying_existing_page() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      reviews.renderAdviceReviewDetails(state.adviceReviewDetails, state);
      let reads = 0;
      globalThis.fetch = async () => ++reads <= 2 ? json({ detail: "unavailable" }, 503) : json([]);
      assert(await reviews.loadMoreAdviceReviews(state) === false, "first failure claimed success");
      assert(await reviews.loadMoreAdviceReviews(state) === false, "second failure claimed success");
      assert(await reviews.loadMoreAdviceReviews(state), "empty final page was treated as failure");
      assert(countPlan(101) === 1 && state.adviceReviewDetails.length === 20, "empty last page erased previous records");
      assert(state.adviceReviewNextOffset === 20 && !state.adviceReviewHasMore && el("reviewPlanLoadMore").hidden, "empty final page did not finish pagination");
      assert(el("reviewPlanPageFeedback").hidden, "empty successful retry kept the error");
    ''')


@pytest.mark.parametrize("cancel", [False, True])
def test_review_stale_or_cancelled_page_failure_cannot_replace_another_stock(cancel: bool) -> None:
    _run_node(SETUP + f"const cancelOld = {str(cancel).lower()};" + r'''
      const state = initialState();
      reviews.renderAdviceReviewDetails(state.adviceReviewDetails, state);
      const reply = deferred();
      const controller = new AbortController();
      globalThis.fetch = async (url) => String(url).includes("offset=20") ? reply.promise : json([detail(201, "000001.SZ")]);
      const pending = reviews.loadMoreAdviceReviews(state, { signal: controller.signal });
      if (cancelOld) controller.abort();
      state.symbol = "000001.SZ";
      assert(await reviews.loadAdviceReviews(state), "new stock did not load");
      const html = el("reviewPlanList").innerHTML;
      el("reviewPlanPageFeedback").textContent = "新股票提示";
      reply.resolve(json({ detail: "old failure" }, 503));
      assert(await pending === false, "old request claimed current success");
      assert(el("reviewPlanList").innerHTML === html && countPlan(201) === 1, "late failure changed another stock's list");
      assert(el("reviewPlanPageFeedback").textContent === "新股票提示", "late failure changed another stock's feedback");
      assert(state.adviceReviewNextOffset === 1, "late failure changed the new stock's cursor");
    ''')


def test_review_pinned_plan_and_normal_page_survive_failed_older_page() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      globalThis.fetch = async (url) => String(url).includes("/plans/") ? json(detail(7)) : json({ detail: "unavailable" }, 503);
      assert(await reviews.loadAdviceReviewPlan(state, 7), "selected plan did not load");
      const html = el("reviewPlanList").innerHTML;
      assert(await reviews.loadMoreAdviceReviews(state) === false, "failed older page claimed success");
      assert(el("reviewPlanList").innerHTML === html && countPlan(7) === 1 && countPlan(101) === 1, "page failure lost pin or ordinary rows");
      assert(state.adviceReviewNextOffset === 20 && state.adviceReviewPinnedDetail.plan.id === 7, "page failure consumed the pin as a page");
      assert(el("reviewPlanPageFeedback").textContent.includes("重试"), "pin bypassed local page-error feedback");
    ''')
