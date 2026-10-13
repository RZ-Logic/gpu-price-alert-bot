# Changelog

## v2.0.0 (2026-10-12)

A rewrite. v1's single file is replaced by the `gpu_alerts` package.

**Retailers.** Six: Best Buy (new, JSON search and stock APIs), Canada Computers, Memory Express (new), Newegg, Amazon.ca and Walmart. Newegg and Amazon.ca work again (they returned 403 and 503 to v1). Listings carry stock, seller, condition, regular price and shipping.

**Matching.** Titles are parsed into model, suffix and VRAM instead of matched by substrings. 33 catalog models, including VRAM variants. Pre-registered evaluation: precision and recall 1.000 on 397 held-out listings and on 869 listings from two retailers the matcher never saw, against 0.746 / 0.812 and 0.776 / 0.945 for v1. Caveats are in the README.

**Alerts.** SQLite instead of JSON files. One ping per crossing, further pings only on a 3% or $25 drop, re-arming after two scans above target in which the retailer that had the deal answered, restocks after a sell-out ping again. Price history is saved on every check.

**Discord.** Slash commands reply through deferred interactions, inputs are validated, `/lookup` and `/check` have cooldowns, `/prices` serves recent data, `/health` shows retailer status, and the owner gets a DM when a retailer keeps failing. No privileged intents. Optional instant command sync to one server.

**Tooling.** CLI (`lookup`, `health`, `import-v1`), 391 offline tests and 6 live ones, CI, MIT licence file, a working `.gitignore`, pinned requirements.

**Review.** Built from a live audit of v1 (four agents: retailers, code, platform facts, market) and a GitHub survey (two agents), which found no maintained Canadian GPU price-alert bot to fork. Three reviewers raised 27 findings after the build (2 high, 9 medium, 16 low); all high and medium findings are fixed with tests (the Canada Computers search depth was widened from 48 to 96 rows, not removed); 9 low ones are fixed, 2 in part, and 5 are left open and documented under Limitations.

## v1.0 (2026-04-11)

Single-file Discord bot: Newegg.ca, Walmart.ca, Amazon.ca and Canada Computers, keyword matching, JSON files, hourly checks.
