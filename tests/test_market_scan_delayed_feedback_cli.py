from __future__ import annotations

from copy import deepcopy
import json

from app.artifacts.io import canonical_json_bytes
from tests.test_market_scan_delayed_feedback import CREATED, DECISION, config, label, prediction
from tools import manage_market_scan_feedback as cli


def write_json(path, payload):
    path.write_bytes(canonical_json_bytes(payload))
    return str(path)


def test_cli_internal_clock_batch_append_verify_and_immutable_outputs(tmp_path, monkeypatch) -> None:
    config_path = write_json(tmp_path / "config.json", config())
    first, second, third = (tmp_path / name for name in ("ledger1.json", "ledger2.json", "ledger3.json"))
    monkeypatch.setattr(cli, "utc_now", lambda: CREATED)
    assert cli.main(["create", "--config", config_path, "--output", str(first)]) == 0
    first_bytes = first.read_bytes()
    ledger = json.loads(first_bytes)
    predictions = write_json(tmp_path / "predictions.json", [prediction()])
    monkeypatch.setattr(cli, "utc_now", lambda: DECISION)
    args = ["append", "--ledger", str(first), "--ledger-digest", ledger["ledger_digest"], "--events", predictions, "--output", str(second)]
    assert cli.main(args) == 0
    assert cli.main(args) == 1
    assert first.read_bytes() == first_bytes
    newer = json.loads(second.read_bytes())
    assert newer["events"][0]["recorded_at"] == "2026-08-24T02:00:00+00:00"
    assert cli.main(["verify", "--ledger", str(second), "--ledger-digest", newer["ledger_digest"]]) == 0
    assert cli.main(["verify", "--ledger", str(second), "--ledger-digest", "f" * 64]) == 1
    labels = write_json(tmp_path / "labels.json", [label()])
    monkeypatch.setattr(cli, "utc_now", lambda: "2026-08-24T15:06:00+08:00")
    assert cli.main(["append", "--ledger", str(second), "--ledger-digest", newer["ledger_digest"],
                     "--events", labels, "--output", str(third)]) == 0
    assert json.loads(third.read_bytes())["summary"]["matured_label_count"] == 1


def test_cli_rejects_injected_clock_duplicate_json_and_overwrite_race(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_bytes(b'{"model_digest":"x","model_digest":"y"}')
    output = tmp_path / "output.json"
    assert cli.main(["create", "--config", str(config_path), "--output", str(output)]) == 1
    assert not output.exists()
    write_json(config_path, config())
    monkeypatch.setattr(cli, "utc_now", lambda: CREATED)
    monkeypatch.setattr(cli, "exclusive_atomic_publish", lambda *args, **kwargs: False)
    assert cli.main(["create", "--config", str(config_path), "--output", str(output)]) == 1
    bad = deepcopy(prediction())
    bad["recorded_at"] = CREATED
    try:
        from app.services.market_scan_delayed_feedback_contracts import admit_feedback_events

        admit_feedback_events([bad])
        raise AssertionError("recorded_at must be supplied by CLI, never event input")
    except ValueError:
        pass
