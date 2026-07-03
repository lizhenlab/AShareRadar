"""Backward-compatible exports for optional data providers.

Concrete provider implementations live in source-specific modules. Existing
imports can keep using this module while new code should import the provider
module it actually touches.
"""

from __future__ import annotations

from app.services.akshare_provider import AKShareProvider, _import_akshare
from app.services.baostock_provider import BaoStockProvider
from app.services.futu_provider import FutuProvider
from app.services.local_metadata_provider import LocalIndividualStockProvider
from app.services.tushare_provider import TushareProvider

__all__ = [
    "AKShareProvider",
    "BaoStockProvider",
    "FutuProvider",
    "LocalIndividualStockProvider",
    "TushareProvider",
    "_import_akshare",
]
