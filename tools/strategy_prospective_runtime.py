"""Seal and run an offline local prospective-study runtime, without copying secrets.

The application and installed packages are real copies. Python's base standard
library remains shared with the local interpreter and is fingerprinted on every
inspection. This is a local integrity receipt, not an independent signature.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import cast
from zoneinfo import ZoneInfo


SCHEMA = "strategy-prospective-runtime-v1"
ENTRYPOINTS = frozenset({"collect_strategy_prospective.py", "run_strategy_prospective.py"})
RUNTIME_DIRECTORY_ENV = "ASHARE_RADAR_PROSPECTIVE_RUNTIME_DIR"
RUNTIME_DIGEST_ENV = "ASHARE_RADAR_PROSPECTIVE_RUNTIME_DIGEST"
LOCK_FILES = (".python-version", "pyproject.toml", "requirements.txt", "requirements-lock.txt")
MAX_RUNTIME_RECEIPT_BYTES = 16 * 1024 * 1024
CONFIGURATION_DEFAULTS = {
    "ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE": "50",
    "ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE": "Asia/Shanghai",
}
FIXED_ENVIRONMENT = {
    "PATH": os.defpath, "LANG": "C.UTF-8", "TZ": "Asia/Shanghai",
    "ASHARE_RADAR_LLM_ENABLED": "false", "ASHARE_RADAR_FUYAO_ENABLED": "false",
    "ASHARE_RADAR_SCHEDULER_ENABLED": "false", "ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED": "false",
    "ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH": "false",
}
_PATH_FLAGS = frozenset({
    "--root", "--database", "--control-root", "--history-root", "--public-root",
    "--official-registry", "--official-raw-root", "--official-session-directory",
})
_PROBE = """import json, platform, sys, sysconfig
print(json.dumps({'version': list(sys.version_info[:3]), 'version_string': sys.version,
 'platform': platform.platform(), 'machine': platform.machine(),
 'base_executable': sys._base_executable, 'stdlib': sysconfig.get_path('stdlib'), 'search_paths': sys.path}))
"""
_LAUNCHER = """# Generated immutable entry point; invoked with -I -S -B.
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent
code = root / 'code'
environment = root / 'venv'
packages = environment / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
sys.prefix = sys.exec_prefix = str(environment)
sys.path[:0] = [str(code), str(packages)]
# Settings otherwise falls back to the user's shell profile, even for absent keys.
import app.config_settings
app.config_settings._SHELL_ENV_VALUES = {}
entrypoint = sys.argv[1]
if entrypoint not in {'collect_strategy_prospective.py', 'run_strategy_prospective.py'}:
    raise ValueError('unsupported prospective runtime entrypoint')
sys.argv = [str(code / 'tools' / entrypoint), *sys.argv[2:]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _absolute(path: Path | str, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts:
        raise ValueError(f"{label} must be an explicit absolute path")
    if value.is_symlink():
        raise ValueError(f"{label} cannot be a symbolic link")
    return value.resolve()


def _configuration(value: Mapping[str, str] | None) -> dict[str, str]:
    supplied = dict(value or {})
    if supplied.keys() - CONFIGURATION_DEFAULTS.keys():
        raise ValueError("only declared non-sensitive runtime configuration is accepted")
    result = {**CONFIGURATION_DEFAULTS, **supplied}
    minimum = result["ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE"]
    if not isinstance(minimum, str) or not minimum.isdecimal() or not 0 <= int(minimum) <= 100:
        raise ValueError("invalid minimum data quality score")
    timezone_name = result["ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE"]
    if not isinstance(timezone_name, str):
        raise ValueError("invalid legacy audit timezone")
    ZoneInfo(timezone_name)
    return result


def _environment(configuration: Mapping[str, str]) -> dict[str, str]:
    # Never inherit the caller's environment, PYTHONPATH, credentials or shell setup.
    return {**FIXED_ENVIRONMENT, **configuration}


def _tree_files(root: Path, *, omit_caches: bool = False) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in [*names, *filenames]):
            raise ValueError("runtime inputs cannot contain symbolic links")
        if omit_caches:
            names[:] = [name for name in names if name != "__pycache__"]
        for name in filenames:
            path = parent / name
            if not path.is_file():
                raise ValueError("runtime inputs must be regular files")
            if omit_caches and (path.suffix in {".pyc", ".pyo"} or name == ".env" or name.startswith(".env.")):
                continue
            files.append(path)
    return sorted(files)


def _tree_manifest(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("runtime component directory is missing or unsafe")
    return {path.relative_to(root).as_posix(): _digest(path) for path in _tree_files(root)}


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("runtime input must be a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_code(source: Path, target: Path) -> None:
    for folder in ("app", "tools"):
        root = source / folder
        if root.is_symlink() or not root.is_dir():
            raise ValueError("application source directory is missing or unsafe")
        for path in _tree_files(root, omit_caches=True):
            if path.suffix == ".py" or (path.is_relative_to(source / "app/resources") and path.suffix == ".json"):
                _copy_file(path, target / path.relative_to(source))
    locks = set(LOCK_FILES) | {p.name for p in source.glob("requirements*-lock.txt")}
    for name in sorted(locks):
        _copy_file(source / name, target / name)
    if not (target / "tools/collect_strategy_prospective.py").is_file():
        raise ValueError("prospective CLI is missing")


def _probe(executable: Path) -> dict[str, object]:
    completed = subprocess.run([str(executable), "-I", "-S", "-B", "-c", _PROBE], check=True,
                               capture_output=True, text=True, env=_environment({}), timeout=30)
    value = json.loads(completed.stdout)
    if value["version"][:2] != [3, 12]:
        raise ValueError("prospective runtime requires Python 3.12")
    return value


def _base_identity(probe: Mapping[str, object]) -> dict[str, object]:
    base = Path(str(probe["base_executable"])).resolve()
    stdlib = Path(str(probe["stdlib"])).resolve()
    files: dict[str, str] = {}
    # Third-party packages are copied separately; executable stdlib inputs are pinned.
    for directory, names, filenames in os.walk(stdlib, followlinks=False):
        names[:] = [name for name in names if name not in {"site-packages", "__pycache__"}]
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.suffix in {".py", ".so", ".dylib", ".zip"}:
                if path.is_symlink():
                    raise ValueError("base Python library contains symbolic links")
                files[path.relative_to(stdlib).as_posix()] = _digest(path)
    search_files = {str(path): _digest(path) for raw in cast(list[str], probe["search_paths"])
                    if (path := Path(raw)).is_file()}
    return {"probe": dict(probe), "executable_sha256": _digest(base), "stdlib_files": files, "search_path_files": search_files}


def _installed_packages(site: Path) -> list[dict[str, str]]:
    return sorted(({"name": str(item.metadata["Name"]), "version": item.version}
                   for item in distributions(path=[str(site)])), key=lambda item: (item["name"], item["version"]))


def _package_directory(environment: Path) -> Path:
    return environment / "lib/python3.12/site-packages"


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith(("#", "--hash=")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;\\]+)\s*\\?", value)
        if match is None:
            raise ValueError("runtime lock must contain unconditional exact package pins")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in versions:
            raise ValueError("runtime lock contains duplicate package pins")
        versions[name] = match[2]
    if not versions:
        raise ValueError("runtime dependency lock is empty")
    return versions


def _validate_dependencies(code: Path, packages: list[dict[str, str]]) -> dict[str, object]:
    if (code / ".python-version").read_text().strip() != "3.12":
        raise ValueError("runtime Python declaration must be 3.12")
    locked = _locked_versions(code / "requirements-lock.txt")
    installed: dict[str, str] = {}
    for package in packages:
        name = re.sub(r"[-_.]+", "-", package["name"]).lower()
        if name in installed:
            raise ValueError("runtime has duplicate installed package identities")
        installed[name] = package["version"]
    mismatches = sorted(name for name, version in locked.items() if installed.get(name) != version)
    if mismatches:
        raise ValueError("installed packages disagree with runtime dependency lock: " + ", ".join(mismatches))
    return {"scope": "requirements-lock.txt-exact-versions", "required_package_count": len(locked),
            "extra_installed_packages": sorted(installed.keys() - locked.keys()),
            "wheel_download_hashes_verified": False, "installed_tree_hashes_verified": True}


def _create_environment(source: Path, target: Path) -> dict[str, object]:
    python = source / "bin/python"
    probe = _probe(python)
    subprocess.run([str(python), "-I", "-S", "-B", "-m", "venv", "--copies", "--without-pip", str(target)],
                   check=True, capture_output=True, text=True, env=_environment({}), timeout=60)
    # POSIX venv creates this compatibility alias even with --copies. The
    # launcher uses lib directly, so retain a link-free sealed environment.
    if (target / "lib64").is_symlink() and (target / "lib64").resolve() == target / "lib":
        (target / "lib64").unlink()
    original = _package_directory(source)
    if not original.is_dir() or original.is_symlink():
        raise ValueError("source environment has no isolated site-packages directory")
    for path in _tree_files(original, omit_caches=True):
        _copy_file(path, _package_directory(target) / path.relative_to(original))
    return _base_identity(probe)


def prepare_runtime(
    source_root: Path, destination: Path, environment_root: Path, *, configuration: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Create a new sealed copy offline; never modify a plan or an existing runtime."""
    source = _absolute(source_root, "source_root")
    target = _absolute(destination, "destination")
    environment = _absolute(environment_root, "environment_root")
    config = _configuration(configuration)
    if target.exists() or any(target.is_relative_to(path) for path in (source / "app", source / "tools", environment)):
        raise ValueError("runtime destination must be new and outside copied input directories")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        _copy_code(source, staging / "code")
        _validate_dependencies(staging / "code", _installed_packages(_package_directory(environment)))
        base = _create_environment(environment, staging / "venv")
        packages = _installed_packages(_package_directory(staging / "venv"))
        lock_validation = _validate_dependencies(staging / "code", packages)
        (staging / "launch.py").write_text(_LAUNCHER, encoding="utf-8")
        manifest: dict[str, object] = {
            "schema_version": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
            "configuration": config, "fixed_environment": FIXED_ENVIRONMENT,
            "code_files": _tree_manifest(staging / "code"), "environment_files": _tree_manifest(staging / "venv"),
            "installed_packages": packages, "dependency_lock_validation": lock_validation,
            "base_python": base, "launcher_sha256": _digest(staging / "launch.py"),
            "isolation": "copied-packages-isolated-python-no-site-hooks-no-inherited-environment",
        }
        manifest["digest"] = hashlib.sha256(_canonical(manifest)).hexdigest()
        (staging / "runtime.json").write_bytes(_canonical(manifest))
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return manifest


def _verify_base_python(root: Path, identity: Mapping[str, object]) -> None:
    original_probe = cast(dict[str, object], identity["probe"])
    if _base_identity(original_probe) != identity:
        raise ValueError("runtime base Python drift")
    current_probe = _probe(root / "venv/bin/python")
    # A venv's copied executable is local; its base executable metadata may name
    # that copy after relocation. Compare the base interpreter recorded at prepare.
    current_probe["base_executable"] = original_probe["base_executable"]
    if current_probe != original_probe:
        raise ValueError("runtime base Python drift")


def _runtime_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("runtime receipt contains duplicate keys")
        value[key] = item
    return value


def _read_runtime_manifest(record: Path) -> dict[str, object]:
    descriptor = os.open(record, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RUNTIME_RECEIPT_BYTES:
            raise ValueError("runtime receipt is not a bounded regular file")
        encoded = stream.read(MAX_RUNTIME_RECEIPT_BYTES + 1)
        after, current = os.fstat(stream.fileno()), record.stat(follow_symlinks=False)
    def facts(item: os.stat_result) -> tuple[int, ...]:
        return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns
    if len(encoded) > MAX_RUNTIME_RECEIPT_BYTES or facts(before) != facts(after) or facts(after) != facts(current):
        raise OSError("runtime receipt changed during inspection")
    value = json.loads(encoded, object_pairs_hook=_runtime_json_object)
    if not isinstance(value, dict):
        raise ValueError("runtime receipt must be an object")
    return value


def _admit_runtime_manifest(record: Path, expected_digest: str | None) -> dict[str, object]:
    """Admit one bounded receipt and its caller pin before checking executable inputs."""
    if record.is_symlink():
        raise ValueError("runtime receipt cannot be a symbolic link")
    manifest = _read_runtime_manifest(record)
    supplied_digest = manifest.pop("digest", None)
    if manifest.get("schema_version") != SCHEMA or supplied_digest != hashlib.sha256(_canonical(manifest)).hexdigest():
        raise ValueError("runtime receipt digest mismatch")
    if expected_digest is not None and supplied_digest != expected_digest:
        raise ValueError("runtime no longer matches its admitted digest")
    manifest["digest"] = supplied_digest
    return manifest


def inspect_runtime(directory: Path, *, expected_digest: str | None = None) -> dict[str, object]:
    """Fail before executing application code if any sealed runtime input drifted."""
    root = _absolute(directory, "runtime directory")
    manifest = _admit_runtime_manifest(root / "runtime.json", expected_digest)
    if _configuration(cast(dict[str, str], manifest["configuration"])) != manifest["configuration"] or manifest["fixed_environment"] != FIXED_ENVIRONMENT:
        raise ValueError("runtime configuration drift")
    for folder, key in (("code", "code_files"), ("venv", "environment_files")):
        if _tree_manifest(root / folder) != manifest[key]:
            raise ValueError(f"runtime {folder} content drift")
    if (root / "launch.py").is_symlink() or _digest(root / "launch.py") != manifest["launcher_sha256"]:
        raise ValueError("runtime launcher drift")
    if _installed_packages(_package_directory(root / "venv")) != manifest["installed_packages"]:
        raise ValueError("runtime installed package identity drift")
    if _validate_dependencies(root / "code", cast(list[dict[str, str]], manifest["installed_packages"])) != manifest["dependency_lock_validation"]:
        raise ValueError("runtime dependency lock validation drift")
    _verify_base_python(root, cast(dict[str, object], manifest["base_python"]))
    return manifest


def inspect_running_runtime() -> dict[str, object]:
    """Revalidate a watch iteration against the runtime digest admitted at launch.

    This intentionally rehashes all inputs on every call. The marker is local
    process context, not external authentication. No inherited secrets are read.
    """
    directory = os.getenv(RUNTIME_DIRECTORY_ENV)
    expected = os.getenv(RUNTIME_DIGEST_ENV)
    if not directory or not expected or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("running prospective study lacks its verified runtime marker")
    manifest = inspect_runtime(Path(directory), expected_digest=expected)
    if any(os.getenv(key) != value for key, value in _environment(cast(dict[str, str], manifest["configuration"])).items()):
        raise ValueError("running runtime environment configuration drift")
    return manifest


def _option_values(argv: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, argument in enumerate(argv):
        if not argument.startswith("--"):
            continue
        flag, separator, value = argument.partition("=")
        if flag in result:
            raise ValueError("duplicate runtime command option")
        result[flag] = value if separator else (argv[index + 1] if index + 1 < len(argv) else "")
    return result


def _validate_arguments(argv: Sequence[str], entrypoint: str) -> dict[str, str]:
    values = _option_values(argv)
    required = {"--root", "--plan-id"}
    if entrypoint == "run_strategy_prospective.py":
        required |= {"--database", "--control-root", "--expected-plan-digest"}
    elif "capture" in argv or "readiness" in argv:
        required.add("--database")
    elif "evidence" in argv:
        required |= {"--history-root", "--public-root"}
    if any(not values.get(flag) or values[flag].startswith("--") for flag in required):
        raise ValueError("runtime command requires explicit data paths and plan identity")
    for flag in values.keys() & _PATH_FLAGS:
        _absolute(values[flag], flag)
    return values


def run_runtime(
    directory: Path, argv: Sequence[str], *, entrypoint: str = "collect_strategy_prospective.py", capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Inspect then launch only an approved CLI, with caller-supplied data paths."""
    if entrypoint not in ENTRYPOINTS:
        raise ValueError("unsupported prospective runtime entrypoint")
    arguments = list(argv)
    values = _validate_arguments(arguments, entrypoint)
    manifest = inspect_runtime(directory)
    root = _absolute(directory, "runtime directory")
    env = _environment(cast(dict[str, str], manifest["configuration"]))
    env[RUNTIME_DIRECTORY_ENV] = str(root)
    env[RUNTIME_DIGEST_ENV] = str(manifest["digest"])
    if "--database" in values:
        env["ASHARE_RADAR_CACHE_PATH"] = values["--database"]
    return subprocess.run([str(root / "venv/bin/python"), "-I", "-S", "-B", str(root / "launch.py"), entrypoint, *arguments],
                          cwd=root / "code", env=env, capture_output=capture_output, text=True, check=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="封存和运行独立前瞻实验代码与离线 Python 环境")
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--source-root", type=Path, required=True)
    prepare.add_argument("--destination", type=Path, required=True)
    prepare.add_argument("--environment-root", type=Path, required=True)
    prepare.add_argument("--min-data-quality-score", default="50")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("directory", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--directory", type=Path, required=True)
    run.add_argument("--entrypoint", choices=sorted(ENTRYPOINTS), default="collect_strategy_prospective.py")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            result = prepare_runtime(args.source_root, args.destination, args.environment_root,
                                     configuration={"ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE": args.min_data_quality_score})
        elif args.operation == "inspect":
            result = inspect_runtime(args.directory)
        else:
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            return run_runtime(args.directory, arguments, entrypoint=args.entrypoint).returncode
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"status": "verified", "digest": result["digest"], "schema_version": result["schema_version"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
