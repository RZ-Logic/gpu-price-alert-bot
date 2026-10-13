"""The v1 bot's listing filter, re-implemented for evaluation.

Copied from gpu_discord_bot.py (JUNK_KEYWORDS, GPU_KEYWORDS, is_valid_gpu and the
per-scraper "all query words appear in the title" check). Kept as a separate
copy so the eval never imports the v1 bot (which needs discord.py and a token).
Substring semantics are deliberate: they are the behaviour being measured.
"""

# Keywords that indicate a result is NOT a GPU (gpu_discord_bot.py:125)
JUNK_KEYWORDS = [
    "RISER", "CABLE", "ADAPTER", "BRACKET", "BRIDGE", "MOUNT", "STAND",
    "CASE", "ENCLOSURE", "DOCK", "HUB", "FAN", "COOLER", "HEATSINK",
    "BACKPLATE", "SHROUD", "COVER", "SKIN", "STICKER", "DECAL",
    "POWER SUPPLY", "PSU", "MOTHERBOARD", "CPU", "RAM", "SSD",
    "MOUSE", "KEYBOARD", "MONITOR", "LAPTOP", "NOTEBOOK",
    "(NO GPU)", "WITHOUT GPU", "NO CARD", "DUMMY", "PLACEHOLDER",
]

# Keywords that confirm a result IS a GPU (gpu_discord_bot.py:135)
GPU_KEYWORDS = [
    "GRAPHICS CARD", "GRAPHIC CARD", "VIDEO CARD", "GPU",
    "GDDR6", "GDDR6X", "GDDR7", "GEFORCE", "RADEON", "ARC",
    "GAMING", "OC", "WINDFORCE", "STRIX", "TUF", "AORUS",
    "VENTUS", "EAGLE", "MASTER", "SUPRIM", "NITRO", "PULSE",
    "DUAL", "TRIO", "HELLHOUND", "PHANTOM", "CHALLENGER",
]

PRICE_FLOOR = 200.0


def is_valid_gpu(title: str, price: float | None) -> bool:
    """v1 is_valid_gpu(). A missing price skips only the $200 floor (see eval/README.md)."""
    t = title.upper()
    if any(junk in t for junk in JUNK_KEYWORDS):
        return False
    if not any(kw in t for kw in GPU_KEYWORDS):
        return False
    if price is not None and price < PRICE_FLOOR:
        return False
    return True


def title_has_query(title: str, query: str) -> bool:
    """v1 model check: every query word is a substring of the uppercased title."""
    title_upper = title.upper()
    return all(w in title_upper for w in query.upper().strip().split())


def accepts(title: str, query: str, price: float | None) -> bool:
    """Would v1 have alerted on this listing for this query?"""
    return title_has_query(title, query) and is_valid_gpu(title, price)
