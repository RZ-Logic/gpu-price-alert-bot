<p align="center">
  <img src="assets/banner.png" alt="GPU Deals Canada logo: a GPU chip with a red maple leaf, with the words Track, Alert, Save" width="240"/>
</p>

<h1 align="center">GPU Deals Canada</h1>

<p align="center">
  <strong>A Discord ping when the exact GPU you want drops under your price.<br/>
  Six Canadian retailers, in stock and sold by the retailer by default, with the title matcher's accuracy measured on held-out listings.</strong>
</p>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="License: MIT"/></a>
  <a href="https://github.com/RZ-Logic/gpu-price-alert-bot/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/RZ-Logic/gpu-price-alert-bot/tests.yml?style=for-the-badge&label=tests" alt="Tests (CI)"/></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/></a>
  <a href="https://discordpy.readthedocs.io/"><img src="https://img.shields.io/badge/Discord-slash_commands-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord slash commands"/></a>
  <img src="https://img.shields.io/badge/Retailers-6_Canadian-D97706?style=for-the-badge" alt="Six Canadian retailers"/>
  <img src="https://img.shields.io/badge/Runtime_AI-none-0EA5E9?style=for-the-badge" alt="No model at runtime"/>
  <img src="https://img.shields.io/badge/Status-v2.0.0-orange?style=for-the-badge" alt="Status v2.0.0"/>
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

**Why not a keyword alert?** Because GPU names are traps. "RTX 5070" is inside every "RTX 5070 Ti" title, "RX 9070" is inside "RX 9070 XT" and "RX 9070 GRE", and "TI" is inside "MULTI" and "EDITION". Version 1 of this bot matched words that way. When I audited it on 2026-10-12, only 18 of the 66 listings it accepted as an RX 9070 in a snapshot of 970 search results were one (0.24 to 0.32 on the labelled test sets [below](#-does-it-work)), and its "best RTX 5090" at Canada Computers was a $5,999.99 laptop. Version 2 parses every title into a model, a suffix and a VRAM size, and its accuracy is [measured on listings it never saw](#-does-it-work).

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
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
.venv/bin/python -m pip install -r requirements.txt       # macOS / Linux
```

Copy [`.env.example`](.env.example) to `.env` (git ignores it) and fill in:

| Variable | What it does |
|---|---|
| `DISCORD_BOT_TOKEN` | Required to run the bot. Not needed for the command-line lookups. |
| `DISCORD_GUILD_ID` | Optional. Your server's ID (Discord Developer Mode, right-click the server, Copy Server ID). Commands then appear instantly, only in that server, and stale global commands from older versions are removed. |
| `CHECK_INTERVAL_MINUTES` | Background check interval, default 60. |
| `DISABLED_RETAILERS` | Comma-separated ids to skip: `bestbuy`, `canadacomputers`, `memoryexpress`, `newegg`, `amazon`, `walmart`. |
| `GPU_ALERTS_DATA_DIR` | Where the SQLite database lives, default `data/` in the repo. |
| `BESTBUY_POSTAL_CODE` | Any valid Canadian postal code for Best Buy's stock lookup. The default is a public downtown Toronto code. |

A variable already set in your terminal wins over `.env`.

### Run

```bash
python -m gpu_alerts lookup "rtx 5070 ti"   # live prices in the terminal, no Discord needed
python -m gpu_alerts                        # start the bot
```

On start the bot logs an invite link with the permissions it needs (View Channels, Send Messages, Embed Links; scopes `bot` and `applications.commands`). Open it once to add the bot to your server. Upgrading from v1? `python -m gpu_alerts import-v1 gpu_alerts.json` carries your old alerts over.

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

Walmart answered `429 Too Many Requests` on that run, after about three hours of testing that evening. The lookup says "blocked" instead of "no listings". In a background check the bot then skips Walmart for the remaining models and `/health` counts the failure (a terminal lookup does neither). v1 could not tell a block from an empty result. When a retailer answers but nothing qualifies, the column says why, for example `sold out (6 found)` or `marketplace only (8 found)`.

---

## ⚡ What It Does

**Slash commands** (GPU arguments autocomplete from a 33-model catalog)

| Command | What it does |
|---|---|
| `/alert gpu target [open_box] [marketplace]` | Ping when the best eligible offer is at or under `target` (CAD, before tax, $100 to $20,000). Up to 10 alerts per user; setting one again updates it. |
| `/remove gpu` · `/list` | Remove an alert; list yours with the last price and whether it is watching or already notified. |
| `/check` | Check your alerts now (once every 5 minutes). |
| `/lookup gpu` | Live prices at all six retailers with a status line per retailer (once every 30 seconds per user). |
| `/prices` | Best current prices for the popular current-generation cards, from data under 2 hours old; a fresh scan runs at most every 10 minutes. |
| `/trend gpu` | Current best, and the low, median and high over 7, 30 and 90 days from stored checks. |
| `/health` | Last status per retailer and failures in a row. The bot also DMs its owner when a retailer fails 3 background runs in a row. |

**Retailers**

| Retailer | How it reads prices | Stock and seller |
|---|---|---|
| Best Buy | Best Buy's JSON search API (its web pages block automated requests), then its stock API | Online stock per SKU; marketplace sellers flagged; bundles dropped |
| Canada Computers | The site's JSON search, sorted by price and limited to the graphics-card category, up to 4 pages of 24 | Online availability; open box flagged |
| Memory Express | The video-card category page filtered by your search (the search page is behind a Cloudflare challenge) | Out of stock and special order flagged |
| Newegg | The JSON state embedded in the search page, In Stock filter, up to 2 pages of 36 | Marketplace sellers flagged; open box and refurbished from the data; shipping included in the price |
| Amazon.ca | The Graphics Cards category, up to 2 pages; sponsored cards skipped | In stock when a buy button is shown; sold by Amazon.ca from the buy-box merchant id |
| Walmart | The JSON embedded in the search page | Every graphics card seen on 2026-10-12 was a marketplace seller; shipping included in the price |

Requests use pinned browser TLS fingerprints (`curl_cffi`), at least 2 seconds apart per site (10 seconds for Amazon.ca), with retries on blocks and transient errors.

**What counts as an offer.** By default a listing must match the model exactly, be at or above a sanity floor for that model (so a $22 riser cable never counts), be new, not be marked sold out, and not come from a marketplace seller. `open_box:True` adds open-box and refurbished listings; `marketplace:True` adds third-party sellers (needed for Walmart).

**When it pings.**
- Once, when the best offer first reaches your target.
- Again only if the price falls a further 3% or $25.
- It re-arms after two scans above target in which the retailer that had the deal answered, so a retailer that is blocked for an hour does not cause a repeat ping, and a card that sells out everywhere and comes back under target pings again.
- Delivery goes to the channel where you set the alert, then falls back to a DM.

**The catalog** ([`catalog.json`](gpu_alerts/data/catalog.json)): RTX 50 series with the RTX 5060 Ti 8GB and 16GB as separate models, RX 9070 XT, 9070, 9070 GRE, RX 9060 XT 8GB and 16GB, RX 9060, Arc B580 and B570, plus RTX 40 and RX 7000 cards marked legacy. A new SKU is usually a data entry, not a code change.

---

## 📊 Does It Work?

### Is it the right card?

The question that matters for a deal alert is precision: of the listings the matcher accepts as your card, how many are your card. A wrong one is a false deal ping. The targets were written down in [`eval/PREREGISTRATION.md`](eval/PREREGISTRATION.md) before the matcher was built: precision at least 0.97 overall and 0.90 for each of the 8 test models, recall at least 0.90. Each test set was scored once.

| Test set (captured 2026-10-12) | Rows scored | v1 precision / recall | v2 precision / recall |
|---|:-:|:-:|:-:|
| Held-out split: Newegg, Amazon.ca, Walmart, Canada Computers | 397 | 0.746 / 0.812 | **1.000 / 1.000** |
| Retailers the matcher never saw: Best Buy, Memory Express | 869 | 0.776 / 0.945 | **1.000 / 1.000** |

v1's worst cases were the base models: RTX 5070 precision 0.40 and 0.44 on the two sets, RX 9070 0.24 and 0.31, because Ti, XT and GRE listings passed as the base card.

**How to read these numbers.** They are better than the bot will be in the wild, for reasons that are on the record:

- **Near-duplicates.** The held-out split shares retailers, queries and capture day with the tuning data: 49% of its titles have a tuning title with at least 80% of the same words. That is why the second set uses retailers the matcher never saw.
- **Clean titles.** Best Buy and Memory Express titles are tidy, and the two blind labelers, who each labelled this whole set, agreed on all 869 rows. Messier stores would test the matcher harder.
- **Who labelled.** On the first set, labels came from an automated labeler checked against blind labels from two Sonnet agents that split the titles between them and read each one against a written rubric without seeing any code. The two label sets agreed on correct versus not correct for 397 of 402 held-out listings; the 5 disagreements are excluded and listed in [`eval/heldout_disagreements.jsonl`](eval/heldout_disagreements.jsonl), and 12 more rows are excluded as unknown or unsure. No human labelled any row, and the 5 disagreements still await a human decision.
- **VRAM is not in the eval.** The labels name the model, not the memory size, so the 8GB versus 16GB rule is covered by unit tests only.
- **The matcher changed after scoring.** A code review then wrote 100 trick titles ("RTX 4080S", "8GB/16GB", "RTX 5070 with a 5070 Ti cooler"). 51 fooled the matcher on its own; after the fixes, 17 still do, mostly laptops and prebuilt PCs that describe their GPU like a card ([`eval/adversarial_titles.py`](eval/adversarial_titles.py) reruns them). Every adapter also filters by the retailer's own category before matching (Walmart's filter has a gap for sponsored rows), which removes most laptops and prebuilt PCs. Re-scoring the fixed matcher on the same sets gave identical results ([regression run](eval/results/2026-10-12-regression-after-review-fixes.txt)), but a set it has already seen is a regression check, not a new measurement. A fresh held-out set is on the roadmap.

### Does it reach the retailers?

On 2026-10-12, from a home connection in Ontario, each adapter was run live for the eight test models (RTX 5090, 5080, 5070 Ti, 5070, 5060 Ti, RX 9070 XT, RX 9070, Arc B580). All six returned results. The limits found are under [Limitations](#-limitations): Amazon.ca showed its bot check after about five searches in a few minutes, and Walmart rate-limited after about three hours of testing.

### Review record

The rewrite started from a live audit of v1 (four agents: retailers, code, platform facts, market) and a GitHub survey of existing projects (two agents), which found no maintained Canadian GPU price-alert bot to fork. After the build, three Opus reviewers read the code in separate slices and raised 27 findings: 2 high, 9 medium and 16 low. All high and medium findings are fixed with a regression test each (the Canada Computers search depth was widened from 48 to 96 rows, not removed); of the low ones, 9 are fixed, 2 in part, and 5 are left open and listed under [Limitations](#-limitations). The suite has 391 offline tests plus 6 live ones that are skipped unless asked for.

---

## 🫡 Limitations

- **Terms of use.** Amazon.ca's and Newegg's terms prohibit automated data collection, and other retailers' terms say similar things. This is a personal, low-volume tool: one scheduled search per GPU per retailer per check (hourly by default; one to four requests plus retries), plus rate-limited scans from `/lookup`, `/check` and `/prices`, spaced out. Turn any retailer off with `DISABLED_RETAILERS`. Do not run it as a public service.
- **Bot checks happen.** Amazon.ca and Walmart push back when searched too often. The bot reports the block, skips that retailer for the rest of the scan and keeps the others; it never tries to solve a challenge.
- **Hourly, not a restock sniper.** Cards that sell out in minutes can come and go between checks. Stock trackers exist for that; this bot is for price targets.
- **Walmart is almost all marketplace** for graphics cards (every card seen on 2026-10-12), so it usually counts only on alerts set with `marketplace:True`.
- **Search depth.** Canada Computers reads the cheapest 96 search results (4 pages of 24) and keeps the graphics cards among them; Newegg and Amazon.ca read two pages. A rare SKU listed past that depth can be missed; the detail column says so when Canada Computers or Memory Express hits its page cap.
- **Prices are before tax**, and include shipping where the retailer adds it to the listing (Newegg, Walmart). Mail-in rebates are ignored.
- **It runs only while your computer is on**, and alerts posted in a server channel show your target to everyone in that channel.
- **Open review items.** `/check` and `/prices` have no overall time limit, so a slow retailer makes them slow; Ctrl+C during a scan waits for the scan to finish; a `# comment` after a value in `.env` becomes part of the value; Walmart's category filter lets sponsored rows without category data through to the matcher.
- **v1 imports** map names to catalog models; names that now have VRAM variants (RTX 5060 Ti, RTX 4060 Ti, RX 9060 XT) are skipped and need re-adding.
- **New cards only.** Used listings (Kijiji, eBay.ca) are on the roadmap.

---

## 📚 Documentation

| Doc | What's in it |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Where the AI is and is not, the pipeline, the components, the stack and the engineering properties. |
| [`docs/build-decisions.md`](docs/build-decisions.md) | Why a rewrite, why rules and not a model for matching, why JSON endpoints and no browser, and the other choices. |
| [`eval/README.md`](eval/README.md) | How the matcher is measured and how to rerun it. |
| [`eval/PREREGISTRATION.md`](eval/PREREGISTRATION.md) | The targets and protocol, fixed before scoring, with both addenda. |
| [`CHANGELOG.md`](CHANGELOG.md) | v1 and v2, what changed and how it was reviewed. |

Development:

```bash
python -m pytest -q                       # 391 offline tests, no network, no token
python -m pytest -q -m live               # live smoke tests against each retailer
python -m gpu_alerts health               # one search on every retailer, with status and timing
python eval/evaluate.py eval/heldout.jsonl --label-field gold_label --check
```

---

## 🗺️ Roadmap

- [ ] **Used market:** Kijiji first, then eBay.ca through eBay's official Browse API. A separate alert type ("a new listing under $X near me"), one ping per listing, checked every 10 to 15 minutes, with filters for wanted ads, bait prices and whole PCs, measured on labelled listings like the matcher.
- [ ] **A fresh held-out set** for the matcher as it is now, captured on a later date and including at least one messier retailer.
- [ ] **A price chart in `/trend`** from the stored history.
- [ ] **A small-server guide:** the bot needs no browser, so it fits next to other services on a 1 GB VPS with a memory cap.
- [ ] **v1 import for VRAM variants:** ask which variant instead of skipping.

---

## 🧭 Why I Built This

I'm an ACCA with audit roots who now builds AI automation. Audit teaches one habit above the rest: test the population, not the sample someone hands you. A deal bot is a sampler by design. It reads a page of search results and trusts the cheapest row.

Version 1 went up in April 2026 and did its job well enough. When I audited it in October, it was quietly broken. Newegg answered every search with a CAPTCHA page and Amazon.ca with an error page, and the bot read both as "no listings". On the two retailers still answering, an RX 9070 alert accepted 9070 XT and 9070 GRE listings, sold-out cards counted as deals, and the hourly check pinged the same deal every hour while never saving the price history its trend command relied on. Nothing crashed. It was confidently wrong, which for an alert is worse than silent.

**On why rules and not a model for matching:** GPU names are a small, closed vocabulary with known traps: suffixes, VRAM variants, part codes, laptops and prebuilts that name the GPU. A tokenizer and rules, scored against labels they never saw, met every pre-registered target (Arc B580 had only 4 accepted rows on the Best Buy and Memory Express set, too few to judge on its own). A model would add cost and non-determinism to a problem that does not need it. Used listings are different, free text written by people, and that is where a classifier may earn its place, once it beats the rules on a labelled set.

It's the same split I use in **[FinAgent OS](https://github.com/RZ-Logic/finagent-os)** (governance for finance agents) and **[RegisterBench](https://github.com/RZ-Logic/RegisterBench)** (a benchmark with seeded ground truth): deterministic code where the answer can be checked, and every claim measured before it is made. The design choices are in [`docs/build-decisions.md`](docs/build-decisions.md).

---

## 🙏 Prior Art & Acknowledgements

- **[discord.py](https://github.com/Rapptz/discord.py)**, **[curl_cffi](https://github.com/lexiforest/curl_cffi)** and **[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)**: the bot, the TLS-fingerprinted HTTP client and the HTML parser.
- **[ecoppen/Amiibot](https://github.com/ecoppen/Amiibot)** uses the same Best Buy Canada search-then-availability pattern for a different product. The adapter here was written from the endpoints themselves; no code was copied (Amiibot is GPL-3.0).
- **[ColbyMay/price-checker](https://github.com/ColbyMay/price-checker)** reports "blocked" separately from "out of stock", the same line this bot draws between a block and an empty result.
- **[jef/streetmerchant](https://github.com/jef/streetmerchant)**: the reference open-source stock checker. This bot watches prices instead of restocks.

---

## 📜 License

**Code:** MIT, see [`LICENSE`](./LICENSE). Use it, fork it, add a retailer; the acknowledgement is a link back.

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
