"""
GPU Price Alert Discord Bot — Canadian Market
==============================================
Scrapes Newegg.ca, Walmart.ca, Amazon.ca, and Canada Computers for real-time GPU prices.
Zero API costs. Zero subscriptions.

Setup:
  pip install discord.py requests beautifulsoup4 curl_cffi
  set DISCORD_BOT_TOKEN=your-bot-token        (Windows)
  export DISCORD_BOT_TOKEN=your-bot-token     (Mac/Linux)
  python gpu_discord_bot.py
"""

import os
import json
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cureq

# ── CONFIG ────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHECK_INTERVAL_HOURS = 1
ALERTS_FILE = Path("gpu_alerts.json")
PRICE_HISTORY_FILE = Path("gpu_price_history.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-CA,en;q=0.9",
}

GPU_SEARCH_MAP = {
    # ── NVIDIA RTX 50 Series ──
    "RTX 5090": "rtx+5090",
    "RTX 5080": "rtx+5080",
    "RTX 5070 TI": "rtx+5070+ti",
    "RTX 5070": "rtx+5070",
    "RTX 5060 TI": "rtx+5060+ti",
    "RTX 5060": "rtx+5060",
    "RTX 5050": "rtx+5050",
    # ── NVIDIA RTX 40 Series ──
    "RTX 4090": "rtx+4090",
    "RTX 4080 SUPER": "rtx+4080+super",
    "RTX 4080": "rtx+4080",
    "RTX 4070 TI SUPER": "rtx+4070+ti+super",
    "RTX 4070 TI": "rtx+4070+ti",
    "RTX 4070 SUPER": "rtx+4070+super",
    "RTX 4070": "rtx+4070",
    "RTX 4060 TI": "rtx+4060+ti",
    "RTX 4060": "rtx+4060",
    # ── AMD Radeon RX 9000 Series ──
    "RX 9070 XT": "rx+9070+xt",
    "RX 9070": "rx+9070",
    "RX 9060 XT": "rx+9060+xt",
    # ── AMD Radeon RX 7000 Series ──
    "RX 7900 XTX": "rx+7900+xtx",
    "RX 7900 XT": "rx+7900+xt",
    "RX 7800 XT": "rx+7800+xt",
    "RX 7700 XT": "rx+7700+xt",
    "RX 7600 XT": "rx+7600+xt",
    "RX 7600": "rx+7600",
    # ── Intel Arc ──
    "ARC B580": "arc+b580",
    "ARC B570": "arc+b570",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gpu-bot")


# ── PERSISTENCE ───────────────────────────────────────────────────
def load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default or {}


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


def load_alerts() -> dict:
    return load_json(ALERTS_FILE)


def save_alerts(alerts: dict):
    save_json(ALERTS_FILE, alerts)


def record_price(gpu: str, price: float, retailer: str):
    history = load_json(PRICE_HISTORY_FILE, {})
    if gpu not in history:
        history[gpu] = []
    history[gpu].append({
        "price": price,
        "retailer": retailer,
        "timestamp": datetime.utcnow().isoformat(),
    })
    history[gpu] = history[gpu][-500:]
    save_json(PRICE_HISTORY_FILE, history)


def get_price_trend(gpu: str) -> str:
    history = load_json(PRICE_HISTORY_FILE, {})
    entries = history.get(gpu, [])
    if len(entries) < 2:
        return "Not enough data yet"
    prices = [e["price"] for e in entries[-10:]]
    low, high, latest, prev = min(prices), max(prices), prices[-1], prices[-2]
    arrow = "📉 Falling" if latest < prev else "📈 Rising" if latest > prev else "➡️ Stable"
    return f"{arrow} • Range: ${low:,.0f}–${high:,.0f} (last {len(prices)} checks)"


# ── SCRAPERS ──────────────────────────────────────────────────────

# Keywords that indicate a result is NOT a GPU
JUNK_KEYWORDS = [
    "RISER", "CABLE", "ADAPTER", "BRACKET", "BRIDGE", "MOUNT", "STAND",
    "CASE", "ENCLOSURE", "DOCK", "HUB", "FAN", "COOLER", "HEATSINK",
    "BACKPLATE", "SHROUD", "COVER", "SKIN", "STICKER", "DECAL",
    "POWER SUPPLY", "PSU", "MOTHERBOARD", "CPU", "RAM", "SSD",
    "MOUSE", "KEYBOARD", "MONITOR", "LAPTOP", "NOTEBOOK",
    "(NO GPU)", "WITHOUT GPU", "NO CARD", "DUMMY", "PLACEHOLDER",
]

# Keywords that confirm a result IS a GPU
GPU_KEYWORDS = [
    "GRAPHICS CARD", "GRAPHIC CARD", "VIDEO CARD", "GPU",
    "GDDR6", "GDDR6X", "GDDR7", "GEFORCE", "RADEON", "ARC",
    "GAMING", "OC", "WINDFORCE", "STRIX", "TUF", "AORUS",
    "VENTUS", "EAGLE", "MASTER", "SUPRIM", "NITRO", "PULSE",
    "DUAL", "TRIO", "HELLHOUND", "PHANTOM", "CHALLENGER",
]


def is_valid_gpu(title: str, price: float) -> bool:
    """Filter out accessories, cables, and other non-GPU results."""
    t = title.upper()
    # Reject if title contains junk keywords
    if any(junk in t for junk in JUNK_KEYWORDS):
        return False
    # Must contain at least one GPU-confirming keyword
    if not any(kw in t for kw in GPU_KEYWORDS):
        return False
    # Price sanity check — real GPUs are $200+
    if price < 200:
        return False
    return True


def scrape_newegg(gpu_name: str) -> list[dict]:
    """Scrape Newegg.ca — returns list of {name, price, url, retailer}."""
    gpu_key = gpu_name.upper().strip()
    query = GPU_SEARCH_MAP.get(gpu_key, gpu_name.lower().replace(" ", "+"))
    # N=4131 filters to "Desktop Graphics Cards" category
    url = f"https://www.newegg.ca/p/pl?d={query}&N=4131"
    log.info(f"Newegg: {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Newegg request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for item in soup.select("div.item-cell"):
        try:
            title_el = item.select_one("a.item-title")
            price_el = item.select_one("li.price-current")
            if not title_el or not price_el:
                continue

            title = title_el.get_text(strip=True)

            # Must contain all words from GPU name
            title_upper = title.upper()
            if not all(w in title_upper for w in gpu_key.split()):
                continue

            price_text = price_el.get_text(strip=True)
            price_match = re.search(r"([\d,]+)\.\d{2}", price_text)
            if not price_match:
                continue
            price = float(price_match.group(0).replace(",", ""))

            if not is_valid_gpu(title, price):
                continue

            link = title_el.get("href", "")

            results.append({
                "name": title[:100],
                "price": price,
                "url": link,
                "retailer": "Newegg.ca",
            })
        except Exception as e:
            log.debug(f"Newegg parse error: {e}")

    results.sort(key=lambda x: x["price"])
    return results


def scrape_walmart(gpu_name: str) -> list[dict]:
    """Scrape Walmart.ca via __NEXT_DATA__ JSON — 100% reliable."""
    gpu_key = gpu_name.upper().strip()
    query = gpu_name.lower().replace(" ", "+") + "+graphics+card"
    url = f"https://www.walmart.ca/search?q={query}"
    log.info(f"Walmart: {url}")

    try:
        resp = cureq.get(url, impersonate="chrome", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Walmart request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    if not script:
        log.error("Walmart: no __NEXT_DATA__ found")
        return []

    try:
        data = json.loads(script.string)
        items = data["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"][0]["items"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.error(f"Walmart JSON parse error: {e}")
        return []

    results = []
    for item in items:
        try:
            name = item.get("name", "")
            price = item.get("price", 0)
            if not name or not price or price < 100:
                continue

            # Must contain GPU name words
            if not all(w in name.upper() for w in gpu_key.split()):
                continue

            if not is_valid_gpu(name, float(price)):
                continue

            # Build product URL
            canonical = item.get("canonicalUrl", "")
            product_url = item.get("productPageUrl", canonical)
            if product_url and not product_url.startswith("http"):
                product_url = f"https://www.walmart.ca{product_url}"

            results.append({
                "name": name[:100],
                "price": float(price),
                "url": product_url,
                "retailer": "Walmart.ca",
            })
        except Exception as e:
            log.debug(f"Walmart item parse error: {e}")

    results.sort(key=lambda x: x["price"])
    return results


def scrape_amazon(gpu_name: str) -> list[dict]:
    """Scrape Amazon.ca via curl_cffi — returns list of {name, price, url, retailer}."""
    gpu_key = gpu_name.upper().strip()
    query = gpu_name.lower().replace(" ", "+") + "+graphics+card"
    url = f"https://www.amazon.ca/s?k={query}"
    log.info(f"Amazon: {url}")

    try:
        resp = cureq.get(url, impersonate="chrome", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Amazon request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    cards = soup.select("div[data-component-type='s-search-result']")
    for card in cards:
        try:
            title_el = card.select_one("h2 a, h2 span, span.a-text-normal")
            price_whole = card.select_one("span.a-price-whole")
            price_frac = card.select_one("span.a-price-fraction")
            link_el = card.select_one("h2 a, a.a-link-normal.s-no-outline")

            if not title_el or not price_whole:
                continue

            title = title_el.get_text(strip=True)

            # Must contain GPU name words
            if not all(w in title.upper() for w in gpu_key.split()):
                continue

            whole = price_whole.get_text(strip=True).replace(",", "").replace(".", "")
            frac = price_frac.get_text(strip=True) if price_frac else "00"
            price = float(f"{whole}.{frac}")
            if price < 100:
                continue

            if not is_valid_gpu(title, price):
                continue

            href = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = f"https://www.amazon.ca{href}"

            results.append({
                "name": title[:100],
                "price": price,
                "url": href,
                "retailer": "Amazon.ca",
            })
        except Exception as e:
            log.debug(f"Amazon parse error: {e}")

    results.sort(key=lambda x: x["price"])
    return results


def scrape_canada_computers(gpu_name: str) -> list[dict]:
    """Scrape Canada Computers via hidden AJAX JSON endpoint."""
    gpu_key = gpu_name.upper().strip()
    query = gpu_name.lower().replace(" ", "+")
    url = f"https://www.canadacomputers.com/en/search?s={query}&t=1&page=1&ajax=true&onlyproducts=1"
    log.info(f"Canada Computers: {url}")

    try:
        resp = cureq.get(url, impersonate="chrome", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Canada Computers request failed: {e}")
        return []

    try:
        data = json.loads(resp.text)
        products = data.get("products", [])
    except (json.JSONDecodeError, KeyError) as e:
        log.error(f"Canada Computers JSON parse error: {e}")
        return []

    results = []
    for item in products:
        try:
            name = item.get("name", "")
            price_str = item.get("price", "")
            product_url = item.get("url", "")

            if not name or not price_str:
                continue

            # Parse price — comes as "$1,809.99"
            price_match = re.search(r"([\d,]+\.\d{2})", str(price_str))
            if not price_match:
                continue
            price = float(price_match.group(1).replace(",", ""))

            # Must contain GPU name words
            if not all(w in name.upper() for w in gpu_key.split()):
                continue

            if not is_valid_gpu(name, price):
                continue

            results.append({
                "name": name[:100],
                "price": price,
                "url": product_url,
                "retailer": "Canada Computers",
            })
        except Exception as e:
            log.debug(f"Canada Computers item parse error: {e}")

    results.sort(key=lambda x: x["price"])
    return results


def get_all_prices(gpu_name: str) -> list[dict]:
    """Scrape all retailers and return combined sorted results."""
    all_results = []
    all_results.extend(scrape_newegg(gpu_name))
    all_results.extend(scrape_walmart(gpu_name))
    all_results.extend(scrape_amazon(gpu_name))
    all_results.extend(scrape_canada_computers(gpu_name))
    all_results.sort(key=lambda x: x["price"])
    return all_results


def get_best_price(gpu_name: str) -> dict | None:
    """Get the lowest price across all retailers."""
    results = get_all_prices(gpu_name)
    if results:
        best = results[0]
        record_price(gpu_name.upper().strip(), best["price"], best["retailer"])
        return best
    return None


# ── DISCORD BOT ───────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

C_RED, C_GREEN, C_BLUE, C_GRAY = 0xE63946, 0x2A9D4A, 0x4A90D9, 0x555555


def embed(title, desc="", color=C_RED):
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text="GPU Price Monitor 🇨🇦 • Newegg + Walmart + Amazon + Canada Computers • CAD")
    e.timestamp = datetime.utcnow()
    return e


async def gpu_autocomplete(interaction, current: str):
    return [
        app_commands.Choice(name=g, value=g)
        for g in GPU_SEARCH_MAP if current.upper() in g
    ][:10]


# ── SLASH COMMANDS ────────────────────────────────────────────────
@tree.command(name="alert", description="Set a price alert for a GPU (CAD)")
@app_commands.describe(gpu="GPU name (e.g. RTX 5090)", target="Target price in CAD")
@app_commands.autocomplete(gpu=gpu_autocomplete)
async def cmd_alert(interaction: discord.Interaction, gpu: str, target: int):
    alerts = load_alerts()
    uid = str(interaction.user.id)
    if uid not in alerts:
        alerts[uid] = {}
    if len(alerts[uid]) >= 10:
        return await interaction.response.send_message(
            embed=embed("❌ Limit Reached", "Max 10 alerts. `/remove` one first."), ephemeral=True)

    key = gpu.upper().strip()
    alerts[uid][key] = {
        "target": target, "channel_id": interaction.channel_id,
        "last_price": None, "last_check": None, "created": datetime.utcnow().isoformat(),
    }
    save_alerts(alerts)
    await interaction.response.send_message(embed=embed(
        "✅ Alert Set",
        f"**{key}** → alert below **${target:,} CAD**\n"
        f"Checking 4 Canadian retailers every {CHECK_INTERVAL_HOURS}h.\n"
        f"I'll ping you here when I find a deal.",
        C_GREEN), ephemeral=True)


@tree.command(name="remove", description="Remove a GPU price alert")
@app_commands.describe(gpu="GPU name to remove")
@app_commands.autocomplete(gpu=gpu_autocomplete)
async def cmd_remove(interaction: discord.Interaction, gpu: str):
    alerts = load_alerts()
    uid, key = str(interaction.user.id), gpu.upper().strip()
    if uid in alerts and key in alerts[uid]:
        del alerts[uid][key]
        if not alerts[uid]:
            del alerts[uid]
        save_alerts(alerts)
        await interaction.response.send_message(embed=embed("🗑️ Removed", f"**{key}** removed."), ephemeral=True)
    else:
        await interaction.response.send_message(
            embed=embed("❌ Not Found", f"No alert for **{key}**. `/list` to see yours."), ephemeral=True)


@tree.command(name="list", description="Show your active alerts")
async def cmd_list(interaction: discord.Interaction):
    alerts = load_alerts()
    ua = alerts.get(str(interaction.user.id), {})
    if not ua:
        return await interaction.response.send_message(
            embed=embed("📋 No Alerts", "Use `/alert RTX 5090 3500` to start."), ephemeral=True)
    lines = [
        f"**{g}** • target: **${d['target']:,}** • last: "
        + (f"${d['last_price']:,.0f}" if d.get("last_price") else "—")
        for g, d in ua.items()
    ]
    await interaction.response.send_message(embed=embed("📋 Your Alerts", "\n".join(lines), C_BLUE), ephemeral=True)


@tree.command(name="check", description="Force immediate price check")
async def cmd_check(interaction: discord.Interaction):
    alerts = load_alerts()
    uid = str(interaction.user.id)
    ua = alerts.get(uid, {})
    if not ua:
        return await interaction.response.send_message(
            embed=embed("❌ No Alerts", "Set one first with `/alert`."), ephemeral=True)

    await interaction.response.send_message(
        embed=embed("🔍 Scanning 4 retailers...", f"Checking {len(ua)} GPU(s)..."))

    for key, data in ua.items():
        try:
            results = await asyncio.to_thread(get_all_prices, key)
            if not results:
                await interaction.channel.send(embed=embed(f"⚠️ {key}", "No listings found.", C_GRAY))
                continue

            best = results[0]
            price = best["price"]
            data["last_price"] = price
            data["last_check"] = datetime.utcnow().isoformat()
            record_price(key, price, best["retailer"])
            trend = get_price_trend(key)

            # Show best price + other retailers
            other_prices = ""
            seen_retailers = {best["retailer"]}
            for r in results[1:5]:
                if r["retailer"] not in seen_retailers:
                    other_prices += f"\n{r['retailer']}: **${r['price']:,.2f}** — {r['name'][:50]}"
                    seen_retailers.add(r["retailer"])

            if price <= data["target"]:
                e = embed(f"🚨 DEAL — {key}",
                    f"**${price:,.2f} CAD** at **{best['retailer']}**\n"
                    f"Target: ${data['target']:,} ✅\n"
                    f"{best['name'][:80]}\n"
                    f"[Buy Now]({best['url']})"
                    f"{other_prices}\n\n{trend}", C_RED)
                await interaction.channel.send(f"<@{uid}>", embed=e)
            else:
                diff = price - data["target"]
                e = embed(f"📊 {key} — ${price:,.2f} CAD",
                    f"**${diff:,.0f} above** target of ${data['target']:,}\n"
                    f"Best: **{best['retailer']}** — {best['name'][:60]}\n"
                    f"[Link]({best['url']})"
                    f"{other_prices}\n\n{trend}", C_GRAY)
                await interaction.channel.send(embed=e)
            await asyncio.sleep(3)
        except Exception as e:
            log.error(f"Error checking {key}: {e}")
            await interaction.channel.send(embed=embed(f"⚠️ {key}", f"Error: {e}", C_GRAY))

    save_alerts(alerts)


@tree.command(name="lookup", description="Check current price for any GPU — no alert needed")
@app_commands.describe(gpu="GPU name (e.g. RTX 5070 TI)")
@app_commands.autocomplete(gpu=gpu_autocomplete)
async def cmd_lookup(interaction: discord.Interaction, gpu: str):
    key = gpu.upper().strip()
    await interaction.response.send_message(
        embed=embed("🔍 Looking up...", f"Searching **{key}** on 4 retailers + Amazon.ca..."))

    try:
        results = await asyncio.to_thread(get_all_prices, key)
        if not results:
            await interaction.channel.send(embed=embed(f"⚠️ {key}", "No listings found.", C_GRAY))
            return

        best = results[0]
        record_price(key, best["price"], best["retailer"])
        trend = get_price_trend(key)

        retailer_prices = {}
        for r in results:
            if r["retailer"] not in retailer_prices:
                retailer_prices[r["retailer"]] = r
        summary = "\n".join(
            f"**{rp['retailer']}**: ${rp['price']:,.2f} — {rp['name'][:50]}"
            for rp in retailer_prices.values()
        )

        e = embed(f"💰 {key} — from ${best['price']:,.2f} CAD",
            f"{best['name'][:70]}\n[View]({best['url']})\n\n{summary}\n\n{trend}", C_BLUE)
        await interaction.channel.send(embed=e)
    except Exception as ex:
        log.error(f"Lookup error: {ex}")
        await interaction.channel.send(embed=embed(f"⚠️ {key}", f"Error: {ex}", C_GRAY))


@tree.command(name="prices", description="Current lowest prices for popular Canadian GPUs")
async def cmd_prices(interaction: discord.Interaction):
    popular = ["RTX 5090", "RTX 5080", "RTX 5070 TI", "RTX 5070", "RTX 5060 TI",
               "RX 9070 XT", "RX 9070", "ARC B580"]
    await interaction.response.send_message(
        embed=embed("🔍 Market Scan", f"Checking {len(popular)} GPUs across 4 retailers..."))

    for gpu in popular:
        try:
            results = await asyncio.to_thread(get_all_prices, gpu)
            if results:
                best = results[0]
                # Summarize retailers
                retailer_prices = {}
                for r in results:
                    if r["retailer"] not in retailer_prices:
                        retailer_prices[r["retailer"]] = r
                summary = "\n".join(
                    f"**{rp['retailer']}**: ${rp['price']:,.2f}"
                    for rp in retailer_prices.values()
                )
                e = embed(f"💰 {gpu} — from ${best['price']:,.2f} CAD",
                    f"{best['name'][:70]}\n[View]({best['url']})\n\n{summary}\n\n{get_price_trend(gpu)}", C_BLUE)
            else:
                e = embed(f"⚠️ {gpu}", "No listings found.", C_GRAY)
            await interaction.channel.send(embed=e)
            await asyncio.sleep(3)
        except Exception as ex:
            log.error(f"Error: {ex}")

    await interaction.channel.send(
        embed=embed("✅ Done", "Use `/alert <gpu> <target>` to get pinged on drops.", C_GREEN))


@tree.command(name="trend", description="Price trend for a GPU")
@app_commands.describe(gpu="GPU name")
@app_commands.autocomplete(gpu=gpu_autocomplete)
async def cmd_trend(interaction: discord.Interaction, gpu: str):
    key = gpu.upper().strip()
    entries = load_json(PRICE_HISTORY_FILE, {}).get(key, [])
    if not entries:
        return await interaction.response.send_message(
            embed=embed("📈 No Data", f"No history for **{key}**. Run `/check` first."), ephemeral=True)

    prices = [e["price"] for e in entries]
    low, high, latest, first = min(prices), max(prices), prices[-1], prices[0]
    change = latest - first
    pct = (change / first) * 100 if first else 0

    # Show price by retailer
    retailer_latest = {}
    for e in reversed(entries):
        r = e.get("retailer", "Unknown")
        if r not in retailer_latest:
            retailer_latest[r] = e["price"]
        if len(retailer_latest) >= 3:
            break
    retailer_lines = "\n".join(f"**{r}**: ${p:,.2f}" for r, p in retailer_latest.items())

    await interaction.response.send_message(embed=embed(
        f"📈 {key} Price History",
        f"**Current:** ${latest:,.2f}\n**Low:** ${low:,.2f}\n**High:** ${high:,.2f}\n"
        f"**Change:** {'+' if change >= 0 else ''}${change:,.0f} ({pct:+.1f}%)\n"
        f"**Data points:** {len(entries)}\n\n"
        f"**Latest by retailer:**\n{retailer_lines}", C_BLUE))


# ── BACKGROUND LOOP ──────────────────────────────────────────────
async def scheduled_check():
    await bot.wait_until_ready()
    log.info(f"Background checker started (every {CHECK_INTERVAL_HOURS}h)")

    while not bot.is_closed():
        try:
            alerts = load_alerts()
            gpus = {g for ua in alerts.values() for g in ua}
            cache = {}
            for g in gpus:
                try:
                    results = await asyncio.to_thread(get_all_prices, g)
                    if results:
                        cache[g] = results[0]
                    await asyncio.sleep(5)
                except Exception as e:
                    log.error(f"BG error {g}: {e}")

            for uid, ua in alerts.items():
                for key, data in ua.items():
                    if key not in cache:
                        continue
                    best = cache[key]
                    price = best["price"]
                    data["last_price"] = price
                    data["last_check"] = datetime.utcnow().isoformat()

                    if price <= data["target"]:
                        try:
                            ch = bot.get_channel(data["channel_id"])
                            if ch:
                                e = embed(f"🚨 PRICE DROP — {key}",
                                    f"**${price:,.2f} CAD** at **{best['retailer']}**\n"
                                    f"Target: ${data['target']:,} ✅\n"
                                    f"{best['name'][:80]}\n"
                                    f"[Buy Now]({best['url']})\n\n"
                                    f"{get_price_trend(key)}\n\n**Move fast** 🏃", C_RED)
                                await ch.send(f"<@{uid}>", embed=e)
                        except Exception as e:
                            log.error(f"Alert send failed: {e}")

            save_alerts(alerts)
            log.info(f"BG check done: {len(cache)} GPUs, {len(alerts)} users")
        except Exception as e:
            log.error(f"Scheduled error: {e}")

        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)


@bot.event
async def on_ready():
    await tree.sync()
    log.info(f"✅ {bot.user} online")
    log.info(f"Invite: https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions=2048&scope=bot%20applications.commands")


async def main():
    async with bot:
        bot.loop.create_task(scheduled_check())
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())