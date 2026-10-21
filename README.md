<p align="center">
  <img src="assets/banner.png" alt="GPU Deals Canada logo: a GPU chip with a red maple leaf, with the words Track, Alert, Save" width="240"/>
</p>

<h1 align="center">GPU Deals Canada</h1>

<p align="center">
  <strong>A Discord ping when the exact GPU you want drops under your price.<br/>
  Six Canadian retailers. Listings marked sold out or sold by marketplace sellers are skipped by default, and the title matcher's accuracy is measured on listings it never saw.</strong>
</p>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="License: MIT"/></a>
  <a href="https://github.com/RZ-Logic/gpu-price-alert-bot/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/RZ-Logic/gpu-price-alert-bot/tests.yml?style=for-the-badge&label=tests" alt="Tests (CI)"/></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/></a>
  <a href="https://discordpy.readthedocs.io/"><img src="https://img.shields.io/badge/Discord-slash_commands-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord slash commands"/></a>
  <img src="https://img.shields.io/badge/Retailers-6_Canadian-D97706?style=for-the-badge" alt="Six Canadian retailers"/>
  <img src="https://img.shields.io/badge/Runtime_AI-none-0EA5E9?style=for-the-badge" alt="No model at runtime"/>
  <img src="https://img.shields.io/badge/Status-v2.1.0-orange?style=for-the-badge" alt="Status v2.1.0"/>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-example">Example</a> •
  <a href="#-what-it-does">What It Does</a> •
  <a href="#-does-it-work">Does It Work?</a> •
  <a href="#-limitations">Limitations</a> •
  <a href="#-documentation">Docs</a> •
  <a href="#%EF%B8%8F-roadmap">Roadmap</a> •
  <a href="#-why-i-built-this">Why</a>
</p>

---

GPU Deals Canada is a self-hosted Discord bot. You tell it `/alert RTX 5070 Ti 1500`, and it checks Best Buy, Canada Computers, Memory Express, Newegg, Amazon.ca and Walmart every hour. When the cheapest listing that is really an RTX 5070 Ti (not a 5070, not a laptop, not a riser cable), new, not marked sold out and not listed by a marketplace seller drops to $1,500 or less, it pings you once, with the link, the next-best retailers and the 30-day context.

<p align="center">
  <img src="assets/discord-lookup.png" alt="Discord /lookup for RTX 5070 Ti: best $1,609.99 at Memory Express, a price and stock line for each of the six retailers, Walmart marketplace only, and the 30-day low, median and high" width="560"/>
  <br/>
  <img src="assets/discord-alert-set.png" alt="Discord /alert reply: alert set for RTX 5090 at or below $3,000, checked every 60 minutes" width="560"/>
  <br/>
  <sub><code>/lookup</code> and <code>/alert</code> in Discord, 2026-10-12</sub>
</p>

**Why not a keyword alert?** Because GPU names are traps. "RTX 5070" is inside every "RTX 5070 Ti" title, "RX 9070" is inside "RX 9070 XT" and "RX 9070 GRE", and "TI" is inside "MULTI" and "EDITION". Version 1 of this bot matched words that way. When I audited it on 2026-10-12, only 18 of the 66 listings it accepted as an RX 9070 in a snapshot of 970 search results were one (0.24 to 0.31 on the labelled test sets [below](#-does-it-work)), and its "best RTX 5090" at Canada Computers was a $5,999.99 laptop. Version 2 parses every title into a model, a suffix and a VRAM size, and its accuracy on eight current models is [measured on listings it never saw](#-does-it-work).

---

## 🚀 Quick Start

### Requirements

- **Python 3.11 or later.** Developed on Python 3.14 on Windows; CI runs the tests on Linux and Windows.
- **A Discord bot token.** In the [Discord Developer Portal](https://discord.com/developers/applications): New Application, then Bot, then Reset Token, and copy it. No privileged intents are needed (leave Message Content off).
- **A computer that stays on** while you want alerts. The bot makes ordinary HTTPS requests; no browser, proxy or paid API.

### Install

```bash
git clone https://github.com/RZ-Logic/gpu-price-alert-bot.git
cd gpu-price-alert-bot
python -m venv .venv
```

Activate the virtual environment, in every new terminal you run the bot from:

| Shell | Command |
|---|---|
| PowerShell | `.venv\Scripts\Activate.ps1` |
| Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `source .venv/bin/activate` |

Then install. With the environment active, every `python` below is the environment's interpreter. (If you prefer not to activate, call `.venv\Scripts\python` on Windows or `.venv/bin/python` elsewhere instead of `python`.)

```bash
python -m pip install -r requirements.txt
```

Copy [`.env.example`](.env.example) to `.env` (git ignores it) and fill in:

| Variable | What it does |
|---|---|
| `DISCORD_BOT_TOKEN` | Required to run the bot. Not needed for the command-line lookups. |
| `DISCORD_GUILD_ID` | Optional. Your server's ID (Discord Developer Mode, right-click the server, Copy Server ID). Commands then appear instantly, only in that server, and stale global commands from older versions are removed. If the bot has not joined that server yet, it says so in the log and keeps running; open the invite link, then restart it. |
| `ALLOWED_GUILD_IDS` | Optional, for global commands (`DISCORD_GUILD_ID` empty). Comma-separated server IDs where the commands answer. With both empty, they answer only in servers the bot's owner is a member of. |
| `CHECK_INTERVAL_MINUTES` | Background check interval in whole minutes, default 60. A value below 1, or one that is not a number, logs a warning and uses 60. |
| `DISABLED_RETAILERS` | Comma-separated ids to skip: `bestbuy`, `canadacomputers`, `memoryexpress`, `newegg`, `amazon`, `walmart`. An entry that is not one of these ids is named in a warning at startup. |
| `GPU_ALERTS_DATA_DIR` | Where the SQLite database lives, default `data/` in the repo. |
| `BESTBUY_POSTAL_CODE` | Any valid Canadian postal code for Best Buy's stock lookup. The default is a public downtown Toronto code. |

A variable already set in your terminal wins over `.env`. A line may start with `export `. A key set twice keeps its first value, and the log names both lines.

**Who can use the commands.** Every search goes out from your connection, so the bot answers only in `DISCORD_GUILD_ID` and `ALLOWED_GUILD_IDS` or, with neither set, in servers you (the application owner) are in. In DMs it answers members of those servers. Anyone else gets a private refusal, except that `/list` and `/remove` still answer, so people can see and remove alerts they already have. The commands cannot be installed to a user account. Also make the bot private, so only you can add it to a server. In the Developer Portal, first set **Installation → Install Link** to **None** and save, then turn off **Public Bot** on the **Bot** page and save. Discord refuses the second step while an install link is set ("Private application cannot have a default authorization link").

### Run

```bash
python -m gpu_alerts lookup "rtx 5070 ti"   # live prices in the terminal, no Discord needed
python -m gpu_alerts                        # start the bot
```

On start the bot logs an invite link with the permissions it needs (View Channels, Send Messages, Embed Links, Send Messages in Threads; scopes `bot` and `applications.commands`), then the check interval and the retailers it will search. Open the link once to add the bot to your server.

Upgrading from v2.0? Run `python -m pip install -r requirements.txt` again (it now sets a minimum certifi version, for a certificate advisory), then start the bot. The database gains its new columns on the first start.

Upgrading from v1? `python -m gpu_alerts import-v1 gpu_alerts.json` carries your old alerts over. Running it again changes nothing: alerts that exist are skipped, and alerts removed since an earlier import stay removed. Add `--force` to set existing alerts to the file's target and channel; whether they already pinged is kept, and removed ones come back.

---

## 🧪 Example

`lookup` from the terminal, 2026-10-12 at 22:37 (the same scan behind `/lookup` in Discord):

```text
$ python -m gpu_alerts lookup "rtx 5070 ti" --top 5
Searching 'rtx 5070 ti' for RTX 5070 Ti (min $700)...
retailer          status   http  raw  listings  best eligible        detail
----------------  -------  ----  ---  --------  -------------------  ------------------------------------------------
Best Buy          ok       200   100  81        $1,689.99, in stock  100 rows, 81 graphics cards
Canada Computers  ok       200   31   31        $1,689.00, in stock  74 results over 4 pages; 14 in-store pickup only
Memory Express    ok       200   24   24        $1,609.99, in stock  24 cards, 24 after dropping bundles
Newegg            ok       200   71   56        $1,669.99, in stock  160 results reported
Amazon.ca         ok       200   58   36        $1,679.99, in stock  113 results reported
Walmart           blocked  429   0    0         blocked              HTTP 429

Top 5 of 60 eligible offers:
 1. $ 1,609.99  Memory Express     MSI GeForce RTX 5070 Ti MLG EDITION OC 16GB PCI-E w/ HDMI, Triple DP
    https://www.memoryexpress.com/Products/MX00135516
 2. $ 1,664.99  Memory Express     Gigabyte GeForce RTX 5070 Ti WindForce OC 16GB PCI-E w/ HDMI, Triple DP
    https://www.memoryexpress.com/Products/MX00132579
 3. $ 1,669.99  Newegg             GIGABYTE WindForce GeForce RTX 5070 Ti Graphics Card GV-N507TWF3OC-16GD
    https://www.newegg.ca/p/N82E16814932771
 4. $ 1,679.99  Amazon.ca          GIGABYTE GeForce RTX 5070 Ti Eagle OC SFF 16G Graphics Card, 16GB 256-bit GDDR7, PCIe 5.0,
    https://www.amazon.ca/dp/B0DTR7GWG6
 5. $ 1,679.99  Memory Express     Gigabyte GeForce RTX 5070 Ti EAGLE OC 16GB PCI-E w/ HDMI, Triple DP
    https://www.memoryexpress.com/Products/MX00132578
```

Walmart answered `429 Too Many Requests` on that run, after about three hours of testing, and the bot reports it as "blocked", not as "no listings" (v1 could not tell the two apart). When a retailer answers but nothing qualifies, the column says why, for example `sold out (6 found)` or `marketplace only (8 found)`.

---

## ⚡ What It Does

**Slash commands** (GPU arguments autocomplete from a 33-model catalog)

| Command | What it does |
|---|---|
| `/alert gpu target [open_box] [marketplace]` | Ping when the best eligible offer is at or under `target` (CAD, before tax, $100 to $20,000). Up to 10 alerts per user; setting one again updates it. The reply warns when the target is below the model's price floor and can never be reached. Set in a channel where the bot cannot post, the alert pings by DM. |
| `/remove gpu` · `/list` | Remove an alert; list yours with the last price (or "nothing eligible") and when it was checked, and whether it is watching or already notified. |
| `/check` | Check your alerts now (once every 5 minutes). If the reply cannot be delivered, the alerts it would have reported are put back and the next background check pings them. |
| `/lookup gpu` | Live prices at all six retailers with a status line per retailer (once every 30 seconds per user). |
| `/prices` | Best current prices for the popular current-generation cards, from data under 2 hours old; a fresh scan runs at most every 10 minutes. |
| `/trend gpu` | Current best, and the low, median and high over 7, 30 and 90 days from stored checks. |
| `/health` | Last status per retailer and failures in a row. The bot also DMs its owner when a retailer fails 3 background runs in a row, and again once it has answered 2 runs in a row. A DM that could not be sent is tried again on the next run. |

**Retailers**

| Retailer | How it reads prices | Stock and seller |
|---|---|---|
| Best Buy | Best Buy's JSON search API (its web pages block automated requests), one page of up to 100 rows in Best Buy's relevance order, then its stock API | Online stock per SKU; a SKU the stock API leaves unanswered counts as sold out; marketplace sellers flagged; bundles dropped |
| Canada Computers | The site's JSON search, sorted by price and limited to the graphics-card category, up to 4 pages of 24 | Online availability; open box flagged |
| Memory Express | The video-card category page filtered by your search, one page of up to 120 (the search page is behind a Cloudflare challenge) | Out of stock and special order flagged |
| Newegg | The JSON state embedded in the search page, In Stock filter, up to 2 pages of 36 | Marketplace sellers flagged; open box and refurbished from the data; shipping included in the price |
| Amazon.ca | The Graphics Cards category, up to 2 pages; sponsored cards skipped | In stock when a buy button is shown; sold by Amazon.ca from the buy-box merchant id |
| Walmart | The JSON embedded in the search page, up to 2 pages | Every graphics card seen on 2026-10-12 was a marketplace seller; shipping included in the price |

Requests use pinned browser TLS fingerprints (`curl_cffi`), at least 2 seconds apart per site (10 seconds for Amazon.ca), with retries on blocks and transient errors. A block stays a block when a retry then times out or fails, and a bot-check page is recognised at any size. When a site's results come back with rows but none the adapter can read, the retailer reports `parse_error`, not an empty result, so a layout change shows up in `/health` and in the owner's DMs.

**What counts as an offer.** By default a listing must match the model exactly, be at or above a sanity floor for that model (so a $22 riser cable never counts), be new, not be marked sold out, and not come from a marketplace seller. `open_box:True` adds open-box and refurbished listings; `marketplace:True` adds third-party sellers (needed for Walmart).

**When it pings.** Once, when the best offer first reaches your target, and again only if the price falls a further 3% or $25. Every retailer that had an offer at or under your target then holds the alert. The alert re-arms after two scans above target (or with nothing eligible), counting from the scan in which the last of those retailers answered completely. So a retailer that is blocked, or whose later pages failed, cannot cause a repeat ping, and a restock after a sell-out pings again. A partial read (a later page failed, or Best Buy gave no stock answer for some cards) still counts for prices and pings, but it is no proof that a deal is gone. A retailer that never answers completely lets go after 24 scans above target, about a day at the default interval. Delivery goes to the alert's channel, then falls back to a DM. If both fail, the alert stays armed and is tried again every check, and the owner gets one DM about it.

**The catalog** ([`catalog.json`](gpu_alerts/data/catalog.json)): RTX 50 series with the RTX 5060 Ti 8GB and 16GB as separate models, RX 9070 XT, 9070, 9070 GRE, RX 9060 XT 8GB and 16GB, RX 9060, Arc B580 and B570, plus RTX 40 and RX 7000 cards marked legacy. Each model lists the memory sizes its desktop card is sold with, so a title that states another size is a different card (an RX 9070 12GB is a GRE). A new SKU is usually a data entry, not a code change; a new memory size under an existing name needs its entry updated.

---

## 📊 Does It Work?

### Is it the right card?

For a deal alert the number that matters is precision: of the listings accepted as your card, how many really are. The targets were fixed in [`eval/PREREGISTRATION.md`](eval/PREREGISTRATION.md) before the matcher was built (precision at least 0.97 overall and 0.90 per model, recall at least 0.90), and each test set was scored once. The eight models scored are the RTX 5090, 5080, 5070 Ti, 5070 and 5060 Ti, the RX 9070 XT and 9070, and the Arc B580.

| Test set (captured 2026-10-12) | Rows scored | v1 precision / recall | v2 precision / recall | v2 95% lower bounds |
|---|:-:|:-:|:-:|:-:|
| Held-out split: Newegg, Amazon.ca, Walmart, Canada Computers | 397 | 0.746 / 0.812 | **1.000 / 1.000** | 0.985 / 0.985 |
| Retailers the matcher never saw: Best Buy, Memory Express | 869 | 0.776 / 0.945 | **1.000 / 1.000** | 0.987 / 0.987 |

The lower bound is the one-sided 95% exact (Clopper-Pearson) bound: 192 right out of 192 accepted is consistent with a true precision as low as 0.985. Precision and recall share it here because the accepted rows and the correct rows are the same 192 on the first set and the same 238 on the second. It is reported next to the pre-registered targets, which are unchanged. The second set's 869 rows are 325 distinct titles, because Best Buy returned 81 to 91 cards per search, most of them other models, and a title is scored once for each search that returned it. All 238 correct rows are distinct titles.

**Per model, the samples are small.** On the held-out split the RTX 5090 had 7 accepted rows, the RX 9070 9 and the Arc B580 10, all correct, which puts their lower bounds at 0.65, 0.72 and 0.74. Showing 0.90 at the 95% bound takes 29 correct rows out of 29. Pooled over both test sets, the RX 9070 has 19 of 19 (0.85), the Arc B580 14 of 14 (0.81) and the RTX 5090 34 of 34 (0.92). So every model meets the 0.90 target as a point estimate; at the 95% bound, only the cells with 29 or more accepted rows show it. The eight queries cover 9 of the 33 catalog models (the 5060 Ti is two, 8GB and 16GB). The other 24, including the RTX 5060, RX 9070 GRE, the RX 9060 family, Arc B570 and the legacy cards, are not in the eval; some have unit tests.

v1's worst cases were the base models: an RTX 5070 alert was right 40 to 44% of the time and an RX 9070 alert 24 to 31%, because Ti, XT and GRE listings passed as the base card.

**Read these as an upper bound.**
- About half the held-out titles closely resemble tuning titles (the same products at several stores), which is why the second set uses retailers the matcher never saw.
- Best Buy and Memory Express titles are clean, and 84% of that set's 631 negative rows do not contain the searched model number at all; messier stores will test the matcher harder.
- The labels come from an automated labeler and blind AI labelers, not from a person; 5 disagreements await a human decision.
- VRAM (8GB versus 16GB) is covered by unit tests, not by this eval.
- The matcher changed after scoring, twice: after the v2.0 code review and again in v2.1. It still gives exactly the same counts on all three labelled sets, and `tests/test_eval.py` now pins them, but a set the matcher has already seen is a regression check, not new evidence. A fresh held-out set is on the roadmap.
- Of the code review's 100 trick titles, 11 still pass the matcher on its own, mostly prebuilt PCs that describe their GPU like a card (see [Limitations](#-limitations)).

The full caveats, with numbers, are in [`eval/README.md`](eval/README.md#caveats-in-full).

### Does it reach the retailers?

All six returned results live for the eight test models on 2026-10-12. Amazon.ca and Walmart push back when searched too often (see [Limitations](#-limitations)). The v2.1 retailer changes are tested on saved pages and have not been run live yet.

### How it was reviewed

Built from a live audit of v1 and a survey of existing GitHub projects. After the v2.0 build, three reviewers raised 27 findings; every high and medium one was fixed with a test. For v2.1, three independent AI audits of v2.0 (Claude, Gemini and GPT) were cross-checked against each other. An issue was kept only when at least two agents confirmed it, one of them after trying to refute it. That left 54 issues: 10 medium, 44 low and none high. All 10 medium issues and 37 low ones are fixed, 6 low ones in part, and 1 is kept as it is by choice; what remains is under [Limitations](#-limitations). The 798 offline tests run in CI on Linux and Windows, and any test that tries to reach a retailer fails. The record is in [`CHANGELOG.md`](CHANGELOG.md).

---

## 🫡 Limitations

- **Terms of use.** Amazon.ca's and Newegg's terms prohibit automated data collection, and other retailers' terms are similar. This is a personal, low-volume tool: a few spaced-out requests per GPU per retailer per check (hourly by default), plus rate-limited commands. Turn any retailer off with `DISABLED_RETAILERS`, and do not run it as a public service.
- **Bot checks happen.** Amazon.ca and Walmart push back when searched too often. The bot reports the block, skips that retailer for the rest of the scan and keeps the others; it never tries to solve a challenge. The background scan order rotates every run, so when a retailer blocks partway through, the alerts that miss it change from run to run.
- **Hourly, not a restock sniper.** Cards that sell out in minutes can come and go between checks. Stock trackers exist for that; this bot is for price targets.
- **Walmart is almost all marketplace** for graphics cards (every card seen on 2026-10-12), so it usually counts only on alerts set with `marketplace:True`.
- **Search depth.** Best Buy reads one page of up to 100 rows in its own relevance order, and its status line does not yet say when that cap is reached. Canada Computers reads its cheapest 96 search results and keeps the graphics cards; Memory Express reads one page of up to 120; Newegg, Amazon.ca and Walmart read two pages. A rare SKU past that depth can be missed.
- **A failing retailer can hold an alert for about a day.** After a ping, a retailer that had an offer at or under your target and then stays blocked, or keeps failing on a later page, holds the alert for up to 24 scans above target. Until then, a new offer elsewhere under your target stays silent unless it is 3% or $25 below the notified price. After the 24 scans the alert re-arms, and the held deal can ping once more when that retailer answers again.
- **Prices are before tax**, and include shipping where the retailer adds it to the listing (Newegg, Walmart). Mail-in rebates are ignored.
- **It runs only while your computer is on**, and alerts posted in a server channel show your target to everyone in that channel. Alert decisions use the computer's clock; if it jumps backwards, they pause until the clock passes the last saved scan.
- **A failed save can repeat a ping once.** If saving an alert's state fails right after a ping (the database locked by another program, a full disk), the bot keeps that state in memory and saves it before judging the alert again. A restart in between can repeat that ping once.
- **Matcher gaps.** Of the code review's 100 trick titles, 11 still pass the matcher on its own: six prebuilt PCs that call the GPU a card and name no CPU and few or no specs ("Skytech Gaming Desktop - RTX 5080 16GB GPU"), a motherboard, a card bundled with a motherboard that is named without the word, a power-connector title, and two used cards, which the matcher leaves to each adapter's condition check (that check reads common wording such as "(Used)", not every phrasing). Accessory titles that carry a VRAM size and have no card noun right before the accessory word ("RTX 5080 16GB Cooler Shroud Only") pass too. Each retailer's category filter removes most such listings before matching, and the price floor stops cheap accessories. A real card titled "... Perfect for Gaming RTX 5070 Builds" is still rejected as an accessory, and French used or refurbished wording is not read as used.
- **Pre-orders count as in stock at Canada Computers.** Its "Online - Available for Pre-order" and "Online - Available to Order" labels are treated as buyable on purpose. So far they were seen only on laptops, prebuilts and accessories.
- **Open review items.** From the v2.0 review: `/check` and `/prices` have no overall time limit; Ctrl+C waits for a running scan; a `# comment` after a `.env` value becomes part of the value; Walmart sponsored rows without category data reach the matcher. From the v2.1 review: if Canada Computers changes both its category ids and its category names, its scan reports an empty result instead of a parse error.
- **Upgrading from v2.0.** Alerts that people outside the allowed servers set before v2.1 are still checked and still ping until they are removed. Those people can remove them with `/list` and `/remove`, which work in any server and in DMs. In guild mode (`DISCORD_GUILD_ID` set) the commands exist only in that server, so you remove such alerts yourself, from the `alerts` table of the database (`data/gpu_alerts.sqlite3` by default).
- **v1 imports** map names to catalog models; names that now have VRAM variants (RTX 5060 Ti, RTX 4060 Ti, RX 9060 XT) are skipped and need re-adding. v2.0 kept no record of imports, so an alert imported with v2.0 and removed before the upgrade comes back the first time `import-v1` runs again, if the file still has it.
- **New cards only.** Used listings (Kijiji, eBay.ca) are on the roadmap.

---

## 📚 Documentation

| Doc | What's in it |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Where the AI is and is not, the pipeline, the components, the stack and the engineering properties. |
| [`docs/build-decisions.md`](docs/build-decisions.md) | Why a rewrite, why rules and not a model for matching, why JSON endpoints and no browser, and the other choices. |
| [`eval/README.md`](eval/README.md) | How the matcher is measured and how to rerun it. |
| [`eval/PREREGISTRATION.md`](eval/PREREGISTRATION.md) | The targets and protocol, fixed before scoring, with the dated addenda and erratum added since. |
| [`CHANGELOG.md`](CHANGELOG.md) | v1, v2.0 and v2.1: what changed and how each was reviewed. |

Development (with the virtual environment active):

```bash
python -m pip install -r requirements-dev.txt   # adds pytest
python -m pytest -q                       # 798 offline tests, no network, no token
python -m pytest -q -m live               # live smoke tests against each retailer
python -m gpu_alerts health               # one search on every retailer, with status and timing
python eval/evaluate.py eval/heldout.jsonl --label-field gold_label --check
```

---

## 🗺️ Roadmap

- [ ] **Used market:** Kijiji first, then eBay.ca through eBay's official Browse API. A separate alert type ("a new listing under $X near me"), one ping per listing, checked every 10 to 15 minutes, with filters for wanted ads, bait prices and whole PCs, measured on labelled listings like the matcher.
- [ ] **A fresh held-out set** for the matcher as it is now, captured on a later date, including at least one messier retailer and enough rows to judge each model at the 95% bound (29 or more accepted rows each).
- [ ] **A price chart in `/trend`** from the stored history.
- [ ] **A small-server guide:** the bot needs no browser, so it fits next to other services on a 1 GB VPS with a memory cap.
- [ ] **v1 import for VRAM variants:** ask which variant instead of skipping.

---

## 🧭 Why I Built This

I'm an ACCA with audit roots who now builds AI automation. Audit teaches one habit above the rest: test the population, not the sample someone hands you. A deal bot is a sampler by design. It reads a page of search results and trusts the cheapest row.

Version 1 went up in April 2026 and did its job well enough. When I audited it in October, it was quietly broken. Newegg answered every search with a CAPTCHA page and Amazon.ca with an error page, and the bot read both as "no listings". On the two retailers still answering, an RX 9070 alert accepted 9070 XT and 9070 GRE listings, sold-out cards counted as deals, and the hourly check pinged the same deal every hour while never saving the price history its trend command relied on. Nothing crashed. It was confidently wrong, which for an alert is worse than silent.

**On why rules and not a model for matching:** GPU names are a small, closed vocabulary with known traps: suffixes, VRAM variants, part codes, laptops and prebuilts that name the GPU. A tokenizer and rules, scored against labels they never saw, met every pre-registered target (one model had too few rows on the second set to judge on its own, and the smallest per-model samples meet 0.90 only as a point estimate). A model would add cost and non-determinism to a problem that does not need it. Used listings are different, free text written by people, and that is where a classifier may earn its place, once it beats the rules on a labelled set.

It's the same split I use in **[FinAgent OS](https://github.com/RZ-Logic/finagent-os)** (governance for finance agents) and **[RegisterBench](https://github.com/RZ-Logic/RegisterBench)** (a benchmark with seeded ground truth): deterministic code where the answer can be checked, and every claim measured before it is made. The design choices are in [`docs/build-decisions.md`](docs/build-decisions.md).

---

## 🙏 Prior Art & Acknowledgements

- **[discord.py](https://github.com/Rapptz/discord.py)**, **[curl_cffi](https://github.com/lexiforest/curl_cffi)** and **[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)**: the bot, the TLS-fingerprinted HTTP client and the HTML parser.
- **[ecoppen/Amiibot](https://github.com/ecoppen/Amiibot)** uses the same Best Buy Canada search-then-availability pattern for a different product. The adapter here was written from the endpoints themselves; no code was copied (Amiibot is GPL-3.0).
- **[ColbyMay/price-checker](https://github.com/ColbyMay/price-checker)** reports "blocked" separately from "out of stock", the same line this bot draws between a block and an empty result.
- **[jef/streetmerchant](https://github.com/jef/streetmerchant)**: the reference open-source stock checker. This bot watches prices instead of restocks.

---

## 📜 License

**Code:** MIT, see [`LICENSE`](./LICENSE). Use it, fork it, add a retailer. Keep the copyright and permission notice; a link back is appreciated.

**Data:** prices and listing titles belong to the retailers. The evaluation files in [`eval/`](eval/) hold listing titles and prices captured on 2026-10-12, kept only to measure the matcher.

---

## 💬 Contact

**Rizwan Ahmed, ACCA**
<br>
Founder, Velocyt Consulting

- Website: [velocyt.ca](https://velocyt.ca)
- GitHub: [@RZ-Logic](https://github.com/RZ-Logic)

Also built: __[FinAgent OS](https://github.com/RZ-Logic/finagent-os)__ · __[regulated-rag](https://github.com/RZ-Logic/regulated-rag)__ · __[RegisterBench](https://github.com/RZ-Logic/RegisterBench)__ · __[trip-planner](https://github.com/RZ-Logic/trip-planner-skill)__ · __[JobSignal Engine](https://github.com/RZ-Logic/jobsignal-engine)__

---

<p align="center">
  <em>If you find this useful, a star helps others find it.</em>
  <br><br>
  Built by a gamer who does <a href="https://www.youtube.com/@BT_01">no-damage boss fights when time permits</a> and builds <a href="https://github.com/RZ-Logic/jobsignal-engine">open-source tools that help people</a>.
</p>
