from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tools import generate_sbom


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_cyclonedx_normalization_removes_volatile_values_and_sorts() -> None:
    first = {
        "bomFormat": "CycloneDX",
        "serialNumber": "urn:uuid:first",
        "metadata": {
            "timestamp": "2026-01-01T00:00:00Z",
            "tools": [{"name": "z"}, {"name": "a"}],
        },
        "components": [{"name": "z"}, {"name": "a"}],
    }
    second = {
        "components": [{"name": "a"}, {"name": "z"}],
        "metadata": {
            "tools": [{"name": "a"}, {"name": "z"}],
            "timestamp": "2030-12-31T23:59:59Z",
        },
        "serialNumber": "urn:uuid:second",
        "bomFormat": "CycloneDX",
    }

    first_bytes = generate_sbom.canonical_bom_bytes(first)
    second_bytes = generate_sbom.canonical_bom_bytes(second)

    assert first_bytes == second_bytes
    normalized = json.loads(first_bytes)
    assert "serialNumber" not in normalized
    assert "timestamp" not in normalized["metadata"]
    assert [item["name"] for item in normalized["components"]] == ["a", "z"]


def test_real_sbom_generation_is_byte_reproducible(tmp_path: Path) -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm is not installed")
    try:
        generate_sbom._cyclonedx_executable()
    except generate_sbom.SbomGenerationError:
        pytest.skip("cyclonedx-py is not installed")

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_outputs = generate_sbom.generate_sboms(first_dir)
    second_outputs = generate_sbom.generate_sboms(second_dir)

    assert [path.name for path in first_outputs] == [
        "python.cdx.json",
        "npm.cdx.json",
    ]
    for first, second in zip(first_outputs, second_outputs, strict=True):
        assert first.read_bytes() == second.read_bytes()
        document = json.loads(first.read_text(encoding="utf-8"))
        assert document["bomFormat"] == "CycloneDX"
        assert "serialNumber" not in document
        assert "timestamp" not in document.get("metadata", {})


def test_sbom_error_output_redacts_credentials_and_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", "highly-sensitive-value")
    detail = (
        f"{generate_sbom.ROOT}/file: https://user:password@example.test "
        "highly-sensitive-value"
    )

    sanitized = generate_sbom._redact_error(detail)

    assert str(generate_sbom.ROOT) not in sanitized
    assert "user:password" not in sanitized
    assert "highly-sensitive-value" not in sanitized
    assert "[REDACTED]" in sanitized


def test_every_github_action_is_sha_pinned_and_checkout_drops_credentials() -> None:
    action_pattern = re.compile(
        r"uses:\s*[^@\s]+@(?P<sha>[0-9a-f]{40})\s+#\s+v\d+(?:\.\d+)*$"
    )
    action_lines: list[str] = []

    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "uses:" not in line:
                continue
            action_lines.append(line)
            assert action_pattern.search(line), f"unpinned action in {workflow.name}: {line}"
            if "uses: actions/checkout@" in line:
                block = "\n".join(lines[index : index + 6])
                assert "persist-credentials: false" in block

    assert action_lines


def test_ci_keeps_node_22_contract_and_adds_node_24_macos_smoke() -> None:
    ci = _read(".github/workflows/ci.yml")

    assert 'node-version: "22"' in ci
    assert "node-24-smoke:" in ci
    node_24_job = ci.split("node-24-smoke:", maxsplit=1)[1].split(
        "\n  browser:", maxsplit=1
    )[0]
    assert "runs-on: macos-latest" in node_24_job
    assert 'node-version: "24"' in node_24_job
    assert "python tools/runtime_contract.py" in node_24_job


def test_security_workflow_enforces_audits_history_redaction_and_sbom_diff() -> None:
    security = _read(".github/workflows/security.yml")

    assert "python -m pip_audit" in security
    assert "--require-hashes" in security
    assert "--requirement requirements-lock.txt" in security
    assert "--requirement requirements-dev-lock.txt" in security
    assert security.count("python -m pip_audit") == 2
    assert "npm audit --audit-level=high" in security
    assert "fetch-depth: 0" in security
    assert security.count("--redact=100") == 2
    assert '"$RUNNER_TEMP/gitleaks" dir' in security
    assert '"$RUNNER_TEMP/gitleaks" git' in security
    assert "--log-opts=--all" in security
    assert "sha256sum --check --strict" in security
    assert security.count("tools/generate_sbom.py") == 2
    assert "diff --recursive --unified" in security
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in security
    assert "${{ secrets." not in security


def test_dependabot_covers_all_dependency_ecosystems() -> None:
    dependabot = _read(".github/dependabot.yml")

    ecosystems = set(re.findall(r"package-ecosystem:\s*([a-z-]+)", dependabot))
    assert ecosystems == {"pip", "npm", "github-actions"}


def test_supply_chain_files_contain_no_machine_specific_paths() -> None:
    paths = [
        ".github/workflows/ci.yml",
        ".github/workflows/security.yml",
        ".github/dependabot.yml",
        "tools/generate_sbom.py",
        "package.json",
    ]

    for path in paths:
        content = _read(path)
        assert "/Users/" not in content
        assert "@cyclonedx/cyclonedx-npm" not in content
