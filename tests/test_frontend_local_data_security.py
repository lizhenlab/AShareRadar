from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_local_data_export_uses_post() -> None:
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            r'''
            const anchor = { click() {} };
            globalThis.document = {
              getElementById() { return null; },
              createElement() { return anchor; },
            };
            globalThis.URL = {
              createObjectURL() { return "blob:test"; },
              revokeObjectURL() {},
            };
            let request;
            globalThis.fetch = async (url, options = {}) => {
              request = { url: String(url), method: options.method };
              return new Response(JSON.stringify({ kind: "ashare-radar-user-data", version: 1 }), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              });
            };
            const { exportLocalUserData } = await import("./static/js/local-data.js");

            await exportLocalUserData({ now: new Date("2026-07-16T00:00:00Z") });

            if (request?.url !== "/api/local-data/export" || request?.method !== "POST") {
              throw new Error("local user-data export must use guarded POST");
            }
            ''',
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
