import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from gpu_alerts import bot as botmod
from gpu_alerts import catalog
from gpu_alerts.bot import GENERIC_ERROR, GPUAlertBot, alert_embed, lookup_embed
from gpu_alerts.pricing import ModelScan
from gpu_alerts.retailers.base import Listing, ScrapeResult, Status
from gpu_alerts.store import Store

M5080 = catalog.by_id("rtx-5080")


@dataclass
class FakeMatch:
    ok: bool


def matcher(title, model):
    return FakeMatch(model.key in title.upper())


def L(price, retailer="bestbuy", **kw):
    kw.setdefault("url", f"https://example.test/{retailer}/{price}")
    kw.setdefault("in_stock", True)
    return Listing(retailer=retailer, title=f"MSI RTX 5080 16GB {retailer}", price=price, **kw)


class FakeScanner:
    """Returns the queued price per call; None means every retailer is blocked."""

    def __init__(self, prices):
        self.prices = list(prices)
        self.calls = 0

    def __call__(self, models, *args, **kwargs):
        price = self.prices[min(self.calls, len(self.prices) - 1)]
        self.calls += 1
        if price is None:
            results = [ScrapeResult("bestbuy", "q", Status.BLOCKED, http_status=403),
                       ScrapeResult("newegg", "q", Status.BLOCKED, http_status=403)]
        else:
            results = [ScrapeResult("bestbuy", "q", Status.OK, [L(price)], n_raw=1),
                       ScrapeResult("newegg", "q", Status.OK, [L(price + 50, retailer="newegg")], n_raw=1)]
        at = datetime.now(timezone.utc)
        return {m.id: ModelScan(m, results, at) for m in models}


@pytest.fixture
def client():
    c = GPUAlertBot(store=Store(), scan_models=FakeScanner([1400.0]), matcher=matcher)
    yield c
    c.store.close()


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def fake_interaction(user_id=1, channel_id=10, guild_id=20):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), channel_id=channel_id, guild_id=guild_id,
        response=FakeResponse(), followup=FakeFollowup(), command=SimpleNamespace(name="x", qualified_name="x"),
    )


def run(coro):
    return asyncio.run(coro)


# Command tree shape


def test_intents_and_commands(client):
    assert client.intents.message_content is False
    names = {c.name for c in client.tree.get_commands()}
    assert names == {"alert", "remove", "list", "check", "lookup", "prices", "trend", "health"}


def test_alert_parameters(client):
    cmd = client.tree.get_command("alert")
    params = {p.name: p for p in cmd.parameters}
    assert list(params) == ["gpu", "target", "open_box", "marketplace"]
    assert params["gpu"].required and params["gpu"].autocomplete
    assert params["gpu"].type is discord.AppCommandOptionType.string
    t = params["target"]
    assert t.type is discord.AppCommandOptionType.integer and (t.min_value, t.max_value) == (100, 20000)
    assert t.required
    for flag in ("open_box", "marketplace"):
        assert params[flag].type is discord.AppCommandOptionType.boolean
        assert not params[flag].required and params[flag].default is False


def test_gpu_commands_autocomplete_and_cooldowns(client):
    for name in ("remove", "lookup", "trend"):
        (p,) = client.tree.get_command(name).parameters
        assert p.name == "gpu" and p.autocomplete and p.required
    for name in ("list", "check", "prices", "health"):
        assert client.tree.get_command(name).parameters == []
    assert client.tree.get_command("lookup").checks  # per-user cooldown
    assert client.tree.get_command("check").checks


def test_gpu_autocomplete_values_are_ids(client):
    cb = client.tree.get_command("alert")._params["gpu"].autocomplete
    choices = run(cb(fake_interaction(), "5070 ti"))
    assert choices and all(isinstance(c, app_commands.Choice) for c in choices)
    assert choices[0].value == "rtx-5070-ti"
    assert all(catalog.by_id(c.value) for c in choices)
    assert len(run(cb(fake_interaction(), ""))) <= 25


# Command handlers called directly with fakes


def test_alert_command_creates_updates_and_caps(client, monkeypatch):
    cmd = client.tree.get_command("alert")
    i = fake_interaction()
    run(cmd.callback(i, "rtx-5080", 1500))
    content, kwargs = i.response.sent[0]
    assert kwargs["ephemeral"] and "Alert set" in content and "$1,500.00" in content
    (a,) = client.store.list_alerts(1)
    assert (a.model_id, a.target_cents, a.channel_id, a.guild_id) == ("rtx-5080", 150_000, 10, 20)

    i = fake_interaction()
    run(cmd.callback(i, "RTX 5080", 1400, True))
    assert "Alert updated" in i.response.sent[0][0]
    assert client.store.list_alerts(1)[0].include_open_box

    i = fake_interaction()
    run(cmd.callback(i, "not a gpu", 1400))
    assert "Unknown GPU" in i.response.sent[0][0]

    monkeypatch.setattr(botmod, "MAX_ALERTS_PER_USER", 1)
    i = fake_interaction()
    run(cmd.callback(i, "rtx-5090", 2500))
    assert "already have 1 alerts" in i.response.sent[0][0]
    i = fake_interaction()
    run(cmd.callback(i, "rtx-5080", 1300))  # updating at the cap still works
    assert "Alert updated" in i.response.sent[0][0]


def test_remove_list_and_own_autocomplete(client):
    client.store.upsert_alert(1, "rtx-5080", 150_000)
    client.store.upsert_alert(1, "rtx-5090", 250_000)
    client.store.upsert_alert(2, "rx-9070", 70_000)
    cb = client.tree.get_command("remove")._params["gpu"].autocomplete
    choices = run(cb(fake_interaction(), "5080"))
    assert [c.value for c in choices] == ["rtx-5080"]
    assert {c.value for c in run(cb(fake_interaction(), ""))} == {"rtx-5080", "rtx-5090"}

    i = fake_interaction()
    run(client.tree.get_command("list").callback(i))
    embed = i.response.sent[0][1]["embed"]
    assert {f.name for f in embed.fields} == {"RTX 5080", "RTX 5090"}
    assert embed.timestamp.tzinfo is not None

    i = fake_interaction()
    run(client.tree.get_command("remove").callback(i, "rtx-5080"))
    assert "Removed" in i.response.sent[0][0] and i.response.sent[0][1]["ephemeral"]
    assert [a.model_id for a in client.store.list_alerts(1)] == ["rtx-5090"]


def test_lookup_replies_once_with_one_embed(client):
    i = fake_interaction()
    run(client.tree.get_command("lookup").callback(i, "rtx-5080"))
    assert i.response.done and len(i.followup.sent) == 1
    embed = i.followup.sent[0][1]["embed"]
    assert "$1,400.00" in embed.description
    assert client.store.price_stats("rtx-5080", 1).latest_cents == 140_000  # lookups are recorded


def test_check_reports_and_disarms(client):
    client.store.upsert_alert(1, "rtx-5080", 150_000)
    i = fake_interaction()
    run(client.tree.get_command("check").callback(i))
    content, kwargs = i.followup.sent[0]
    assert kwargs["ephemeral"] and "under your target" in kwargs["embed"].fields[0].value
    a = client.store.list_alerts(1)[0]
    assert not a.armed and a.notified_price_cents == 140_000 and a.last_price_cents == 140_000


def test_prices_uses_cache_then_cooldown(client):
    scanner = client.scan_models
    i = fake_interaction()
    run(client.tree.get_command("prices").callback(i))
    first_calls = scanner.calls
    assert first_calls == 1 and len(i.followup.sent) == 1
    embed = i.followup.sent[0][1]["embed"]
    assert len(embed.fields) == len([m for m in catalog.load() if m.popular and not m.legacy])
    run(client.tree.get_command("prices").callback(fake_interaction()))
    assert scanner.calls == first_calls  # served from snapshots newer than 2 h


def test_trend_and_health(client):
    run(client.record(client.scan_models([M5080])))
    i = fake_interaction()
    run(client.tree.get_command("trend").callback(i, "rtx-5080"))
    embed = i.response.sent[0][1]["embed"]
    assert "$1,400.00" in embed.description and len(embed.fields) == 3
    i = fake_interaction()
    run(client.tree.get_command("health").callback(i))
    embed = i.response.sent[0][1]["embed"]
    assert i.response.sent[0][1]["ephemeral"] and "ok" in embed.description


def test_tree_error_hides_exception_text(client):
    i = fake_interaction()
    err = app_commands.CommandInvokeError(client.tree.get_command("list"), RuntimeError("C:/secret/path"))
    run(client.on_tree_error(i, err))
    content, kwargs = i.response.sent[0]
    assert content == GENERIC_ERROR and kwargs["ephemeral"] and "secret" not in content

    i = fake_interaction()
    i.response.done = True
    run(client.on_tree_error(i, app_commands.CommandOnCooldown(app_commands.Cooldown(1, 30), 12.3)))
    assert "12 s" in i.followup.sent[0][0]


# Background loop


def test_background_notifies_once_then_rearms(client, monkeypatch):
    client.scan_models = FakeScanner([1400.0, 1400.0, None, 1390.0, 1600.0, 1600.0, 1450.0])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = []

    async def deliver(alert, embed):
        sent.append(embed)
        return True

    monkeypatch.setattr(client, "deliver", deliver)
    for _ in range(7):
        run(client.run_checks())
    # crossed at 1400; same price silent; blocked run silent; $10 drop silent;
    # two scans above target re-arm; back under target notifies again.
    assert [e.title for e in sent] == ["RTX 5080 is under your target"] * 2
    assert "$1,450.00" in sent[1].description
    assert sent[0].timestamp.tzinfo is not None
    assert {f.name for f in sent[0].fields} >= {"Your target", "Next best"}
    a = client.store.list_alerts(1)[0]
    assert not a.armed and a.notified_price_cents == 145_000
    # Every run was recorded, including the blocked one.
    assert len(client.store.best_history("rtx-5080")) == 6
    assert client.store._query("SELECT COUNT(*) FROM scrape_runs")[0][0] == 14


def test_failed_delivery_retries_next_run(client, monkeypatch):
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    outcomes = [False, True]
    calls = []

    async def deliver(alert, embed):
        calls.append(alert.id)
        return outcomes.pop(0)

    monkeypatch.setattr(client, "deliver", deliver)
    run(client.run_checks())
    assert client.store.list_alerts(1)[0].armed
    run(client.run_checks())
    assert len(calls) == 2 and not client.store.list_alerts(1)[0].armed


def test_one_bad_alert_does_not_stop_the_loop(client, monkeypatch):
    client.store.upsert_alert(1, "rtx-5080", 150_000)
    client.store.upsert_alert(2, "rtx-5080", 150_000)
    sent = []

    async def deliver(alert, embed):
        if alert.user_id == 1:
            raise RuntimeError("boom")
        sent.append(alert.user_id)
        return True

    monkeypatch.setattr(client, "deliver", deliver)
    run(client.run_checks())
    assert sent == [2]


class FakeChannel:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        self.sent.append((content, kwargs))


def _alert(client, channel_id=10):
    a, _ = client.store.upsert_alert(7, "rtx-5080", 150_000, channel_id=channel_id)
    return a


def test_deliver_to_channel_mentions_only_the_user(client, monkeypatch):
    ch = FakeChannel()
    monkeypatch.setattr(client, "get_channel", lambda cid: ch)
    assert run(client.deliver(_alert(client), discord.Embed(title="x")))
    content, kwargs = ch.sent[0]
    assert content == "<@7>"
    am = kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and [u.id for u in am.users] == [7]


def test_deliver_falls_back_to_fetch_then_dm(client, monkeypatch):
    fetched = FakeChannel(fail=True)
    dm = FakeChannel()

    async def fetch_channel(cid):
        return fetched

    async def fetch_user(uid):
        return dm

    monkeypatch.setattr(client, "get_channel", lambda cid: None)
    monkeypatch.setattr(client, "fetch_channel", fetch_channel)
    monkeypatch.setattr(client, "get_user", lambda uid: None)
    monkeypatch.setattr(client, "fetch_user", fetch_user)
    assert run(client.deliver(_alert(client), discord.Embed(title="x")))
    assert len(dm.sent) == 1

    async def fetch_missing(cid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")

    dm.fail = True
    monkeypatch.setattr(client, "fetch_channel", fetch_missing)
    assert not run(client.deliver(_alert(client), discord.Embed(title="x")))


def test_owner_hears_once_after_three_failed_runs(client, monkeypatch):
    client.scan_models = FakeScanner([None, None, None, None, 1400.0, None])
    client.store.upsert_alert(1, "rtx-5080", 150_000)
    dms = []

    async def dm_owner(text):
        dms.append(text)

    async def deliver(alert, embed):
        return True

    monkeypatch.setattr(client, "dm_owner", dm_owner)
    monkeypatch.setattr(client, "deliver", deliver)
    for _ in range(6):
        run(client.run_checks())
    assert len([d for d in dms if "failed 3 checks" in d]) == 2  # bestbuy and newegg, once each
    assert len([d for d in dms if "working again" in d]) == 2


def test_embeds_fit_discord_limits(client):
    scan = client.scan_models([M5080])["rtx-5080"]
    found = [L(1400.0), L(1450.0, retailer="newegg")]
    e = lookup_embed(scan, found, None)
    assert len(e) < 6000 and "no history yet" in e.fields[1].value
    a = _alert(client)
    e = alert_embed(a, M5080, found[0], found, None, "crossed")
    assert e.url == found[0].url and len(e.title) <= 256


class ScriptedScanner:
    """Returns the queued list of ScrapeResults per call."""

    def __init__(self, runs):
        self.runs = list(runs)
        self.calls = 0

    def __call__(self, models, *args, **kwargs):
        results = self.runs[min(self.calls, len(self.runs) - 1)]
        self.calls += 1
        at = datetime.now(timezone.utc)
        return {m.id: ModelScan(m, results, at) for m in models}


def _ok(retailer, *listings):
    return ScrapeResult(retailer, "q", Status.OK, list(listings), n_raw=len(listings))


def _blocked(retailer):
    return ScrapeResult(retailer, "q", Status.BLOCKED, http_status=403)


def _collect(client, monkeypatch):
    sent = []

    async def deliver(alert, embed):
        sent.append(embed.description)
        return True

    monkeypatch.setattr(client, "deliver", deliver)
    return sent


def test_cheapest_retailer_blocked_for_two_runs_does_not_ping_again(client, monkeypatch):
    both = [_ok("bestbuy", L(1400.0)), _ok("newegg", L(1600.0, retailer="newegg"))]
    bb_blocked = [_blocked("bestbuy"), _ok("newegg", L(1600.0, retailer="newegg"))]
    client.scan_models = ScriptedScanner([both, bb_blocked, bb_blocked, bb_blocked, both])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    for _ in range(5):
        run(client.run_checks())
    assert len(sent) == 1 and "$1,400.00" in sent[0]
    a = client.store.list_alerts(1)[0]
    assert not a.armed and a.notified_retailer == "bestbuy"


def test_restock_under_target_after_selling_out_everywhere_pings_again(client, monkeypatch):
    in_stock = [_ok("bestbuy", L(1400.0)), _ok("newegg", L(1600.0, retailer="newegg"))]
    sold_out = [_ok("bestbuy", L(1400.0, in_stock=False)),
                _ok("newegg", L(1600.0, retailer="newegg", in_stock=False))]
    client.scan_models = ScriptedScanner([in_stock] + [sold_out] * 5 + [in_stock])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    for _ in range(7):
        run(client.run_checks())
    assert len(sent) == 2 and all("$1,400.00" in d for d in sent)


def test_disabled_notified_retailer_does_not_hold_the_alert_forever(client, monkeypatch):
    both = [_ok("bestbuy", L(1400.0)), _ok("newegg", L(1600.0, retailer="newegg"))]
    bb_off = [ScrapeResult("bestbuy", "q", Status.DISABLED), _ok("newegg", L(1600.0, retailer="newegg"))]
    client.scan_models = ScriptedScanner([both, bb_off, bb_off])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    _collect(client, monkeypatch)
    for _ in range(3):
        run(client.run_checks())
    assert client.store.list_alerts(1)[0].armed


def test_rearm_with_same_target_during_delivery_is_kept(client, monkeypatch):
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)

    async def deliver(alert, embed):
        # The user runs /alert again with the same target while the ping is being sent.
        client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10, include_open_box=True)
        return True

    monkeypatch.setattr(client, "deliver", deliver)
    run(client.run_checks())
    a = client.store.list_alerts(1)[0]
    assert a.armed and a.notified_price_cents is None and a.include_open_box


def test_owner_warning_names_the_real_block_not_the_skip(client, monkeypatch):
    from gpu_alerts import pricing

    real = ScrapeResult("amazon", "a", Status.BLOCKED, http_status=503, detail="bot check")
    skip = ScrapeResult("amazon", "b", Status.BLOCKED, detail=pricing.SKIPPED_DETAIL)
    at = datetime.now(timezone.utc)
    scans = {"rtx-5080": ModelScan(M5080, [real], at),
             "rtx-5090": ModelScan(catalog.by_id("rtx-5090"), [skip], at)}
    dms = []

    async def dm_owner(text):
        dms.append(text)

    monkeypatch.setattr(client, "dm_owner", dm_owner)
    for _ in range(3):
        run(client.watch_health(scans))
    assert len(dms) == 1 and "HTTP 503" in dms[0]


def test_guild_sync_also_clears_stale_global_commands(client, monkeypatch):
    calls = []

    async def sync(*, guild=None):
        calls.append((guild.id if guild else None, len(client.tree.get_commands(guild=guild))))
        return []

    monkeypatch.setattr(botmod.config, "DISCORD_GUILD_ID", 42)
    monkeypatch.setattr(client.tree, "sync", sync)
    monkeypatch.setattr(client.check_loop, "start", lambda *a, **k: None)
    run(client.setup_hook())
    # The guild gets every command; the global tree is synced empty to remove old copies.
    assert calls == [(42, 8), (None, 0)]
    assert {c.name for c in client.tree.get_commands(guild=discord.Object(id=42))} >= {"alert", "check"}


def test_global_sync_without_a_guild(client, monkeypatch):
    calls = []

    async def sync(*, guild=None):
        calls.append(guild)
        return []

    monkeypatch.setattr(botmod.config, "DISCORD_GUILD_ID", None)
    monkeypatch.setattr(client.tree, "sync", sync)
    monkeypatch.setattr(client.check_loop, "start", lambda *a, **k: None)
    run(client.setup_hook())
    assert calls == [None] and len(client.tree.get_commands()) == 8


# Config and CLI


def test_dotenv_with_bom_and_utf16(tmp_path, monkeypatch):
    from gpu_alerts import config

    for key in ("DISCORD_BOT_TOKEN", "GPU_ALERTS_TEST_KEY"):
        monkeypatch.setenv(key, "")   # records the original so the loader's writes are undone
        monkeypatch.delenv(key)
    path = tmp_path / ".env"
    path.write_bytes("DISCORD_BOT_TOKEN=abc\nGPU_ALERTS_TEST_KEY=1\n".encode("utf-8-sig"))
    config._load_dotenv(path)
    assert config.discord_token() == "abc"
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    path.write_bytes("DISCORD_BOT_TOKEN=xyz\n".encode("utf-16"))
    config._load_dotenv(path)  # ignored with a warning, no crash
    assert "DISCORD_BOT_TOKEN" not in os.environ


def test_bad_integer_setting_falls_back(monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    from gpu_alerts import config

    for value, expected in (("", 60), ("abc", 60), ("15", 15), ("30  # half hour", 60)):
        monkeypatch.setenv("GPU_ALERTS_TEST_INT", value)
        assert config._int_setting("GPU_ALERTS_TEST_INT", 60) == expected
    # Importing config with a malformed value must not crash any command.
    env = {**os.environ, "CHECK_INTERVAL_MINUTES": "soon"}
    out = subprocess.run(
        [sys.executable, "-c", "from gpu_alerts import config; print(config.CHECK_INTERVAL_MINUTES)"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0 and out.stdout.strip() == "60"


def test_cli_prints_symbol_titles_on_a_cp1252_console(monkeypatch):
    import io
    import sys

    from gpu_alerts import __main__ as cli
    from gpu_alerts import pricing

    title = "ASUS TUF GeForce RTX\u2122 5080 16GB\uff08OC\uff09\u3001PCIe\u00ae 5.0"
    listing = Listing(retailer="bestbuy", title=title, price=1400.0, url="https://example.test/x", in_stock=True)
    monkeypatch.setattr(pricing, "scan", lambda q: [_ok("bestbuy", listing)])
    monkeypatch.setattr(pricing, "offers", lambda model, results, policy: [listing])
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert cli.main(["lookup", "rtx-5080"]) == 0
    assert cli.main(["lookup", "RTX\u2122 \uff15\uff10\uff18\uff10 nope"]) == 2
    out.flush()
    assert b"16GB?OC??PCIe" in out.buffer.getvalue()   # unencodable symbols replaced, no crash


def test_dm_owner_resolves_a_team_owner_to_a_user(client, monkeypatch):
    dm = FakeChannel()
    member = SimpleNamespace(id=99)   # like discord.TeamMember: no send()
    app = SimpleNamespace(team=SimpleNamespace(owner=member), owner=None)

    async def application_info():
        return app

    async def fetch_user(uid):
        assert uid == 99
        return dm

    monkeypatch.setattr(client, "application_info", application_info)
    monkeypatch.setattr(client, "get_user", lambda uid: None)
    monkeypatch.setattr(client, "fetch_user", fetch_user)
    run(client.dm_owner("hello"))
    assert dm.sent == [("hello", {})]
