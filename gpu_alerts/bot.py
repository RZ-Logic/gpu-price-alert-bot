"""The Discord bot: slash commands, the background price check and alert delivery."""

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from . import catalog, config, pricing
from .alerts import CROSSED, AlertState, Decision, decide
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
PRUNE_EVERY_S = 24 * 3600
INVITE_PERMISSIONS = 19456     # View Channel + Send Messages + Embed Links
GENERIC_ERROR = "Something went wrong on my side. It has been logged; please try again in a few minutes."
COLOR_OK = 0x2ECC71
COLOR_INFO = 0x3498DB
COLOR_WARN = 0xE67E22


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
    safe = url.replace(")", "%29").replace(" ", "%20")
    return f"[{label.replace('[', '(').replace(']', ')')}]({safe})"


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
        f"high {money(stats.high_cents)} over {stats.count} checks"
    )


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
    embed.set_footer(text=f"{scan.n_ok} of {len(scan.results)} retailers answered")
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
        self.tree = app_commands.CommandTree(self)
        self.tree.error(self.on_tree_error)
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self.store = store
        self.scan_models = scan_models
        self.matcher = matcher
        self._eval_lock = asyncio.Lock()
        self._prices_lock = asyncio.Lock()
        self._prices_scan_at = 0.0
        self._last_prune = 0.0
        self._fail_streak: dict[str, int] = defaultdict(int)
        self._owner_warned: set[str] = set()
        self.check_loop = tasks.loop(minutes=max(1, config.CHECK_INTERVAL_MINUTES))(self._check_tick)
        self.check_loop.before_loop(self._before_check)
        self._register_commands()

    # Lifecycle

    async def setup_hook(self) -> None:
        if self.store is None:
            self.store = await asyncio.to_thread(Store, self.db_path)
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=config.DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to guild %s", len(synced), config.DISCORD_GUILD_ID)
            # Commands registered globally earlier (by v1, or by a run without
            # DISCORD_GUILD_ID) would show up a second time next to the guild copies.
            # Syncing an empty global tree removes them; the guild copies made above
            # keep serving every command. In guild mode the commands therefore work
            # only in that server, not in DMs. Unset DISCORD_GUILD_ID to go back to
            # global commands (the guild copies then stay until removed in Discord).
            self.tree.clear_commands(guild=None)
            try:
                await self.tree.sync()
                log.info("cleared stale global commands")
            except discord.HTTPException:
                log.warning("could not clear stale global commands", exc_info=True)
        else:
            synced = await self.tree.sync()
            log.info("synced %d global commands (can take up to an hour to appear)", len(synced))
        self.check_loop.start()
        if self.application_id:
            url = discord.utils.oauth_url(
                self.application_id,
                permissions=discord.Permissions(INVITE_PERMISSIONS),
                scopes=("bot", "applications.commands"),
            )
            log.info("invite URL: %s", url)

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def close(self) -> None:
        self.check_loop.cancel()
        await super().close()
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
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _scan(self, models: list[GPUModel]) -> dict[str, ModelScan]:
        return await asyncio.to_thread(self.scan_models, models)

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
                n_active = sum(1 for r in scan.results if Status(r.status) is not Status.DISABLED)
                await self._db(
                    store.record_scan, scan.model.id, found,
                    n_ok=scan.n_ok, n_retailers=n_active, at=scan.scanned_at,
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

    # Alert evaluation and delivery

    async def evaluate(self, alert_id: int, scan: ModelScan, *, deliver: bool = True) -> Evaluation | None:
        """Run the state machine for one alert against one scan and persist the outcome.

        With deliver=False the caller shows the result itself (as /check does), so a
        notification counts as delivered.
        """
        store = self._store()
        async with self._eval_lock:
            alert = await self._db(store.get_alert, alert_id)
            if alert is None:
                return None
            model = catalog.by_id(alert.model_id) or scan.model
            found = await asyncio.to_thread(self._offers, scan, policy_for(alert))
            best = found[0] if found else None
            price = to_cents(best.price) if best and scan.n_ok else None
            # Retailers that answered (ok or empty). None of them: no change. Some of
            # them with nothing eligible (sold out): counts toward re-arming.
            answered = {r.retailer for r in scan.results if r.ok}
            active = {r.retailer for r in scan.results if Status(r.status) is not Status.DISABLED}
            # A retailer turned off in config never answers again; it must not hold
            # the alert disarmed forever, so forget it as the notified retailer.
            held_by = alert.notified_retailer if alert.notified_retailer in active else None
            state = AlertState(
                alert.target_cents, alert.armed, alert.notified_price_cents, alert.above_streak, held_by
            )
            decision = decide(state, price, best.retailer if price is not None else None, answered)
            delivered = not decision.notify or not deliver
            if decision.notify and deliver and best is not None:
                stats = await self._db(
                    store.price_stats, model.id, 30,
                    include_open_box=alert.include_open_box, include_marketplace=alert.include_marketplace,
                )
                delivered = await self.deliver(alert, alert_embed(alert, model, best, found, stats, decision.kind))
            now = utcnow()
            last_price = price if price is not None else alert.last_price_cents
            if delivered:
                new = decision.new_state
                await self._db(
                    store.update_alert_state, alert.id,
                    armed=new.armed, notified_price_cents=new.notified_price_cents,
                    notified_at=now if decision.notify else alert.notified_at,
                    last_price_cents=last_price, last_checked_at=now,
                    expected_target_cents=alert.target_cents,
                    above_streak=new.above_streak,
                    notified_retailer=new.notified_retailer,
                    expected_rev=alert.rev,
                )
            else:
                # Keep the alert armed so the next run tries again.
                await self._db(store.touch_alert, alert.id, last_price, now)
            return Evaluation(alert, model, best, decision, delivered)

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
                    return True
                except discord.HTTPException as exc:
                    log.info("alert %s: send to channel failed (%s), trying DM", alert.id, exc.status)
        try:
            user = self.get_user(alert.user_id) or await self.fetch_user(alert.user_id)
            await user.send(embed=embed, allowed_mentions=mentions)
            return True
        except discord.HTTPException:
            log.warning("alert %s: could not reach user %s by channel or DM", alert.id, alert.user_id)
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
        """Count background runs where a retailer failed every search; tell the owner at 3."""
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
                if rid in self._owner_warned:
                    self._owner_warned.discard(rid)
                    log.info("%s recovered", rid)
                    await self.dm_owner(f"{retailer_name(rid)} is working again.")
                continue
            self._fail_streak[rid] += 1
            if self._fail_streak[rid] >= OWNER_ALERT_AFTER and rid not in self._owner_warned:
                # The placeholders scan_models adds after a block carry no HTTP status.
                real = [r for r in results if not r.detail.startswith(pricing.SKIPPED_PREFIX)]
                last = (real or results)[-1]
                detail = f"{Status(last.status).value}" + (f" (HTTP {last.http_status})" if last.http_status else "")
                log.warning("%s failed %d runs in a row: %s %s", rid, self._fail_streak[rid], detail, last.detail)
                self._owner_warned.add(rid)
                await self.dm_owner(
                    f"{retailer_name(rid)} has failed {self._fail_streak[rid]} checks in a row: {detail}. "
                    "Run `python -m gpu_alerts health` on the bot PC for details."
                )

    async def dm_owner(self, text: str) -> None:
        try:
            app = self.application or await self.application_info()
            owner = app.team.owner if app.team and app.team.owner else app.owner
            if owner is not None:
                # A team owner is a TeamMember, which cannot be messaged; resolve a real User.
                user = self.get_user(owner.id) or await self.fetch_user(owner.id)
                await user.send(text)
        except Exception:
            log.warning("could not DM the application owner", exc_info=True)

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
            words = current.lower().replace("-", " ").split()
            out = []
            for a in alerts:
                model = catalog.by_id(a.model_id)
                name = model.name if model else a.model_id
                if all(w in f"{name.lower()} {a.model_id.replace('-', ' ')}" for w in words):
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
            try:
                alert, created = await self._db(
                    store.upsert_alert, interaction.user.id, model.id, target * 100,
                    channel_id=interaction.channel_id, guild_id=interaction.guild_id,
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
            at, latest = await self._db(
                store.latest_offers, model.id, include_open_box=open_box, include_marketplace=marketplace
            )
            stats = await self._db(
                store.price_stats, model.id, 30, include_open_box=open_box, include_marketplace=marketplace
            )
            verb = "Alert set" if created else "Alert updated"
            lines = [f"{verb}: **{model.name}** at or below **{money(alert.target_cents)}** ({policy_note(policy)})."]
            if latest:
                o = latest[0]
                lines.append(
                    f"Current best: {money(o.price_cents)} at {retailer_name(o.retailer)} "
                    f"(checked {discord.utils.format_dt(at, 'R')})."
                )
                if o.price_cents <= alert.target_cents:
                    lines.append("That is already at or under your target, so expect a ping on the next check.")
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
                checked = discord.utils.format_dt(a.last_checked_at, "R") if a.last_checked_at else "not checked yet"
                extras = [x for x, on in (("open box", a.include_open_box), ("marketplace", a.include_marketplace)) if on]
                embed.add_field(
                    name=model.name if model else a.model_id,
                    value=(
                        f"Target {money(a.target_cents)}, last price {money(a.last_price_cents)} ({checked})\n"
                        f"{state}" + (f"\nIncludes: {', '.join(extras)}" if extras else "")
                    ),
                    inline=False,
                )
            await self._reply(interaction, embed=embed, ephemeral=True)

        @tree.command(name="check", description="Check prices for your alerts now")
        @app_commands.checks.cooldown(1, CHECK_COOLDOWN_S)
        async def check_cmd(interaction: discord.Interaction) -> None:
            store = self._store()
            alerts = await self._db(store.list_alerts, interaction.user.id)
            if not alerts:
                await self._reply(interaction, "You have no alerts. Add one with /alert.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            models = [m for m in (catalog.by_id(a.model_id) for a in alerts) if m]
            scans = await self._scan(models)
            await self.record(scans)
            embed = discord.Embed(title="Your alerts, checked now", color=COLOR_INFO, timestamp=discord.utils.utcnow())
            for a in alerts:
                scan = scans.get(a.model_id)
                if scan is None:
                    continue
                ev = await self.evaluate(a.id, scan, deliver=False)
                if ev is None:
                    continue
                if ev.best is None:
                    value = f"No eligible offer ({scan.n_ok} of {len(scan.results)} retailers answered)."
                else:
                    value = (
                        f"{link(f'${ev.best.price:,.2f}', ev.best.url)} at {retailer_name(ev.best.retailer)}, "
                        f"target {money(ev.alert.target_cents)}"
                    )
                    if to_cents(ev.best.price) <= ev.alert.target_cents:
                        value += "\n**At or under your target.**"
                embed.add_field(name=ev.model.name, value=value[:1024], inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

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
    token = config.discord_token()
    GPUAlertBot().run(token, log_handler=None)
