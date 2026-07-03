from __future__ import annotations

from app.db import mappers
from app.db.market_mappers import row_to_quote
from app.db.system_mappers import row_to_provider_status
from app.db.user_mappers import alert_condition_label, row_to_alert_rule


def test_db_mapper_facade_preserves_legacy_imports() -> None:
    assert mappers.row_to_quote is row_to_quote
    assert mappers.row_to_provider_status is row_to_provider_status
    assert mappers.row_to_alert_rule is row_to_alert_rule
    assert mappers.alert_condition_label is alert_condition_label
