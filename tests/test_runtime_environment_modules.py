from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from app.runtime_environment import isolate_user_site_packages


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
    assert "/opt/anaconda3/" in payload["numpy"]
    assert "/opt/anaconda3/" in payload["pandas"]
