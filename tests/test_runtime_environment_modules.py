from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.runtime_environment import isolate_user_site_packages
from tools import runtime_contract
from tools.runtime_contract import declaration_errors, runtime_contract_errors


def test_isolate_user_site_packages_removes_user_path(monkeypatch) -> None:
    fake_user_site = "/tmp/ashare-radar-user-site"
    monkeypatch.setattr("site.USER_SITE", fake_user_site)
    monkeypatch.setattr(sys, "path", ["/app", fake_user_site, "/runtime"])

    isolate_user_site_packages()

    assert sys.path == ["/app", "/runtime"]


def test_app_import_isolates_provider_runtime_from_user_site_packages() -> None:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONNOUSERSITE", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.main, json, numpy, pandas, site, sys; "
                "print(json.dumps({'user_site': site.USER_SITE, 'paths': sys.path, "
                "'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
                "'numpy': numpy.__file__, 'pandas': pandas.__file__}))"
            ),
        ],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    user_site = payload["user_site"]

    assert user_site not in payload["paths"]
    runtime_roots = (Path(payload["prefix"]).resolve(), Path(payload["base_prefix"]).resolve())
    for package in ("numpy", "pandas"):
        package_path = Path(payload[package]).resolve()
        assert user_site not in str(package_path)
        assert any(package_path.is_relative_to(root) for root in runtime_roots)


def test_runtime_contract_accepts_supported_lts_versions() -> None:
    for node_version, npm_version in (("v22.23.1", "10.9.4"), ("v24.14.1", "11.11.0")):
        assert runtime_contract_errors(
            python_version=(3, 12),
            node_version=node_version,
            npm_version=npm_version,
        ) == []


def test_runtime_contract_rejects_undeclared_versions(monkeypatch) -> None:
    monkeypatch.setattr("tools.runtime_contract.declaration_errors", lambda: [])

    errors = runtime_contract_errors(
        python_version=(3, 11),
        node_version="v23.0.0",
        npm_version="12.0.0",
    )

    assert errors == [
        "Python 必须为 3.12.x，当前为 3.11",
        "Node.js 必须为 22.x 或 24.x",
        "npm 必须为 10.x 或 11.x",
    ]


def test_runtime_declaration_files_stay_synchronized() -> None:
    assert declaration_errors() == []


def test_runtime_contract_main_reports_success_declared_errors_and_detection_failure(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(runtime_contract, "detected_runtime_versions", lambda: ((3, 12), "v22.1.0", "10.2.0"))
    monkeypatch.setattr(runtime_contract, "declaration_errors", lambda: [])
    assert runtime_contract.main() == 0
    assert "runtime ok: Python 3.12" in capsys.readouterr().out

    monkeypatch.setattr(runtime_contract, "detected_runtime_versions", lambda: ((3, 11), "v23.0.0", "12.0.0"))
    assert runtime_contract.main() == 1
    errors = capsys.readouterr().err
    assert "Python 必须为 3.12.x" in errors
    assert "Node.js 必须为 22.x" in errors

    monkeypatch.setattr(
        runtime_contract,
        "detected_runtime_versions",
        lambda: (_ for _ in ()).throw(RuntimeError("missing runtime")),
    )
    assert runtime_contract.main() == 1
    assert "运行时契约检查失败：missing runtime" in capsys.readouterr().err


def test_runtime_contract_command_and_version_parsing_boundaries(monkeypatch) -> None:
    completed = subprocess.CompletedProcess(["node", "--version"], 0, stdout="v24.1.0\n", stderr="")
    monkeypatch.setattr(runtime_contract.subprocess, "run", lambda *_args, **_kwargs: completed)
    assert runtime_contract._command_version("node", "--version") == "v24.1.0"
    assert runtime_contract._major_version("not-a-version") is None

    monkeypatch.setattr(
        runtime_contract.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing")),
    )
    with pytest.raises(RuntimeError, match="无法读取 node 版本"):
        runtime_contract._command_version("node", "--version")
