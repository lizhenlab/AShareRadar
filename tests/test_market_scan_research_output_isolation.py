from __future__ import annotations

import os.path
from pathlib import Path

import pytest

from app.services import market_scan_research_experiment as experiment
from app.services.market_scan_trial_registry import load_trial_registry
from tests.test_run_market_scan_research_cli import bundle as bundle_fixture
from tools import run_market_scan_research as cli


bundle = bundle_fixture


def _arguments(command: str, bundle: Path, registration_id: str, output: Path) -> list[str]:
    result = [command, "--registry-root", str(bundle.parent / "registries"),
              "--registration-id", registration_id, "--output", str(output)]
    if command != "export-registration":
        result.extend(["--bundle", str(bundle)])
    if command == "verify":
        result.extend(["--report", str(bundle.parent / "source-report.json")])
    return result


def _registry_bytes(directory: Path) -> dict[str, bytes] | None:
    return {path.name: path.read_bytes() for path in directory.iterdir()} if directory.exists() else None


def _prepare_registries(bundle: Path, command: str) -> Path:
    for operation in ("register", "run"):
        assert cli.main(_arguments(operation, bundle, "other", bundle.parent / f"other-{operation}.json")) == 0
    if command != "register":
        assert cli.main(_arguments("register", bundle, "source", bundle.parent / "source-registration.json")) == 0
    if command in {"replay", "verify"}:
        assert cli.main(_arguments("run", bundle, "source", bundle.parent / "source-report.json")) == 0
    return bundle.parent / "registries"


@pytest.mark.parametrize("command", ["register", "run", "replay", "verify", "export-registration"])
@pytest.mark.parametrize("path_style", ["direct", "parent", "symlink"])
def test_output_cannot_pollute_another_sealed_registration(bundle, monkeypatch, capsys, command, path_style) -> None:
    monkeypatch.setattr(experiment, "research_implementation_digest", lambda: "a" * 64)
    root = _prepare_registries(bundle, command)
    if path_style == "symlink":
        target = bundle.parent / "output-alias"
        target.symlink_to(root / "other", target_is_directory=True)
    else:
        target = root / "other" if path_style == "direct" else root / "other" / ".." / "other"
    output = target / "unexpected-report.json"
    before_source = _registry_bytes(root / "source")
    before_other = _registry_bytes(root / "other")
    sealed_other = load_trial_registry(root, "other")
    assert sealed_other.seal is not None

    assert cli.main(_arguments(command, bundle, "source", output)) == 1

    assert "output must be outside the immutable registry root" in capsys.readouterr().err
    assert _registry_bytes(root / "source") == before_source
    assert _registry_bytes(root / "other") == before_other
    assert load_trial_registry(root, "other") == sealed_other
    assert not output.exists()


def test_output_cannot_create_a_file_in_registry_root(bundle, monkeypatch, capsys) -> None:
    monkeypatch.setattr(experiment, "research_implementation_digest", lambda: "a" * 64)
    root = _prepare_registries(bundle, "export-registration")
    output = root / "receipt.json"
    assert cli.main(_arguments("export-registration", bundle, "source", output)) == 1
    assert "output must be outside the immutable registry root" in capsys.readouterr().err
    assert not output.exists()


def test_similar_prefix_outside_registry_root_remains_a_valid_output(bundle, monkeypatch) -> None:
    monkeypatch.setattr(experiment, "research_implementation_digest", lambda: "a" * 64)
    root = _prepare_registries(bundle, "export-registration")
    before = _registry_bytes(root / "source")
    output = bundle.parent / "registries-reports" / "registration.json"
    assert cli.main(_arguments("export-registration", bundle, "source", output)) == 0
    assert output.is_file()
    assert _registry_bytes(root / "source") == before


@pytest.mark.parametrize("argument", ["--registry-root", "--output"])
def test_user_home_paths_share_the_artifact_publish_boundary(bundle, monkeypatch, capsys, argument) -> None:
    monkeypatch.setattr(experiment, "research_implementation_digest", lambda: "a" * 64)
    root = _prepare_registries(bundle, "run")
    original_expanduser = os.path.expanduser

    def expanduser(path):
        # Resolve a virtual user's home without changing the process HOME.
        prefix = "~research-isolation-fixture"
        if path == prefix or path.startswith(prefix + "/"):
            return str(bundle.parent / path[len(prefix):].lstrip("/"))
        return original_expanduser(path)

    monkeypatch.setattr(os.path, "expanduser", expanduser)
    output = root / "other" / "unexpected-report.json"
    args = _arguments("run", bundle, "source", output)
    chosen = Path(args[args.index(argument) + 1])
    args[args.index(argument) + 1] = "~research-isolation-fixture/" + str(chosen.relative_to(bundle.parent))
    before_source = _registry_bytes(root / "source")
    before_other = _registry_bytes(root / "other")
    sealed_other = load_trial_registry(root, "other")

    assert cli.main(args) == 1

    assert "output must be outside the immutable registry root" in capsys.readouterr().err
    assert _registry_bytes(root / "source") == before_source
    assert _registry_bytes(root / "other") == before_other
    assert load_trial_registry(root, "other") == sealed_other
    assert not output.exists()
