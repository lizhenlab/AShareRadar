from __future__ import annotations

import ast
import asyncio
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


@dataclass(frozen=True)
class _BoundaryPolicy:
    mode: str
    reason: str


# Catching BaseException is reserved for process, task, transaction, and resource
# ownership boundaries. Every exception must either propagate or follow one of the
# narrowly reviewed hand-off policies below.
_BASE_EXCEPTION_BOUNDARIES = {
    ("app/artifacts/io.py", "_publish_at_directory_descriptor"): _BoundaryPolicy(
        "propagate", "Remove an unpublished temporary artifact after cancellation or fatal publication failure."
    ),
    ("app/artifacts/io.py", "_publish_with_guarded_paths"): _BoundaryPolicy(
        "propagate", "Remove an unpublished temporary artifact after cancellation or fatal fallback publication failure."
    ),
    ("app/db/advice_review_schema.py", "apply_advice_review_compat_schema"): _BoundaryPolicy(
        "propagate", "Roll back the compatibility-schema transaction before preserving the original failure."
    ),
    ("app/main.py", "lifespan"): _BoundaryPolicy(
        "propagate", "Clean up a partially started application before preserving startup cancellation or failure."
    ),
    ("app/main.py", "_stop_runtime"): _BoundaryPolicy(
        "aggregate", "Attempt every runtime shutdown step, then raise the collected failures as one boundary error."
    ),
    ("app/main.py", "_shutdown_container"): _BoundaryPolicy(
        "aggregate", "Attempt every application resource cleanup, then raise all collected failures."
    ),
    ("app/services/daemon_executor.py", "DaemonThreadPoolExecutor.submit"): _BoundaryPolicy(
        "future", "Transfer worker-start failure to the Future that represents the submitted operation."
    ),
    ("app/services/daemon_executor.py", "_worker"): _BoundaryPolicy(
        "future", "Transfer every worker outcome, including fatal failures, to the owning Future."
    ),
    ("app/services/instance_guard.py", "FileInstanceGuard.acquire"): _BoundaryPolicy(
        "propagate", "Release the partially acquired file handle and lock before preserving the failure."
    ),
    ("app/services/market_scan_lifecycle.py", "MarketScanLifecycle.ensure_instance_guard"): _BoundaryPolicy(
        "propagate", "Release market-scan ownership when reconciliation fails, including during cancellation."
    ),
    ("app/services/market_scan_lifecycle.py", "MarketScanLifecycle.release_instance_guard"): _BoundaryPolicy(
        "propagate", "Restore in-memory ownership state when lock release fails, then preserve the failure."
    ),
    ("app/services/market_scan_probability_history.py", "_stage_and_publish"): _BoundaryPolicy(
        "propagate", "Remove a partially published history database before preserving cancellation or fatal failure."
    ),
    ("app/services/market_scan_probability_replay.py", "_readonly_connection"): _BoundaryPolicy(
        "propagate", "Close the immutable replay connection before preserving cancellation or fatal body failure."
    ),
    ("app/services/runtime_backup.py", "_runtime_backup_operation_lease"): _BoundaryPolicy(
        "propagate", "Remember the body failure while releasing every backup-operation lease, then re-raise it."
    ),
    ("app/services/runtime_backup.py", "_release_operation_resources"): _BoundaryPolicy(
        "return", "Attempt all releases and return the first fatal cleanup error to the owning context manager."
    ),
    ("app/services/runtime_backup.py", "runtime_backup_session"): _BoundaryPolicy(
        "propagate", "Finalize a quiesced backup session without replacing the caller's cancellation or failure."
    ),
    ("app/services/runtime_backup.py", "_create_runtime_backup_bundle"): _BoundaryPolicy(
        "propagate", "Remove an incomplete backup bundle before preserving the original failure."
    ),
    ("app/services/runtime_backup.py", "_replace_from_verified_backup"): _BoundaryPolicy(
        "propagate", "Recover an atomically replaced database if needed, then preserve the restore failure."
    ),
    ("app/services/runtime_backup.py", "_recover_failed_restore"): _BoundaryPolicy(
        "propagate", "Wrap a failed rollback with both restore errors while retaining exception chaining."
    ),
    ("app/services/runtime_backup.py", "_restore_guard"): _BoundaryPolicy(
        "propagate", "Release all restore guards while preserving a failure raised by the guarded body."
    ),
    ("app/services/runtime_backup.py", "_sqlite_snapshot"): _BoundaryPolicy(
        "propagate", "Close the SQLite snapshot and delete partial output before preserving the failure."
    ),
    ("app/services/runtime_backup.py", "_copy_database_file"): _BoundaryPolicy(
        "propagate", "Delete an incomplete staged database copy before preserving the failure."
    ),
    ("app/services/runtime_coordinator.py", "RuntimeCoordinator._try_activate"): _BoundaryPolicy(
        "propagate", "Roll back partially activated runtime services and leadership before re-raising."
    ),
    ("app/services/scheduler_lifecycle.py", "SchedulerLifecycleMixin.start"): _BoundaryPolicy(
        "propagate", "Abort partial scheduler startup and preserve cancellation or fatal failure."
    ),
    ("app/services/scheduler_lifecycle.py", "SchedulerLifecycleMixin._release_instance_guard"): _BoundaryPolicy(
        "propagate", "Restore scheduler ownership state when asynchronous lock release fails."
    ),
    ("app/services/task_run_lifecycle.py", "_TaskRunStartHandoff.run"): _BoundaryPolicy(
        "future", "Transfer database start failure across the thread-to-async Future hand-off."
    ),
    ("app/services/task_run_lifecycle.py", "start_task_run_cancel_safe"): _BoundaryPolicy(
        "propagate", "Notify the worker hand-off of cancellation before preserving the original failure."
    ),
    ("app/services/trading_calendar.py", "_save_days"): _BoundaryPolicy(
        "propagate", "Delete the temporary calendar file before preserving an atomic-write failure."
    ),
    ("app/services/user_data_portability.py", "import_user_data"): _BoundaryPolicy(
        "propagate", "Roll back the user-data import transaction before preserving any fatal failure."
    ),
}

_BASE_EXCEPTION_SUPPRESSIONS = {
    ("app/main.py", "_cleanup_failed_start"): (
        1,
        "Best-effort cleanup runs while lifespan is already preserving the original startup failure.",
    ),
    ("app/main.py", "_close_container_resources_safely"): (
        2,
        "Best-effort close is used only after container validation or startup has already failed.",
    ),
}

# These handlers are terminal observers: the cancelled task or SSE connection has
# already reached its ownership boundary, so there is no caller left to cancel.
_CANCELLATION_CONSUMERS = {
    ("app/api/routes/quotes.py", "_QuoteStreamResponse.__call__"): "ASGI disconnect is terminal for the SSE response.",
    ("app/api/routes/quotes.py", "_quote_stream_events"): "Client disconnect is terminal for the SSE generator.",
    ("app/services/datahub.py", "_consume_provider_close_exception"): "Done callback only observes a completed close task.",
    ("app/services/datahub_runtime.py", "ProviderRuntime._finish_provider_call"): "Done callback only consumes a completed provider Future.",
    ("app/services/market_scan_lifecycle.py", "MarketScanLifecycle._task_done"): "Done callback removes bookkeeping for an already completed task.",
    ("app/services/market_scan_manager.py", "_consume_stop_exception"): "Done callback only observes the shielded stop task.",
    ("app/services/runtime_coordinator.py", "_consume_future_exception"): "Done callback only retrieves an already completed Future exception.",
    ("app/services/scheduler_helpers.py", "_consume_future_exception"): "Done callback prevents unobserved-exception warnings after completion.",
    ("app/services/task_run_lifecycle.py", "_consume_future_exception"): "Done callback observes a completed thread hand-off Future.",
    ("app/services/workbench_context.py", "_consume_task_exception"): "Done callback observes a completed shared context task.",
}

_PROVIDER_SANITIZER = "app.utils.provider_errors"
_LEGACY_PROVIDER_FACADE = "app.services.provider_errors"
_REQUIRED_PROVIDER_SANITIZATION_BOUNDARIES = {
    ("app/api/errors.py", "_api_exception"),
    ("app/api/routes/quotes.py", "_next_quote_stream_event"),
    ("app/db/system_mappers.py", "_sanitized_provider_error"),
    ("app/repositories/provider_status.py", "_trim_error"),
    ("app/services/datahub_runtime.py", "ProviderRuntime._sanitized_error_text"),
}

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_RESPONSE_CONSTRUCTORS = {"HTTPException", "JSONResponse", "PlainTextResponse", "Response"}
_SENSITIVE_LITERAL_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|authorization|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"client[_-]?secret|secret|credential)\b\s*[:=]\s*(?!<redacted>|\*{3,}|x{3,})"
    r"(?:bearer\s+)?[a-z0-9._~+/=-]{6,}"
)
_BEARER_LITERAL_RE = re.compile(r"(?i)\bbearer\s+(?!<redacted>)[a-z0-9._~+/=-]{6,}")


def _python_units() -> tuple[tuple[str, ast.Module], ...]:
    return tuple(
        (str(path.relative_to(ROOT)), ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(APP.rglob("*.py"))
    )


def _scoped_nodes(tree: ast.Module, node_types: type[ast.AST] | tuple[type[ast.AST], ...]) -> list[tuple[str, ast.AST]]:
    found: list[tuple[str, ast.AST]] = []
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            stack.append(node.name)
            if isinstance(node, node_types):
                found.append((".".join(stack), node))
            super().generic_visit(node)
            stack.pop()

        visit_ClassDef = _visit_scope
        visit_FunctionDef = _visit_scope
        visit_AsyncFunctionDef = _visit_scope

        def generic_visit(self, node: ast.AST) -> None:
            if isinstance(node, node_types):
                found.append((".".join(stack) or "<module>", node))
            super().generic_visit(node)

    Visitor().visit(tree)
    return found


def _caught_names(node: ast.expr | None) -> set[str]:
    if node is None:
        return {"<bare>"}
    if isinstance(node, ast.Tuple):
        return {name for item in node.elts for name in _caught_names(item)}
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return {ast.unparse(node)}


def _handler_has_raise(handler: ast.ExceptHandler) -> bool:
    class RaiseFinder(ast.NodeVisitor):
        found = False

        def visit_Raise(self, node: ast.Raise) -> None:
            self.found = True

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

    finder = RaiseFinder()
    for statement in handler.body:
        finder.visit(statement)
    return finder.found


def _call_terminal_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _function_has_call(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    return any(isinstance(node, ast.Call) and _call_terminal_name(node) == name for node in ast.walk(function))


def _function_returns_name(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    return any(
        isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id == name
        for node in ast.walk(function)
    )


def _function_nodes() -> dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef]:
    functions: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for path, tree in _python_units():
        for qualname, node in _scoped_nodes(tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            functions[(path, qualname)] = node
    return functions


def _is_base_exception_suppression(node: ast.With | ast.AsyncWith) -> bool:
    for item in node.items:
        expression = item.context_expr
        if not isinstance(expression, ast.Call) or _call_terminal_name(expression) != "suppress":
            continue
        if any("BaseException" in _caught_names(argument) for argument in expression.args):
            return True
    return False


def _attribute_chain(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_attribute_chain(node.value), node.attr)
    return ()


def _is_output_call(call: ast.Call) -> bool:
    chain = _attribute_chain(call.func)
    if chain == ("print",):
        return True
    if len(chain) >= 2 and chain[-1] == "write" and chain[-2] in {"stdout", "stderr"}:
        return True
    if chain and chain[-1] in _RESPONSE_CONSTRUCTORS:
        return True
    return bool(
        chain
        and chain[-1] in _LOG_METHODS
        and any(part.lower() == "logging" or part.lower().endswith("logger") for part in chain[:-1])
    )


def _is_sensitive_identifier(name: str, *, include_ambiguous_token: bool = False) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    parts = tuple(part for part in normalized.split("_") if part)
    if "api_key" in normalized or "apikey" in normalized or "authorization" in parts:
        return True
    if any(part in {"password", "passwd", "credential", "credentials"} for part in parts):
        return True
    if "secret" in parts:
        return True
    return (include_ambiguous_token and normalized == "token") or normalized.endswith("_token")


def _sensitive_expression_markers(node: ast.AST, *, include_ambiguous_token: bool) -> set[str]:
    markers: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and _is_sensitive_identifier(
            child.id,
            include_ambiguous_token=include_ambiguous_token,
        ):
            markers.add(child.id)
        elif isinstance(child, ast.Attribute) and _is_sensitive_identifier(
            child.attr,
            include_ambiguous_token=include_ambiguous_token,
        ):
            markers.add(child.attr)
        elif isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant):
            key = child.slice.value
            if isinstance(key, str) and _is_sensitive_identifier(
                key,
                include_ambiguous_token=include_ambiguous_token,
            ):
                markers.add(key)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            if _SENSITIVE_LITERAL_RE.search(child.value) or _BEARER_LITERAL_RE.search(child.value):
                markers.add("literal-secret")
    return markers


def _sensitive_output_findings(path: str, tree: ast.Module) -> list[str]:
    findings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_output_call(node):
            markers = _sensitive_expression_markers(node, include_ambiguous_token=True)
            if markers:
                findings.add(f"{path}:{node.lineno} ({', '.join(sorted(markers))})")
        elif isinstance(node, ast.Raise) and node.exc is not None:
            markers = _sensitive_expression_markers(node.exc, include_ambiguous_token=False)
            if markers:
                findings.add(f"{path}:{node.lineno} ({', '.join(sorted(markers))})")
    return sorted(findings)


def test_base_exception_catches_are_explicit_reviewed_boundaries() -> None:
    functions = _function_nodes()
    actual_keys: set[tuple[str, str]] = set()
    propagation_failures: list[str] = []

    for path, tree in _python_units():
        for qualname, node in _scoped_nodes(tree, ast.ExceptHandler):
            assert isinstance(node, ast.ExceptHandler)
            caught = _caught_names(node.type)
            if "<bare>" in caught:
                propagation_failures.append(f"{path}:{node.lineno} {qualname} uses bare except")
                continue
            if "BaseException" not in caught:
                continue
            key = (path, qualname)
            actual_keys.add(key)
            policy = _BASE_EXCEPTION_BOUNDARIES.get(key)
            if policy is None:
                propagation_failures.append(f"{path}:{node.lineno} {qualname} is not a reviewed boundary")
            elif policy.mode == "propagate" and not _handler_has_raise(node):
                propagation_failures.append(f"{path}:{node.lineno} {qualname} no longer re-raises")

    assert all(len(policy.reason.strip()) >= 20 for policy in _BASE_EXCEPTION_BOUNDARIES.values())
    assert actual_keys == set(_BASE_EXCEPTION_BOUNDARIES), (
        "BaseException boundary allowlist is stale; review additions and remove obsolete entries: "
        f"actual_only={sorted(actual_keys - set(_BASE_EXCEPTION_BOUNDARIES))}, "
        f"allowlist_only={sorted(set(_BASE_EXCEPTION_BOUNDARIES) - actual_keys)}"
    )
    assert propagation_failures == []

    evidence_failures: list[str] = []
    for key, policy in _BASE_EXCEPTION_BOUNDARIES.items():
        function = functions[key]
        if policy.mode == "aggregate" and not _function_has_call(function, "_raise_cleanup_errors"):
            evidence_failures.append(f"{key} no longer raises its aggregated cleanup errors")
        elif policy.mode == "future" and not _function_has_call(function, "set_exception"):
            evidence_failures.append(f"{key} no longer transfers the failure to its Future")
        elif policy.mode == "return" and not _function_returns_name(function, "first_error"):
            evidence_failures.append(f"{key} no longer returns the collected cleanup error")
        elif policy.mode not in {"propagate", "aggregate", "future", "return"}:
            evidence_failures.append(f"{key} has unknown policy {policy.mode!r}")
    assert evidence_failures == []


def test_base_exception_suppression_is_limited_to_documented_cleanup_paths() -> None:
    actual: Counter[tuple[str, str]] = Counter()
    for path, tree in _python_units():
        for qualname, node in _scoped_nodes(tree, (ast.With, ast.AsyncWith)):
            assert isinstance(node, (ast.With, ast.AsyncWith))
            if _is_base_exception_suppression(node):
                actual[(path, qualname)] += 1

    expected = Counter({key: count for key, (count, _reason) in _BASE_EXCEPTION_SUPPRESSIONS.items()})
    assert all(len(reason.strip()) >= 20 for _count, reason in _BASE_EXCEPTION_SUPPRESSIONS.values())
    assert actual == expected


def test_cancelled_error_propagates_except_at_terminal_observers() -> None:
    non_propagating: Counter[tuple[str, str]] = Counter()
    offenders: list[str] = []

    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception)

    for path, tree in _python_units():
        for qualname, node in _scoped_nodes(tree, ast.ExceptHandler):
            assert isinstance(node, ast.ExceptHandler)
            if "CancelledError" not in _caught_names(node.type) or _handler_has_raise(node):
                continue
            key = (path, qualname)
            non_propagating[key] += 1
            if key not in _CANCELLATION_CONSUMERS:
                offenders.append(f"{path}:{node.lineno} {qualname}")

    expected = Counter({key: 1 for key in _CANCELLATION_CONSUMERS})
    assert all(len(reason.strip()) >= 20 for reason in _CANCELLATION_CONSUMERS.values())
    assert non_propagating == expected
    assert offenders == []


def test_cancelled_error_handlers_are_not_shadowed_by_base_exception() -> None:
    offenders: list[str] = []
    for path, tree in _python_units():
        for _qualname, node in _scoped_nodes(tree, (ast.Try, ast.TryStar)):
            assert isinstance(node, (ast.Try, ast.TryStar))
            base_exception_seen = False
            for handler in node.handlers:
                caught = _caught_names(handler.type)
                if "CancelledError" in caught and base_exception_seen:
                    offenders.append(f"{path}:{handler.lineno}")
                if "BaseException" in caught or "<bare>" in caught:
                    base_exception_seen = True
    assert offenders == []


def test_sensitive_values_are_not_sent_directly_to_logs_responses_or_errors() -> None:
    offenders = [finding for path, tree in _python_units() for finding in _sensitive_output_findings(path, tree)]
    assert offenders == []


def test_sensitive_output_guard_distinguishes_secret_values_from_configuration_labels() -> None:
    tree = ast.parse(
        """
def sample(settings, logger):
    logger.info("Required variable ASHARE_LLM_API_KEY is missing")
    logger.error("credential=%s", settings.api_key)
    print("Authorization: Bearer abcdef123456")
"""
    )
    findings = _sensitive_output_findings("sample.py", tree)
    assert len(findings) == 2
    assert any("api_key" in finding for finding in findings)
    assert any("literal-secret" in finding for finding in findings)


def test_provider_error_sanitizer_has_one_canonical_production_entry_point() -> None:
    definitions: list[tuple[str, int]] = []
    wrong_imports: list[str] = []
    legacy_imports: list[str] = []

    for path, tree in _python_units():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "sanitize_provider_error":
                definitions.append((path, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                if "sanitize_provider_error" in imported and node.module != _PROVIDER_SANITIZER:
                    wrong_imports.append(f"{path}:{node.lineno} from {node.module}")
                if node.module == _LEGACY_PROVIDER_FACADE and path != "app/services/provider_errors.py":
                    legacy_imports.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Import):
                if any(alias.name == _LEGACY_PROVIDER_FACADE for alias in node.names):
                    legacy_imports.append(f"{path}:{node.lineno}")

    assert [path for path, _line in definitions] == ["app/utils/provider_errors.py"]
    assert wrong_imports == []
    assert legacy_imports == []

    facade = ast.parse(
        (ROOT / "app/services/provider_errors.py").read_text(encoding="utf-8"),
        filename="app/services/provider_errors.py",
    )
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in ast.walk(facade))
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == _PROVIDER_SANITIZER
        and "sanitize_provider_error" in {alias.name for alias in node.names}
        for node in facade.body
    )


def test_provider_errors_are_sanitized_at_external_and_persistence_boundaries() -> None:
    functions = _function_nodes()
    missing = [
        key
        for key in sorted(_REQUIRED_PROVIDER_SANITIZATION_BOUNDARIES)
        if key not in functions or not _function_has_call(functions[key], "sanitize_provider_error")
    ]
    assert missing == []
