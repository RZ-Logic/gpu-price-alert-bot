"""Retailer registry. Adapters are imported lazily so one broken module cannot stop the rest."""

import importlib
from types import ModuleType

from .. import config

RETAILERS: dict[str, str] = {
    "bestbuy": "gpu_alerts.retailers.bestbuy",
    "canadacomputers": "gpu_alerts.retailers.canadacomputers",
    "memoryexpress": "gpu_alerts.retailers.memoryexpress",
    "newegg": "gpu_alerts.retailers.newegg",
    "amazon": "gpu_alerts.retailers.amazon",
    "walmart": "gpu_alerts.retailers.walmart",
}


def get(retailer_id: str) -> ModuleType:
    return importlib.import_module(RETAILERS[retailer_id])


def enabled_ids() -> list[str]:
    return [rid for rid in RETAILERS if rid not in config.DISABLED_RETAILERS]


def display_name(retailer_id: str) -> str:
    try:
        return get(retailer_id).DISPLAY_NAME
    except Exception:
        return retailer_id
