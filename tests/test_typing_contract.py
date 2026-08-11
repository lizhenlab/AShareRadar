from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]

# This is a review-visible floor, not a target. New files may be added freely;
# removing any protected file requires an explicit contract change.
MINIMUM_MYPY_FILES = frozenset(
    {
        "app/api/container.py",
        "app/api/deps.py",
        "app/api/errors.py",
        "app/api/routes/health.py",
        "app/api/routes/monitoring.py",
        "app/api/routes/quotes.py",
        "app/api/security.py",
        "app/api/static_assets.py",
        "app/config.py",
        "app/config_settings.py",
        "app/config_shell.py",
        "app/config_validation.py",
        "app/main.py",
        "app/models/market_scan.py",
        "app/models/reliability.py",
        "app/models/system.py",
        "app/repositories/base.py",
        "app/repositories/maintenance.py",
        "app/repositories/market_klines.py",
        "app/repositories/market_quotes.py",
        "app/repositories/market_scan.py",
        "app/repositories/market_scan_context.py",
        "app/repositories/market_scan_lifecycle.py",
        "app/repositories/market_scan_mapping.py",
        "app/repositories/market_scan_queries.py",
        "app/repositories/market_scan_results.py",
        "app/repositories/provider_status.py",
        "app/repositories/reliability.py",
        "app/repositories/update_fields.py",
        "app/services/daemon_executor.py",
        "app/services/datahub_metadata.py",
        "app/services/datahub_metadata_coordinator.py",
        "app/services/datahub_metadata_mapping.py",
        "app/services/datahub_metadata_provider.py",
        "app/services/datahub_metadata_stock_pool.py",
        "app/services/datahub_quotes.py",
        "app/services/datahub_source_plan.py",
        "app/services/datahub_status_service.py",
        "app/services/market_scan_completion.py",
        "app/services/market_scan_execution.py",
        "app/services/market_scan_lifecycle.py",
        "app/services/market_scan_manager.py",
        "app/services/market_scan_scoring.py",
        "app/services/market_scan_universe.py",
        "app/services/optional_providers.py",
        "app/services/provider_errors.py",
        "app/services/provider_failure_status.py",
        "app/services/provider_registry.py",
        "app/services/provider_stock_mappers.py",
        "app/services/provider_utils.py",
        "app/services/providers.py",
        "app/services/runtime_backup.py",
        "app/services/runtime_coordinator.py",
        "app/services/scheduler.py",
        "app/services/scheduler_contracts.py",
        "app/services/scheduler_execution.py",
        "app/services/scheduler_health.py",
        "app/services/scheduler_helpers.py",
        "app/services/scheduler_lifecycle.py",
        "app/services/scheduler_schedule.py",
        "app/services/scheduler_service.py",
        "app/services/scheduler_tasks.py",
        "app/services/stock_overview.py",
        "app/services/system_diagnostics.py",
        "app/services/task_run_lifecycle.py",
        "app/utils/audit_time.py",
        "app/utils/clock.py",
        "app/utils/market_time.py",
        "app/utils/provider_errors.py",
        "app/utils/stock_pool.py",
        "app/utils/text.py",
        "app/utils/time.py",
        "tools/api_inventory.py",
        "tools/architecture_inventory.py",
    }
)


def _mypy_config() -> dict[str, object]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["mypy"]


def test_mypy_scope_cannot_shrink_silently() -> None:
    config = _mypy_config()
    configured_files = config["files"]

    assert isinstance(configured_files, list)
    assert all(isinstance(path, str) for path in configured_files)
    typed_files = set(configured_files)

    assert configured_files == sorted(typed_files), "mypy files must stay unique and sorted"
    assert MINIMUM_MYPY_FILES <= typed_files, (
        "mypy scope dropped protected files: "
        + ", ".join(sorted(MINIMUM_MYPY_FILES - typed_files))
    )
    assert len(typed_files) >= len(MINIMUM_MYPY_FILES)


def test_mypy_scope_uses_explicit_existing_python_files() -> None:
    configured_files = _mypy_config()["files"]
    assert isinstance(configured_files, list)

    invalid = [
        path
        for path in configured_files
        if not isinstance(path, str)
        or not path.endswith(".py")
        or any(character in path for character in "*?[]")
        or not (ROOT / path).is_file()
    ]

    assert invalid == []


def test_mypy_scope_covers_a_meaningful_share_of_application_modules() -> None:
    configured_files = _mypy_config()["files"]
    assert isinstance(configured_files, list)
    app_files = {path.as_posix() for path in (ROOT / "app").rglob("*.py")}
    typed_app_files = {path for path in configured_files if isinstance(path, str) and path.startswith("app/")}

    assert len(typed_app_files) / len(app_files) >= 0.4


def test_mypy_scope_does_not_hide_errors() -> None:
    config = _mypy_config()

    assert config.get("ignore_errors") is not True
    overrides = config.get("overrides", [])
    assert isinstance(overrides, list)
    assert all(
        not isinstance(override, dict) or override.get("ignore_errors") is not True
        for override in overrides
    )
