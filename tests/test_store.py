import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from gpu_alerts.retailers.base import Listing, ScrapeResult, Status
from gpu_alerts.store import AlertLimitError, Store, iso, to_cents, utcnow


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "test.sqlite3")
    yield s
    s.close()


def L(price, retailer="bestbuy", **kw):
    kw.setdefault("url", f"https://example.test/{retailer}/{price}")
    return Listing(retailer=retailer, title=f"RTX 5080 {retailer}", price=price, **kw)


def test_schema_pragmas(store):
    assert store._query("PRAGMA journal_mode")[0][0] == "wal"
    assert store._query("PRAGMA foreign_keys")[0][0] == 1


def test_to_cents_rounds():
    assert to_cents(1599.99) == 159999
    assert to_cents(0.1 + 0.2) == 30


def test_upsert_update_and_cap(store):
    a, created = store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10, guild_id=20)
    assert created and a.armed and a.target_cents == 150_000 and a.created_at.tzinfo is not None
    store.update_alert_state(a.id, armed=False, notified_price_cents=149_000, notified_at=utcnow(),
                             last_price_cents=149_000)
    b, created = store.upsert_alert(1, "rtx-5080", 140_000, include_open_box=True)
    assert not created and b.id == a.id
    assert b.target_cents == 140_000 and b.armed and b.notified_price_cents is None and b.include_open_box
    assert b.last_price_cents == 149_000

    for i, mid in enumerate(["rtx-5090", "rtx-5070", "rtx-5070-ti"]):
        store.upsert_alert(1, mid, 100_000 + i, max_per_user=4)
    with pytest.raises(AlertLimitError):
        store.upsert_alert(1, "rx-9070", 50_000, max_per_user=4)
    # Updating an existing alert at the cap still works.
    c, created = store.upsert_alert(1, "rtx-5090", 200_000, max_per_user=4)
    assert not created and c.target_cents == 200_000
    with pytest.raises(ValueError):
        store.upsert_alert(2, "rtx-5080", 0)


def test_list_remove_all(store):
    store.upsert_alert(1, "rtx-5080", 150_000)
    store.upsert_alert(1, "rtx-5090", 250_000)
    store.upsert_alert(2, "rtx-5080", 140_000)
    assert [a.model_id for a in store.list_alerts(1)] == ["rtx-5080", "rtx-5090"]
    assert len(store.all_alerts()) == 3
    assert store.remove_alert(1, "rtx-5080")
    assert not store.remove_alert(1, "rtx-5080")
    assert [a.user_id for a in store.all_alerts()] == [1, 2]


def test_state_write_never_resurrects_or_clobbers(store):
    a, _ = store.upsert_alert(1, "rtx-5080", 150_000)
    stale = store.get_alert(a.id)
    # The user changes the target while a background check is running.
    store.upsert_alert(1, "rtx-5080", 120_000)
    assert not store.update_alert_state(
        stale.id, armed=False, notified_price_cents=149_000, notified_at=utcnow(),
        last_price_cents=149_000, expected_target_cents=stale.target_cents,
    )
    assert store.get_alert(a.id).armed
    # The user removes the alert; a late state write must not bring it back.
    store.remove_alert(1, "rtx-5080")
    assert not store.update_alert_state(a.id, armed=True, notified_price_cents=None, notified_at=None,
                                        last_price_cents=1)
    assert store.all_alerts() == []


def _alert_writes(store, uid):
    for i in range(20):
        store.upsert_alert(uid, f"m{i % 5}", 100_000 + i)
        if i % 3 == 0:
            store.remove_alert(uid, f"m{(i + 1) % 5}")


def test_concurrent_writers_from_threads(store):
    start = threading.Barrier(8, timeout=30)

    def worker(uid):
        start.wait()   # all eight write at once
        _alert_writes(store, uid)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, uid) for uid in range(8)]
    for f in futures:
        f.result()   # a write that raised in its thread fails the test here
    # Each user's rows end as that user's writes leave them with nothing else running.
    with Store() as alone:
        _alert_writes(alone, 0)
        expected = [(a.model_id, a.target_cents, a.armed) for a in alone.list_alerts(0)]
    assert len(expected) == 5
    for uid in range(8):
        assert [(a.model_id, a.target_cents, a.armed) for a in store.list_alerts(uid)] == expected


def _scan(store, model_id, prices, at, **kw):
    offers = [L(p, retailer=f"r{i}", in_stock=True) for i, p in enumerate(prices)]
    return store.record_scan(model_id, offers, n_ok=kw.pop("n_ok", 1), n_retailers=6, at=at, **kw)


def test_history_and_stats(store):
    now = utcnow()
    _scan(store, "rtx-5080", [1500, 1600], now - timedelta(days=60))
    _scan(store, "rtx-5080", [1400, 1450], now - timedelta(days=20))
    _scan(store, "rtx-5080", [1450], now - timedelta(days=5))
    _scan(store, "rtx-5080", [], now - timedelta(days=3), n_ok=0)       # every retailer failed: ignored
    _scan(store, "rtx-5080", [1480, 1700], now - timedelta(hours=1))
    store.record_scan("rtx-5080", [L(1000, retailer="mkt", first_party=False),
                                   L(1100, condition="open_box")], n_ok=2, n_retailers=6, at=now)

    hist = store.best_history("rtx-5080")
    assert [p for _, p in hist] == [150_000, 140_000, 145_000, 148_000, None]
    s7 = store.price_stats("rtx-5080", 7)
    assert (s7.low_cents, s7.median_cents, s7.high_cents, s7.count) == (145_000, 146_500, 148_000, 2)
    assert s7.latest_cents is None  # the newest scan only had marketplace and open-box offers
    s90 = store.price_stats("rtx-5080", 90)
    assert (s90.low_cents, s90.high_cents, s90.count) == (140_000, 150_000, 4)
    wide = store.price_stats("rtx-5080", 7, include_open_box=True, include_marketplace=True)
    assert wide.latest_cents == 100_000 and wide.low_cents == 100_000
    empty = store.price_stats("rx-9070", 30)
    assert empty.count == 0 and empty.latest_cents is None and empty.low_cents is None


def test_latest_offers(store):
    now = utcnow()
    _scan(store, "rtx-5080", [1500], now - timedelta(hours=5))
    _scan(store, "rtx-5080", [1480, 1450], now - timedelta(minutes=10))
    at, offers = store.latest_offers("rtx-5080")
    assert [o.price_cents for o in offers] == [145_000, 148_000]
    assert offers[0].in_stock is True and offers[0].first_party is None
    assert store.latest_offers("rtx-5080", max_age=timedelta(minutes=5)) == (None, [])
    assert store.latest_offers("rx-9070") == (None, [])


def test_retailer_health(store):
    now = utcnow()

    def run(status, minutes_ago, http=None):
        store.record_runs([ScrapeResult("newegg", "q", status, http_status=http, detail="d")],
                          at=now - timedelta(minutes=minutes_ago))

    run(Status.OK, 50)
    run(Status.BLOCKED, 40, 403)
    run(Status.BLOCKED, 30, 403)
    run(Status.PARSE_ERROR, 20)
    store.record_runs([ScrapeResult("bestbuy", "q", Status.EMPTY)], at=now)
    store.record_runs([ScrapeResult("walmart", "q", Status.DISABLED)], at=now)
    health = {h.retailer: h for h in store.retailer_health()}
    assert set(health) == {"newegg", "bestbuy"}  # disabled runs are not recorded
    n = health["newegg"]
    assert n.last_status == "parse_error" and n.consecutive_failures == 3
    assert n.last_ok_at is not None and health["bestbuy"].consecutive_failures == 0
    run(Status.OK, 0)
    assert {h.retailer: h for h in store.retailer_health()}["newegg"].consecutive_failures == 0


def test_never_ok_retailer_counts_all_failures(store):
    for i in range(4):
        store.record_runs([ScrapeResult("amazon", "q", Status.BLOCKED, http_status=503)],
                          at=utcnow() - timedelta(minutes=i))
    (h,) = store.retailer_health()
    assert h.consecutive_failures == 4 and h.last_ok_at is None and h.last_http_status == 503


def test_retailer_health_is_not_cut_short_by_a_prune(store, monkeypatch):
    # Walmart's only run is older than the retention, so the daily prune removes it.
    store.record_runs([ScrapeResult("walmart", "q", Status.OK)], at=utcnow() - timedelta(days=200))
    pruner = threading.Thread(target=store.prune, args=(180,))
    real = store._query

    def query(sql, params=()):
        rows = real(sql, params)
        if "DISTINCT" in sql and pruner.ident is None:
            # The background loop prunes from another thread right after the retailers are listed.
            pruner.start()
            pruner.join(timeout=0.3)
        return rows

    monkeypatch.setattr(store, "_query", query)
    assert [h.retailer for h in store.retailer_health()] == ["walmart"]
    pruner.join()
    assert store.retailer_health() == []


def test_prune(store):
    now = utcnow()
    old = _scan(store, "rtx-5080", [1500, 1400], now - timedelta(days=200))
    _scan(store, "rtx-5080", [1450], now)
    store.record_runs([ScrapeResult("bestbuy", "q", Status.OK)], at=now - timedelta(days=200))
    store.record_runs([ScrapeResult("bestbuy", "q", Status.OK)], at=now)
    assert store.prune(180) == 4  # 2 snapshots, 1 scan, 1 run
    assert store._query("SELECT COUNT(*) FROM snapshots WHERE scan_id = ?", (old,))[0][0] == 0
    assert len(store.best_history("rtx-5080")) == 1


def test_import_v1(store, tmp_path):
    path = tmp_path / "gpu_alerts.json"
    path.write_text(json.dumps({
        "111111111111111111": {
            "RTX 5080": {"target": 1500, "channel_id": 222222222222222222, "last_price": 1699.0,
                         "last_check": "2026-06-07T22:07:33.599418", "created": "2026-04-29T17:20:23.229468"},
            "5070 TI": {"target": 900, "channel_id": 5},
            "Voodoo 5": {"target": 100},
            "RX 9070 XT": {"channel_id": 5},
        },
        "not-a-user": {},
    }), encoding="utf-8")
    report = store.import_v1(path)
    assert len(report.imported) == 2 and len(report.skipped) == 3
    alerts = {a.model_id: a for a in store.list_alerts(111111111111111111)}
    assert set(alerts) == {"rtx-5080", "rtx-5070-ti"}
    a = alerts["rtx-5080"]
    assert a.target_cents == 150_000 and a.channel_id == 222222222222222222 and a.armed
    assert a.last_price_cents == 169_900
    assert iso(a.created_at).startswith("2026-04-29T17:20:23")  # naive v1 times are read as UTC
    # Importing twice does not duplicate.
    store.import_v1(path)
    assert len(store.all_alerts()) == 2


def _v1_file(tmp_path, entries=None):
    path = tmp_path / "gpu_alerts.json"
    path.write_text(json.dumps({"111": entries or {
        "RTX 5080": {"target": 1500, "channel_id": 555, "last_price": 1699.0},
        "RTX 5090": {"target": 3000, "channel_id": 555},
    }}), encoding="utf-8")
    return path


def _changed_and_pinged(store):
    """User 111 re-sets the RTX 5080 alert in Discord, and it pings at $1,250."""
    a, _ = store.upsert_alert(111, "rtx-5080", 130_000, channel_id=777, guild_id=1,
                              include_open_box=True, include_marketplace=True)
    assert store.update_alert_state(a.id, armed=False, notified_price_cents=125_000, notified_at=utcnow(),
                                    last_price_cents=125_000, notified_retailer="bestbuy",
                                    holding_retailers={"bestbuy"}, expected_rev=a.rev)
    return store.get_alert(a.id)


def test_import_v1_again_leaves_existing_alerts_alone(store, tmp_path):
    path = _v1_file(tmp_path)
    assert len(store.import_v1(path).imported) == 2
    before = _changed_and_pinged(store)
    report = store.import_v1(path)
    assert report.imported == [] and report.updated == []
    assert sorted(report.skipped) == ["111 RTX 5080: already exists", "111 RTX 5090: already exists"]
    # The target, channel and flags the user set stay, and the alert is not re-armed to ping again.
    assert store.get_alert(before.id) == before


def test_import_v1_again_does_not_bring_back_a_removed_alert(store, tmp_path):
    path = _v1_file(tmp_path)
    store.import_v1(path)
    store.remove_alert(111, "rtx-5090")
    report = store.import_v1(path)
    assert "111 RTX 5090: removed since an earlier import" in report.skipped
    assert [a.model_id for a in store.list_alerts(111)] == ["rtx-5080"]


def test_import_v1_does_not_overwrite_an_alert_made_in_discord(store, tmp_path):
    before = _changed_and_pinged(store)
    report = store.import_v1(_v1_file(tmp_path))
    assert report.skipped == ["111 RTX 5080: already exists"] and len(report.imported) == 1
    assert store.get_alert(before.id) == before   # not even the stale v1 last price


def test_import_v1_force_applies_the_file_but_keeps_the_ping_state(store, tmp_path):
    path = _v1_file(tmp_path)
    store.import_v1(path)
    store.remove_alert(111, "rtx-5090")
    before = _changed_and_pinged(store)
    report = store.import_v1(path, force=True)
    assert report.updated == ["111 RTX 5080 -> rtx-5080 at $1,500.00"] and len(report.imported) == 1
    after = store.get_alert(before.id)
    assert (after.target_cents, after.channel_id) == (150_000, 555)
    # Whether it already pinged, and the settings v1 never had, stay as they were.
    assert (after.armed, after.notified_price_cents, after.notified_at, after.notified_retailer) == (
        before.armed, before.notified_price_cents, before.notified_at, before.notified_retailer)
    assert (after.holding_retailers, after.last_price_cents) == (before.holding_retailers, before.last_price_cents)
    assert (after.guild_id, after.include_open_box, after.include_marketplace) == (1, True, True)
    assert after.rev == before.rev + 1   # a check that read the old target does not write over it
    assert {a.model_id for a in store.list_alerts(111)} == {"rtx-5080", "rtx-5090"}


def test_import_v1_into_a_v2_0_database(tmp_path):
    import sqlite3

    path = tmp_path / "v2.0.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(V2_0_ALERTS)
    conn.execute("INSERT INTO alerts (user_id, model_id, target_cents, armed, notified_price_cents, created_at)"
                 " VALUES (111, 'rtx-5080', 130000, 0, 125000, '2026-10-01T00:00:00.000000+00:00')")
    conn.commit()
    conn.close()
    v1 = _v1_file(tmp_path)
    with Store(path) as s:
        report = s.import_v1(v1)
        assert report.skipped == ["111 RTX 5080: already exists"] and len(report.imported) == 1
        (a,) = [x for x in s.all_alerts() if x.model_id == "rtx-5080"]
        assert (a.target_cents, a.armed, a.notified_price_cents) == (130_000, False, 125_000)
        s.remove_alert(111, "rtx-5080")
    with Store(path) as s:   # the record of what was imported survives a restart
        assert s.import_v1(v1).skipped == ["111 RTX 5080: removed since an earlier import",
                                           "111 RTX 5090: already exists"]
        assert [a.model_id for a in s.all_alerts()] == ["rtx-5090"]


def test_memory_store():
    with Store() as s:
        s.upsert_alert(1, "rtx-5080", 100_000)
        assert len(s.all_alerts()) == 1


def test_skipped_placeholders_are_not_recorded_as_failures(store):
    from gpu_alerts import pricing

    # One real block, then placeholders for every later query in the same run.
    results = [ScrapeResult("amazon", "rtx 5080", Status.BLOCKED, http_status=503, detail="bot check")]
    results += [ScrapeResult("amazon", f"q{i}", Status.BLOCKED, detail=pricing.SKIPPED_DETAIL) for i in range(5)]
    results += [ScrapeResult("bestbuy", "rtx 5080", Status.OK)]
    assert store.record_runs(results) == 2
    h = {x.retailer: x for x in store.retailer_health()}["amazon"]
    assert h.consecutive_failures == 1
    assert h.last_detail == "bot check" and h.last_http_status == 503


def test_state_write_after_a_same_target_rearm_is_skipped(store):
    a, _ = store.upsert_alert(1, "rtx-5080", 150_000)
    stale = store.get_alert(a.id)
    # While a delivery is in flight the user re-sets the same target with open box on.
    store.upsert_alert(1, "rtx-5080", 150_000, include_open_box=True)
    assert not store.update_alert_state(
        stale.id, armed=False, notified_price_cents=140_000, notified_at=utcnow(),
        last_price_cents=140_000, expected_target_cents=stale.target_cents, expected_rev=stale.rev,
    )
    fresh = store.get_alert(a.id)
    assert fresh.armed and fresh.notified_price_cents is None and fresh.rev == stale.rev + 1
    # A write made with the current revision goes through and keeps the notified retailer.
    assert store.update_alert_state(
        fresh.id, armed=False, notified_price_cents=140_000, notified_at=utcnow(),
        last_price_cents=140_000, expected_rev=fresh.rev, notified_retailer="bestbuy",
    )
    assert store.get_alert(a.id).notified_retailer == "bestbuy"
    # Re-setting the alert forgets the notified retailer.
    assert store.upsert_alert(1, "rtx-5080", 150_000)[0].notified_retailer is None


def test_state_writes_skip_a_reused_id(store):
    a, _ = store.upsert_alert(1, "rtx-5080", 150_000)
    # The newest alert is removed and the next /alert, by anyone, gets the same id.
    store.remove_alert(1, "rtx-5080")
    b, _ = store.upsert_alert(2, "rtx-5090", 150_000)
    assert b.id == a.id and b.rev == a.rev
    assert not store.update_alert_state(
        a.id, armed=False, notified_price_cents=140_000, notified_at=utcnow(), last_price_cents=140_000,
        expected_target_cents=a.target_cents, expected_rev=a.rev,
        expected_user_id=a.user_id, expected_model_id=a.model_id,
    )
    store.touch_alert(a.id, 140_000, expected_user_id=a.user_id, expected_model_id=a.model_id)
    fresh = store.get_alert(b.id)
    assert fresh.armed and fresh.notified_price_cents is None and fresh.last_price_cents is None
    assert store.update_alert_state(
        b.id, armed=False, notified_price_cents=290_000, notified_at=utcnow(), last_price_cents=290_000,
        expected_user_id=b.user_id, expected_model_id=b.model_id,
    )


def test_undo_notification_only_undoes_that_notification(store):
    a, _ = store.upsert_alert(1, "rtx-5080", 150_000)

    def notify(price):
        assert store.update_alert_state(a.id, armed=False, notified_price_cents=price, notified_at=utcnow(),
                                        last_price_cents=price, notified_retailer="bestbuy",
                                        holding_retailers={"bestbuy"})

    notify(140_000)
    assert store.undo_notification(a, 140_000)
    b = store.get_alert(a.id)
    assert b.armed and (b.notified_price_cents, b.notified_at, b.notified_retailer) == (None, None, None)
    assert b.last_price_cents == 140_000 and b.holding_retailers == {"bestbuy"}   # what was seen stays
    notify(140_000)
    notify(135_000)   # a later check notified again: that one reached the user
    assert not store.undo_notification(a, 140_000)
    store.upsert_alert(1, "rtx-5080", 150_000)   # the user re-set the alert
    assert not store.undo_notification(a, 135_000)


def test_state_write_from_an_older_scan_is_skipped(store):
    a, _ = store.upsert_alert(1, "rtx-5080", 150_000)
    now = utcnow()

    def write(price, observed_at):
        return store.update_alert_state(a.id, armed=True, notified_price_cents=None, notified_at=None,
                                        last_price_cents=price, observed_at=observed_at)

    assert write(160_000, now)
    assert not write(140_000, now - timedelta(minutes=1))   # read before the scan already saved
    assert not write(140_000, now)                          # the same scan again
    a = store.get_alert(a.id)
    assert a.last_price_cents == 160_000 and a.observed_at == now
    assert write(150_000, now + timedelta(minutes=1))


def test_old_database_gets_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE alerts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, channel_id INTEGER,"
        " guild_id INTEGER, model_id TEXT NOT NULL, target_cents INTEGER NOT NULL,"
        " include_open_box INTEGER NOT NULL DEFAULT 0, include_marketplace INTEGER NOT NULL DEFAULT 0,"
        " armed INTEGER NOT NULL DEFAULT 1, notified_price_cents INTEGER, notified_at TEXT,"
        " last_price_cents INTEGER, last_checked_at TEXT, created_at TEXT NOT NULL, UNIQUE (user_id, model_id))"
    )
    conn.execute("INSERT INTO alerts (user_id, model_id, target_cents, armed, notified_price_cents, created_at)"
                 " VALUES (1, 'rtx-5080', 150000, 0, 140000, '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    with Store(path) as s:
        (a,) = s.all_alerts()
        assert (a.above_streak, a.notified_retailer, a.rev) == (0, None, 0)
        assert a.notified_price_cents == 140_000 and not a.armed
        assert a.holding_retailers == frozenset() and a.held_streak == 0


# The alerts table as v2.0.0 created it.
V2_0_ALERTS = """
CREATE TABLE alerts (
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
)
"""


def test_v2_0_database_is_migrated_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "v2.0.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(V2_0_ALERTS)
    conn.execute("INSERT INTO alerts (user_id, model_id, target_cents, armed, notified_price_cents,"
                 " notified_retailer, above_streak, rev, created_at)"
                 " VALUES (1, 'rtx-5080', 150000, 0, 140000, 'bestbuy', 1, 3, '2026-10-01T00:00:00.000000+00:00')")
    conn.execute("INSERT INTO alerts (user_id, model_id, target_cents, created_at)"
                 " VALUES (1, 'rtx-5090', 250000, '2026-10-02T00:00:00.000000+00:00')")
    conn.commit()
    conn.close()
    with Store(path) as s:
        notified, armed = s.all_alerts()
        # The retailer that had the notified deal keeps holding the alert after the upgrade.
        assert notified.holding_retailers == {"bestbuy"} and armed.holding_retailers == frozenset()
        assert (notified.notified_price_cents, notified.above_streak, notified.rev) == (140_000, 1, 3)
        assert notified.observed_at is None and armed.observed_at is None
        assert notified.held_streak == 0 and armed.held_streak == 0
        assert s.update_alert_state(
            notified.id, armed=False, notified_price_cents=140_000, notified_at=utcnow(),
            last_price_cents=145_000, holding_retailers={"newegg", "amazon"}, held_streak=5, expected_rev=3,
        )
        saved = s.get_alert(notified.id)
        assert saved.holding_retailers == {"amazon", "newegg"} and saved.held_streak == 5
        # Re-setting the alert re-arms it and empties the set.
        reset = s.upsert_alert(1, "rtx-5080", 150_000)[0]
        assert reset.holding_retailers == frozenset() and reset.held_streak == 0
    with Store(path) as s:   # opening again changes nothing
        assert [a.holding_retailers for a in s.all_alerts()] == [frozenset(), frozenset()]
