import { readdirSync } from "node:fs";
import { extname, join, relative } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const extensions = new Set([".js", ".mjs", ".cjs"]);

function javascriptFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return javascriptFiles(path);
    return entry.isFile() && extensions.has(extname(entry.name)) ? [path] : [];
  });
}

function main() {
  const files = [
    join(root, "playwright.config.js"),
    ...["static", "tests", "tools"].flatMap((directory) => javascriptFiles(join(root, directory))),
  ].sort();
  let failed = false;
  for (const file of files) {
    // Node only checks its first file argument; every source needs its own call.
    const result = spawnSync(process.execPath, ["--check", file], {
      cwd: root,
      stdio: "inherit",
      timeout: 10_000,
    });
    if (result.status !== 0) {
      failed = true;
      console.error(`JavaScript syntax check failed: ${relative(root, file)}`);
      if (result.error) console.error(result.error.message);
    }
  }
  if (!failed) console.log(`JavaScript syntax OK: ${files.length} files checked.`);
  return failed ? 1 : 0;
}

try {
  process.exitCode = main();
} catch (error) {
  console.error(`JavaScript syntax check could not complete: ${error.message}`);
  process.exitCode = 1;
}
