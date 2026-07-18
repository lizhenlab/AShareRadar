from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "research-activity.js"


def test_mixed_sources_are_normalized_sorted_and_labeled() -> None:
    script = r'''
      import { ACTIVITY_FILTERS, mergeResearchActivity } from "./static/js/research-activity.js";

      let fetchCalls = 0;
      globalThis.fetch = () => { fetchCalls += 1; throw new Error("network request was not expected"); };
      const result = mergeResearchActivity({
        adviceItems: [{
          id: 1,
          action: "\u4e70\u5165",
          reason: "\u4f18\u5148\u4f7f\u7528\u7ed3\u8bba\u4f9d\u636e",
          summary: "\u4e0d\u5e94\u4f7f\u7528\u7684\u5907\u7528\u6458\u8981",
          risk_level: "\u4e2d\u7b49",
          trend_label: "\u504f\u5f3a",
          repeat_count: 3,
          has_changes: true,
          changes: [],
          created_at: "2026-07-15 10:00:00",
        }],
        alertEvents: [{
          id: 2,
          name: "\u7a81\u7834\u63d0\u9192",
          event_type: "\u89e6\u53d1",
          message: "\u4ef7\u683c\u7a81\u7834\u9608\u503c",
          price: 12.34567,
          threshold: 12,
          created_at: "2026-07-15 12:00:00",
        }],
        notes: [
          {
            id: 3,
            note_type: "\u590d\u76d8",
            content: "\u66f4\u65b0\u540e\u7684\u7b14\u8bb0",
            price: 11.8,
            trade_date: "2026-07-15",
            created_at: "2026-07-15 09:00:00",
            updated_at: "2026-07-15 11:00:00",
          },
          {
            id: 4,
            note_type: "\u89c2\u5bdf",
            content: "\u521b\u5efa\u65f6\u95f4\u7b14\u8bb0",
            created_at: "2026-07-15 08:00:00",
            updated_at: "",
          },
        ],
      });

      assert(fetchCalls === 0, "merge made a network request");
      assert(JSON.stringify(result.skippedBySource) === JSON.stringify({ advice: 0, alert: 0, note: 0 }), "clean records were skipped");
      assert(result.items.map((item) => item.id).join(",") === "alert:2,note:3,advice:1,note:4", `mixed sort failed: ${JSON.stringify(result.items)}`);
      assert(result.items.every((item) => Number.isFinite(item.timestamp)), "timestamp was not finite");
      assert(JSON.stringify(Object.keys(result.items[0])) === JSON.stringify(["id", "kind", "occurredAt", "timestamp", "title", "summary", "meta", "tone"]), "stable item fields changed");

      const advice = result.items.find((item) => item.kind === "advice");
      const alert = result.items.find((item) => item.kind === "alert");
      const updatedNote = result.items.find((item) => item.id === "note:3");
      const createdNote = result.items.find((item) => item.id === "note:4");
      assert(advice.title === "\u4e70\u5165 \u00b7 \u7ed3\u8bba\u53d8\u5316", `advice title lost comparison semantics: ${advice.title}`);
      assert(advice.summary === "\u4f18\u5148\u4f7f\u7528\u7ed3\u8bba\u4f9d\u636e", "reason did not take precedence over summary");
      assert(advice.tone === "good", `advice tone was not derived safely: ${advice.tone}`);
      for (const text of ["\u98ce\u9669 \u4e2d\u7b49", "\u8d8b\u52bf \u504f\u5f3a", "\u5f52\u5e76 3 \u6b21"]) assert(advice.meta.includes(text), `advice meta omitted ${text}`);
      assert(alert.title === "\u7a81\u7834\u63d0\u9192 \u00b7 \u89e6\u53d1" && alert.summary === "\u4ef7\u683c\u7a81\u7834\u9608\u503c", "alert fields were not preserved");
      assert(alert.meta.includes("\u4ef7\u683c 12.3457") && alert.meta.includes("\u9608\u503c 12"), `alert meta was incomplete: ${alert.meta}`);
      assert(updatedNote.occurredAt === "2026-07-15 11:00:00" && updatedNote.meta.includes("\u66f4\u65b0\u65f6\u95f4"), "note did not use and identify updated_at");
      assert(createdNote.occurredAt === "2026-07-15 08:00:00" && createdNote.meta.includes("\u521b\u5efa\u65f6\u95f4"), "note did not identify created_at fallback");
      assert(ACTIVITY_FILTERS.map((item) => item.value).join(",") === "all,advice,alert,note" && Object.isFrozen(ACTIVITY_FILTERS), "activity filters are not stable");

      function assert(condition, message) {
        if (!condition) throw new Error(message);
      }
    '''
    _run_node_script(script)


def test_equal_timestamps_keep_source_and_input_order_stable() -> None:
    script = r'''
      import { mergeResearchActivity } from "./static/js/research-activity.js";

      const time = "2026-07-15T10:00:00+08:00";
      const payload = {
        adviceItems: [advice(1), advice(2), { id: 5, action: null, reason: "legacy", created_at: time }],
        alertEvents: [alert(3), { ...alert(6), event_type: "\u6062\u590d" }],
        notes: [note(4)],
      };
      const first = mergeResearchActivity(payload);
      const second = mergeResearchActivity(payload);
      const expected = "advice:1,advice:2,advice:5,alert:3,alert:6,note:4";
      if (first.items.map((item) => item.id).join(",") !== expected) {
        throw new Error(`equal-time order was unstable: ${JSON.stringify(first.items)}`);
      }
      if (JSON.stringify(first) !== JSON.stringify(second)) throw new Error("same input produced a different result");
      if (!first.items.find((item) => item.id === "advice:5")?.title.includes("\u5efa\u8bae\u5f85\u786e\u8ba4")) throw new Error("legacy null action was discarded");
      if (first.items.find((item) => item.id === "alert:6")?.tone !== "good") throw new Error("recovery alert retained warning tone");

      function advice(id) {
        return { id, action: `A${id}`, reason: `R${id}`, has_changes: false, created_at: time };
      }
      function alert(id) {
        return { id, name: `N${id}`, event_type: "E", message: "M", price: 10, threshold: 9, created_at: time };
      }
      function note(id) {
        return { id, note_type: "T", content: "C", created_at: time, updated_at: "" };
      }
    '''
    _run_node_script(script)


def test_limit_validation_filtering_and_flat_item_markup() -> None:
    script = r'''
      import { mergeResearchActivity, renderResearchActivity } from "./static/js/research-activity.js";

      const payload = {
        adviceItems: [advice(1, "09"), advice(2, "12")],
        alertEvents: [alert(3, "11")],
        notes: [note(4, "10")],
      };
      const limited = mergeResearchActivity({ ...payload, limit: 2 });
      assert(limited.items.map((item) => item.id).join(",") === "advice:2,alert:3", `limit was applied before sorting: ${JSON.stringify(limited.items)}`);
      assert(mergeResearchActivity({ ...payload, limit: 1 }).items.length === 1, "minimum limit failed");
      assert(mergeResearchActivity({ ...payload, limit: 100 }).items.length === 4, "maximum limit failed");
      for (const bad of [0, 101, 1.5, "2", Infinity, NaN]) {
        let threw = false;
        try { mergeResearchActivity({ ...payload, limit: bad }); } catch { threw = true; }
        assert(threw, `invalid limit was accepted: ${String(bad)}`);
      }

      const merged = mergeResearchActivity(payload);
      const target = element();
      const filtered = renderResearchActivity({
        ...merged,
        activeKind: "alert",
        sourceStates: readyStates(),
      }, target);
      assert(filtered.visibleCount === 1 && target.innerHTML.includes('data-kind="alert"'), "alert filter did not isolate alerts");
      assert((target.innerHTML.match(/role="listitem"/g) || []).length === 1, "filtered render emitted the wrong item count");
      for (const marker of ["research-activity-kind", "<time", "research-activity-title", "research-activity-summary", "research-activity-meta"]) {
        assert(target.innerHTML.includes(marker), `rendered item omitted ${marker}`);
      }
      assert(!/<article[^>]*>[\s\S]*<article/.test(target.innerHTML), "activity cards were nested");

      const all = renderResearchActivity({ ...merged, activeKind: "invalid", sourceStates: readyStates() }, target);
      assert(all.activeKind === "all" && all.visibleCount === 4, "invalid filter did not fall back to all");
      const noteOnly = renderResearchActivity({ ...merged, activeKind: "note", sourceStates: readyStates() }, target);
      assert(noteOnly.visibleCount === 1 && target.innerHTML.includes('data-kind="note"'), "note filter failed");

      function advice(id, hour) {
        return { id, action: `A${id}`, reason: "R", has_changes: false, created_at: `2026-07-15 ${hour}:00:00` };
      }
      function alert(id, hour) {
        return { id, name: "N", event_type: "E", message: "M", price: 10, threshold: 9, created_at: `2026-07-15 ${hour}:00:00` };
      }
      function note(id, hour) {
        return { id, note_type: "T", content: "C", created_at: `2026-07-15 ${hour}:00:00` };
      }
      function readyStates() {
        return { advice: { phase: "ready", message: "" }, alert: { phase: "ready", message: "" }, note: { phase: "ready", message: "" } };
      }
      function element() {
        return { innerHTML: "", attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
      }
      function assert(condition, message) {
        if (!condition) throw new Error(message);
      }
    '''
    _run_node_script(script)


def test_dirty_records_arrays_and_non_finite_values_are_reported() -> None:
    script = r'''
      import { mergeResearchActivity, renderResearchActivity } from "./static/js/research-activity.js";

      const dirty = mergeResearchActivity({
        adviceItems: [
          null,
          { id: 1, action: {}, reason: "R", created_at: "2026-07-15 10:00:00" },
          { id: 2, action: "A", reason: [], created_at: "2026-07-15 10:00:00" },
          { id: 3, action: "A", reason: "R", confidence: Infinity, created_at: "2026-07-15 10:00:00" },
          { id: 4, action: "A", reason: "R", created_at: "2026-02-30 10:00:00" },
          { id: 5, action: "A", reason: "safe", has_changes: false, created_at: "2026-07-15 10:00:00" },
        ],
        alertEvents: [
          { id: 6, name: "N", event_type: "E", message: {}, price: 10, threshold: 9, created_at: "2026-07-15 11:00:00" },
          { id: 7, name: "N", event_type: "E", message: "M", price: NaN, threshold: 9, created_at: "2026-07-15 11:00:00" },
          { id: 8, name: "N", event_type: "E", message: "M", price: 10, threshold: "9", created_at: "2026-07-15 11:00:00" },
        ],
        notes: [
          { id: 9, note_type: "T", content: ["bad"], created_at: "2026-07-15 12:00:00" },
          { id: 10, note_type: "T", content: "C", price: -Infinity, created_at: "2026-07-15 12:00:00" },
          { id: 11, note_type: "T", content: "C", updated_at: {}, created_at: "2026-07-15 12:00:00" },
        ],
      });
      assert(dirty.items.length === 1 && dirty.items[0].id === "advice:5", `dirty values entered the result: ${JSON.stringify(dirty)}`);
      assert(JSON.stringify(dirty.skippedBySource) === JSON.stringify({ advice: 5, alert: 3, note: 3 }), `skip counts were wrong: ${JSON.stringify(dirty.skippedBySource)}`);
      const serialized = JSON.stringify(dirty);
      for (const unsafe of ["[object Object]", "Infinity", "NaN"]) assert(!serialized.includes(unsafe), `unsafe value leaked as ${unsafe}`);

      const invalidArrays = mergeResearchActivity({ adviceItems: {}, alertEvents: null, notes: "bad" });
      assert(invalidArrays.items.length === 0, "non-array sources produced items");
      assert(JSON.stringify(invalidArrays.skippedBySource) === JSON.stringify({ advice: 1, alert: 1, note: 1 }), "non-array sources were silently treated as empty");

      const target = { innerHTML: "", setAttribute() {} };
      renderResearchActivity({ ...invalidArrays, sourceStates: readyStates() }, target);
      assert(target.innerHTML.includes("\u672c\u5730\u7814\u7a76\u8bb0\u5f55\u65e0\u6cd5\u5b89\u5168\u5c55\u793a"), "all-dirty data was rendered as empty");
      for (const text of ["\u5efa\u8bae 1 \u6761", "\u63d0\u9192 1 \u6761", "\u7b14\u8bb0 1 \u6761"]) assert(target.innerHTML.includes(text), `source skip detail omitted ${text}`);
      assert(!target.innerHTML.includes("\u6682\u65e0\u672c\u5730\u7814\u7a76\u6d3b\u52a8"), "dirty input was mislabeled as a true empty state");

      function readyStates() {
        return { advice: { phase: "ready", message: "" }, alert: { phase: "ready", message: "" }, note: { phase: "ready", message: "" } };
      }
      function assert(condition, message) {
        if (!condition) throw new Error(message);
      }
    '''
    _run_node_script(script)


def test_output_text_and_source_messages_have_hard_length_caps() -> None:
    script = r'''
      import { mergeResearchActivity, renderResearchActivity } from "./static/js/research-activity.js";

      const long = "X".repeat(2000);
      const longSourceMessage = "Y".repeat(2000);
      const merged = mergeResearchActivity({
        adviceItems: [{ id: 1, action: long, reason: long, risk_level: long, trend_label: long, repeat_count: 999999, has_changes: true, created_at: "2026-07-15 10:00:00" }],
        alertEvents: [{ id: 2, name: long, event_type: long, message: long, price: 1e308, threshold: -1e-7, created_at: "2026-07-15 11:00:00" }],
        notes: [{ id: 3, note_type: long, content: long, trade_date: long, created_at: "2026-07-15 12:00:00" }],
      });
      assert(merged.items.length === 3, `long scalar text was rejected instead of bounded: ${JSON.stringify(merged.skippedBySource)}`);
      for (const item of merged.items) {
        assert(item.id.length <= 80, "id exceeded its cap");
        assert(item.occurredAt.length <= 40, "occurredAt exceeded its cap");
        assert(item.title.length <= 120, "title exceeded its cap");
        assert(item.summary.length <= 500, "summary exceeded its cap");
        assert(item.meta.length <= 240, "meta exceeded its cap");
        assert(item.kind.length <= 6 && item.tone.length <= 7, "enum output was unexpectedly long");
        assert(Number.isFinite(item.timestamp), "timestamp was non-finite");
      }

      const target = { innerHTML: "", setAttribute() {} };
      renderResearchActivity({
        ...merged,
        sourceStates: {
          advice: { phase: "ready", message: "" },
          alert: { phase: "unavailable", message: longSourceMessage },
          note: { phase: "ready", message: "" },
        },
      }, target);
      assert(target.innerHTML.includes("Y".repeat(180)), "source message was truncated below its cap");
      assert(!target.innerHTML.includes("Y".repeat(181)), "source message exceeded its cap");

      function assert(condition, message) {
        if (!condition) throw new Error(message);
      }
    '''
    _run_node_script(script)


def test_partial_and_total_unavailable_states_remain_distinct() -> None:
    script = r'''
      import { mergeResearchActivity, renderResearchActivity } from "./static/js/research-activity.js";

      const merged = mergeResearchActivity({
        adviceItems: [{ id: 1, action: "A", reason: "R", created_at: "2026-07-15 10:00:00" }],
        alertEvents: [],
        notes: [{ id: 2, note_type: "T", content: "C", created_at: "2026-07-15 11:00:00" }],
      });
      const target = element();
      renderResearchActivity({
        ...merged,
        sourceStates: {
          advice: { phase: "ready", message: "" },
          alert: { phase: "unavailable", message: "\u672c\u5730\u63d0\u9192\u8bfb\u53d6\u5931\u8d25" },
          note: { phase: "ready", message: "" },
        },
      }, target);
      assert(target.innerHTML.includes("\u90e8\u5206\u672c\u5730\u8bb0\u5f55\u6682\u4e0d\u53ef\u7528"), "partial unavailable banner was missing");
      assert(target.innerHTML.includes("\u63d0\u9192\uff1a\u672c\u5730\u63d0\u9192\u8bfb\u53d6\u5931\u8d25"), "unavailable category and detail were missing");
      assert((target.innerHTML.match(/role="listitem"/g) || []).length === 2, "available records were not rendered with a partial failure");

      renderResearchActivity({
        items: [],
        skippedBySource: { advice: 0, alert: 0, note: 0 },
        sourceStates: {
          advice: { phase: "unavailable", message: "A" },
          alert: { phase: "unavailable", message: "B" },
          note: { phase: "unavailable", message: "C" },
        },
      }, target);
      assert(target.innerHTML.includes("\u5168\u90e8\u672c\u5730\u8bb0\u5f55\u6682\u4e0d\u53ef\u7528"), "all-unavailable state was missing");
      for (const label of ["\u5efa\u8bae\uff1aA", "\u63d0\u9192\uff1aB", "\u7b14\u8bb0\uff1aC"]) assert(target.innerHTML.includes(label), `all-unavailable detail omitted ${label}`);
      assert(!target.innerHTML.includes("\u6682\u65e0\u672c\u5730\u7814\u7a76\u6d3b\u52a8"), "all-unavailable state was mislabeled as empty");

      function element() {
        return { innerHTML: "", attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
      }
      function assert(condition, message) {
        if (!condition) throw new Error(message);
      }
    '''
    _run_node_script(script)


def test_loading_filter_empty_and_true_empty_states_do_not_collide() -> None:
    script = r'''
      import { mergeResearchActivity, renderResearchActivity } from "./static/js/research-activity.js";

      const target = element();
      renderResearchActivity({
        items: [],
        skippedBySource: { advice: 0, alert: 0, note: 0 },
        sourceStates: {
          advice: { phase: "loading", message: "\u8bfb\u53d6\u4e2d" },
          alert: { phase: "ready", message: "" },
          note: { phase: "ready", message: "" },
        },
      }, target);
      assert(target.innerHTML.includes("\u90e8\u5206\u672c\u5730\u8bb0\u5f55\u6b63\u5728\u52a0\u8f7d"), "loading state was missing");
      assert(!target.innerHTML.includes("\u6682\u65e0\u672c\u5730\u7814\u7a76\u6d3b\u52a8"), "loading was mislabeled as empty");
      assert(target.attributes["aria-busy"] === "true", "loading did not set aria-busy");

      renderResearchActivity({ items: [], skippedBySource: { advice: 0, alert: 0, note: 0 }, sourceStates: readyStates() }, target);
      assert(target.innerHTML.includes("\u6682\u65e0\u672c\u5730\u7814\u7a76\u6d3b\u52a8"), "true empty state was missing");
      assert(!target.innerHTML.includes("\u6682\u4e0d\u53ef\u7528") && !target.innerHTML.includes("\u52a0\u8f7d\u4e2d"), "true empty state retained another phase");
      assert(target.attributes["aria-busy"] === "false", "ready empty state remained busy");

      const merged = mergeResearchActivity({
        adviceItems: [{ id: 1, action: "A", reason: "R", created_at: "2026-07-15 10:00:00" }],
        alertEvents: [],
        notes: [],
      });
      renderResearchActivity({ ...merged, activeKind: "note", sourceStates: readyStates() }, target);
      assert(target.innerHTML.includes("\u5f53\u524d\u7c7b\u522b\u6682\u65e0\u672c\u5730\u7814\u7a76\u6d3b\u52a8"), "filter-empty state was not distinct");

      function readyStates() {
        return { advice: { phase: "ready", message: "" }, alert: { phase: "ready", message: "" }, note: { phase: "ready", message: "" } };
      }
      function element() {
        return { innerHTML: "", attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
      }
      function assert(condition, message) {
        if (!condition) throw new Error(message);
      }
    '''
    _run_node_script(script)


def test_all_dynamic_html_is_escaped_and_invalid_render_items_are_visible_as_skips() -> None:
    script = r'''
      import { mergeResearchActivity, renderResearchActivity } from "./static/js/research-activity.js";

      const attack = '<img src=x onerror="globalThis.pwned=1">';
      const scriptAttack = "<script>globalThis.pwned=2</script>";
      const merged = mergeResearchActivity({
        adviceItems: [{ id: 1, action: attack, reason: scriptAttack, risk_level: "<b>risk</b>", trend_label: "<svg>trend</svg>", created_at: "2026-07-15 10:00:00" }],
        alertEvents: [{ id: 2, name: attack, event_type: "<i>event</i>", message: scriptAttack, price: 10, threshold: 9, created_at: "2026-07-15 11:00:00" }],
        notes: [{ id: 3, note_type: attack, content: scriptAttack, trade_date: "<u>date</u>", created_at: "2026-07-15 12:00:00" }],
      });
      const invalidRenderedItem = { ...merged.items[0], id: ["bad"], title: { bad: true } };
      const target = { innerHTML: "", setAttribute() {} };
      renderResearchActivity({
        items: [...merged.items, invalidRenderedItem],
        skippedBySource: merged.skippedBySource,
        sourceStates: {
          advice: { phase: "ready", message: "" },
          alert: { phase: "unavailable", message: '<iframe src="x"></iframe>' },
          note: { phase: "ready", message: "" },
        },
      }, target);

      for (const tag of ["<img", "<script", "<b>", "<svg", "<i>", "<u>", "<iframe"]) {
        assert(!target.innerHTML.includes(tag), `dynamic markup was not escaped: ${tag}`);
      }
      for (const escaped of ["&lt;img", "&lt;script&gt;", "&lt;b&gt;", "&lt;svg&gt;", "&lt;iframe"]) {
        assert(target.innerHTML.includes(escaped), `escaped text was lost: ${escaped}`);
      }
      assert((target.innerHTML.match(/role="listitem"/g) || []).length === 3, "invalid render item changed the safe item count");
      assert(target.innerHTML.includes("\u6d3b\u52a8\u9879 1 \u6761"), "invalid render item was silently hidden");
      assert(!target.innerHTML.includes("[object Object]") && !globalThis.pwned, "object content leaked or executed");

      function assert(condition, message) {
        if (!condition) throw new Error(message);
      }
    '''
    _run_node_script(script)


def test_module_is_ascii_and_has_no_request_implementation() -> None:
    source = MODULE.read_bytes()
    assert source.isascii()
    text = source.decode("ascii")
    assert "fetch(" not in text
    assert "XMLHttpRequest" not in text
    assert "/api/" not in text


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
