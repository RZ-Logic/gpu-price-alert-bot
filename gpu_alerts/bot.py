"""The Discord bot: slash commands, the background price check and alert delivery."""

import asyncio
import functools
import logging
import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from . import catalog, config, pricing
from .alerts import CROSSED, UNCHANGED, AlertState, Decision, decide
from .catalog import GPUModel
from .pricing import DEFAULT_POLICY, RECORD_POLICY, ModelScan, Policy
from .retailers.base import Listing, ScrapeResult, Status
from .store import Alert, AlertLimitError, PriceStats, Store, to_cents, utcnow

log = logging.getLogger(__name__)

MAX_ALERTS_PER_USER = 10
LOOKUP_COOLDOWN_S = 30
CHECK_COOLDOWN_S = 300
PRICES_FRESH = timedelta(hours=2)
PRICES_SCAN_COOLDOWN_S = 600
OWNER_ALERT_AFTER = 3          # failed background runs in a row before the owner hears about it
OWNER_RECOVERED_AFTER = 2      # good runs in a row before the owner hears it works again
SCAN_THREADS = 2               # scans at once (each also runs one thread per retailer); more wait their turn
PRUNE_EVERY_S = 24 * 3600
ACCESS_CACHE_S = 3600          # how long a server membership check is trusted
INVITE_PERMISSIONS = 19456 | 1 << 38   # View Channel + Send Messages + Embed Links + Send Messages in Threads
THREAD_TYPES = {discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread}
GENERIC_ERROR = "Something went wrong on my side. It has been logged; please try again in a few minutes."
NOT_ALLOWED = (
    "This bot only answers in its owner's servers, and in DMs to their members. "
    "/list and /remove still work anywhere for alerts you already have."
)
# Commands that read or delete only the caller's own alerts and start no scan, open to everyone.
OWN_ALERT_COMMANDS = frozenset({"list", "remove"})
COLOR_OK = 0x2ECC71
COLOR_INFO = 0x3498DB
COLOR_WARN = 0xE67E22
# Discord markdown, plus "<" (timestamps, mentions) and "@" (@everyone); a backslash shows each as typed.
_LABEL_ESCAPE = re.compile(r"([\\*_~|`<@])")


# Formatting helpers (pure, so they can be tested without Discord)

def money(cents: int | None) -> str:
    return "n/a" if cents is None else f"${cents / 100:,.2f}"


def retailer_name(retailer_id: str) -> str:
    from . import retailers

    return retailers.display_name(retailer_id)


def short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def link(label: str, url: str) -> str:
    """A masked link. Labels are often listing titles, which sellers write, so their
    markdown, mentions and timestamps show as typed instead of styling the embed.
    Line-start markdown (headers, quotes, lists) cannot start inside the brackets."""
    label = "".join(ch for ch in label if unicodedata.category(ch) not in ("Cc", "Cf"))   # bidi, zero-width
    label = _LABEL_ESCAPE.sub(r"\\\1", label.replace("[", "(").replace("]", ")"))
    safe = url.replace(")", "%29").replace(" ", "%20")
    return f"[{label}]({safe})"


def can_post(interaction: discord.Interaction) -> bool:
    """Whether the bot may post a ping (a message with an embed) where the command ran."""
    perms = interaction.app_permissions
    in_thread = getattr(interaction.channel, "type", None) in THREAD_TYPES
    send = perms.send_messages_in_threads if in_thread else perms.send_messages
    return perms.view_channel and send and perms.embed_links


def policy_for(alert: Alert) -> Policy:
    return Policy(include_open_box=alert.include_open_box, include_marketplace=alert.include_marketplace)


def policy_note(policy: Policy) -> str:
    parts = ["new" if not policy.include_open_box else "new, open box or refurbished"]
    parts.append("any seller" if policy.include_marketplace else "sold by the retailer")
    return ", ".join(parts)


def stats_line(stats: PriceStats | None) -> str:
    if stats is None or stats.count == 0:
        return "no history yet"
    return (
        f"low {money(stats.low_cents)}, median {money(stats.median_cents)}, "
        f"high {money(stats.high_cents)} over {stats.count} check{'' if stats.count == 1 else 's'}"
    )


def n_active(scan: ModelScan) -> int:
    """Retailers the scan asked, leaving out those turned off in config."""
    return sum(1 for r in scan.results if Status(r.status) is not Status.DISABLED)


def retailer_lines(scan: ModelScan, found: list[Listing]) -> list[str]:
    per = pricing.best_per_retailer(found)
    lines = []
    for result in scan.results:
        offer = per.get(result.retailer)
        label = pricing.status_label(result, offer, scan.model)
        name = retailer_name(result.retailer)
        lines.append(f"**{name}**: {link(label, offer.url) if offer else label}")
    return lines


def lookup_embed(scan: ModelScan, found: list[Listing], stats: PriceStats | None) -> discord.Embed:
    model = scan.model
    best = found[0] if found else None
    embed = discord.Embed(title=f"{model.name}: live prices", color=COLOR_INFO, timestamp=discord.utils.utcnow())
    if best:
        embed.description = (
            f"Best: **${best.price:,.2f}** at {retailer_name(best.retailer)}\n"
            f"{link(short(best.title), best.url)}"
        )
    else:
        embed.description = "No eligible offer right now (new, in stock or unknown stock, sold by the retailer)."
    embed.add_field(name="By retailer", value="\n".join(retailer_lines(scan, found))[:1024] or "none", inline=False)
    embed.add_field(name="Last 30 days", value=stats_line(stats), inline=False)
    embed.set_footer(text=f"{scan.n_ok} of {n_active(scan)} retailers answered")
    return embed


def alert_embed(
    alert: Alert, model: GPUModel, best: Listing, found: list[Listing], stats: PriceStats | None, kind: str
) -> discord.Embed:
    title = f"{model.name} is under your target" if kind == CROSSED else f"{model.name} dropped again"
    embed = discord.Embed(
        title=title, url=best.url, color=COLOR_OK, timestamp=discord.utils.utcnow(),
        description=f"**${best.price:,.2f}** at {retailer_name(best.retailer)}\n{link(short(best.title), best.url)}",
    )
    embed.add_field(name="Your target", value=money(alert.target_cents), inline=True)
    if stats and stats.count:
        embed.add_field(name="30-day low", value=money(stats.low_cents), inline=True)
        embed.add_field(name="30-day median", value=money(stats.median_cents), inline=True)
    others = [li for rid, li in pricing.best_per_retailer(found).items() if rid != best.retailer][:3]
    if others:
        embed.add_field(
            name="Next best",
            value="\n".join(f"{retailer_name(li.retailer)}: {link(f'${li.price:,.2f}', li.url)}" for li in others),
            inline=False,
        )
    embed.set_footer(text=f"Counting {policy_note(policy_for(alert))}. You will hear again only on a further drop.")
    return embed


@dataclass
class Evaluation:
    alert: Alert
    model: GPUModel
    best: Listing | None
    decision: Decision
    delivered: bool


def configured_guilds() -> set[int]:
    return {g for g in (config.DISCORD_GUILD_ID, *config.ALLOWED_GUILD_IDS) if g}


class AccessTree(app_commands.CommandTree):
    """Runs GPUAlertBot.check_access before every command and autocomplete."""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return await self.client.check_access(interaction)


class GPUAlertBot(discord.Client):
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        store: Store | None = None,
        scan_models: Callable[..., dict[str, ModelScan]] = pricing.scan_models,
        matcher: pricing.Matcher | None = None,
    ) -> None:
        super().__init__(intents=discord.Intents.default())
        # Installed to servers only; the commands work there and in DMs with the bot.
        self.tree = AccessTree(
            self,
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=False),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        )
        self.tree.error(self.on_tree_error)
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self.store = store
        self.scan_models = scan_models
        self.matcher = matcher
        # A scan holds its thread for seconds to minutes. On asyncio's default pool
        # (as few as 5 threads) a handful of scans left every command's first database
        # read, and aiohttp's DNS lookups, queued past Discord's 3 s reply deadline.
        # Scans get a small pool of their own; SQLite gets one thread (Store
        # serializes on a lock anyway), so database work never waits behind a scan.
        self._scan_pool = ThreadPoolExecutor(max_workers=SCAN_THREADS, thread_name_prefix="scan-run")
        self._db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self._eval_lock = asyncio.Lock()
        self._unsaved: dict[int, dict] = {}   # alert id -> state of a sent ping not yet saved
        self._prices_lock = asyncio.Lock()
        self._prices_scan_at = 0.0
        self._last_prune = 0.0
        self._runs = 0   # background runs so far, to rotate the scan order
        self._fail_streak: dict[str, int] = defaultdict(int)
        self._ok_streak: dict[str, int] = defaultdict(int)
        self._owner_warned: set[str] = set()
        self._unreachable: set[int] = set()   # alerts the owner was told cannot be delivered
        self._members: dict[tuple[int, int], tuple[bool, float]] = {}   # (guild, user) -> (member, checked at)
        self.check_loop = tasks.loop(minutes=max(1, config.CHECK_INTERVAL_MINUTES))(self._check_tick)
        self.check_loop.before_loop(self._before_check)
        self._register_commands()

    # Lifecycle

    async def setup_hook(self) -> None:
        # First, so the link is there even when the command sync below fails: the
        # README has users start the bot before they have added it to their server.
        if self.application_id:
            url = discord.utils.oauth_url(
                self.application_id,
                permissions=discord.Permissions(INVITE_PERMISSIONS),
                scopes=("bot", "applications.commands"),
            )
            log.info("invite URL: %s", url)
        if not configured_guilds():
            log.info("DISCORD_GUILD_ID and ALLOWED_GUILD_IDS are unset: commands answer in the owner's servers")
        if self.store is None:
            self.store = await self._db(Store, self.db_path)
        # A failed sync leaves the commands Discord already has; the background check
        # still starts, so existing alerts keep going out.
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=config.DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                log.error(
                    "the bot is not in guild %s yet: open the invite URL, then restart", config.DISCORD_GUILD_ID
                )
                synced = None
            except discord.HTTPException:
                log.error("could not sync commands to guild %s", config.DISCORD_GUILD_ID, exc_info=True)
                synced = None
            # Commands registered globally earlier (by v1, or by a run without
            # DISCORD_GUILD_ID) would show up a second time next to the guild copies.
            # Syncing an empty global tree removes them; the guild copies made above
            # keep serving every command. In guild mode the commands therefore work
            # only in that server, not in DMs. Unset DISCORD_GUILD_ID to go back to
            # global commands (the guild copies then stay until removed in Discord).
            # After a failed guild sync the global ones are all there is, so they stay.
            if synced is not None:
                log.info("synced %d commands to guild %s", len(synced), config.DISCORD_GUILD_ID)
                self.tree.clear_commands(guild=None)
                try:
                    await self.tree.sync()
                    log.info("cleared stale global commands")
                except discord.HTTPException:
                    log.warning("could not clear stale global commands", exc_info=True)
        else:
            try:
                synced = await self.tree.sync()
                log.info("synced %d global commands (can take up to an hour to appear)", len(synced))
            except discord.HTTPException:
                log.error("could not sync commands; Discord keeps the ones from the last sync", exc_info=True)
        from . import retailers

        log.info(
            "checking prices every %d minutes at: %s",
            self.check_loop.minutes, ", ".join(retailers.enabled_ids()) or "no retailer (all disabled)",
        )
        self.check_loop.start()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def close(self) -> None:
        self.check_loop.cancel()
        await super().close()
        self._scan_pool.shutdown(wait=False, cancel_futures=True)
        self._db_pool.shutdown(wait=True)   # lets a write in progress finish
        if self.store is not None:
            self.store.close()

    async def _before_check(self) -> None:
        await self.wait_until_ready()

    async def _check_tick(self) -> None:
        try:
            await self.run_checks()
        except Exception:
            log.exception("background check failed")

    # Helpers

    def _store(self) -> Store:
        if self.store is None:
            raise RuntimeError("store is not open")
        return self.store

    async def _db(self, fn: Callable, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self._db_pool, functools.partial(fn, *args, **kwargs))

    async def _scan(self, models: list[GPUModel]) -> dict[str, ModelScan]:
        return await asyncio.get_running_loop().run_in_executor(self._scan_pool, self.scan_models, models)

    def _offers(self, scan: ModelScan, policy: Policy) -> list[Listing]:
        return pricing.offers(scan.model, scan.results, policy, self.matcher)

    async def record(self, scans: dict[str, ModelScan]) -> None:
        """Store scrape runs once per distinct query and the matched offers per model."""
        store = self._store()
        seen: set[int] = set()
        for scan in scans.values():
            if id(scan.results) not in seen:
                seen.add(id(scan.results))
                try:
                    await self._db(store.record_runs, scan.results, scan.scanned_at)
                except Exception:
                    log.exception("could not record scrape runs for %s", scan.model.id)
            try:
                found = await asyncio.to_thread(self._offers, scan, RECORD_POLICY)
                await self._db(
                    store.record_scan, scan.model.id, found,
                    n_ok=scan.n_ok, n_retailers=n_active(scan), at=scan.scanned_at,
                )
            except Exception:
                log.exception("could not record scan for %s", scan.model.id)

    @staticmethod
    def _model_arg(value: str) -> GPUModel | None:
        return catalog.by_id(value) or catalog.resolve(value)

    async def _reply(self, interaction: discord.Interaction, content: str | None = None, **kwargs) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, **kwargs)
            else:
                await interaction.response.send_message(content, **kwargs)
        except discord.HTTPException:
            log.warning("could not reply to /%s", getattr(interaction.command, "name", "?"), exc_info=True)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Please wait {error.retry_after:.0f} s before using this command again."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "You cannot use this command here."
        else:
            name = getattr(interaction.command, "qualified_name", "?")
            original = getattr(error, "original", error)
            log.error("/%s failed", name, exc_info=(type(original), original, original.__traceback__))
            msg = GENERIC_ERROR
        await self._reply(interaction, msg, ephemeral=True)

    # Access. Every scan goes out from the owner's connection, so the commands answer
    # only in DISCORD_GUILD_ID and ALLOWED_GUILD_IDS or, with neither set, in servers
    # the application owner is in; DMs only for members of one of those servers.
    # /list and /remove answer everyone: alerts made before a server lost access (or
    # before this gate existed) are still checked and still ping until removed.

    async def check_access(self, interaction: discord.Interaction) -> bool:
        if getattr(interaction.command, "name", None) in OWN_ALERT_COMMANDS:
            return True
        try:
            allowed = await self._allowed(interaction.guild_id, interaction.user.id)
        except discord.HTTPException:
            log.warning("could not check access for user %s", interaction.user.id, exc_info=True)
            allowed = False
        if allowed:
            return True
        log.info("refused user %s in guild %s", interaction.user.id, interaction.guild_id)
        try:
            # Returning False alone sends nothing, and Discord shows "did not respond".
            if interaction.type is discord.InteractionType.autocomplete:
                await interaction.response.autocomplete([])
            else:
                await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
        except discord.HTTPException:
            log.warning("could not send the refusal", exc_info=True)
        return False

    async def _allowed(self, guild_id: int | None, user_id: int) -> bool:
        configured = configured_guilds()
        if guild_id is not None:
            return await self._guild_allowed(guild_id, configured, user_id)
        for gid in configured or [g.id for g in self.guilds]:
            if await self._guild_allowed(gid, configured, user_id) and await self._member(gid, user_id):
                return True
        return False

    async def _guild_allowed(self, guild_id: int, configured: set[int], user_id: int) -> bool:
        if configured:
            return guild_id in configured
        owner = await self._owner()
        return owner is not None and (owner.id == user_id or await self._member(guild_id, owner.id))

    async def _member(self, guild_id: int, user_id: int) -> bool:
        """_is_member, remembered for ACCESS_CACHE_S."""
        hit = self._members.get((guild_id, user_id))
        if hit is None or time.monotonic() - hit[1] >= ACCESS_CACHE_S:
            hit = (await self._is_member(guild_id, user_id), time.monotonic())
            self._members[(guild_id, user_id)] = hit
        return hit[0]

    async def _is_member(self, guild_id: int, user_id: int) -> bool:
        guild = self.get_guild(guild_id)
        if guild is None:
            return False   # the bot itself is not in that server
        if guild.get_member(user_id) is not None:
            return True
        try:
            await guild.fetch_member(user_id)   # one API call; needs no members intent
        except discord.NotFound:
            return False
        return True

    async def _owner(self) -> discord.abc.User | discord.TeamMember | None:
        app = self.application or await self.application_info()
        return app.team.owner if app.team and app.team.owner else app.owner

    # Alert evaluation and delivery

    async def evaluate(self, alert_id: int, scan: ModelScan, *, deliver: bool = True) -> Evaluation | None:
        """Run the state machine for one alert against one scan and persist the outcome.

        With deliver=False the caller shows the result itself (as /check does), so a
        notification counts as delivered. Returns None when the alert is gone, its id
        now belongs to another alert, or the state of its last ping cannot be saved yet.
        """
        store = self._store()
        async with self._eval_lock:
            if not await self._save_unsaved(alert_id):
                return None
            alert = await self._db(store.get_alert, alert_id)
            # SQLite gives a removed alert's id to the next new alert, which can be
            # another GPU (and another user) than the one this scan is for.
            if alert is None or alert.model_id != scan.model.id:
                return None
            model = scan.model
            found = await asyncio.to_thread(self._offers, scan, policy_for(alert))
            best = found[0] if found else None
            price = to_cents(best.price) if best and scan.n_ok else None
            # Retailers that answered completely (ok or empty, nothing left unread). None
            # of them: no evidence for re-arming. Some of them with nothing eligible (sold
            # out): counts toward re-arming. A partial read still adds its offers above.
            answered = {r.retailer for r in scan.results if r.ok and not r.partial}
            under = {li.retailer for li in found if to_cents(li.price) <= alert.target_cents}
            active = {r.retailer for r in scan.results if Status(r.status) is not Status.DISABLED}
            # A retailer turned off in config never answers again; it must not hold
            # the alert disarmed forever.
            state = AlertState(
                alert.target_cents, alert.armed, alert.notified_price_cents, alert.above_streak,
                alert.notified_retailer, alert.holding_retailers & active, alert.held_streak,
            )
            # Change nothing when no retailer answered (nothing was seen, so the last
            # price and its check time stay) or a newer scan already decided this alert
            # (a /check during a background run that read the prices earlier).
            if not scan.n_ok or (alert.observed_at is not None and scan.scanned_at <= alert.observed_at):
                return Evaluation(alert, model, best, Decision(False, UNCHANGED, state), True)
            decision = decide(state, price, best.retailer if price is not None else None, answered, under)
            delivered = not decision.notify or not deliver
            if decision.notify and deliver and best is not None:
                stats = await self._db(
                    store.price_stats, model.id, 30,
                    include_open_box=alert.include_open_box, include_marketplace=alert.include_marketplace,
                )
                delivered = await self.deliver(alert, alert_embed(alert, model, best, found, stats, decision.kind))
            now = utcnow()
            last_price = price   # None when retailers answered but nothing was eligible
            if delivered:
                new = decision.new_state
                fields = dict(
                    armed=new.armed, notified_price_cents=new.notified_price_cents,
                    notified_at=now if decision.notify else alert.notified_at,
                    last_price_cents=last_price, last_checked_at=now,
                    expected_target_cents=alert.target_cents,
                    above_streak=new.above_streak,
                    notified_retailer=new.notified_retailer,
                    holding_retailers=new.holding_retailers, held_streak=new.held_streak,
                    observed_at=scan.scanned_at,
                    expected_rev=alert.rev,
                    expected_user_id=alert.user_id, expected_model_id=alert.model_id,
                )
                try:
                    await self._db(store.update_alert_state, alert.id, **fields)
                except Exception:
                    if decision.notify and deliver:
                        # The ping goes out before the state is saved, so a failed send
                        # is retried next run rather than lost. When the save fails
                        # instead (database locked, disk full), keep the state here and
                        # save it before this alert is judged again, so the deal is not
                        # pinged every run. A restart in between repeats the ping once.
                        self._unsaved[alert.id] = fields
                    raise
            else:
                # Keep the alert armed so the next run tries again.
                await self._db(
                    store.touch_alert, alert.id, last_price, now,
                    expected_user_id=alert.user_id, expected_model_id=alert.model_id,
                )
            return Evaluation(alert, model, best, decision, delivered)

    async def _save_unsaved(self, alert_id: int) -> bool:
        """Save the state of a ping that went out but could not be saved. Returns False
        while it still cannot be saved. The write keeps its guards, so a user edit made
        since (which re-arms the alert) wins and the kept state is dropped."""
        fields = self._unsaved.pop(alert_id, None)
        if fields is None:
            return True
        try:
            await self._db(self._store().update_alert_state, alert_id, **fields)
        except Exception:
            self._unsaved[alert_id] = fields
            log.warning("alert %s: the state of a sent ping still cannot be saved", alert_id, exc_info=True)
            return False
        return True

    async def _undo_notification(self, ev: Evaluation) -> None:
        try:
            async with self._eval_lock:
                await self._db(
                    self._store().undo_notification, ev.alert, ev.decision.new_state.notified_price_cents
                )
        except Exception:
            log.exception("alert %s: could not undo an undelivered /check notification", ev.alert.id)

    async def deliver(self, alert: Alert, embed: discord.Embed) -> bool:
        """Send to the alert's channel, falling back to a DM. Returns True when sent."""
        mentions = discord.AllowedMentions(
            everyone=False, roles=False, replied_user=False, users=[discord.Object(id=alert.user_id)]
        )
        content = f"<@{alert.user_id}>"
        if alert.channel_id:
            channel = self.get_channel(alert.channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(alert.channel_id)
                except discord.HTTPException as exc:
                    log.info("alert %s: channel %s unavailable (%s), trying DM", alert.id, alert.channel_id, exc.status)
            if channel is not None and hasattr(channel, "send"):
                try:
                    await channel.send(content, embed=embed, allowed_mentions=mentions)
                    self._unreachable.discard(alert.id)
                    return True
                except discord.HTTPException as exc:
                    log.info("alert %s: send to channel failed (%s), trying DM", alert.id, exc.status)
        try:
            user = self.get_user(alert.user_id) or await self.fetch_user(alert.user_id)
            await user.send(embed=embed, allowed_mentions=mentions)
            self._unreachable.discard(alert.id)
            return True
        except discord.HTTPException:
            log.warning("alert %s: could not reach user %s by channel or DM", alert.id, alert.user_id)
        # The alert stays armed and is tried again every check, which reaches the user
        # once they open their DMs. Nothing else tells anyone, so tell the owner once.
        if alert.id not in self._unreachable:
            model = catalog.by_id(alert.model_id)
            if await self.dm_owner(
                f"An alert for {model.name if model else alert.model_id} cannot reach user {alert.user_id}: "
                "posting in its channel and a DM both failed. It is tried again every check."
            ):
                self._unreachable.add(alert.id)
        return False

    # Background check

    async def run_checks(self) -> None:
        store = self._store()
        alerts = await self._db(store.all_alerts)
        if time.monotonic() - self._last_prune > PRUNE_EVERY_S or not self._last_prune:
            self._last_prune = time.monotonic()
            try:
                removed = await self._db(store.prune, 180)
                if removed:
                    log.info("pruned %d old history rows", removed)
            except Exception:
                log.exception("prune failed")
        if not alerts:
            return
        models: list[GPUModel] = []
        for model_id in dict.fromkeys(a.model_id for a in alerts):
            model = catalog.by_id(model_id)
            if model is None:
                log.warning("alert model %r is not in the catalog; skipping", model_id)
            else:
                models.append(model)
        # A retailer that blocks partway through is skipped for the models after it
        # (scan_models). Starting one model later each run spreads that loss over every
        # alert instead of always costing the newest ones the same retailer.
        if models:
            k = self._runs % len(models)
            models = models[k:] + models[:k]
        self._runs += 1
        start = time.monotonic()
        scans = await self._scan(models)
        await self.record(scans)
        try:
            await self.watch_health(scans)
        except Exception:
            log.exception("health check failed")
        sent = 0
        for alert in alerts:
            scan = scans.get(alert.model_id)
            if scan is None:
                continue
            try:
                ev = await self.evaluate(alert.id, scan)
                if ev and ev.decision.notify and ev.delivered:
                    sent += 1
            except Exception:
                log.exception("evaluating alert %s failed", alert.id)
        log.info(
            "checked %d alerts over %d models in %.0f s, %d notifications",
            len(alerts), len(models), time.monotonic() - start, sent,
        )

    async def watch_health(self, scans: dict[str, ModelScan]) -> None:
        """Count background runs where a retailer failed every search; tell the owner at 3,
        and that it works again after 2 good runs, so a retailer that answers now and
        then does not send a pair of DMs every few hours. A DM that could not be sent
        is tried again on the next run."""
        per: dict[str, list[ScrapeResult]] = defaultdict(list)
        seen: set[int] = set()
        for scan in scans.values():
            if id(scan.results) in seen:
                continue
            seen.add(id(scan.results))
            for r in scan.results:
                if Status(r.status) is not Status.DISABLED:
                    per[r.retailer].append(r)
        for rid, results in per.items():
            if any(r.ok for r in results):
                self._fail_streak[rid] = 0
                self._ok_streak[rid] += 1
                if rid in self._owner_warned and self._ok_streak[rid] >= OWNER_RECOVERED_AFTER:
                    log.info("%s recovered", rid)
                    if await self.dm_owner(f"{retailer_name(rid)} is working again."):
                        self._owner_warned.discard(rid)
                continue
            self._ok_streak[rid] = 0
            self._fail_streak[rid] += 1
            if self._fail_streak[rid] >= OWNER_ALERT_AFTER and rid not in self._owner_warned:
                # The placeholders scan_models adds after a block carry no HTTP status.
                real = [r for r in results if not r.detail.startswith(pricing.SKIPPED_PREFIX)]
                last = (real or results)[-1]
                detail = f"{Status(last.status).value}" + (f" (HTTP {last.http_status})" if last.http_status else "")
                log.warning("%s failed %d runs in a row: %s %s", rid, self._fail_streak[rid], detail, last.detail)
                if await self.dm_owner(
                    f"{retailer_name(rid)} has failed {self._fail_streak[rid]} checks in a row: {detail}. "
                    "Run `python -m gpu_alerts health` on the bot PC for details."
                ):
                    self._owner_warned.add(rid)

    async def dm_owner(self, text: str) -> bool:
        """DM the application owner. Returns True when sent."""
        try:
            owner = await self._owner()
            if owner is not None:
                # A team owner is a TeamMember, which cannot be messaged; resolve a real User.
                user = self.get_user(owner.id) or await self.fetch_user(owner.id)
                await user.send(text)
                return True
        except Exception:
            log.warning("could not DM the application owner", exc_info=True)
        return False

    # Slash commands

    def _register_commands(self) -> None:
        tree = self.tree

        async def gpu_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            return [
                app_commands.Choice(name=m.name + (" (previous gen)" if m.legacy else ""), value=m.id)
                for m in catalog.search(current)[:25]
            ]

        async def own_alert_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            alerts = await self._db(self._store().list_alerts, interaction.user.id)
            # The same matching as the GPU list, so "RTX 5060 8GB" offers the plain RTX 5060.
            hits = {m.id for m in catalog.search(current, limit=len(catalog.load()))}
            words = current.lower().replace("-", " ").split()
            out = []
            for a in alerts:
                model = catalog.by_id(a.model_id)
                # A model since dropped from the catalog can still be found by its id, to remove it.
                if a.model_id in hits or (model is None and all(w in a.model_id.replace("-", " ") for w in words)):
                    name = model.name if model else a.model_id
                    out.append(app_commands.Choice(name=f"{name} (target {money(a.target_cents)})", value=a.model_id))
            return out[:25]

        unknown_gpu = "Unknown GPU. Pick one from the list that appears as you type."

        @tree.command(name="alert", description="Get a ping when a GPU drops to your target price")
        @app_commands.describe(
            gpu="GPU model (pick from the list)",
            target="Target price in CAD, before tax",
            open_box="Also count open-box and refurbished listings",
            marketplace="Also count third-party marketplace sellers",
        )
        @app_commands.autocomplete(gpu=gpu_autocomplete)
        async def alert_cmd(
            interaction: discord.Interaction,
            gpu: str,
            target: app_commands.Range[int, 100, 20000],
            open_box: bool = False,
            marketplace: bool = False,
        ) -> None:
            model = self._model_arg(gpu)
            if model is None:
                await self._reply(interaction, unknown_gpu, ephemeral=True)
                return
            store = self._store()
            # A ping in a channel the bot cannot post in would fail every check (slash
            # commands still work there), so such an alert goes straight to DMs.
            by_dm = interaction.guild_id is not None and not can_post(interaction)
            try:
                alert, created = await self._db(
                    store.upsert_alert, interaction.user.id, model.id, target * 100,
                    channel_id=None if by_dm else interaction.channel_id, guild_id=interaction.guild_id,
                    include_open_box=open_box, include_marketplace=marketplace,
                    max_per_user=MAX_ALERTS_PER_USER,
                )
            except AlertLimitError:
                await self._reply(
                    interaction,
                    f"You already have {MAX_ALERTS_PER_USER} alerts. Remove one with /remove first "
                    "(changing the target of an existing alert always works).",
                    ephemeral=True,
                )
                return
            policy = policy_for(alert)
            # Only a recent scan says what the next check will see; an older one may be
            # from a /lookup months ago.
            at, latest = await self._db(
                store.latest_offers, model.id, max_age=PRICES_FRESH,
                include_open_box=open_box, include_marketplace=marketplace,
            )
            stats = await self._db(
                store.price_stats, model.id, 30, include_open_box=open_box, include_marketplace=marketplace
            )
            verb = "Alert set" if created else "Alert updated"
            lines = [f"{verb}: **{model.name}** at or below **{money(alert.target_cents)}** ({policy_note(policy)})."]
            if by_dm:
                lines.append(
                    "I cannot post in this channel, so pings come by DM. Allow direct messages from "
                    "server members for this server, or set the alert again in a channel where I can post."
                )
            floor = to_cents(model.min_price)
            if alert.target_cents < floor:
                lines.append(
                    f"Listings under {money(floor)} are not counted for the {model.name} (that floor keeps out "
                    "cables and parts), so this target cannot be reached."
                )
            if latest:
                o = latest[0]
                lines.append(
                    f"Current best: {money(o.price_cents)} at {retailer_name(o.retailer)} "
                    f"(checked {discord.utils.format_dt(at, 'R')})."
                )
                if o.price_cents <= alert.target_cents:
                    lines.append("That is already at or under your target, so expect a ping on the next check.")
            elif at is not None:
                lines.append(f"Checked {discord.utils.format_dt(at, 'R')}: nothing eligible under these settings.")
            else:
                lines.append("No recent price yet; the next check will fetch one.")
            if stats.count:
                lines.append(f"30-day low: {money(stats.low_cents)}.")
            lines.append(f"Prices are checked every {max(1, config.CHECK_INTERVAL_MINUTES)} minutes.")
            await self._reply(interaction, "\n".join(lines), ephemeral=True)

        @tree.command(name="remove", description="Remove one of your price alerts")
        @app_commands.describe(gpu="One of your alerts")
        @app_commands.autocomplete(gpu=own_alert_autocomplete)
        async def remove_cmd(interaction: discord.Interaction, gpu: str) -> None:
            model = self._model_arg(gpu)
            model_id = model.id if model else gpu
            removed = await self._db(self._store().remove_alert, interaction.user.id, model_id)
            name = model.name if model else "that GPU"
            msg = f"Removed your alert for {name}." if removed else f"You have no alert for {name}."
            await self._reply(interaction, msg, ephemeral=True)

        @tree.command(name="list", description="Show your price alerts")
        async def list_cmd(interaction: discord.Interaction) -> None:
            alerts = await self._db(self._store().list_alerts, interaction.user.id)
            if not alerts:
                await self._reply(interaction, "You have no alerts. Add one with /alert.", ephemeral=True)
                return
            embed = discord.Embed(title="Your GPU alerts", color=COLOR_INFO, timestamp=discord.utils.utcnow())
            for a in alerts:
                model = catalog.by_id(a.model_id)
                state = (
                    "watching" if a.armed
                    else f"notified at {money(a.notified_price_cents)}, waiting for a further drop or a rise above target"
                )
                if a.last_checked_at is None:
                    seen = "not checked yet"
                elif a.last_price_cents is None:
                    seen = f"nothing eligible ({discord.utils.format_dt(a.last_checked_at, 'R')})"
                else:
                    seen = f"last price {money(a.last_price_cents)} ({discord.utils.format_dt(a.last_checked_at, 'R')})"
                extras = [x for x, on in (("open box", a.include_open_box), ("marketplace", a.include_marketplace)) if on]
                embed.add_field(
                    name=model.name if model else a.model_id,
                    value=(
                        f"Target {money(a.target_cents)}, {seen}\n"
                        f"{state}" + (f"\nIncludes: {', '.join(extras)}" if extras else "")
                    ),
                    inline=False,
                )
            await self._reply(interaction, embed=embed, ephemeral=True)

        @tree.command(name="check", description="Check prices for your alerts now")
        @app_commands.checks.cooldown(1, CHECK_COOLDOWN_S)
        async def check_cmd(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            store = self._store()
            alerts = await self._db(store.list_alerts, interaction.user.id)
            if not alerts:
                await self._reply(interaction, "You have no alerts. Add one with /alert.", ephemeral=True)
                return
            models = [m for m in (catalog.by_id(a.model_id) for a in alerts) if m]
            scans = await self._scan(models)
            await self.record(scans)
            embed = discord.Embed(title="Your alerts, checked now", color=COLOR_INFO, timestamp=discord.utils.utcnow())
            notified: list[Evaluation] = []
            try:
                for a in alerts:
                    scan = scans.get(a.model_id)
                    if scan is None:
                        continue
                    ev = await self.evaluate(a.id, scan, deliver=False)
                    if ev is None:
                        continue
                    if ev.decision.notify:
                        notified.append(ev)
                    if ev.best is None:
                        value = f"No eligible offer ({scan.n_ok} of {n_active(scan)} retailers answered)."
                    else:
                        value = (
                            f"{link(f'${ev.best.price:,.2f}', ev.best.url)} at {retailer_name(ev.best.retailer)}, "
                            f"target {money(ev.alert.target_cents)}"
                        )
                        if to_cents(ev.best.price) <= ev.alert.target_cents:
                            value += "\n**At or under your target.**"
                    embed.add_field(name=ev.model.name, value=value[:1024], inline=False)
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception:
                # evaluate saved these notifications as delivered by this reply, which
                # never arrived (e.g. the interaction token expired on a slow scan). Put
                # them back so the next background run pings instead.
                for ev in notified:
                    await self._undo_notification(ev)
                raise

        @tree.command(name="lookup", description="Live prices for one GPU across every retailer")
        @app_commands.describe(gpu="GPU model (pick from the list)")
        @app_commands.autocomplete(gpu=gpu_autocomplete)
        @app_commands.checks.cooldown(1, LOOKUP_COOLDOWN_S)
        async def lookup_cmd(interaction: discord.Interaction, gpu: str) -> None:
            model = self._model_arg(gpu)
            if model is None:
                await self._reply(interaction, unknown_gpu, ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            scans = await self._scan([model])
            await self.record(scans)
            scan = scans[model.id]
            found = await asyncio.to_thread(self._offers, scan, DEFAULT_POLICY)
            stats = await self._db(self._store().price_stats, model.id, 30)
            await interaction.followup.send(embed=lookup_embed(scan, found, stats))

        @tree.command(name="prices", description="Best current prices for popular GPUs")
        async def prices_cmd(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True)
            store = self._store()
            popular = [m for m in catalog.load() if m.popular and not m.legacy]
            note = ""
            async with self._prices_lock:
                fresh = {
                    m.id: await self._db(store.latest_offers, m.id, max_age=PRICES_FRESH) for m in popular
                }
                stale = [m for m in popular if fresh[m.id][0] is None]
                if stale:
                    if time.monotonic() - self._prices_scan_at >= PRICES_SCAN_COOLDOWN_S or not self._prices_scan_at:
                        self._prices_scan_at = time.monotonic()
                        await self.record(await self._scan(stale))
                        for m in stale:
                            fresh[m.id] = await self._db(store.latest_offers, m.id, max_age=PRICES_FRESH)
                    else:
                        note = "Some prices are missing; a fresh scan runs at most every 10 minutes."
            embed = discord.Embed(title="GPU prices in Canada", color=COLOR_INFO, timestamp=discord.utils.utcnow())
            for m in popular:
                at, offers = fresh[m.id]
                if at is None:
                    value = "no recent data"
                elif not offers:
                    value = f"no eligible offer (checked {discord.utils.format_dt(at, 'R')})"
                else:
                    o = offers[0]
                    value = (
                        f"{link(money(o.price_cents), o.url)} at {retailer_name(o.retailer)}\n"
                        f"checked {discord.utils.format_dt(at, 'R')}"
                    )
                embed.add_field(name=m.name, value=value, inline=True)
            embed.set_footer(text="New, sold by the retailer, not known to be out of stock. " + note)
            await interaction.followup.send(embed=embed)

        @tree.command(name="trend", description="Price history for one GPU")
        @app_commands.describe(gpu="GPU model (pick from the list)")
        @app_commands.autocomplete(gpu=gpu_autocomplete)
        async def trend_cmd(interaction: discord.Interaction, gpu: str) -> None:
            model = self._model_arg(gpu)
            if model is None:
                await self._reply(interaction, unknown_gpu, ephemeral=True)
                return
            store = self._store()
            windows = [await self._db(store.price_stats, model.id, d) for d in (7, 30, 90)]
            latest = windows[0]
            embed = discord.Embed(title=f"{model.name}: price history", color=COLOR_INFO, timestamp=discord.utils.utcnow())
            if latest.latest_at is None:
                embed.description = "No history yet. Prices are stored by alerts, /lookup and /prices."
            else:
                embed.description = (
                    f"Current best: **{money(latest.latest_cents)}** "
                    f"(checked {discord.utils.format_dt(latest.latest_at, 'R')})"
                )
                for stats in windows:
                    embed.add_field(name=f"Last {stats.days} days", value=stats_line(stats), inline=False)
            await self._reply(interaction, embed=embed)

        @tree.command(name="health", description="Scraper status per retailer")
        async def health_cmd(interaction: discord.Interaction) -> None:
            from . import retailers

            rows = {h.retailer: h for h in await self._db(self._store().retailer_health)}
            enabled = set(retailers.enabled_ids())
            lines = []
            for rid in retailers.RETAILERS:
                name = retailer_name(rid)
                if rid not in enabled:
                    lines.append(f"**{name}**: disabled in config")
                    continue
                h = rows.get(rid)
                if h is None:
                    lines.append(f"**{name}**: no runs yet")
                    continue
                text = f"**{name}**: {h.last_status}"
                if h.last_http_status and h.last_status != "ok":
                    text += f" (HTTP {h.last_http_status})"
                text += f", {discord.utils.format_dt(h.last_at, 'R')}"
                if h.consecutive_failures:
                    ok = discord.utils.format_dt(h.last_ok_at, "R") if h.last_ok_at else "never"
                    text += f", {h.consecutive_failures} failures in a row, last ok {ok}"
                lines.append(text)
            embed = discord.Embed(
                title="Retailer health", description="\n".join(lines), color=COLOR_INFO,
                timestamp=discord.utils.utcnow(),
            )
            await self._reply(interaction, embed=embed, ephemeral=True)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.warn_unknown_retailers()
    token = config.discord_token()
    GPUAlertBot().run(token, log_handler=None)
