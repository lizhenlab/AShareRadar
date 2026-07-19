from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_local_data_import_requires_matching_dry_run_before_commit() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: true }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
          calls.push({ url: String(url), options });
          const dryRun = String(url).includes("dry_run=true");
          return new Response(JSON.stringify({
            bundle_version: 1, mode: "merge", dry_run: dryRun, committed: !dryRun,
            conflict_strategy: "source_wins_on_primary_key", tables: {},
            totals: { incoming: 1, inserted: 1, updated: 0, unchanged: 0, deleted: 0 },
            preview_token: dryRun ? "preview-token-with-at-least-thirty-two-characters" : null,
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        };
        const { readLocalDataFile, previewLocalDataImport, commitLocalDataImport, invalidateLocalDataImportPreview } = await import("./static/js/local-data.js");
        const state = {};
        const bundle = { kind: "ashare-radar-user-data", version: 1, tables: { watchlist: {} } };
        const file = { name: "user-data.json", size: 80, lastModified: 7, async text() { return JSON.stringify(bundle); } };

        await readLocalDataFile(state, file);
        await previewLocalDataImport(state);
        assert(elements.get("commitLocalDataImport").disabled === false, "successful preview did not unlock commit");
        elements.get("localDataImportMode").value = "replace";
        invalidateLocalDataImportPreview(state);
        await expectFailure(() => commitLocalDataImport(state), "请先对当前文件");
        elements.get("localDataImportMode").value = "merge";
        await previewLocalDataImport(state);
        await commitLocalDataImport(state);

        assert(calls.filter((call) => call.url.includes("dry_run=true")).length === 2, "preview was not sent as dry-run");
        assert(calls.at(-1).url.includes("dry_run=false"), "commit did not use the mutating endpoint");
        assert(calls.at(-1).url.includes("preview_token=preview-token"), "commit did not bind the server preview token");
        async function expectFailure(action, text) {
          try { await action(); } catch (error) { if (error.message.includes(text)) return; }
          throw new Error("expected failure was not raised");
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_new_file_selection_immediately_revokes_old_import_state_on_validation_failure() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: true }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        globalThis.fetch = async () => jsonResponse({
          dry_run: true,
          totals: { inserted: 1, updated: 0, unchanged: 0, deleted: 0 },
          preview_token: "preview-token-with-at-least-thirty-two-characters",
        });
        const {
          MAX_LOCAL_DATA_IMPORT_BYTES,
          commitLocalDataImport,
          previewLocalDataImport,
          readLocalDataFile,
        } = await import("./static/js/local-data.js");
        const state = {};

        await establishPreview("before-oversized");
        let oversizedTextCalls = 0;
        const oversizedPromise = readLocalDataFile(state, {
          name: "oversized.json", size: MAX_LOCAL_DATA_IMPORT_BYTES + 1, lastModified: 2,
          async text() { oversizedTextCalls += 1; return "{}"; },
        });
        assertInvalidated("oversized selection");
        await expectFailure(() => oversizedPromise, "不能超过50 MB");
        await expectFailure(() => commitLocalDataImport(state), "请先选择");
        assert(oversizedTextCalls === 0, "oversized file was read before size validation");

        await establishPreview("before-malformed");
        const malformedText = deferred();
        const malformedPromise = readLocalDataFile(state, {
          name: "malformed.json", size: 20, lastModified: 3,
          text() { return malformedText.promise; },
        });
        assertInvalidated("pending malformed selection");
        malformedText.resolve("{not-json");
        await expectFailure(() => malformedPromise, "不是有效 JSON");
        assertInvalidated("malformed selection");
        await expectFailure(() => commitLocalDataImport(state), "请先选择");

        await establishPreview("before-read-error");
        const failedRead = deferred();
        const failedReadPromise = readLocalDataFile(state, {
          name: "unreadable.json", size: 20, lastModified: 4,
          text() { return failedRead.promise; },
        });
        assertInvalidated("pending failed read");
        failedRead.reject(new Error("磁盘读取失败"));
        await expectFailure(() => failedReadPromise, "磁盘读取失败");
        assertInvalidated("failed read");
        await expectFailure(() => commitLocalDataImport(state), "请先选择");

        async function establishPreview(name) {
          const bundle = { kind: "ashare-radar-user-data", version: 1, marker: name };
          await readLocalDataFile(state, {
            name: `${name}.json`, size: 40, lastModified: 1,
            async text() { return JSON.stringify(bundle); },
          });
          await previewLocalDataImport(state);
          assert(elements.get("commitLocalDataImport").disabled === false, `${name} preview did not enable commit`);
        }
        function assertInvalidated(label) {
          assert(state.localDataImportBundle === null, `${label} retained the old bundle`);
          assert(state.localDataImportFileKey === "", `${label} retained the old file key`);
          assert(state.localDataImportPreview === null, `${label} retained the old preview token`);
          assert(elements.get("localDataImportPreview").innerHTML === "", `${label} retained the old preview rendering`);
          assert(elements.get("commitLocalDataImport").disabled === true, `${label} left commit enabled`);
        }
        function deferred() {
          let resolve;
          let reject;
          const promise = new Promise((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
          return { promise, resolve, reject };
        }
        function jsonResponse(value) {
          return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        async function expectFailure(action, text) {
          try { await action(); } catch (error) { if (error.message.includes(text)) return; }
          throw new Error("expected failure was not raised");
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_overlapping_local_data_file_reads_are_latest_selection_wins() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "old preview" }],
          ["commitLocalDataImport", { disabled: false }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const { readLocalDataFile } = await import("./static/js/local-data.js");
        const firstText = deferred();
        const secondText = deferred();
        const state = {};
        const firstRead = readLocalDataFile(state, {
          name: "first.json", size: 10, lastModified: 1,
          text() { return firstText.promise; },
        });
        const secondRead = readLocalDataFile(state, {
          name: "second.json", size: 11, lastModified: 2,
          text() { return secondText.promise; },
        });

        secondText.resolve(JSON.stringify({ marker: "second" }));
        const secondBundle = await secondRead;
        firstText.resolve(JSON.stringify({ marker: "first" }));
        const staleBundle = await firstRead;

        assert(secondBundle.marker === "second", "latest read did not return its bundle");
        assert(staleBundle === null, "stale read was not discarded");
        assert(state.localDataImportBundle.marker === "second", "late first read replaced the latest bundle");
        assert(state.localDataImportFileKey === "second.json:11:2", "late first read replaced the latest file key");
        assert(elements.get("localDataFeedback").textContent === "已读取 second.json", "late first read replaced current feedback");
        assert(elements.get("commitLocalDataImport").disabled === true, "new selection did not keep commit disabled");
        function deferred() {
          let resolve;
          const promise = new Promise((onResolve) => { resolve = onResolve; });
          return { promise, resolve };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_overlapping_local_data_previews_are_latest_request_wins() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: true }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const requests = [];
        globalThis.fetch = (url, options = {}) => {
          const response = deferred();
          requests.push({ url: String(url), options, response });
          return response.promise;
        };
        const { previewLocalDataImport } = await import("./static/js/local-data.js");
        const state = {
          localDataImportBundle: { kind: "ashare-radar-user-data", version: 1 },
          localDataImportFileKey: "bundle.json:20:1",
        };

        const firstPreview = previewLocalDataImport(state);
        const secondPreview = previewLocalDataImport(state);
        assert(requests.length === 2, "overlapping previews were not both requested");
        assert(elements.get("commitLocalDataImport").disabled === true, "new preview request retained an old commit authority");

        requests[1].response.resolve(previewResponse("latest-preview-token", 22));
        const latestResult = await secondPreview;
        assert(latestResult.preview_token === "latest-preview-token", "latest preview result was lost");
        assert(state.localDataImportPreview.preview_token === "latest-preview-token", "latest token was not installed");
        assert(elements.get("localDataImportPreview").innerHTML.includes("新增 22"), "latest preview was not rendered");
        assert(elements.get("commitLocalDataImport").disabled === false, "latest preview did not enable commit");

        requests[0].response.resolve(previewResponse("stale-preview-token", 11));
        const staleResult = await firstPreview;
        assert(staleResult === null, "stale preview response was not discarded");
        assert(state.localDataImportPreview.preview_token === "latest-preview-token", "stale preview replaced the latest token");
        assert(elements.get("localDataImportPreview").innerHTML.includes("新增 22"), "stale preview rendered over the latest result");
        assert(!elements.get("localDataImportPreview").innerHTML.includes("新增 11"), "stale preview totals remained visible");
        assert(elements.get("commitLocalDataImport").disabled === false, "stale preview disabled the current commit authority");
        function previewResponse(token, inserted) {
          return new Response(JSON.stringify({
            dry_run: true,
            totals: { inserted, updated: 0, unchanged: 0, deleted: 0 },
            preview_token: token,
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        function deferred() {
          let resolve;
          const promise = new Promise((onResolve) => { resolve = onResolve; });
          return { promise, resolve };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_pending_commit_does_not_revoke_a_newer_file_preview() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: true }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const commitResponse = deferred();
        let previewCount = 0;
        globalThis.fetch = (url) => {
          if (String(url).includes("dry_run=true")) {
            previewCount += 1;
            return Promise.resolve(jsonResponse({
              dry_run: true,
              totals: { inserted: previewCount, updated: 0, unchanged: 0, deleted: 0 },
              preview_token: `preview-token-${previewCount}-with-at-least-thirty-two-characters`,
            }));
          }
          return commitResponse.promise;
        };
        const { commitLocalDataImport, previewLocalDataImport, readLocalDataFile } = await import("./static/js/local-data.js");
        const state = {};

        await readLocalDataFile(state, file("first.json", 1));
        await previewLocalDataImport(state);
        const firstCommit = commitLocalDataImport(state);
        await Promise.resolve();

        await readLocalDataFile(state, file("second.json", 2));
        await previewLocalDataImport(state);
        const secondPreview = state.localDataImportPreview;
        assert(secondPreview.preview_token.includes("preview-token-2"), "new file preview was not installed during the old commit");

        commitResponse.resolve(jsonResponse({
          dry_run: false,
          committed: true,
          totals: { inserted: 1, updated: 0, unchanged: 0, deleted: 0 },
        }));
        await firstCommit;

        assert(state.localDataImportBundle.marker === 2, "old commit replaced the current file bundle");
        assert(state.localDataImportPreview === secondPreview, "old commit revoked the current preview token");
        assert(elements.get("commitLocalDataImport").disabled === false, "old commit disabled the current commit authority");
        assert(elements.get("localDataImportPreview").innerHTML.includes("新增 2"), "old commit rendered over the current preview");
        assert(elements.get("localDataFeedback").textContent.includes("先前选择") && elements.get("localDataFeedback").dataset.tone === "warn", "old commit completion was not explained");

        function file(name, marker) {
          const bundle = { kind: "ashare-radar-user-data", version: 1, marker };
          return { name, size: 20, lastModified: marker, async text() { return JSON.stringify(bundle); } };
        }
        function deferred() {
          let resolve;
          const promise = new Promise((onResolve) => { resolve = onResolve; });
          return { promise, resolve };
        }
        function jsonResponse(value) {
          return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_failed_pending_commit_does_not_pollute_a_newer_file_preview() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: true }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const commitResponse = deferred();
        let previewCount = 0;
        globalThis.fetch = (url) => {
          if (String(url).includes("dry_run=true")) {
            previewCount += 1;
            return Promise.resolve(jsonResponse({
              dry_run: true,
              totals: { inserted: previewCount, updated: 0, unchanged: 0, deleted: 0 },
              preview_token: `preview-token-${previewCount}-with-at-least-thirty-two-characters`,
            }));
          }
          return commitResponse.promise;
        };
        const { commitLocalDataImport, previewLocalDataImport, readLocalDataFile } = await import("./static/js/local-data.js");
        const state = {};

        await readLocalDataFile(state, file("first.json", 1));
        await previewLocalDataImport(state);
        const firstCommit = commitLocalDataImport(state);
        await Promise.resolve();

        await readLocalDataFile(state, file("second.json", 2));
        await previewLocalDataImport(state);
        const secondPreview = state.localDataImportPreview;
        commitResponse.resolve(jsonResponse({ detail: "旧文件备份失败" }, 503));

        assert(await firstCommit === null, "superseded commit failure was exposed as the current action");
        assert(state.localDataImportPreview === secondPreview, "old commit failure revoked the current preview");
        assert(elements.get("commitLocalDataImport").disabled === false, "old commit failure disabled current commit authority");
        assert(elements.get("localDataImportPreview").innerHTML.includes("新增 2"), "old commit failure replaced the current preview");
        assert(elements.get("localDataFeedback").textContent.includes("先前选择") && elements.get("localDataFeedback").textContent.includes("导入失败"), "superseded failure was not scoped to the old selection");

        function file(name, marker) {
          const bundle = { kind: "ashare-radar-user-data", version: 1, marker };
          return { name, size: 20, lastModified: marker, async text() { return JSON.stringify(bundle); } };
        }
        function deferred() {
          let resolve;
          const promise = new Promise((onResolve) => { resolve = onResolve; });
          return { promise, resolve };
        }
        function jsonResponse(value, status = 200) {
          return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_pending_local_data_preview_is_bound_to_file_key_and_mode() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: true }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const requests = [];
        globalThis.fetch = () => {
          const response = deferred();
          requests.push(response);
          return response.promise;
        };
        const {
          invalidateLocalDataImportPreview,
          previewLocalDataImport,
          readLocalDataFile,
        } = await import("./static/js/local-data.js");
        const state = {};
        await readLocalDataFile(state, file("first.json", 1));

        const firstFilePreview = previewLocalDataImport(state);
        await readLocalDataFile(state, file("second.json", 2));
        requests[0].resolve(previewResponse("first-file-token", 1));
        assert(await firstFilePreview === null, "preview for the old file was accepted");
        assertPreviewCleared("old file preview");

        const mergePreview = previewLocalDataImport(state);
        elements.get("localDataImportMode").value = "replace";
        invalidateLocalDataImportPreview(state);
        requests[1].resolve(previewResponse("merge-mode-token", 2));
        assert(await mergePreview === null, "preview for the old mode was accepted");
        assertPreviewCleared("old mode preview");
        assert(elements.get("localDataFeedback").textContent.includes("模式已变化"), "stale completion replaced mode-change feedback");

        function file(name, marker) {
          const bundle = { kind: "ashare-radar-user-data", version: 1, marker };
          return { name, size: 20, lastModified: marker, async text() { return JSON.stringify(bundle); } };
        }
        function assertPreviewCleared(label) {
          assert(state.localDataImportPreview === null, `${label} installed a token`);
          assert(elements.get("localDataImportPreview").innerHTML === "", `${label} rendered stale output`);
          assert(elements.get("commitLocalDataImport").disabled === true, `${label} enabled commit`);
        }
        function previewResponse(token, inserted) {
          return new Response(JSON.stringify({
            dry_run: true,
            totals: { inserted, updated: 0, unchanged: 0, deleted: 0 },
            preview_token: token,
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        function deferred() {
          let resolve;
          const promise = new Promise((onResolve) => { resolve = onResolve; });
          return { promise, resolve };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_local_data_export_uses_portable_filename_and_revokes_object_url() -> None:
    _run_node(
        r'''
        let clicked = false;
        let revoked = "";
        const anchor = { href: "", download: "", click() { clicked = true; } };
        const feedback = { textContent: "", dataset: {}, hidden: true };
        globalThis.document = {
          getElementById(id) { return id === "localDataFeedback" ? feedback : null; },
          createElement() { return anchor; },
        };
        globalThis.URL = {
          createObjectURL() { return "blob:test"; },
          revokeObjectURL(value) { revoked = value; },
        };
        globalThis.fetch = async () => new Response(JSON.stringify({ kind: "ashare-radar-user-data", version: 1 }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
        const { exportLocalUserData } = await import("./static/js/local-data.js");

        await exportLocalUserData({ now: new Date("2026-07-16T00:00:00Z") });

        assert(clicked && anchor.download === "ashare-radar-user-data-2026-07-16.json", "portable export filename was not used");
        assert(revoked === "blob:test", "temporary object URL was not revoked");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_cleanup_preview_failure_is_visible_and_disables_commit() -> None:
    _run_node(
        r'''
        const preview = { innerHTML: "尚未读取" };
        const button = { disabled: false };
        globalThis.document = {
          getElementById(id) {
            if (id === "runtimeCleanupPreview") return preview;
            if (id === "runRuntimeCleanup") return button;
            return null;
          },
        };
        globalThis.fetch = async () => new Response(JSON.stringify({ detail: "数据库忙" }), {
          status: 503, headers: { "Content-Type": "application/json" },
        });
        const { loadRuntimeCleanupPreview } = await import("./static/js/local-data.js");

        let failed = false;
        try { await loadRuntimeCleanupPreview(); } catch { failed = true; }

        assert(failed, "cleanup preview failure was swallowed");
        assert(preview.innerHTML.includes("读取失败") && preview.innerHTML.includes("数据库忙"), "cleanup failure was not rendered");
        assert(button.disabled === true, "cleanup remained enabled after preview failure");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_overlapping_cleanup_previews_only_render_the_latest_request() -> None:
    _run_node(
        r'''
        const preview = { innerHTML: "尚未读取" };
        const button = { disabled: true };
        globalThis.document = {
          getElementById(id) {
            if (id === "runtimeCleanupPreview") return preview;
            if (id === "runRuntimeCleanup") return button;
            return null;
          },
        };
        const requests = [];
        globalThis.fetch = () => {
          const response = deferred();
          requests.push(response);
          return response.promise;
        };
        const { loadRuntimeCleanupPreview } = await import("./static/js/local-data.js");

        const first = loadRuntimeCleanupPreview();
        const second = loadRuntimeCleanupPreview();
        requests[1].resolve(jsonResponse({ total_rows: 2, user_history_rows: 0, requires_user_backup: false }));
        assert((await second).total_rows === 2, "latest cleanup preview was not returned");
        assert(preview.innerHTML.includes("预计清理 2 条") && button.disabled === false, "latest cleanup preview was not rendered");

        requests[0].resolve(jsonResponse({ total_rows: 9, user_history_rows: 9, requires_user_backup: true }));
        assert(await first === null, "stale cleanup preview was not discarded");
        assert(preview.innerHTML.includes("预计清理 2 条") && !preview.innerHTML.includes("9 条"), "stale cleanup preview repainted the tools panel");
        assert(button.disabled === false, "stale cleanup preview changed the current action state");

        function deferred() {
          let resolve;
          const promise = new Promise((onResolve) => { resolve = onResolve; });
          return { promise, resolve };
        }
        function jsonResponse(value) {
          return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_failed_import_commit_consumes_browser_preview_and_requires_retry() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: true }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        let commitAttempts = 0;
        globalThis.fetch = async (url) => {
          if (String(url).includes("dry_run=true")) {
            return jsonResponse({
              bundle_version: 1, mode: "merge", dry_run: true, committed: false,
              conflict_strategy: "remap_surrogate_ids_source_wins_on_stable_keys", tables: {},
              totals: { incoming: 1, inserted: 1, updated: 0, unchanged: 0, deleted: 0, remapped: 0 },
              preview_token: "preview-token-with-at-least-thirty-two-characters",
            }, 200);
          }
          commitAttempts += 1;
          return jsonResponse({ detail: "备份失败" }, 503);
        };
        const { previewLocalDataImport, commitLocalDataImport } = await import("./static/js/local-data.js");
        const state = {
          localDataImportBundle: { kind: "ashare-radar-user-data", version: 1 },
          localDataImportFileKey: "bundle.json:1:1",
        };

        await previewLocalDataImport(state);
        await expectFailure(() => commitLocalDataImport(state), "备份失败");
        await expectFailure(() => commitLocalDataImport(state), "请先对当前文件");

        assert(commitAttempts === 1, "failed one-use token was submitted twice");
        assert(state.localDataImportPreview === null && elements.get("commitLocalDataImport").disabled, "failed commit kept a stale preview enabled");
        function jsonResponse(value, status) {
          return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
        }
        async function expectFailure(action, text) {
          try { await action(); } catch (error) { if (error.message.includes(text)) return; }
          throw new Error("expected failure was not raised");
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_expired_import_preview_disables_commit_and_clears_browser_state() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["localDataImportMode", { value: "merge" }],
          ["localDataImportPreview", { innerHTML: "" }],
          ["commitLocalDataImport", { disabled: false }],
          ["localDataFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        globalThis.fetch = async () => new Response(JSON.stringify({
          bundle_version: 1, mode: "merge", dry_run: true, committed: false,
          conflict_strategy: "remap_surrogate_ids_source_wins_on_stable_keys", tables: {},
          totals: { incoming: 0, inserted: 0, updated: 0, unchanged: 0, deleted: 0, remapped: 0 },
          preview_token: "expired-preview-token-with-thirty-two-characters",
          preview_expires_at: "2000-01-01T00:00:00Z",
        }), { status: 200, headers: { "Content-Type": "application/json" } });
        const { previewLocalDataImport } = await import("./static/js/local-data.js");
        const state = {
          localDataImportBundle: { kind: "ashare-radar-user-data", version: 1 },
          localDataImportFileKey: "bundle.json:1:1",
        };

        await previewLocalDataImport(state);
        await new Promise((resolve) => setTimeout(resolve, 0));

        assert(state.localDataImportPreview === null, "expired preview remained in browser state");
        assert(elements.get("commitLocalDataImport").disabled === true, "expired preview left commit enabled");
        assert(elements.get("localDataFeedback").textContent.includes("已过期"), "expiry was not explained");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_system_diagnostics_renders_over_budget_usage_and_row_boundaries() -> None:
    _run_node(
        r'''
        const storage = { innerHTML: "" };
        const messages = { innerHTML: "" };
        globalThis.document = { getElementById(id) { return id === "storageDiagnostics" ? storage : id === "diagnosticMessages" ? messages : null; } };
        const { renderSystemDiagnostics } = await import("./static/js/diagnostics.js");

        renderSystemDiagnostics({
          storage: {
            db_size_mb: 20, budget_bytes: 16 * 1024 * 1024, usage_pct: 125,
            sqlite_size_bytes: 16 * 1024 * 1024, backup_size_bytes: 4 * 1024 * 1024,
            managed_backup_count: 2, cache_rows: 30, runtime_rows: 4, user_rows: 7,
            quote_rows: 11, kline_rows: 12, market_scan_rows: 3,
          },
          warnings: ["本地数据库已超过容量预算。"],
          suggestions: ["先备份用户数据。"],
        });

        assert(storage.innerHTML.includes("125%") && storage.innerHTML.includes("7</b>用户数据"), "storage diagnostics hid over-budget or user rows");
        assert(storage.innerHTML.includes("备份 4.00 MB（2 份）") && storage.innerHTML.includes("11</b>行情") && storage.innerHTML.includes("3</b>全市场扫描"), "storage diagnostics hid category details");
        assert(messages.innerHTML.includes("超过容量预算") && messages.innerHTML.includes("先备份"), "diagnostic guidance was not rendered");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def _run_node(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
