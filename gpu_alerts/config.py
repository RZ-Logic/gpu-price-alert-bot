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
    rather than breaking every command at import.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        log.warning("%s is not UTF-8 text; ignoring it. Save it as UTF-8.", path)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int_or_none(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value.isdigit() else None


def _int_setting(name: str, default: int) -> int:
    """An integer setting; an empty or malformed value falls back to the default."""
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        log.warning("%s=%r is not a whole number; using %d", name, value, default)
        return default


DATA_DIR = Path(os.getenv("GPU_ALERTS_DATA_DIR", "data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
DB_PATH = DATA_DIR / "gpu_alerts.sqlite3"

CHECK_INTERVAL_MINUTES = _int_setting("CHECK_INTERVAL_MINUTES", 60)

# Comma-separated retailer ids to skip, e.g. "amazon,walmart".
DISABLED_RETAILERS = {
    s.strip().lower() for s in os.getenv("DISABLED_RETAILERS", "").split(",") if s.strip()
}

# Optional: sync slash commands to one server instantly instead of globally.
DISCORD_GUILD_ID = _int_or_none("DISCORD_GUILD_ID")

# Best Buy's availability API needs a valid Canadian postal code. Any public
# one works; this default is a downtown Toronto code, not a home address.
BESTBUY_POSTAL_CODE = os.getenv("BESTBUY_POSTAL_CODE", "M5G2C3")


def discord_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Put it in the environment or a .env file.")
    return token
