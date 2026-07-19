from __future__ import annotations

from starlette.responses import Response
from starlette.staticfiles import StaticFiles


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


__all__ = ["RevalidatingStaticFiles"]
