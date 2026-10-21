import asyncio
import logging
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from gpu_alerts import bot as botmod
from gpu_alerts import catalog
from gpu_alerts.alerts import RELEASE_AFTER
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


def scan_time(calls):
    """When the fake scanner's `calls`-th scan ran: that many minutes from now, so each
    scan is newer than the last even on a clock that ticks every 15.6 ms (Windows before
    Python 3.13). evaluate skips a scan that is not newer than the alert's last one."""
    return datetime.now(timezone.utc) + timedelta(minutes=calls)


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
        at = scan_time(self.calls)
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

    async def autocomplete(self, choices):
        self.done = True
        self.sent.append((None, {"choices": choices}))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def fake_interaction(user_id=1, channel_id=10, guild_id=20, kind=discord.InteractionType.application_command,
                     permissions=botmod.INVITE_PERMISSIONS, channel_type=discord.ChannelType.text):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), channel_id=channel_id, guild_id=guild_id, type=kind,
        app_permissions=discord.Permissions(permissions), channel=SimpleNamespace(type=channel_type),
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


# Who may use the bot


@pytest.fixture
def members(client, monkeypatch):
    """User 1 owns the application. The bot is in servers 20 (with users 1 and 2) and 30 (user 5 only)."""
    guilds = {20: {1, 2}, 30: {5}}
    checks = []

    async def owner():
        return SimpleNamespace(id=1)

    async def is_member(guild_id, user_id):
        checks.append((guild_id, user_id))
        return user_id in guilds.get(guild_id, set())

    monkeypatch.setattr(client, "_owner", owner)
    monkeypatch.setattr(client, "_is_member", is_member)
    monkeypatch.setattr(GPUAlertBot, "guilds", property(lambda self: [SimpleNamespace(id=g) for g in guilds]))
    monkeypatch.setattr(botmod.config, "DISCORD_GUILD_ID", None)
    monkeypatch.setattr(botmod.config, "ALLOWED_GUILD_IDS", frozenset())
    return checks


def _allowed(client, **kw):
    i = fake_interaction(**kw)
    ok = run(client.tree.interaction_check(i))
    if not ok:
        assert i.response.sent == [(botmod.NOT_ALLOWED, {"ephemeral": True})]   # never a silent failure
    return ok


def test_without_configured_servers_only_the_owners_servers_may_use_commands(client, members):
    assert _allowed(client, user_id=2, guild_id=20)        # the owner is a member there
    assert not _allowed(client, user_id=5, guild_id=30)    # someone else added the bot to their server
    assert _allowed(client, user_id=2, guild_id=None)      # a DM from a member of an allowed server
    assert not _allowed(client, user_id=5, guild_id=None)
    assert _allowed(client, user_id=2, guild_id=20)
    assert members.count((20, 1)) == 1                     # the owner check is cached


def test_configured_servers_are_the_allow_list(client, members, monkeypatch):
    monkeypatch.setattr(botmod.config, "DISCORD_GUILD_ID", 30)
    monkeypatch.setattr(botmod.config, "ALLOWED_GUILD_IDS", frozenset({40}))
    assert _allowed(client, user_id=5, guild_id=30) and _allowed(client, user_id=6, guild_id=40)
    assert not _allowed(client, user_id=1, guild_id=20)
    assert _allowed(client, user_id=5, guild_id=None) and not _allowed(client, user_id=2, guild_id=None)


def test_refused_interaction_never_runs_the_command(client, members):
    i = fake_interaction(user_id=5, guild_id=30)
    run(client.tree._call(i))   # the tree's dispatcher stops at the check
    assert i.command_failed and client.store.all_alerts() == []
    i = fake_interaction(user_id=5, guild_id=30, kind=discord.InteractionType.autocomplete)
    assert not run(client.tree.interaction_check(i))
    assert i.response.sent == [(None, {"choices": []})]   # autocomplete can only answer with choices


def test_access_check_that_cannot_reach_discord_refuses(client, members, monkeypatch):
    async def owner():
        raise discord.HTTPException(SimpleNamespace(status=503, reason="Service Unavailable"), "down")

    monkeypatch.setattr(client, "_owner", owner)
    assert not _allowed(client, user_id=2, guild_id=20)


def _gateway_interaction(client, *, user_id, guild_id, gpu="rtx-5080", target=1500, permissions="19456",
                         command="alert", options=None, kind=2):
    """A real discord.Interaction (by default /alert; kind 4 is autocomplete), built from
    the payload Discord sends."""
    user = {"id": str(user_id), "username": f"u{user_id}", "discriminator": "0", "avatar": None, "global_name": None}
    if options is None:
        options = [{"name": "gpu", "type": 3, "value": gpu}, {"name": "target", "type": 4, "value": target}]
    data = {
        "id": "1", "application_id": "999", "type": kind, "token": "t", "version": 1, "channel_id": "555",
        "locale": "en-US", "entitlements": [], "app_permissions": permissions, "attachment_size_limit": 10485760,
        "data": {"id": "7", "name": command, "type": 1, "options": options},
    }
    if guild_id is None:
        data.update(user=user, context=1, channel={"id": "555", "type": 1})
    else:
        data.update(
            guild_id=str(guild_id), guild_locale="en-US", context=0,
            member={"user": user, "roles": [], "joined_at": "2026-01-01T00:00:00+00:00", "deaf": False,
                    "mute": False, "flags": 0, "permissions": "0"},
            channel={"id": "555", "type": 0, "guild_id": str(guild_id), "name": "deals", "position": 0,
                     "permission_overwrites": [], "nsfw": False, "parent_id": None},
        )
    state = client._connection
    state.user = discord.ClientUser(state=state, data={**user, "id": "999", "bot": True, "verified": True,
                                                       "mfa_enabled": False})
    i = discord.Interaction(data=data, state=state)
    i._cs_response = FakeResponse()   # nothing reaches Discord
    return i


def test_tree_dispatch_refuses_other_servers_and_runs_allowed_ones(client, members):
    for guild_id, user_id in ((999, 5), (None, 5)):   # a stranger's server; a DM from a non-member
        i = _gateway_interaction(client, user_id=user_id, guild_id=guild_id)
        run(client.tree._call(i))
        assert i.response.sent == [(botmod.NOT_ALLOWED, {"ephemeral": True})]
    assert client.store.all_alerts() == []
    i = _gateway_interaction(client, user_id=2, guild_id=20)
    run(client.tree._call(i))
    assert "Alert set" in i.response.sent[0][0]
    assert [(a.user_id, a.guild_id) for a in client.store.all_alerts()] == [(2, 20)]


def test_anyone_can_list_and_remove_their_own_alerts(client, members):
    # Alerts made in a server before it lost access are still checked and still ping, so
    # /list, /remove and its autocomplete (the caller's own rows only, no scan) stay open.
    client.store.upsert_alert(5, "rtx-5080", 150_000, guild_id=999)
    client.store.upsert_alert(2, "rtx-5090", 250_000, guild_id=20)
    for guild_id in (999, None):   # the stranger's server; a DM
        i = _gateway_interaction(client, user_id=5, guild_id=guild_id, command="list", options=[])
        run(client.tree._call(i))
        assert [f.name for f in i.response.sent[0][1]["embed"].fields] == ["RTX 5080"]
    gpu = {"name": "gpu", "type": 3, "value": "5080", "focused": True}
    i = _gateway_interaction(client, user_id=5, guild_id=999, command="remove", options=[gpu], kind=4)
    run(client.tree._call(i))
    assert [c.value for c in i.response.sent[0][1]["choices"]] == ["rtx-5080"]
    gpu = {"name": "gpu", "type": 3, "value": "rtx-5080"}
    i = _gateway_interaction(client, user_id=5, guild_id=999, command="remove", options=[gpu])
    run(client.tree._call(i))
    assert "Removed" in i.response.sent[0][0]
    assert [(a.user_id, a.model_id) for a in client.store.all_alerts()] == [(2, "rtx-5090")]
    # Everything else stays refused there, the GPU list included.
    i = _gateway_interaction(client, user_id=5, guild_id=999, command="alert", kind=4,
                             options=[{"name": "gpu", "type": 3, "value": "5080", "focused": True}])
    run(client.tree._call(i))
    assert i.response.sent == [(None, {"choices": []})]


def test_commands_install_to_servers_and_work_there_and_in_bot_dms(client):
    for cmd in client.tree.get_commands():
        d = cmd.to_dict(client.tree)
        assert d["integration_types"] == [0] and d["contexts"] == [0, 1]


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


def _alert_reply(client, gpu, target):
    i = fake_interaction()
    run(client.tree.get_command("alert").callback(i, gpu, target))
    return i.response.sent[0][0]


def test_alert_under_the_models_price_floor_says_it_cannot_fire(client):
    reply = _alert_reply(client, "arc-b580", 240)   # listings under $250 never count for a B580
    assert "Alert set" in reply and "under $250.00 are not counted" in reply
    assert "not counted" not in _alert_reply(client, "arc-b580", 250)


def test_alert_does_not_promise_a_ping_from_an_old_price(client):
    old = datetime.now(timezone.utc) - timedelta(days=150)   # last looked up months ago
    client.store.record_scan("rtx-5080", [L(1199.99)], n_ok=1, n_retailers=1, at=old)
    reply = _alert_reply(client, "rtx-5080", 1300)
    assert "expect a ping" not in reply and "$1,199.99" not in reply and "No recent price yet" in reply


def test_alert_after_a_check_that_found_nothing_eligible_says_so(client):
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    marketplace = Listing(retailer="walmart", title="Arc B580", price=289.0, url="https://example.test/w",
                          in_stock=True, first_party=False)
    client.store.record_scan("arc-b580", [marketplace], n_ok=5, n_retailers=6, at=recent)
    reply = _alert_reply(client, "arc-b580", 300)
    assert "nothing eligible" in reply and "No recent price yet" not in reply


def test_invite_asks_for_what_a_ping_needs():
    assert discord.Permissions(botmod.INVITE_PERMISSIONS) == discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True, send_messages_in_threads=True)


@pytest.mark.parametrize("permissions, channel_type", [
    (discord.Permissions(view_channel=True, embed_links=True).value, discord.ChannelType.text),   # read-only channel
    (discord.Permissions(view_channel=True, send_messages=True).value, discord.ChannelType.text),  # no embeds
    (discord.Permissions(view_channel=True, send_messages=True, embed_links=True).value,
     discord.ChannelType.public_thread),   # a forum post or thread without Send Messages in Threads
], ids=["no-send", "no-embeds", "thread"])
def test_alert_where_the_bot_cannot_post_pings_by_dm(client, permissions, channel_type):
    i = fake_interaction(permissions=permissions, channel_type=channel_type)
    run(client.tree.get_command("alert").callback(i, "rtx-5080", 1500))
    content = i.response.sent[0][0]
    assert "Alert set" in content and "pings come by DM" in content
    (a,) = client.store.list_alerts(1)
    assert a.channel_id is None and a.guild_id == 20


def test_alert_in_a_thread_the_bot_can_post_in_keeps_the_channel(client):
    i = fake_interaction(channel_type=discord.ChannelType.public_thread)
    run(client.tree.get_command("alert").callback(i, "rtx-5080", 1500))
    assert "by DM" not in i.response.sent[0][0] and client.store.list_alerts(1)[0].channel_id == 10


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


def test_remove_autocomplete_matches_like_the_gpu_list(client):
    client.store.upsert_alert(1, "rtx-5060", 40_000)
    client.store.upsert_alert(1, "rtx-5060-ti-16gb", 60_000)
    client.store.upsert_alert(1, "rtx-2080-old", 50_000)   # a model since dropped from the catalog
    cb = client.tree.get_command("remove")._params["gpu"].autocomplete
    # The plain RTX 5060 has no VRAM in its name; a typed size is compared with its VRAM.
    assert [c.value for c in run(cb(fake_interaction(), "RTX 5060 8GB"))] == ["rtx-5060"]
    assert [c.value for c in run(cb(fake_interaction(), "5060"))] == ["rtx-5060", "rtx-5060-ti-16gb"]
    assert [c.value for c in run(cb(fake_interaction(), "2080 old"))] == ["rtx-2080-old"]


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


async def _expired_followup(content=None, **kwargs):
    raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Webhook")


@pytest.mark.parametrize("prices, kind", [
    ([1400.0], "RTX 5080 is under your target"),
    ([1450.0, 1400.0], "RTX 5080 dropped again"),   # pinged at $1,450 first, /check finds $1,400
], ids=["crossed", "further-drop"])
def test_failed_check_reply_leaves_the_ping_to_the_background(client, monkeypatch, prices, kind):
    client.scan_models = FakeScanner(prices)
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    titles = []

    async def deliver(alert, embed):
        titles.append(embed.title)
        return True

    monkeypatch.setattr(client, "deliver", deliver)
    if len(prices) > 1:
        run(client.run_checks())
    before = client.store.list_alerts(1)[0]
    i = fake_interaction()
    i.followup.send = _expired_followup   # e.g. the 15-minute interaction token ran out
    with pytest.raises(discord.NotFound):
        run(client.tree.get_command("check").callback(i))
    a = client.store.list_alerts(1)[0]
    assert (a.armed, a.notified_price_cents, a.notified_at) == (
        before.armed, before.notified_price_cents, before.notified_at)
    assert a.last_price_cents == 140_000
    run(client.run_checks())
    assert titles[-1] == kind and len(titles) == len(prices)


def test_check_that_fails_partway_puts_back_the_alerts_it_disarmed(client, monkeypatch):
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    client.store.upsert_alert(1, "rtx-5090", 250_000, channel_id=10)
    real = client.evaluate

    async def evaluate(alert_id, scan, **kwargs):
        if scan.model.id == "rtx-5090":
            raise sqlite3.OperationalError("disk I/O error")
        return await real(alert_id, scan, **kwargs)

    monkeypatch.setattr(client, "evaluate", evaluate)
    i = fake_interaction()
    with pytest.raises(sqlite3.OperationalError):
        run(client.tree.get_command("check").callback(i))
    assert i.followup.sent == [] and all(a.armed for a in client.store.list_alerts(1))


def test_commands_answer_while_scans_hold_their_threads(client):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    lock = threading.Lock()
    running, peak = [0], [0]
    inner = client.scan_models

    def slow_scan(models, *args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(10)   # a real scan holds its thread for 10 s to several minutes
        with lock:
            running[0] -= 1
        return inner(models, *args, **kwargs)

    client.scan_models = slow_scan
    client.store.upsert_alert(1, "rtx-5080", 150_000)

    async def main():
        # asyncio's default pool is min(32, CPUs + 4) threads: 5 on a 1-vCPU VPS.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(2))
        lookup = client.tree.get_command("lookup").callback
        scans = [asyncio.create_task(lookup(fake_interaction(user_id=u), "rtx-5080")) for u in range(2, 6)]
        for _ in range(500):   # until every scan thread is busy
            if running[0] == 2:
                break
            await asyncio.sleep(0.01)
        i = fake_interaction()
        try:
            await asyncio.wait_for(client.tree.get_command("list").callback(i), 2)
        finally:
            release.set()
        await asyncio.gather(*scans)
        return i

    i = run(main())
    assert i.response.sent[0][1]["embed"].fields   # /list answered while four lookups were scanning
    assert peak[0] == 2                             # scans are capped, not one thread each


def test_check_defers_before_reading_the_database(client, monkeypatch):
    i = fake_interaction()
    real = client.store.list_alerts
    deferred = []

    def list_alerts(user_id):
        deferred.append(i.response.done)
        return real(user_id)

    monkeypatch.setattr(client.store, "list_alerts", list_alerts)
    run(client.tree.get_command("check").callback(i))
    assert deferred == [True]
    content, kwargs = i.followup.sent[0]
    assert "no alerts" in content and kwargs["ephemeral"]


def test_prices_serves_fresh_snapshots_without_scanning(client):
    scanner = client.scan_models
    i = fake_interaction()
    run(client.tree.get_command("prices").callback(i))
    first_calls = scanner.calls
    assert first_calls == 1 and len(i.followup.sent) == 1
    embed = i.followup.sent[0][1]["embed"]
    assert len(embed.fields) == len([m for m in catalog.load() if m.popular and not m.legacy])
    run(client.tree.get_command("prices").callback(fake_interaction()))
    assert scanner.calls == first_calls  # served from snapshots newer than 2 h


# The numbers the README documents, read from the code that enforces them.


def _prices_footer(client):
    i = fake_interaction()
    run(client.tree.get_command("prices").callback(i))
    return i.followup.sent[0][1]["embed"].footer.text


def test_prices_scans_for_missing_data_at_most_every_10_minutes(client, monkeypatch):
    assert botmod.PRICES_SCAN_COOLDOWN_S == 600
    clock = [50_000.0]
    monkeypatch.setattr(botmod, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client.scan_models = scanner = FakeScanner([None])   # every retailer blocked: the prices stay missing
    note = "a fresh scan runs at most every 10 minutes"
    assert note not in _prices_footer(client) and scanner.calls == 1
    assert note in _prices_footer(client) and scanner.calls == 1   # asked again at once: no second scan
    clock[0] += 599
    assert note in _prices_footer(client) and scanner.calls == 1
    clock[0] += 1                                                  # 10 minutes after the last scan
    assert note not in _prices_footer(client) and scanner.calls == 2


def test_prices_counts_data_up_to_2_hours_old_as_fresh(client):
    assert botmod.PRICES_FRESH == timedelta(hours=2)
    popular = [m for m in catalog.load() if m.popular and not m.legacy]
    now = datetime.now(timezone.utc)
    for m in popular[1:]:
        client.store.record_scan(m.id, [L(1400.0)], n_ok=1, n_retailers=6, at=now - timedelta(minutes=119))
    client.store.record_scan(popular[0].id, [L(1400.0)], n_ok=1, n_retailers=6, at=now - timedelta(minutes=121))
    scanned = []
    inner = client.scan_models

    def scanner(models, *args, **kwargs):
        scanned.append([m.id for m in models])
        return inner(models, *args, **kwargs)

    client.scan_models = scanner
    _prices_footer(client)
    assert scanned == [[popular[0].id]]   # only the model whose data is over 2 h old


@pytest.mark.parametrize("command, seconds", [("lookup", 30), ("check", 300)])
def test_lookup_and_check_cooldowns_are_per_user(client, command, seconds):
    assert (botmod.LOOKUP_COOLDOWN_S, botmod.CHECK_COOLDOWN_S) == (30, 300)
    (cooldown,) = client.tree.get_command(command).checks
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def use(user_id, after_s):
        i = fake_interaction(user_id=user_id)
        i.created_at = t0 + timedelta(seconds=after_s)
        return run(cooldown(i))

    assert use(1, 0) and use(2, 0)   # one user's use does not hold up another
    with pytest.raises(app_commands.CommandOnCooldown) as exc:
        use(1, seconds - 1)
    assert (exc.value.cooldown.rate, exc.value.cooldown.per) == (1, seconds)
    assert exc.value.retry_after == pytest.approx(1)
    assert use(1, seconds + 1) and use(2, seconds + 1)


def test_eleventh_alert_is_refused(client):
    assert botmod.MAX_ALERTS_PER_USER == 10
    ids = [m.id for m in catalog.load()][:11]
    for model_id in ids[:10]:
        assert "Alert set" in _alert_reply(client, model_id, 2000)
    assert "You already have 10 alerts" in _alert_reply(client, ids[10], 2000)
    assert "Alert updated" in _alert_reply(client, ids[0], 1900)   # changing one at the cap still works
    assert len(client.store.list_alerts(1)) == 10


def test_targets_run_from_100_to_20000_dollars(client):
    cmd = client.tree.get_command("alert")
    (target,) = [o for o in cmd.to_dict(client.tree)["options"] if o["name"] == "target"]
    assert (target["type"], target["min_value"], target["max_value"]) == (4, 100, 20_000)   # Discord enforces it
    for dollars in (100, 20_000):
        _alert_reply(client, "rtx-5080", dollars)
        assert client.store.list_alerts(1)[0].target_cents == dollars * 100


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


def test_a_retailer_that_blocks_partway_does_not_always_miss_the_same_alerts(client, monkeypatch):
    from gpu_alerts import pricing

    for user_id, model_id in ((1, "rtx-5090"), (1, "rtx-5080"), (2, "rtx-5070-ti")):
        client.store.upsert_alert(user_id, model_id, 100_000)
    _collect(client, monkeypatch)
    amazon_answered = defaultdict(int)

    def scan_models(models):
        searches = []

        def search(query, ids):   # Amazon answers two searches per run, then shows a bot check
            searches.append(query)
            blocked = len(searches) > 2
            return [ScrapeResult(rid, query, Status.BLOCKED if rid == "amazon" and blocked else Status.EMPTY)
                    for rid in ids]

        # The real breaker: after the block, Amazon is skipped for the rest of the run.
        scans = pricing.scan_models(models, ["bestbuy", "amazon"], scanner=search)
        for model_id, scan in scans.items():
            amazon_answered[model_id] += any(r.retailer == "amazon" and r.ok for r in scan.results)
        return scans

    client.scan_models = scan_models
    for _ in range(3):
        run(client.run_checks())
    assert amazon_answered == {"rtx-5090": 2, "rtx-5080": 2, "rtx-5070-ti": 2}


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


def _failing_saves(client, monkeypatch, n):
    """Make the next n update_alert_state calls fail as if the database were locked."""
    real = client.store.update_alert_state
    calls = []

    def update_alert_state(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) <= n:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(client.store, "update_alert_state", update_alert_state)


def test_failed_save_after_a_ping_does_not_ping_again(client, monkeypatch):
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    _failing_saves(client, monkeypatch, 2)   # the save after the ping and its first retry
    for _ in range(3):
        run(client.run_checks())
    assert len(sent) == 1
    a = client.store.list_alerts(1)[0]
    assert not a.armed and a.notified_price_cents == 140_000 and a.notified_at is not None


def test_unsaved_ping_is_dropped_when_the_user_resets_the_alert(client, monkeypatch):
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    _failing_saves(client, monkeypatch, 1)
    run(client.run_checks())
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)   # /alert again: judged fresh
    run(client.run_checks())
    assert len(sent) == 2 and not client.store.list_alerts(1)[0].armed


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


def test_undeliverable_alert_is_retried_and_the_owner_hears_once(client, monkeypatch):
    user = FakeChannel(fail=True)   # e.g. DMs from server members turned off
    told = []

    async def fetch_user(uid):
        return user

    async def dm_owner(text):
        told.append(text)
        return True

    monkeypatch.setattr(client, "get_channel", lambda cid: FakeChannel(fail=True))
    monkeypatch.setattr(client, "get_user", lambda uid: None)
    monkeypatch.setattr(client, "fetch_user", fetch_user)
    monkeypatch.setattr(client, "dm_owner", dm_owner)
    a = _alert(client)
    for _ in range(3):
        assert not run(client.deliver(a, discord.Embed(title="x")))
    assert len(told) == 1 and "user 7" in told[0] and "RTX 5080" in told[0]
    user.fail = False
    assert run(client.deliver(a, discord.Embed(title="x")))
    user.fail = True   # reachable in between, so a new failure is news again
    assert not run(client.deliver(a, discord.Embed(title="x")))
    assert len(told) == 2


def _owner_dms(client, monkeypatch, prices):
    client.scan_models = FakeScanner(prices)
    client.store.upsert_alert(1, "rtx-5080", 150_000)
    dms = []

    async def dm_owner(text):
        dms.append(text)
        return True

    async def deliver(alert, embed):
        return True

    monkeypatch.setattr(client, "dm_owner", dm_owner)
    monkeypatch.setattr(client, "deliver", deliver)
    for _ in prices:
        run(client.run_checks())
    return [d for d in dms if "failed 3 checks" in d], [d for d in dms if "working again" in d]


def test_owner_hears_once_after_three_failed_runs(client, monkeypatch):
    warned, recovered = _owner_dms(client, monkeypatch, [None, None, None, None, 1400.0, 1400.0, None])
    assert len(warned) == 2 and len(recovered) == 2   # bestbuy and newegg, once each


def test_retailer_that_answers_now_and_then_does_not_flood_the_owner(client, monkeypatch):
    # Blocked three runs, answers one, and again, for a day.
    warned, recovered = _owner_dms(client, monkeypatch, ([None] * 3 + [1400.0]) * 6)
    assert len(warned) == 2 and recovered == []


def test_owner_warning_that_could_not_be_sent_is_sent_on_the_next_failed_run(client, monkeypatch):
    blocked = ScrapeResult("amazon", "q", Status.BLOCKED, http_status=503)
    scans = {"rtx-5080": ModelScan(M5080, [blocked], datetime.now(timezone.utc))}
    outcomes = [False, True]   # Discord was unreachable for the first DM
    attempts = []

    async def dm_owner(text):
        attempts.append(text)
        return outcomes.pop(0) if outcomes else True

    monkeypatch.setattr(client, "dm_owner", dm_owner)
    for _ in range(6):
        run(client.watch_health(scans))
    assert len(attempts) == 2 and all("failed" in a for a in attempts)


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
        at = scan_time(self.calls)
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


def _partial(retailer, status, *listings):
    return ScrapeResult(retailer, "q", status, list(listings), n_raw=len(listings), partial=True)


@pytest.mark.parametrize("partial", [
    # A later page failed: OK with only a dearer page-1 listing.
    _partial("bestbuy", Status.OK, L(1650.0)),
    # A later page failed and page 1 had nothing eligible.
    _partial("bestbuy", Status.EMPTY),
    # The availability API gave no stock answer for the deal's SKU, so it counts as sold out.
    _partial("bestbuy", Status.OK, L(1400.0, in_stock=False)),
], ids=["ok", "empty", "stock-unknown"])
def test_partial_read_of_the_notified_retailer_does_not_rearm(client, monkeypatch, partial):
    full = [_ok("bestbuy", L(1400.0)), _ok("newegg", L(1600.0, retailer="newegg"))]
    part = [partial, _ok("newegg", L(1600.0, retailer="newegg"))]
    client.scan_models = ScriptedScanner([full, part, part, full])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    for _ in range(4):
        run(client.run_checks())
    assert len(sent) == 1 and "$1,400.00" in sent[0]
    assert not client.store.list_alerts(1)[0].armed


def test_partial_read_still_pings_an_offer_under_target(client, monkeypatch):
    client.scan_models = ScriptedScanner([[_partial("bestbuy", Status.OK, L(1400.0)), _blocked("newegg")]])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    run(client.run_checks())
    assert len(sent) == 1 and "$1,400.00" in sent[0]


def test_second_retailer_under_target_blocked_does_not_repeat_its_offer(client, monkeypatch):
    newegg = _ok("newegg", L(1600.0, retailer="newegg"))
    first = [_ok("bestbuy", L(1400.0)), _ok("amazon", L(1450.0, retailer="amazon")), newegg]
    amazon_blocked = [_ok("bestbuy", L(1600.0)), _blocked("amazon"), newegg]
    amazon_back = [_ok("bestbuy", L(1600.0)), _ok("amazon", L(1450.0, retailer="amazon")), newegg]
    client.scan_models = ScriptedScanner([first, amazon_blocked, amazon_blocked, amazon_back])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    for _ in range(4):
        run(client.run_checks())
    # Amazon's $1,450 was under target the whole time (and shown as "Next best" in the ping).
    assert len(sent) == 1 and "$1,400.00" in sent[0]
    a = client.store.list_alerts(1)[0]
    assert not a.armed and a.holding_retailers == {"amazon"}


def test_deal_that_moved_without_a_ping_holds_the_alert(client, monkeypatch):
    newegg = _ok("newegg", L(1600.0, retailer="newegg"))
    first = [_ok("bestbuy", L(1400.0)), _ok("amazon", L(1650.0, retailer="amazon")), newegg]
    moved = [_ok("bestbuy", L(1600.0)), _ok("amazon", L(1480.0, retailer="amazon")), newegg]
    amazon_blocked = [_ok("bestbuy", L(1600.0)), _blocked("amazon"), newegg]
    client.scan_models = ScriptedScanner([first, moved, amazon_blocked, amazon_blocked, moved])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    for _ in range(5):
        run(client.run_checks())
    assert len(sent) == 1 and "$1,400.00" in sent[0]


def test_retailer_that_keeps_reading_partially_lets_go_of_the_alert_in_the_end(client, monkeypatch):
    newegg = _ok("newegg", L(1600.0, retailer="newegg"))
    first = [_ok("bestbuy", L(1400.0)), _ok("amazon", L(1480.0, retailer="amazon")), newegg]
    # Amazon's page 2 keeps failing, so its $1,480 "Next best" is never seen gone.
    half_read = [_ok("bestbuy", L(1600.0)), _partial("amazon", Status.OK, L(1650.0, retailer="amazon")), newegg]
    crossed = [half_read[0], half_read[1], _ok("newegg", L(1450.0, retailer="newegg"))]
    client.scan_models = ScriptedScanner([first] + [half_read] * RELEASE_AFTER + [crossed])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    for _ in range(RELEASE_AFTER):
        run(client.run_checks())
    a = client.store.list_alerts(1)[0]
    assert not a.armed and a.holding_retailers == {"amazon"} and a.held_streak == RELEASE_AFTER - 1
    for _ in range(2):   # the alert stops waiting for Amazon and re-arms; then Newegg crosses
        run(client.run_checks())
    assert len(sent) == 2 and "$1,450.00" in sent[1]


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


def test_list_does_not_show_an_old_price_as_just_checked(client, monkeypatch):
    in_stock = [_ok("bestbuy", L(1400.0)), _ok("newegg", L(1600.0, retailer="newegg"))]
    sold_out = [_ok("bestbuy", L(1400.0, in_stock=False)),
                _ok("newegg", L(1600.0, retailer="newegg", in_stock=False))]
    client.scan_models = ScriptedScanner([in_stock, sold_out, [_blocked("bestbuy"), _blocked("newegg")]])
    client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    _collect(client, monkeypatch)

    def listed():
        i = fake_interaction()
        run(client.tree.get_command("list").callback(i))
        return i.response.sent[0][1]["embed"].fields[0].value.splitlines()[0]

    run(client.run_checks())
    assert "last price $1,400.00" in listed()
    run(client.run_checks())   # every retailer answered: sold out everywhere
    a = client.store.list_alerts(1)[0]
    assert a.last_price_cents is None
    assert "$1,400.00" not in listed() and "nothing eligible" in listed()
    run(client.run_checks())   # no retailer answered: nothing was seen, so the check time stays
    assert client.store.list_alerts(1)[0].last_checked_at == a.last_checked_at


def test_retailers_turned_off_are_not_counted_as_not_answering(client):
    off = [ScrapeResult(rid, "q", Status.DISABLED, detail="disabled in config") for rid in ("amazon", "walmart")]
    scan = ModelScan(M5080, [_ok("bestbuy", L(1400.0)), _ok("newegg")] + off)
    assert lookup_embed(scan, [L(1400.0)], None).footer.text == "2 of 2 retailers answered"
    client.scan_models = ScriptedScanner([[_ok("bestbuy"), _blocked("newegg")] + off])
    client.store.upsert_alert(1, "rtx-5080", 150_000)
    i = fake_interaction()
    run(client.tree.get_command("check").callback(i))
    assert i.followup.sent[0][1]["embed"].fields[0].value == "No eligible offer (1 of 2 retailers answered)."


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


def _reuse_id_during_scan(client, old):
    """A scanner that removes `old` (the newest alert) mid-scan; user 2's new RTX 5090 alert takes its id."""
    inner = client.scan_models

    def scanner(models, *args, **kwargs):
        client.store.remove_alert(old.user_id, old.model_id)
        new, _ = client.store.upsert_alert(2, "rtx-5090", 300_000, channel_id=10)
        assert new.id == old.id
        return inner(models, *args, **kwargs)

    return scanner


def test_alert_id_reused_during_the_scan_is_not_judged_on_the_old_scan(client, monkeypatch):
    old, _ = client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    client.scan_models = _reuse_id_during_scan(client, old)
    sent = _collect(client, monkeypatch)
    run(client.run_checks())
    assert sent == []
    (a,) = client.store.all_alerts()
    assert (a.user_id, a.model_id) == (2, "rtx-5090")
    assert a.armed and a.notified_price_cents is None and a.last_price_cents is None


def test_alert_id_reused_during_a_check_is_not_shown_or_changed(client):
    old, _ = client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    client.scan_models = _reuse_id_during_scan(client, old)
    i = fake_interaction()
    run(client.tree.get_command("check").callback(i))
    assert i.followup.sent[0][1]["embed"].fields == []   # nothing about user 2's RTX 5090
    (a,) = client.store.all_alerts()
    assert (a.user_id, a.armed, a.notified_price_cents, a.last_price_cents) == (2, True, None, None)


@pytest.mark.parametrize("user_id, model_id", [(2, "rtx-5080"), (1, "rtx-5090")])
@pytest.mark.parametrize("delivered", [True, False])
def test_alert_id_reused_during_delivery_is_not_written(client, monkeypatch, user_id, model_id, delivered):
    old, _ = client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)

    async def deliver(alert, embed):
        # The alert is removed and a new one with the same target takes its id while the ping is sent.
        client.store.remove_alert(1, "rtx-5080")
        new, _ = client.store.upsert_alert(user_id, model_id, 150_000, channel_id=10)
        assert new.id == old.id
        return delivered

    monkeypatch.setattr(client, "deliver", deliver)
    run(client.run_checks())
    (a,) = client.store.all_alerts()
    assert (a.user_id, a.model_id) == (user_id, model_id)
    assert a.armed and a.notified_price_cents is None and a.last_price_cents is None


@pytest.mark.parametrize("background_price, check_price", [(1400.0, 1600.0), (1600.0, 1400.0)])
def test_older_background_scan_does_not_overwrite_a_newer_check(
    client, monkeypatch, background_price, check_price
):
    a, _ = client.store.upsert_alert(1, "rtx-5080", 150_000, channel_id=10)
    sent = _collect(client, monkeypatch)
    at = datetime.now(timezone.utc)
    # The background run read this model first; a /check read it later but was judged first.
    background = ModelScan(M5080, [_ok("bestbuy", L(background_price))], at)
    check = ModelScan(M5080, [_ok("bestbuy", L(check_price))], at + timedelta(minutes=1))
    run(client.evaluate(a.id, check, deliver=False))
    after_check = client.store.get_alert(a.id)
    ev = run(client.evaluate(a.id, background))
    assert sent == [] and not ev.decision.notify
    assert client.store.get_alert(a.id) == after_check
    assert after_check.observed_at == check.scanned_at


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
        return True

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


def _http_error(cls, status, code, message):
    return cls(SimpleNamespace(status=status, reason=message), {"code": code, "message": message})


@pytest.mark.parametrize("guild_id, error, says", [
    # Discord's answer when the bot was never added to that server (README order: .env, run, invite).
    (42, _http_error(discord.Forbidden, 403, 50001, "Missing Access"), "not in guild 42 yet"),
    (42, _http_error(discord.HTTPException, 503, 0, "Service Unavailable"), "could not sync commands"),
    (None, _http_error(discord.HTTPException, 503, 0, "Service Unavailable"), "could not sync commands"),
], ids=["guild-not-joined", "guild-discord-error", "global-discord-error"])
def test_failed_command_sync_still_logs_the_invite_and_starts_the_loop(
    client, monkeypatch, caplog, guild_id, error, says
):
    calls, started = [], []

    # Only Discord's HTTP answers are faked; the real CommandTree.sync runs.
    async def upsert_guild(app_id, gid, payload):
        calls.append(gid)
        raise error

    async def upsert_global(app_id, payload):
        calls.append(None)
        if guild_id is None:
            raise error
        return []

    monkeypatch.setattr(botmod.config, "DISCORD_GUILD_ID", guild_id)
    monkeypatch.setattr(client.http, "bulk_upsert_guild_commands", upsert_guild)
    monkeypatch.setattr(client.http, "bulk_upsert_global_commands", upsert_global)
    monkeypatch.setattr(client.check_loop, "start", lambda *a, **k: started.append(True))
    client._connection.application_id = 999
    with caplog.at_level(logging.INFO, logger="gpu_alerts.bot"):
        run(client.setup_hook())
    messages = [r.getMessage() for r in caplog.records]
    assert started == [True]   # alerts keep going out by DM and in other channels
    assert any("invite URL" in m and "client_id=999" in m for m in messages)
    assert any(says in m for m in messages)
    assert any(m.startswith(f"checking prices every {client.check_loop.minutes:.0f} minutes at: ") for m in messages)
    # A failed guild sync skips the global clear, which would leave no commands anywhere.
    assert calls == [guild_id]


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
    assert run(client.dm_owner("hello")) is True
    assert dm.sent == [("hello", {})]
    dm.fail = True
    assert run(client.dm_owner("hello")) is False


@pytest.mark.parametrize("title, label", [
    # A marketplace seller's title shown as a bold fake price next to the real one.
    ("ASUS RTX 5080 **$899 TODAY** ~~$1,999~~", r"ASUS RTX 5080 \*\*$899 TODAY\*\* \~\~$1,999\~\~"),
    ("RTX 5080 ||click|| `code` _OC_", r"RTX 5080 \|\|click\|\| \`code\` \_OC\_"),
    ("RTX 5080 <t:0:R> <@&123> @everyone", r"RTX 5080 \<t:0:R> \<\@&123> \@everyone"),
    ("RTX 5080 16GB \\", "RTX 5080 16GB \\\\"),            # a lone trailing backslash would eat the "]"
    ("[NEW] RTX 5080\u202e 16GB\u200b", "(NEW) RTX 5080 16GB"),   # brackets; bidi and zero-width marks dropped
])
def test_link_shows_listing_text_as_typed(title, label):
    from gpu_alerts.bot import link

    assert link(title, "https://www.walmart.ca/en/ip/x (1)") == f"[{label}](https://www.walmart.ca/en/ip/x%20(1%29)"


def test_alert_embed_escapes_the_listing_title(client):
    best = Listing(retailer="walmart", title="MSI RTX 5080 **$899**", price=1239.99,
                   url="https://example.test/w", in_stock=True, first_party=False)
    e = alert_embed(_alert(client), M5080, best, [best], None, "crossed")
    assert r"[MSI RTX 5080 \*\*$899\*\*](https://example.test/w)" in e.description


def test_stats_line_says_check_for_one():
    from gpu_alerts.bot import stats_line
    from gpu_alerts.store import PriceStats

    one = PriceStats("rtx-5080", 30, 160_999, None, 160_999, 160_999, 160_999, 1)
    three = PriceStats("rtx-5080", 30, 160_999, None, 150_000, 160_999, 170_000, 3)
    assert stats_line(one).endswith("over 1 check")
    assert stats_line(three).endswith("over 3 checks")
