"""SQLite storage for alerts, price history and scraper health.

One connection guarded by a lock, so every method is safe to call from
asyncio.to_thread. Every write touches only the rows it means to change, so
a background check can never resurrect an alert a user just removed. Prices
are integer cents; timestamps are UTC ISO 8601 strings with an offset.
"""

import json
import sqlite3
import statistics
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import catalog
from .pricing import SKIPPED_PREFIX
from .retailers.base import Listing, ScrapeResult, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    channel_id INTEGER,
    guild_id INTEGER,
    model_id TEXT NOT NULL,
    target_cents INTEGER NOT NULL CHECK (target_cents > 0),
    include_open_box INTEGER NOT NULL DEFAULT 0,
    include_marketplace INTEGER NOT NULL DEFAULT 0,
    armed INTEGER NOT NULL DEFAULT 1,
    notified_price_cents INTEGER,
    notified_at TEXT,
    above_streak INTEGER NOT NULL DEFAULT 0,
    notified_retailer TEXT,
    rev INTEGER NOT NULL DEFAULT 0,
    last_price_cents INTEGER,
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, model_id)
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    model_id TEXT NOT NULL,
    n_ok INTEGER NOT NULL,
    n_retailers INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scans_model_at ON scans (model_id, scanned_at);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scans (id) ON DELETE CASCADE,
    scanned_at TEXT NOT NULL,
    model_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    sku TEXT,
    title TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    in_stock INTEGER,
    first_party INTEGER,
    condition TEXT NOT NULL,
    url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_scan ON snapshots (scan_id);
CREATE INDEX IF NOT EXISTS snapshots_model_at ON snapshots (model_id, scanned_at);
CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    retailer TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    n_raw INTEGER NOT NULL,
    n_listings INTEGER NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS scrape_runs_retailer_at ON scrape_runs (retailer, at);
"""

OK_STATUSES = (Status.OK.value, Status.EMPTY.value)

# Columns added after the first release, with their definitions, for ALTER TABLE.
_ALERT_MIGRATIONS = {
    "above_streak": "INTEGER NOT NULL DEFAULT 0",
    "notified_retailer": "TEXT",
    "rev": "INTEGER NOT NULL DEFAULT 0",     # bumped by every user write (upsert_alert)
}


class AlertLimitError(Exception):
    """The user already has the maximum number of alerts."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_cents(price: float) -> int:
    return int(round(price * 100))


def _bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _int_or_none(value: bool | None) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True)
class Alert:
    id: int
    user_id: int
    channel_id: int | None
    guild_id: int | None
    model_id: str
    target_cents: int
    include_open_box: bool
    include_marketplace: bool
    armed: bool
    notified_price_cents: int | None
    notified_at: datetime | None
    last_price_cents: int | None
    last_checked_at: datetime | None
    created_at: datetime
    above_streak: int = 0
    notified_retailer: str | None = None
    rev: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Alert":
        keys = row.keys()
        return cls(
            id=row["id"], user_id=row["user_id"], channel_id=row["channel_id"],
            guild_id=row["guild_id"], model_id=row["model_id"], target_cents=row["target_cents"],
            include_open_box=bool(row["include_open_box"]),
            include_marketplace=bool(row["include_marketplace"]),
            armed=bool(row["armed"]), notified_price_cents=row["notified_price_cents"],
            notified_at=parse_iso(row["notified_at"]), last_price_cents=row["last_price_cents"],
            last_checked_at=parse_iso(row["last_checked_at"]),
            created_at=parse_iso(row["created_at"]),
            above_streak=row["above_streak"] if "above_streak" in keys else 0,
            notified_retailer=row["notified_retailer"] if "notified_retailer" in keys else None,
            rev=row["rev"] if "rev" in keys else 0,
        )


@dataclass(frozen=True)
class Offer:
    """A stored matched listing from one scan."""

    scanned_at: datetime
    model_id: str
    retailer: str
    sku: str | None
    title: str
    price_cents: int
    in_stock: bool | None
    first_party: bool | None
    condition: str
    url: str


@dataclass(frozen=True)
class PriceStats:
    model_id: str
    days: int
    latest_cents: int | None      # best price in the latest scan that reached a retailer
    latest_at: datetime | None
    low_cents: int | None
    median_cents: int | None
    high_cents: int | None
    count: int                    # scans in the window with at least one eligible offer


@dataclass(frozen=True)
class RetailerHealth:
    retailer: str
    last_status: str
    last_at: datetime
    last_detail: str
    last_http_status: int | None
    consecutive_failures: int     # failed scrapes since the last ok or empty one
    last_ok_at: datetime | None


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _policy_sql(include_open_box: bool, include_marketplace: bool) -> str:
    conditions = "('new', 'open_box', 'refurbished')" if include_open_box else "('new')"
    sql = f"o.condition IN {conditions}"
    if not include_marketplace:
        sql += " AND (o.first_party IS NULL OR o.first_party = 1)"
    return sql


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)")}
            for name, definition in _ALERT_MIGRATIONS.items():
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # Alerts

    def upsert_alert(
        self,
        user_id: int,
        model_id: str,
        target_cents: int,
        *,
        channel_id: int | None = None,
        guild_id: int | None = None,
        include_open_box: bool = False,
        include_marketplace: bool = False,
        max_per_user: int | None = None,
        created_at: datetime | None = None,
    ) -> tuple[Alert, bool]:
        """Create or update one alert. Returns (alert, created).

        Updating an existing alert always works, even at the cap, and re-arms it
        so the new target is judged fresh.
        """
        if target_cents <= 0:
            raise ValueError("target must be positive")
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id FROM alerts WHERE user_id = ? AND model_id = ?", (user_id, model_id)
            ).fetchone()
            if existing is None:
                if max_per_user is not None:
                    (n,) = self._conn.execute(
                        "SELECT COUNT(*) FROM alerts WHERE user_id = ?", (user_id,)
                    ).fetchone()
                    if n >= max_per_user:
                        raise AlertLimitError(f"limit of {max_per_user} alerts reached")
                self._conn.execute(
                    "INSERT INTO alerts (user_id, channel_id, guild_id, model_id, target_cents,"
                    " include_open_box, include_marketplace, armed, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (user_id, channel_id, guild_id, model_id, target_cents,
                     int(include_open_box), int(include_marketplace), iso(created_at or utcnow())),
                )
            else:
                self._conn.execute(
                    "UPDATE alerts SET channel_id = ?, guild_id = ?, target_cents = ?,"
                    " include_open_box = ?, include_marketplace = ?, armed = 1,"
                    " notified_price_cents = NULL, notified_at = NULL, above_streak = 0,"
                    " notified_retailer = NULL, rev = rev + 1 WHERE id = ?",
                    (channel_id, guild_id, target_cents, int(include_open_box),
                     int(include_marketplace), existing["id"]),
                )
            row = self._conn.execute(
                "SELECT * FROM alerts WHERE user_id = ? AND model_id = ?", (user_id, model_id)
            ).fetchone()
        return Alert.from_row(row), existing is None

    def remove_alert(self, user_id: int, model_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM alerts WHERE user_id = ? AND model_id = ?", (user_id, model_id)
            )
        return cur.rowcount > 0

    def get_alert(self, alert_id: int) -> Alert | None:
        rows = self._query("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        return Alert.from_row(rows[0]) if rows else None

    def list_alerts(self, user_id: int) -> list[Alert]:
        rows = self._query("SELECT * FROM alerts WHERE user_id = ? ORDER BY created_at, id", (user_id,))
        return [Alert.from_row(r) for r in rows]

    def all_alerts(self) -> list[Alert]:
        return [Alert.from_row(r) for r in self._query("SELECT * FROM alerts ORDER BY id")]

    def update_alert_state(
        self,
        alert_id: int,
        *,
        armed: bool,
        notified_price_cents: int | None,
        notified_at: datetime | None,
        last_price_cents: int | None,
        last_checked_at: datetime | None = None,
        expected_target_cents: int | None = None,
        above_streak: int = 0,
        notified_retailer: str | None = None,
        expected_rev: int | None = None,
    ) -> bool:
        """Write the state-machine columns of one alert.

        With expected_target_cents, the write is skipped if the user changed the
        target since the alert was read. With expected_rev, it is skipped if the
        user wrote the alert at all since it was read (upsert_alert bumps rev),
        so a re-arm with the same target is not undone. Returns False when
        nothing was written.
        """
        sql = (
            "UPDATE alerts SET armed = ?, notified_price_cents = ?, notified_at = ?,"
            " last_price_cents = ?, last_checked_at = ?, above_streak = ?, notified_retailer = ?"
            " WHERE id = ?"
        )
        params: list = [
            int(armed), notified_price_cents, iso(notified_at) if notified_at else None,
            last_price_cents, iso(last_checked_at or utcnow()), above_streak, notified_retailer,
            alert_id,
        ]
        if expected_target_cents is not None:
            sql += " AND target_cents = ?"
            params.append(expected_target_cents)
        if expected_rev is not None:
            sql += " AND rev = ?"
            params.append(expected_rev)
        with self._lock, self._conn:
            cur = self._conn.execute(sql, params)
        return cur.rowcount > 0

    def touch_alert(self, alert_id: int, last_price_cents: int | None, at: datetime | None = None) -> None:
        """Record the latest price without changing the notification state."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE alerts SET last_price_cents = ?, last_checked_at = ? WHERE id = ?",
                (last_price_cents, iso(at or utcnow()), alert_id),
            )

    # Scans and history

    def record_runs(self, results: Iterable[ScrapeResult], at: datetime | None = None) -> int:
        """One scrape_runs row per retailer result.

        Disabled retailers are not recorded, and neither are the placeholders
        pricing.scan_models makes for a retailer it skipped after a block: no
        request was sent, so they would inflate the failure streak in
        retailer_health and hide the real block reason.
        """
        stamp = iso(at or utcnow())
        rows = [
            (stamp, r.retailer, r.query, Status(r.status).value, r.http_status, r.n_raw,
             len(r.listings), r.elapsed_ms, (r.detail or "")[:300])
            for r in results
            if Status(r.status) is not Status.DISABLED and not (r.detail or "").startswith(SKIPPED_PREFIX)
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO scrape_runs (at, retailer, query, status, http_status, n_raw,"
                " n_listings, elapsed_ms, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def record_scan(
        self,
        model_id: str,
        offers: Iterable[Listing],
        *,
        n_ok: int,
        n_retailers: int,
        at: datetime | None = None,
    ) -> int:
        """Store the matched offers of one scan of one model. Returns the scan id."""
        stamp = iso(at or utcnow())
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO scans (scanned_at, model_id, n_ok, n_retailers) VALUES (?, ?, ?, ?)",
                (stamp, model_id, n_ok, n_retailers),
            )
            scan_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO snapshots (scan_id, scanned_at, model_id, retailer, sku, title,"
                " price_cents, in_stock, first_party, condition, url)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (scan_id, stamp, model_id, li.retailer, li.sku, li.title[:300],
                     to_cents(li.price), _int_or_none(li.in_stock),
                     _int_or_none(li.first_party), li.condition, li.url)
                    for li in offers
                ],
            )
        return scan_id

    def best_history(
        self,
        model_id: str,
        days: int | None = None,
        *,
        include_open_box: bool = False,
        include_marketplace: bool = False,
    ) -> list[tuple[datetime, int | None]]:
        """(scanned_at, best price) per scan that reached at least one retailer, oldest first."""
        sql = (
            "SELECT s.scanned_at, MIN(o.price_cents) AS best FROM scans s"
            f" LEFT JOIN snapshots o ON o.scan_id = s.id AND {_policy_sql(include_open_box, include_marketplace)}"
            " WHERE s.model_id = ? AND s.n_ok > 0"
        )
        params: list = [model_id]
        if days is not None:
            sql += " AND s.scanned_at >= ?"
            params.append(iso(utcnow() - timedelta(days=days)))
        sql += " GROUP BY s.id ORDER BY s.scanned_at, s.id"
        return [(parse_iso(r["scanned_at"]), r["best"]) for r in self._query(sql, params)]

    def price_stats(
        self,
        model_id: str,
        days: int = 30,
        *,
        include_open_box: bool = False,
        include_marketplace: bool = False,
    ) -> PriceStats:
        history = self.best_history(
            model_id, days, include_open_box=include_open_box, include_marketplace=include_marketplace
        )
        latest = self.best_history(
            model_id, None, include_open_box=include_open_box, include_marketplace=include_marketplace
        )[-1:]
        prices = [p for _, p in history if p is not None]
        latest_at, latest_cents = latest[0] if latest else (None, None)
        return PriceStats(
            model_id=model_id, days=days, latest_cents=latest_cents, latest_at=latest_at,
            low_cents=min(prices) if prices else None,
            median_cents=int(round(statistics.median(prices))) if prices else None,
            high_cents=max(prices) if prices else None,
            count=len(prices),
        )

    def latest_offers(
        self,
        model_id: str,
        *,
        max_age: timedelta | None = None,
        include_open_box: bool = False,
        include_marketplace: bool = False,
    ) -> tuple[datetime | None, list[Offer]]:
        """Offers from the latest scan that reached a retailer, cheapest first.

        Returns (None, []) when there is no such scan or it is older than max_age.
        """
        rows = self._query(
            "SELECT id, scanned_at FROM scans WHERE model_id = ? AND n_ok > 0"
            " ORDER BY scanned_at DESC, id DESC LIMIT 1",
            (model_id,),
        )
        if not rows:
            return None, []
        at = parse_iso(rows[0]["scanned_at"])
        if max_age is not None and utcnow() - at > max_age:
            return None, []
        offer_rows = self._query(
            f"SELECT * FROM snapshots o WHERE o.scan_id = ? AND {_policy_sql(include_open_box, include_marketplace)}"
            " ORDER BY o.price_cents, o.retailer",
            (rows[0]["id"],),
        )
        return at, [
            Offer(
                scanned_at=at, model_id=r["model_id"], retailer=r["retailer"], sku=r["sku"],
                title=r["title"], price_cents=r["price_cents"], in_stock=_bool(r["in_stock"]),
                first_party=_bool(r["first_party"]), condition=r["condition"], url=r["url"],
            )
            for r in offer_rows
        ]

    # Health and housekeeping

    def retailer_health(self) -> list[RetailerHealth]:
        out: list[RetailerHealth] = []
        retailers = [r["retailer"] for r in self._query(
            "SELECT DISTINCT retailer FROM scrape_runs ORDER BY retailer")]
        ok_list = ", ".join(f"'{s}'" for s in OK_STATUSES)
        for rid in retailers:
            last = self._query(
                "SELECT * FROM scrape_runs WHERE retailer = ? ORDER BY at DESC, id DESC LIMIT 1", (rid,)
            )[0]
            ok_row = self._query(
                f"SELECT MAX(at) AS at FROM scrape_runs WHERE retailer = ? AND status IN ({ok_list})", (rid,)
            )[0]
            last_ok = ok_row["at"]
            (failures,) = self._query(
                f"SELECT COUNT(*) FROM scrape_runs WHERE retailer = ? AND status NOT IN ({ok_list})"
                " AND at > ?",
                (rid, last_ok or ""),
            )[0]
            out.append(RetailerHealth(
                retailer=rid, last_status=last["status"], last_at=parse_iso(last["at"]),
                last_detail=last["detail"], last_http_status=last["http_status"],
                consecutive_failures=failures, last_ok_at=parse_iso(last_ok),
            ))
        return out

    def prune(self, days: int = 180) -> int:
        """Delete scans, snapshots and scrape runs older than `days`. Returns rows removed."""
        cutoff = iso(utcnow() - timedelta(days=days))
        with self._lock, self._conn:
            n = self._conn.execute(
                "DELETE FROM snapshots WHERE scan_id IN (SELECT id FROM scans WHERE scanned_at < ?)",
                (cutoff,),
            ).rowcount
            n += self._conn.execute("DELETE FROM scans WHERE scanned_at < ?", (cutoff,)).rowcount
            n += self._conn.execute("DELETE FROM scrape_runs WHERE at < ?", (cutoff,)).rowcount
        return n

    def import_v1(self, path: str | Path) -> ImportReport:
        """Import alerts from the v1 gpu_alerts.json ({user_id: {"RTX 5080": {...}}})."""
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        report = ImportReport()
        if not isinstance(doc, dict):
            raise ValueError("expected a JSON object of user ids")
        for uid, entries in doc.items():
            if not isinstance(entries, dict) or not str(uid).isdigit():
                report.skipped.append(f"{uid}: not a user entry")
                continue
            for name, data in entries.items():
                label = f"{uid} {name}"
                model = catalog.resolve(str(name))
                if model is None:
                    report.skipped.append(f"{label}: unknown GPU name")
                    continue
                try:
                    target = to_cents(float(data["target"]))
                except (KeyError, TypeError, ValueError):
                    report.skipped.append(f"{label}: missing or bad target")
                    continue
                if target <= 0:
                    report.skipped.append(f"{label}: target not positive")
                    continue
                channel = data.get("channel_id")
                try:
                    created = parse_iso(data.get("created")) or utcnow()
                except ValueError:
                    created = utcnow()
                alert, _ = self.upsert_alert(
                    int(uid), model.id, target,
                    channel_id=int(channel) if isinstance(channel, int) or str(channel).isdigit() else None,
                    created_at=created,
                )
                last_price = data.get("last_price")
                if isinstance(last_price, (int, float)) and last_price > 0:
                    try:
                        checked = parse_iso(data.get("last_check"))
                    except ValueError:
                        checked = None
                    self.touch_alert(alert.id, to_cents(last_price), checked)
                report.imported.append(f"{label} -> {model.id} at ${target / 100:,.2f}")
        return report
