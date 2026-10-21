"""Runtime configuration from environment variables (and an optional .env file).

Nothing here reads the Discord token at import time, so the scrapers, the CLI
and the tests work without one.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal KEY=VALUE loader. Real environment variables win.

    utf-8-sig drops a byte order mark (Notepad and PowerShell write one), which
    would otherwise become part of the first key. A file that is not UTF-8 at all
    (PowerShell 5.1 `echo ... > .env` writes UTF-16) is skipped with a warning
    rather than breaking every command at import. A shell-style `export ` before
    the key is dropped. A key set twice keeps its first value, with a warning: a
    line appended below the one .env.example already has would otherwise be
    ignored without a word.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        log.warning("%s is not UTF-8 text; ignoring it. Save it as UTF-8.", path)
        return
    first_line: dict[str, int] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        words = key.split()
        key = words[1] if len(words) == 2 and words[0] == "export" else key.strip()
        if key in first_line:
            log.warning("%s line %d sets %s again; the value on line %d is used", path.name, n, key, first_line[key])
            continue
        first_line[key] = n
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load_dotenv()


def _int_or_none(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value.isdigit() else None


def _id_set(name: str) -> frozenset[int]:
    """Comma-separated Discord ids; an entry that is not a number is skipped with a warning."""
    ids = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
        elif part:
            log.warning("%s: %r is not a server id; ignoring it", name, part)
    return frozenset(ids)


def _int_setting(name: str, default: int, minimum: int | None = None) -> int:
    """An integer setting; an empty, malformed or too small value falls back to the default."""
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        log.warning("%s=%r is not a whole number; using %d", name, value, default)
        return default
    if minimum is not None and number < minimum:
        log.warning("%s=%r is below %d; using %d", name, value, minimum, default)
        return default
    return number


DATA_DIR = Path(os.getenv("GPU_ALERTS_DATA_DIR", "").strip() or "data")
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
DB_PATH = DATA_DIR / "gpu_alerts.sqlite3"

# 0 or less is a mistake (perhaps meant as "off"), not a request to scan back to back.
CHECK_INTERVAL_MINUTES = _int_setting("CHECK_INTERVAL_MINUTES", 60, minimum=1)

# Comma-separated retailer ids to skip, e.g. "amazon,walmart".
DISABLED_RETAILERS = {
    s.strip().lower() for s in os.getenv("DISABLED_RETAILERS", "").split(",") if s.strip()
}

# Optional: sync slash commands to one server instantly instead of globally.
DISCORD_GUILD_ID = _int_or_none("DISCORD_GUILD_ID")

# Optional, for global commands: more servers whose members may use them, comma-
# separated ids. With neither set, they answer only in servers the application
# owner is in (bot.GPUAlertBot.check_access).
ALLOWED_GUILD_IDS = _id_set("ALLOWED_GUILD_IDS")

# Best Buy's availability API needs a valid Canadian postal code. Any public
# one works; this default is a downtown Toronto code, not a home address.
BESTBUY_POSTAL_CODE = os.getenv("BESTBUY_POSTAL_CODE", "").strip() or "M5G2C3"


def warn_unknown_retailers() -> None:
    """Warn about DISABLED_RETAILERS entries that are not retailer ids, which turn
    nothing off. Called at startup: the retailer registry imports this module."""
    from .retailers import RETAILERS

    unknown = sorted(DISABLED_RETAILERS - set(RETAILERS))
    if unknown:
        log.warning(
            "DISABLED_RETAILERS: nothing is turned off for %s (not a retailer id). The ids are: %s",
            ", ".join(map(repr, unknown)), ", ".join(RETAILERS),
        )


def discord_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Put it in the environment or a .env file.")
    return token
