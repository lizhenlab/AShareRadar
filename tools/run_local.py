"""Start the local service using the optional Git-ignored Fuyao credential file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import MutableMapping, Sequence

import uvicorn


ROOT = Path(__file__).resolve().parent.parent


def configure_local_fuyao(environment: MutableMapping[str, str], root: Path) -> bool:
    """Set non-secret defaults only; credential loading remains in FuyaoClient."""
    key_file = root / "data" / "fuyao-api-key"
    if key_file.is_symlink() or not key_file.is_file():
        return False
    environment.setdefault("ASHARE_RADAR_FUYAO_ENABLED", "true")
    environment.setdefault("ASHARE_RADAR_FUYAO_API_KEY_FILE", str(key_file))
    environment.setdefault("ASHARE_RADAR_FUYAO_DOWNLOAD_HOSTS", "o.thsi.cn")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    configure_local_fuyao(os.environ, ROOT)
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=1, timeout_graceful_shutdown=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
