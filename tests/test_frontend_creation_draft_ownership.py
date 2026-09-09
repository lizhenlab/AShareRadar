"""Delayed creation receipts may clear only their complete submitted form draft."""
from __future__ import annotations

import pytest

from tests.test_frontend_notes_alerts_requests import _run_node_script


@pytest.mark.parametrize("operation", ["alert", "note"])
@pytest.mark.parametrize("edit", ["unchanged", "type", "value", "symbol"])
def test_creation_receipt_preserves_a_different_form_draft(operation: str, edit: str) -> None:
    _run_node_script(f'const operation = "{operation}"; const edit = "{edit}";\n' + r'''
      import { addAlertRule } from "./static/js/alerts.js";
      import { addStockNote } from "./static/js/notes.js";
      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH", lastAnalysis: null };
      const pending = deferredReply();
      const writes = [];
      const typeId = operation === "alert" ? "alertType" : "noteType";
      const valueId = operation === "alert" ? "alertThreshold" : "noteContent";
      const submittedType = operation === "alert" ? "price_above" : "观察";
      const submittedValue = operation === "alert" ? "12.5" : "观察关键价位";
      const revisedType = operation === "alert" ? "price_below" : "风险";
      const revisedValue = operation === "alert" ? "23.5" : "准备下一份草稿";
      dom.element(typeId).value = submittedType;
      dom.element(valueId).value = submittedValue;
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "POST") {
          writes.push({ url: String(url), body: JSON.parse(options.body), signal: options.signal });
          return pending.promise;
        }
        return jsonResponse([]);
      };
      const submission = operation === "alert"
        ? addAlertRule(state) : addStockNote(state, async () => {});
      await Promise.resolve();
      assert(writes.length === 1, "creation must start exactly one write");
      if (edit === "type") dom.element(typeId).value = revisedType;
      if (edit === "value") dom.element(valueId).value = revisedValue;
      if (edit === "symbol") state.symbol = "000001.SZ";
      const receipt = operation === "alert"
        ? { id: 1, symbol: "600519.SH", condition_type: submittedType, threshold: 12.5 }
        : { id: 1, symbol: "600519.SH", note_type: submittedType, content: submittedValue,
            visible: true, price: null, trade_date: null, color: null,
            created_at: "2026-09-10", updated_at: "2026-09-10", revision: "a".repeat(64) };
      pending.resolve(jsonResponse(receipt));
      await submission;
      const expectedValue = edit === "unchanged" ? "" : edit === "value" ? revisedValue : submittedValue;
      assert(dom.element(valueId).value === expectedValue,
        `late ${operation} receipt must preserve ${edit} draft; actual ${dom.element(valueId).value}`);
      assert(dom.element(typeId).value === (edit === "type" ? revisedType : submittedType), "receipt changed the selected type");
      assert(writes.length === 1 && !writes[0].signal.aborted, "view changes must not abort or repeat persistence");
      assert(writes[0].body.symbol === "600519.SH", "pending write changed its original stock");
      assert(writes[0].body[operation === "alert" ? "condition_type" : "note_type"] === submittedType,
        "pending write changed its original type");
      assert(writes[0].body[operation === "alert" ? "threshold" : "content"] === (operation === "alert" ? 12.5 : submittedValue),
        "pending write changed its original value");
    ''')
