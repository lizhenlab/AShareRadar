"""Pydantic model package for AShareRadar."""

from __future__ import annotations

from app.models import schemas as _schemas

__all__ = list(_schemas.__all__)
globals().update({name: getattr(_schemas, name) for name in __all__})
