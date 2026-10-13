"""The GPU models the bot knows about, loaded from data/catalog.json."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).parent / "data" / "catalog.json"


@dataclass(frozen=True)
class GPUModel:
    id: str                 # stable key, e.g. "rtx-5070-ti"
    name: str               # display name, e.g. "RTX 5070 Ti"
    vendor: str             # nvidia | amd | intel
    family: str             # RTX | RX | ARC
    number: str             # "5070", or "B580" for Arc
    suffix: str | None      # None | TI | SUPER | TI SUPER | XT | XTX | GRE
    vram_gb: int | None     # None means any VRAM
    query: str              # search string sent to retailers
    min_price: float        # CAD sanity floor
    legacy: bool = False
    popular: bool = False

    @property
    def key(self) -> str:
        """Canonical model key without VRAM, e.g. "RTX 5070 TI" or "ARC B580"."""
        return " ".join(p for p in (self.family, self.number, self.suffix) if p)


@lru_cache(maxsize=1)
def load() -> tuple[GPUModel, ...]:
    doc = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return tuple(GPUModel(**m) for m in doc["models"])


def by_id(model_id: str) -> GPUModel | None:
    return next((m for m in load() if m.id == model_id), None)


def search(text: str, limit: int = 25) -> list[GPUModel]:
    """Autocomplete: models whose name or id contains every typed word."""
    words = text.lower().replace("-", " ").split()
    hits = [
        m for m in load()
        if all(w in f"{m.name.lower()} {m.id.replace('-', ' ')}" for w in words)
    ]
    hits.sort(key=lambda m: (m.legacy, not m.popular, m.name))
    return hits[:limit]


def resolve(text: str) -> GPUModel | None:
    """Exact id, exact name (case-insensitive), or a single autocomplete hit."""
    t = text.strip().lower()
    for m in load():
        if t in (m.id, m.name.lower()):
            return m
    hits = search(text)
    return hits[0] if len(hits) == 1 else None
