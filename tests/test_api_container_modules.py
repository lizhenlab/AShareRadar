from __future__ import annotations

from app.api.container import build_container


def test_container_reuses_datahub_workbench_context_cache() -> None:
    container = build_container()

    assert container.workbench_contexts is container.datahub.workbench_contexts
