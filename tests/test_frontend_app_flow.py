from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_app_state_helpers_guard_current_symbol_and_stream_frames() -> None:
    script = r'''
      const elements = new Map();

      function classList() {
        const values = new Set();
        return {
          add(value) { values.add(value); },
          remove(value) { values.delete(value); },
          toggle(value, active) {
            if (active) values.add(value);
            else values.delete(value);
          },
          contains(value) { return values.has(value); },
        };
      }

      function element(id) {
        if (!elements.has(id)) {
          elements.set(id, {
            id,
            value: "",
            innerHTML: "",
            textContent: "",
            className: "",
            dataset: {},
            disabled: false,
            classList: classList(),
            addEventListener(type, handler) {
              this.listeners = this.listeners || {};
              this.listeners[type] = handler;
            },
            querySelector() {
              return element(`${id}-button`);
            },
            querySelectorAll() {
              return [];
            },
            closest() {
              return null;
            },
          });
        }
        return elements.get(id);
      }

      globalThis.__ASHARE_RADAR_DISABLE_AUTOLOAD__ = true;
      globalThis.window = globalThis;
      globalThis.window.addEventListener = () => {};
      globalThis.requestAnimationFrame = (callback) => callback();
      globalThis.document = {
        hidden: false,
        body: element("body"),
        getElementById: element,
        querySelector(selector) {
          if (selector === ".workspace-tabs") return element("workspaceTabs");
          if (selector === ".monitor-actions") return element("monitorActions");
          return element(selector);
        },
        querySelectorAll() {
          return [];
        },
        addEventListener() {},
      };

      async function waitFor(condition, label) {
        for (let index = 0; index < 20; index += 1) {
          if (condition()) return;
          await Promise.resolve();
        }
        throw new Error(`timed out waiting for ${label}`);
      }

      let resolveAdviceJson;
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/advice/history")) {
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveAdviceJson = resolve;
              });
            },
          };
        }
        return {
          ok: true,
          async json() {
            return {};
          },
        };
      };

      const streams = [];
      globalThis.EventSource = class {
        constructor(url) {
          this.url = url;
          this.closed = false;
          this.listeners = {};
          streams.push(this);
        }
        addEventListener(name, handler) {
          this.listeners[name] = handler;
        }
        close() {
          this.closed = true;
        }
      };

      const { __appTest } = await import("./static/app.js");

      __appTest.setActiveSymbol("000001");
      if (__appTest.state.symbol !== "000001.SZ") {
        throw new Error(`state symbol not normalized: ${__appTest.state.symbol}`);
      }
      if (element("symbolInput").value !== "000001" || element("watchSymbolInput").value !== "000001") {
        throw new Error("active symbol inputs were not synchronized");
      }

      element("adviceTimeline").innerHTML = "current timeline";
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 11;
      const staleAdviceLoad = __appTest.loadAdviceTimeline();
      await waitFor(() => typeof resolveAdviceJson === "function", "advice json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 12;
      resolveAdviceJson([
        {
          action: "控制风险",
          confidence: 60,
          created_at: "2026-07-01 10:00:00",
          updated_at: "2026-07-01 10:00:00",
          trend_score: 55,
          data_quality_level: "优秀",
          data_quality_score: 88,
          repeat_count: 1,
          reason: "旧请求",
        },
      ]);
      await staleAdviceLoad;
      if (element("adviceTimeline").innerHTML !== "current timeline") {
        throw new Error("stale advice timeline replaced the current symbol panel");
      }

      __appTest.state.symbol = "600519.SH";
      __appTest.state.watchlist = [{ symbol: "000001.SZ" }];
      __appTest.startStream();
      const stream = streams.at(-1);
      if (!stream.url.includes("600519.SH") || !stream.url.includes("000001.SZ")) {
        throw new Error(`stream URL did not include current/watch symbols: ${stream.url}`);
      }
      stream.onmessage({ data: "{bad json" });
      if (!element("dataStatus").textContent.includes("实时行情数据异常")) {
        throw new Error("invalid SSE JSON did not update stream warning state");
      }

      element("minuteAnalysis").innerHTML = "current minute panel";
      let resolveMinuteJson;
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/stock/minute-analysis")) {
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveMinuteJson = resolve;
              });
            },
          };
        }
        return {
          ok: true,
          async json() {
            return {};
          },
        };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 20;
      const staleMinuteLoad = __appTest.loadMinuteAnalysis();
      await waitFor(() => typeof resolveMinuteJson === "function", "minute json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 21;
      element("minuteAnalysis").innerHTML = "new symbol minute panel";
      resolveMinuteJson({ sample_count: 0 });
      await staleMinuteLoad;
      if (element("minuteAnalysis").innerHTML !== "new symbol minute panel") {
        throw new Error("stale minute analysis replaced the current symbol panel");
      }

      let resolveMarksJson;
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/stock/chart-marks")) {
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveMarksJson = resolve;
              });
            },
          };
        }
        return {
          ok: true,
          async json() {
            return {};
          },
        };
      };
      const preservedMarks = [{ category: "当前", visible: true }];
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 30;
      __appTest.state.chartMarks = preservedMarks;
      const staleMarksLoad = __appTest.loadChartMarks();
      await waitFor(() => typeof resolveMarksJson === "function", "chart mark json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 31;
      resolveMarksJson({ marks: [{ category: "旧请求" }], categories: ["旧请求"] });
      await staleMarksLoad;
      if (__appTest.state.chartMarks !== preservedMarks) {
        throw new Error("stale chart marks replaced the current symbol marks");
      }

      globalThis.fetch = async (url) => {
        if (String(url) === "/api/market") {
          return {
            ok: true,
            async json() {
              return { indices: [] };
            },
          };
        }
        if (String(url) === "/api/strong-stocks") {
          return {
            ok: true,
            async json() {
              return {
                scope: "自定义列表",
                sample_count: 12,
                updated_at: "2026-07-01 10:00:00",
                items: [
                  {
                    name: "测试强股",
                    code: "600519",
                    reason: "样本领先",
                    tags: ["趋势"],
                    rank: 1,
                    leader_score: 88,
                    change_pct: 2.5,
                  },
                ],
              };
            },
          };
        }
        return {
          ok: true,
          async json() {
            return {};
          },
        };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 50;
      await __appTest.loadMarketPanels(50, "600519.SH");
      if (!element("leaderList").innerHTML.includes("自定义列表 · 样本 12")) {
        throw new Error(`strong stock meta was not rendered: ${element("leaderList").innerHTML}`);
      }

      element("alertType").value = "price_below";
      element("alertThreshold").value = "1200";
      const alertButton = element("alertForm-button");
      alertButton.textContent = "添加";
      let alertPostCalls = 0;
      let resolveAlertPost;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/alerts" && options.method === "POST") {
          alertPostCalls += 1;
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveAlertPost = () => resolve({});
              });
            },
          };
        }
        if (String(url).startsWith("/api/alerts")) {
          return {
            ok: true,
            async json() {
              return [];
            },
          };
        }
        return {
          ok: true,
          async json() {
            return {};
          },
        };
      };
      const alertSubmit = element("alertForm").listeners.submit;
      const firstAlertSubmit = alertSubmit({ preventDefault() {}, currentTarget: element("alertForm") });
      await waitFor(() => typeof resolveAlertPost === "function", "alert post resolver");
      const secondAlertSubmit = alertSubmit({ preventDefault() {}, currentTarget: element("alertForm") });
      if (alertPostCalls !== 1 || !alertButton.disabled || alertButton.textContent !== "添加中") {
        throw new Error("alert submit did not guard duplicate clicks");
      }
      resolveAlertPost();
      await firstAlertSubmit;
      await secondAlertSubmit;
      if (alertButton.disabled || alertButton.textContent !== "添加") {
        throw new Error("alert submit button did not recover");
      }

      element("noteContent").value = "观察";
      element("noteType").value = "复盘";
      const noteButton = element("noteForm-button");
      noteButton.textContent = "保存";
      let notePostCalls = 0;
      let resolveNotePost;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/stock/notes" && options.method === "POST") {
          notePostCalls += 1;
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveNotePost = () => resolve({});
              });
            },
          };
        }
        if (String(url).startsWith("/api/stock/notes")) {
          return {
            ok: true,
            async json() {
              return [];
            },
          };
        }
        if (String(url).startsWith("/api/stock/chart-marks")) {
          return {
            ok: true,
            async json() {
              return { marks: [], categories: [] };
            },
          };
        }
        return {
          ok: true,
          async json() {
            return {};
          },
        };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 40;
      __appTest.state.lastAnalysis = null;
      const noteSubmit = element("noteForm").listeners.submit;
      const firstNoteSubmit = noteSubmit({ preventDefault() {}, currentTarget: element("noteForm") });
      await waitFor(() => typeof resolveNotePost === "function", "note post resolver");
      const secondNoteSubmit = noteSubmit({ preventDefault() {}, currentTarget: element("noteForm") });
      if (notePostCalls !== 1 || !noteButton.disabled || noteButton.textContent !== "保存中") {
        throw new Error("note submit did not guard duplicate clicks");
      }
      resolveNotePost();
      await firstNoteSubmit;
      await secondNoteSubmit;
      if (noteButton.disabled || noteButton.textContent !== "保存") {
        throw new Error("note submit button did not recover");
      }
    '''
    _run_node_script(script)


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
