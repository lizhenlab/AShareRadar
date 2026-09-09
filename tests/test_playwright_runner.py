from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools/run_playwright.mjs"
PLAYWRIGHT = ROOT / "node_modules/@playwright/test/index.mjs"


def _node(source: str, payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "-e", source], input=json.dumps(payload),
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )


def _entries(project: str, file: str, count: int) -> list[dict[str, str]]:
    return [{"project": project, "file": file, "identity": f"{project}:{file}:{i}"} for i in range(count)]


def test_webkit_batches_preserve_files_and_every_original_identity() -> None:
    entries = _entries("desktop-firefox", "a.spec.js", 70)
    entries += _entries("desktop-webkit", "a.spec.js", 16)
    entries += _entries("desktop-webkit", "b.spec.js", 16)
    entries += _entries("desktop-webkit", "c.spec.js", 17)
    source = f"""
import {{ planBatches }} from {json.dumps(RUNNER.as_uri())};
let input = ''; for await (const chunk of process.stdin) input += chunk;
console.log(JSON.stringify(planBatches(JSON.parse(input))));
"""
    result = _node(source, entries)
    assert result.returncode == 0, result.stderr
    batches = json.loads(result.stdout)
    assert [len(batch) for batch in batches] == [70, 32, 17]
    assert [entry for batch in batches for entry in batch] == entries
    assert all(len({index for index, batch in enumerate(batches) if any(
        entry["project"] == "desktop-webkit" and entry["file"] == file for entry in batch
    )}) == 1 for file in ["a.spec.js", "b.spec.js", "c.spec.js"])


def test_oversized_webkit_file_is_rejected_without_splitting_its_tests() -> None:
    result = _node(f"""
import {{ planBatches }} from {json.dumps(RUNNER.as_uri())};
let input = ''; for await (const chunk of process.stdin) input += chunk;
planBatches(JSON.parse(input));
""", _entries("desktop-webkit", "oversized.spec.js", 33))
    assert result.returncode != 0
    assert "33 tests; split the file to stay within 32" in result.stderr


@pytest.mark.parametrize("actual", [["a"], ["a", "a"], ["a", "b", "c"]])
def test_report_identity_check_rejects_missing_overlap_and_unexpected_tests(actual: list[str]) -> None:
    result = _node(f"""
import {{ assertSameIdentities }} from {json.dumps(RUNNER.as_uri())};
let input = ''; for await (const chunk of process.stdin) input += chunk;
const entries = values => values.map(identity => ({{ identity }}));
assertSameIdentities(entries(['a', 'b']), entries(JSON.parse(input)), 'probe');
""", actual)
    assert result.returncode != 0
    assert "identities changed, overlapped, or disappeared" in result.stderr


def test_identity_check_keeps_repeat_each_multiplicity() -> None:
    result = _node(f"""
import {{ assertSameIdentities }} from {json.dumps(RUNNER.as_uri())};
const entries = values => values.map(identity => ({{ identity }}));
assertSameIdentities(entries(['a', 'b', 'a']), entries(['a', 'a', 'b']), 'repeat');
""", None)
    assert result.returncode == 0, result.stderr


def _suite(tmp_path: Path, *, failing: bool = False, count: int = 11) -> Path:
    directory = tmp_path / "suite with spaces"
    directory.mkdir()
    for name in ["a", "b", "c"]:
        (directory / f"{name}.spec.mjs").write_text(f"""
import {{ test, expect }} from {json.dumps(PLAYWRIGHT.as_uri())};
for (let i = 0; i < {count}; i += 1) {{
  test(`case ${{i}}`, async ({{}}, info) => {{
    test.skip(i === 1, 'intentional fixture skip');
    await info.attach('evidence', {{ body: Buffer.from('local fixture'), contentType: 'text/plain' }});
    const fail = {str(failing).lower()} && {json.dumps(name)} === 'a' && i === 0 && info.project.name === 'desktop-webkit';
    expect(fail).toBe(false);
  }});
}}
""", encoding="utf-8")
    config = tmp_path / "playwright.config.mjs"
    config.write_text("export default " + json.dumps({
        "testDir": str(directory), "testMatch": "*.spec.mjs", "timeout": 15000,
        "expect": {"timeout": 5000}, "workers": 1, "fullyParallel": False,
        "projects": [{"name": "desktop-chromium"}, {"name": "desktop-webkit"}],
    }) + ";\n", encoding="utf-8")
    return config


def _run(config: Path, report: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(RUNNER), "--config", str(config), "--reporter=line,json", *args],
        cwd=ROOT, env={**os.environ, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report)},
        capture_output=True, text=True, timeout=120,
    )


def _tests(report: dict) -> list[dict]:
    tests = []
    for suite in report.get("suites", []):
        tests.extend(test for spec in suite.get("specs", []) for test in spec["tests"])
        tests.extend(_tests(suite))
    return tests


@pytest.mark.parametrize("failing", [False, True])
def test_real_cli_merges_all_batches_and_preserves_failure_and_skip_results(tmp_path: Path, failing: bool) -> None:
    config = _suite(tmp_path, failing=failing)
    report = tmp_path / "combined.json"
    output = tmp_path / "artifacts with spaces"
    result = _run(config, report, "--output", str(output))
    assert result.returncode == int(failing), result.stdout + result.stderr
    combined = json.loads(report.read_text())
    tests = _tests(combined)
    assert len(tests) == 66
    assert Counter(test["projectName"] for test in tests) == {"desktop-chromium": 33, "desktop-webkit": 33}
    assert combined["stats"]["expected"] == 60 - int(failing)
    assert combined["stats"]["skipped"] == 6
    assert combined["stats"]["unexpected"] == int(failing)
    assert combined["stats"]["flaky"] == 0
    assert all(len(test["results"]) == 1 and test["results"][0]["retry"] == 0 for test in tests)
    assert all(test["timeout"] == 15000 for test in tests)
    assert any(test["results"][0]["attachments"] for test in tests)
    pointer = json.loads(Path(str(report) + ".runner.json").read_text())
    directory = Path(pointer["directory"])
    assert directory.parent == output
    batches = json.loads((directory / "batches.json").read_text())
    assert [len(batch["expected"]) for batch in batches] == [33, 22, 11]
    assert len({batch["blob"] for batch in batches}) == 3
    assert len({arg for batch in batches for arg in batch["command"] if arg.startswith("--output=")}) == 3
    assert all(Path(batch["blob"]).is_file() for batch in batches)


@pytest.mark.parametrize("args", [("--list",), ("--list", "--shard=1/2"), ("--help",)])
def test_readonly_and_explicit_shard_options_are_passed_through(tmp_path: Path, args: tuple[str, ...]) -> None:
    config = _suite(tmp_path, count=2)
    report = tmp_path / "listed.json"
    result = _run(config, report, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed unchanged to the original CLI" in result.stderr
    assert not Path(str(report) + ".runner.json").exists()
    if "--help" not in args:
        argv = json.loads(report.read_text())["config"]["argv"]
        assert all(arg in argv for arg in args)


def test_package_default_e2e_command_uses_the_bounded_runner() -> None:
    package = json.loads((ROOT / "package.json").read_text())
    assert package["scripts"]["test:e2e"] == "node tools/run_playwright.mjs"


def test_explicit_empty_selection_keeps_native_pass_with_no_tests_semantics(tmp_path: Path) -> None:
    config = _suite(tmp_path, count=1)
    report = tmp_path / "empty.json"
    result = _run(config, report, "--grep=does not match any fixture", "--pass-with-no-tests")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed unchanged to the original CLI" in result.stderr
    assert _tests(json.loads(report.read_text())) == []
    assert not Path(str(report) + ".runner.json").exists()


def test_configured_reporter_and_output_options_are_preserved(tmp_path: Path) -> None:
    config = _suite(tmp_path, count=1)
    configured_report = tmp_path / "configured-output.json"
    content = config.read_text()
    content = content.replace('"projects":', '"reporter": [["json", {"outputFile": '
                              + json.dumps(str(configured_report)) + '}]], "projects":')
    config.write_text(content)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PLAYWRIGHT_JSON_OUTPUT")}
    result = subprocess.run(
        ["node", str(RUNNER), "--config", str(config)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(configured_report.read_text())
    assert len(_tests(report)) == 6
    assert report["stats"]["expected"] == 6
    assert report["stats"]["unexpected"] == 0


@pytest.mark.parametrize(("signal", "expected_code"), [("SIGINT", 130), ("SIGTERM", 143)])
def test_parent_interruption_cannot_be_overwritten_by_clean_child_exit(signal: str, expected_code: int) -> None:
    # Control the child exit race, exercising the actual runner and signal handlers.
    result = _node(f"""
import childProcess from 'node:child_process';
import {{ syncBuiltinESMExports }} from 'node:module';
import {{ EventEmitter }} from 'node:events';
childProcess.spawn = () => {{
  const child = new EventEmitter();
  child.kill = () => {{ queueMicrotask(() => child.emit('exit', 0, null)); return true; }};
  queueMicrotask(() => process.emit({json.dumps(signal)}));
  return child;
}};
syncBuiltinESMExports();
const {{ runPlaywright }} = await import({json.dumps(RUNNER.as_uri())});
console.log(JSON.stringify({{ code: await runPlaywright(['--help']) }}));
""", None)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"code": expected_code}
