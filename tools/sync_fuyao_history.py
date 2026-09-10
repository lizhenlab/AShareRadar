"""Explicit Fuyao history sync, integrity checks, and isolated raw research export."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings  # noqa: E402
from app.repositories.fuyao_research import FuyaoResearchRepository  # noqa: E402
from app.services.fuyao_client import FuyaoClient  # noqa: E402
from app.services.fuyao_contracts import FuyaoError  # noqa: E402
from app.services.fuyao_dumps import FuyaoDumpError, read_dump_status, sync_market_dumps  # noqa: E402
from app.services.fuyao_dumps_export import export_dump_research  # noqa: E402
from app.services.fuyao_service import FuyaoPersistentBudget  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("operation", choices=("status", "verify", "full", "incremental", "export"))
    result.add_argument("--root", type=Path, help="离线 status/verify/export 的历史目录；联网同步固定使用项目配置目录")
    result.add_argument("--output", type=Path, help="export 使用的全新独立目录")
    result.add_argument("--max-download-bytes", type=int, default=2_000_000_000)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        payload = _execute(args)
    except (FuyaoDumpError, FuyaoError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print(json.dumps({"status": "failed", "message": "配置或本地文件校验失败"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.operation in ("full", "incremental"):
        if args.root is not None:
            raise FuyaoDumpError("联网同步使用配置中的历史目录与持久预算，不接受 --root")
        return asyncio.run(_sync(get_settings(), args))
    root = args.root or get_settings().cache_path.with_suffix(".fuyao") / "history"
    if args.operation == "export":
        if args.output is None:
            raise FuyaoDumpError("导出需要 --output 指定全新目录")
        output = export_dump_research(root, args.output)
        return {"status": "exported", "manifest": str(output.absolute()), "point_in_time_verified": False}
    manifest = read_dump_status(root, verify_files=args.operation == "verify")
    return {"status": "available" if manifest else "unavailable", "manifest": manifest.model_dump(mode="json") if manifest else None}


async def _sync(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    root = settings.cache_path.with_suffix(".fuyao")
    budget = FuyaoPersistentBudget(FuyaoResearchRepository(root / "research.sqlite3"), settings.fuyao_daily_request_limit)
    client = FuyaoClient(settings, budget=budget)
    try:
        manifest = await sync_market_dumps(client, root / "history", args.operation,
                                          allowed_download_hosts=settings.fuyao_download_hosts, max_download_bytes=args.max_download_bytes)
        return {"status": "published", "manifest": manifest.model_dump(mode="json")}
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(main())
