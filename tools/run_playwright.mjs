import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const CLI = require.resolve("@playwright/test/cli");
const BOUNDED_PROJECT = "desktop-webkit";
export const MAX_BATCH_TESTS = 32;
const DIRECT_OPTIONS = new Set([
  "--list", "--help", "-h", "--version", "-V", "--shard", "--test-list",
  "--test-list-invert", "--ui", "--ui-host", "--ui-port", "--debug",
  "--last-failed", "--only-changed", "--global-timeout", "--max-failures", "-x",
  "--pass-with-no-tests", "--add-reporter", "--retries",
]);

function optionName(argument) {
  return argument.split("=", 1)[0];
}

function takeOption(args, name) {
  let value;
  const rest = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === name) {
      value = args[++index];
      if (!value || value.startsWith("--")) throw new Error(`${name} requires a value`);
    } else if (arg.startsWith(`${name}=`)) value = arg.slice(name.length + 1);
    else rest.push(arg);
  }
  return { value, rest };
}

export function reportEntries(report) {
  const entries = [];
  function visit(suite, parents = []) {
    const titles = [...parents, suite.title];
    for (const spec of suite.specs || []) {
      for (const test of spec.tests) {
        entries.push({
          file: spec.file,
          project: test.projectName,
          identity: JSON.stringify([spec.file, ...titles, spec.title, test.projectName]),
        });
      }
    }
    for (const child of suite.suites || []) visit(child, titles);
  }
  for (const suite of report.suites || []) visit(suite);
  return entries;
}

export function assertSameIdentities(expected, actual, context) {
  const counts = (entries) => {
    const result = new Map();
    for (const { identity } of entries) result.set(identity, (result.get(identity) || 0) + 1);
    return [...result].sort(([a], [b]) => a.localeCompare(b));
  };
  if (JSON.stringify(counts(expected)) !== JSON.stringify(counts(actual))) {
    throw new Error(`${context}: collected test identities changed, overlapped, or disappeared`);
  }
}

export function planBatches(entries) {
  const ordinary = entries.filter((entry) => entry.project !== BOUNDED_PROJECT);
  const batches = ordinary.length ? [ordinary] : [];
  const files = new Map();
  for (const entry of entries.filter((item) => item.project === BOUNDED_PROJECT)) {
    if (!files.has(entry.file)) files.set(entry.file, []);
    files.get(entry.file).push(entry);
  }
  let batch = [];
  for (const [file, group] of files) {
    if (group.length > MAX_BATCH_TESTS) {
      throw new Error(`${BOUNDED_PROJECT}: ${file} has ${group.length} tests; split the file to stay within ${MAX_BATCH_TESTS}`);
    }
    if (batch.length + group.length > MAX_BATCH_TESTS) {
      batches.push(batch);
      batch = [];
    }
    batch.push(...group);
  }
  if (batch.length) batches.push(batch);
  assertSameIdentities(entries, batches.flat(), "Batch plan");
  return batches;
}

function testList(entries) {
  const lines = entries.map(({ file, project }) => {
    if (/[\r\n›]/.test(file + project) || !file || !project) {
      throw new Error("Project/file cannot be represented in an official Playwright test list");
    }
    return `[${project}] › ${file}`;
  });
  return [...new Set(lines)].join("\n") + "\n";
}

function json(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function save(path, value) {
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n");
}

async function runCli(args, env) {
  return await new Promise((resolveResult, reject) => {
    const child = spawn(process.execPath, [CLI, ...args], { env, stdio: "inherit" });
    let interruptedBy = null;
    const onInterrupt = () => { interruptedBy ||= "SIGINT"; child.kill("SIGINT"); };
    const onTerminate = () => { interruptedBy ||= "SIGTERM"; child.kill("SIGTERM"); };
    process.once("SIGINT", onInterrupt);
    process.once("SIGTERM", onTerminate);
    const cleanup = () => {
      process.removeListener("SIGINT", onInterrupt);
      process.removeListener("SIGTERM", onTerminate);
    };
    child.once("error", (error) => { cleanup(); reject(error); });
    child.once("exit", (code, signal) => {
      cleanup();
      const interruptedCode = interruptedBy === "SIGINT" ? 130 : 143;
      resolveResult({
        code: interruptedBy ? interruptedCode : (code ?? (signal === "SIGINT" ? 130 : 1)),
        interrupted: interruptedBy !== null,
      });
    });
  });
}

async function collect(args, file, env) {
  const result = await runCli(["test", ...args, "--list", "--reporter=json"], {
    ...env, PLAYWRIGHT_JSON_OUTPUT_FILE: file,
  });
  if (result.code !== 0 || result.interrupted) throw new Error(`Playwright collection failed: ${file}`);
  const report = json(file);
  if (report.errors?.length) throw new Error(`Playwright collection contains errors: ${file}`);
  return report;
}

async function prepareBatches(args, entries, directory, env) {
  const jobs = [];
  for (const [index, expected] of planBatches(entries).entries()) {
    const name = `batch-${String(index + 1).padStart(2, "0")}`;
    const path = join(directory, name);
    mkdirSync(path);
    const list = join(path, "tests.txt");
    writeFileSync(list, testList(expected));
    const command = [...args, `--test-list=${list}`, `--output=${join(path, "artifacts")}`];
    const listed = await collect(command, join(path, "collection.json"), env);
    assertSameIdentities(expected, reportEntries(listed), name);
    jobs.push({ name, command, expected, blob: join(path, "report.zip") });
  }
  return jobs;
}

async function executeBatches(jobs, directory, env) {
  const blobs = join(directory, "blobs");
  mkdirSync(blobs);
  for (const job of jobs) {
    const result = await runCli(["test", ...job.command, "--reporter=line,blob"], {
      ...env, PLAYWRIGHT_BLOB_OUTPUT_FILE: job.blob,
    });
    job.result = result;
    save(join(directory, "batches.json"), jobs);
    if (result.interrupted) return { interrupted: true, code: result.code };
    // Missing/corrupt reports fail closed; earlier reports and traces remain available.
    copyFileSync(job.blob, join(blobs, `${job.name}.zip`));
  }
  return { interrupted: false, code: jobs.some((job) => job.result.code !== 0) ? 1 : 0 };
}

async function mergeReports(directory, reporters, configFile, env, expected) {
  const file = join(directory, "merged.json");
  const formats = [...new Set([...(reporters?.split(",") || []), "json"])].join(",");
  const result = await runCli(["merge-reports", `--reporter=${formats}`, join(directory, "blobs")], {
    ...env, PLAYWRIGHT_JSON_OUTPUT_FILE: file,
  });
  if (result.code !== 0 || result.interrupted) throw new Error("Playwright report merge failed");
  const report = json(file);
  assertSameIdentities(expected, reportEntries(report), "Merged report");
  if (!reporters) {
    // Let Playwright load reporter paths/options; do not approximate config loading.
    const rendered = await runCli(["merge-reports", `--config=${configFile}`, join(directory, "blobs")], env);
    if (rendered.code !== 0 || rendered.interrupted) throw new Error("Configured Playwright reporters failed");
  }
  if (env.PLAYWRIGHT_JSON_OUTPUT_FILE) {
    const destination = resolve(env.PLAYWRIGHT_JSON_OUTPUT_FILE);
    mkdirSync(dirname(destination), { recursive: true });
    copyFileSync(file, destination);
    save(`${destination}.runner.json`, { directory, identities: expected.length });
  } else if (reporters?.split(",").includes("json")) {
    process.stdout.write(readFileSync(file, "utf8"));
  }
  return report;
}

export async function runPlaywright(args, env = process.env) {
  if (args.some((arg) => DIRECT_OPTIONS.has(optionName(arg))) || env.PWDEBUG) {
    console.error("Playwright: explicit/interactive/read-only selection is passed unchanged to the original CLI.");
    return (await runCli(["test", ...args], env)).code;
  }
  const reporter = takeOption(args, "--reporter");
  const output = takeOption(reporter.rest, "--output");
  const base = output.value ? resolve(output.value) : tmpdir();
  mkdirSync(base, { recursive: true });
  const directory = mkdtempSync(join(base, "ashare-playwright-"));
  console.error(`Playwright process evidence: ${directory}`);
  const listed = await collect(output.rest, join(directory, "collection.json"), env);
  if (listed.config.globalTimeout || listed.config.maxFailures) {
    throw new Error("A global timeout/failure limit cannot be reset per batch; use the original Playwright CLI explicitly");
  }
  if (listed.config.projects.some((project) => project.retries)) {
    throw new Error("Configured retries can exceed the batch context bound; use the original Playwright CLI explicitly");
  }
  const expected = reportEntries(listed);
  if (!expected.length) throw new Error("No tests collected for bounded Playwright execution");
  const jobs = await prepareBatches(output.rest, expected, directory, env);
  save(join(directory, "plan.json"), { args, maxBatchTests: MAX_BATCH_TESTS, jobs });
  const executed = await executeBatches(jobs, directory, env);
  if (executed.interrupted) return executed.code;
  const report = await mergeReports(directory, reporter.value, listed.config.configFile, env, expected);
  const code = executed.code || (report.errors?.length || report.stats.unexpected ? 1 : 0);
  save(join(directory, "result.json"), { code, stats: report.stats, identities: expected.length });
  return code;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runPlaywright(process.argv.slice(2)).then((code) => { process.exitCode = code; }).catch((error) => {
    console.error(`Playwright process runner failed: ${error.message}`);
    process.exitCode = 1;
  });
}
