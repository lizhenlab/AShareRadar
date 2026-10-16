import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const root = fileURLToPath(new URL("../../", import.meta.url));
const serverFile = fileURLToPath(new URL("./backend_server.py", import.meta.url));

function spawnBackend(directory) {
  const python = process.env.PYTHON || join(root, ".venv/bin/python");
  const child = spawn(python, [serverFile, "--directory", directory], {
    cwd: root,
    env: {
      PATH: process.env.PATH, LANG: "C.UTF-8", TMPDIR: tmpdir(), PYTHONNOUSERSITE: "1",
      PYTHONDONTWRITEBYTECODE: "1", ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH: "0",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const state = { child, output: "", errors: "", baseURL: null, exited: false, failure: null };
  child.stdout.on("data", (chunk) => {
    state.output = (state.output + chunk).slice(-16384);
    for (const line of state.output.split("\n")) {
      try { state.baseURL ||= JSON.parse(line).baseURL; } catch { /* Uvicorn may also print non-JSON output. */ }
    }
  });
  child.stderr.on("data", chunk => { state.errors = (state.errors + chunk).slice(-16384); });
  child.on("error", error => { state.failure = error; });
  state.closed = new Promise(resolve => child.once("close", (code, signal) => {
    state.exited = true;
    resolve({ code, signal });
  }));
  return state;
}

async function waitReady(state) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (state.failure) throw state.failure;
    if (state.exited) throw new Error(`Integration backend exited: ${state.errors}`);
    if (state.baseURL) {
      try {
        const response = await fetch(`${state.baseURL}/api/health/ready`, { signal: AbortSignal.timeout(1000) });
        if (response.ok && (await response.json()).status === "ready") return state.baseURL;
      } catch { /* Startup can precede Uvicorn accepting connections. */ }
    }
    await delay(50);
  }
  throw new Error(`Integration backend did not become ready: ${state.errors}`);
}

async function stopBackend(state) {
  if (!state) return;
  if (!state.exited) state.child.kill("SIGINT");
  const killTimer = setTimeout(() => state.child.kill("SIGKILL"), 5000);
  try {
    const result = await state.closed;
    if (result.code !== 0) throw new Error(`Integration backend shutdown failed: ${result.signal || result.code}; ${state.errors}`);
  } finally { clearTimeout(killTimer); }
}

export async function createBackend() {
  const directory = await mkdtemp(join(tmpdir(), "ashare-backend-smoke-"));
  let state;
  async function start() {
    state = spawnBackend(directory);
    return waitReady(state);
  }
  async function close() {
    try { await stopBackend(state); } finally { await rm(directory, { recursive: true, force: true }); }
  }
  try {
    await writeFile(join(directory, ".integration-owned"), "AShareRadar isolated backend smoke v1\n");
    const baseURL = await start();
    return { baseURL, directory, close, async restart() { await stopBackend(state); return start(); } };
  } catch (error) {
    try { await close(); } catch { /* Preserve the original startup failure after bounded cleanup. */ }
    throw error;
  }
}
