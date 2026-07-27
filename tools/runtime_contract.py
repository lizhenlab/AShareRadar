from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHON = (3, 12)
SUPPORTED_NODE_MAJORS = (22, 24)
SUPPORTED_NPM_MAJORS = (10, 11)
DEFAULT_NODE_MAJOR = 22
PYTHON_DECLARATION = "3.12"
NODE_ENGINE_DECLARATION = "^22.0.0 || ^24.0.0"
NPM_ENGINE_DECLARATION = ">=10 <12"


def runtime_contract_errors(
    *,
    python_version: tuple[int, int],
    node_version: str,
    npm_version: str,
) -> list[str]:
    errors = declaration_errors()
    if python_version != SUPPORTED_PYTHON:
        errors.append(f"Python 必须为 {PYTHON_DECLARATION}.x，当前为 {python_version[0]}.{python_version[1]}")
    node_major = _major_version(node_version)
    if node_major not in SUPPORTED_NODE_MAJORS:
        errors.append("Node.js 必须为 22.x 或 24.x")
    npm_major = _major_version(npm_version)
    if npm_major not in SUPPORTED_NPM_MAJORS:
        errors.append("npm 必须为 10.x 或 11.x")
    return errors


def declaration_errors() -> list[str]:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    engines = package.get("engines") or {}
    declarations = {
        ".python-version": ((ROOT / ".python-version").read_text(encoding="utf-8").strip(), PYTHON_DECLARATION),
        ".node-version": ((ROOT / ".node-version").read_text(encoding="utf-8").strip(), str(DEFAULT_NODE_MAJOR)),
        "package.json engines.node": (engines.get("node"), NODE_ENGINE_DECLARATION),
        "package.json engines.npm": (engines.get("npm"), NPM_ENGINE_DECLARATION),
    }
    return [f"{name} 运行时声明漂移：应为 {expected}" for name, (actual, expected) in declarations.items() if actual != expected]


def detected_runtime_versions() -> tuple[tuple[int, int], str, str]:
    return (
        (sys.version_info.major, sys.version_info.minor),
        _command_version("node", "--version"),
        _command_version("npm", "--version"),
    )


def _command_version(command: str, argument: str) -> str:
    try:
        result = subprocess.run(
            [command, argument],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法读取 {command} 版本") from exc
    return result.stdout.strip()


def _major_version(value: str) -> int | None:
    match = re.match(r"^v?(\d+)(?:\.|$)", value.strip())
    return int(match.group(1)) if match else None


def main() -> int:
    try:
        python_version, node_version, npm_version = detected_runtime_versions()
        errors = runtime_contract_errors(
            python_version=python_version,
            node_version=node_version,
            npm_version=npm_version,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"运行时契约检查失败：{exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"runtime ok: Python {python_version[0]}.{python_version[1]}, Node {node_version}, npm {npm_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
