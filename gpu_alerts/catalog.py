"""The GPU models the bot knows about, loaded from data/catalog.json."""

import json
import re
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
    vram_gb: int | None     # the title must state this VRAM; None means any VRAM
    query: str              # search string sent to retailers
    min_price: float        # CAD sanity floor
    legacy: bool = False
    popular: bool = False
    vram_options: tuple[int, ...] | None = None   # sizes sold when vram_gb is None; another stated size rejects

    @property
    def key(self) -> str:
        """Canonical model key without VRAM, e.g. "RTX 5070 TI" or "ARC B580"."""
        return " ".join(p for p in (self.family, self.number, self.suffix) if p)


@lru_cache(maxsize=1)
def load() -> tuple[GPUModel, ...]:
    doc = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return tuple(
        GPUModel(**{**m, "vram_options": tuple(m["vram_options"]) if m.get("vram_options") else None})
        for m in doc["models"]
    )


def by_id(model_id: str) -> GPUModel | None:
    return next((m for m in load() if m.id == model_id), None)


_VRAM_WORD = re.compile(r"(\d+)gb?")


def _split_vram(words: list[str]) -> tuple[list[str], set[int]]:
    """Typed words without VRAM sizes ("8gb", "16g", "8 gb"), and the sizes."""
    rest, sizes = [], set()
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w.isdigit() and nxt in ("gb", "g"):
            sizes.add(int(w))
            i += 2
            continue
        m = _VRAM_WORD.fullmatch(w)
        if m:
            sizes.add(int(m.group(1)))
        else:
            rest.append(w)
        i += 1
    return rest, sizes


def search(text: str, limit: int = 25) -> list[GPUModel]:
    """Autocomplete: models whose name or id contains every typed word.

    A typed VRAM size is compared with the model's VRAM, not its name: only the 8GB/16GB
    variants carry a size in their name, so "RTX 5060 8GB" would otherwise leave just the
    RTX 5060 Ti 8GB. Models with any VRAM (vram_gb None) stay in the list.
    """
    words, sizes = _split_vram(text.lower().replace("-", " ").split())
    hits = [
        m for m in load()
        if all(w in f"{m.name.lower()} {m.id.replace('-', ' ')}" for w in words)
        and (not sizes or m.vram_gb is None or m.vram_gb in sizes)
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
