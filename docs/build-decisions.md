# Build decisions and lessons learned

The non-obvious choices in v2 and v2.1, each with what it replaced and the evidence that forced it. Back to the [README](../README.md).

## 1. A rewrite, not a patch

A live audit of v1 on 2026-10-12 found it broken in layers. Newegg.ca answered every search with a CAPTCHA page, and its markup had changed, so even a working fetch would have parsed nothing. Amazon.ca answered with an error page because `curl_cffi`'s `"chrome"` alias now pointed at a fingerprint Amazon refuses. The matcher accepted the wrong variant. There was no stock or seller check, the hourly loop never saved history, the same deal was pinged every hour, and a load, await, save cycle on a JSON file could drop alerts edited mid-scan. Each fix touched the others, so v2 was rebuilt as a package with fixed interfaces (`Listing`, `ScrapeResult`, `Status`, `match()`), and the modules were built against them in parallel.

> **The principle:** when the defects share a cause (no contract between fetching, matching and alerting), fix the contract, not the symptoms.

## 2. Rules, measured, instead of a model, for matching

GPU names are a closed vocabulary with known traps: suffixes (Ti, SUPER, XT, XTX, GRE), VRAM variants, vendor part codes (`GV-N507T...` is a 5070 Ti), China variants (5090 D), workstation cards, laptops and prebuilts that name the GPU. A tokenizer and ordered rules handle those, and they are cheap, fast and repeatable. A language model would have added cost and run-to-run variation to a problem that rules could already solve, and there would have been no way to know without a measurement. So the targets were pre-registered, the matcher was tuned only on a development split, and it was scored once on a held-out split and once on two retailers it never saw.

> **The principle:** use a model where the rules run out, and prove that they ran out first.

## 3. The evaluation guards against its own optimism

The first held-out score was perfect, which is a reason to check, not to celebrate. Two problems were on the record. First, 49% of held-out titles had a development title with at least 80% of the same words, because the same products are listed at several stores. Second, the labels came from an automated labeler whose logic could have leaked into the matcher. The answers: the matcher's author was told not to open the old labeler, its transcript was checked for any access to the held-out files, two blind labeler agents labelled the titles from a rubric (each held-out title by one of them; both labelled every title in the second test set), and a second test set came from two retailers absent from development. A code review then wrote 100 adversarial titles, and 51 of them fooled the matcher on its own; no test set contained them. The fixes after that review brought the count to 17, and v2.1 brought it to 11. Those 11 are documented and still accepted: mostly prebuilt PCs that describe their GPU like a card, where the obvious rules (a bare DESKTOP, or dropping the card-noun gate) would also reject real cards in the labelled data, such as cards titled "..., Desktop PC". The README reports the changed matcher's numbers on already-seen sets as a regression check, not as new evidence. Since v2.1 it also shows a 95% lower bound next to each precision, because 7 correct out of 7 is weak evidence for a 0.90 target: the bound is 0.65.

> **The principle:** a held-out number is only as good as the ways it could have leaked. Name them next to the number.

## 4. JSON endpoints, not browsers

Every retailer that blocked a plain request had a structured route that did not need a browser. Best Buy's web pages block automated requests, but its JSON search and stock APIs answer them. Memory Express's search page is behind a Cloudflare challenge, but its video-card category page accepts a search term. Newegg and Walmart embed their results as JSON in the page. A headless browser typically needs hundreds of megabytes of memory per instance and breaks when a page layout changes. Requests and JSON keep the bot small enough to run next to other services.

> **The principle:** look for the data the page is built from before automating the page.

## 5. Pinned fingerprints and a block is a status

`impersonate="chrome"` follows the library version, and the version installed during the audit mapped it to a fingerprint Amazon.ca refuses. v2 pins the targets that were measured to work (`chrome124`, `safari18_0`, then fallbacks) and pins `curl_cffi` itself. Every outcome is a `Status`: a 403, a CAPTCHA, Amazon's 200-status interstitial and Walmart's block page are `BLOCKED`, and a block on page 2 still marks the retailer, so the run skips it instead of asking again. v1 read a block as "no listings", which is how two of its four retailers could fail without anyone noticing (they were failing when it was audited in October). v2.1 closed three gaps the review found. A block followed by a timeout or a 5xx on retry was reported as the last error, which hid it from the run's breaker; it now stays a block. Challenge pages were checked only when small; the specific markers are now looked for in the first 200 KB of any page. And result rows that are there but unreadable (a layout change) read as an empty page at four retailers; they are now `PARSE_ERROR`, which the owner hears about.

> **The principle:** a scraper that cannot tell "blocked" from "nothing for sale" will eventually lie to you quietly.

## 6. The default offer is the one you can buy

The cheapest row in a search is often unbuyable: sold out (Canada Computers' cheapest RX 9070 XT in the audit), a marketplace reseller at a strange price, open box, or the wrong card. The default policy is new, not marked sold out, sold by the retailer, at or above a per-model sanity floor. Open box and marketplace sellers are per-alert options. Walmart's graphics cards all came from marketplace sellers, so Walmart contributes only to alerts that opt in. That is a visible trade-off, not a bug.

> **The principle:** a deal alert's price must be a price you can pay.

## 7. Alerts are a state machine with a memory

"Price under target, so notify" pings every hour. v2 stores the notified price and, since v2.1, every retailer that had an offer at or under target. A second ping needs a real further drop (3% or $25). Re-arming needs two scans above target, counted from the one in which the last of those retailers answered completely, because a retailer that is blocked, or whose second page timed out, makes the best price look higher for that run. v2.0 tracked only the notified retailer, and the review showed the gap: when the deal had moved to another retailer and that one was blocked, the ping repeated. A partial read still feeds prices and pings, but it never counts as proof that a deal is gone. The hold has a limit: after 24 scans above target in which a holder did not answer completely, the alert re-arms, so a retailer that stays broken cannot mute it for good. "Everything answered, nothing buyable" counts toward re-arming, so a restock after a sell-out pings again. The machine is a pure function with table-driven tests.

> **The principle:** an alert that repeats itself trains you to ignore it.

## 8. Build or fork: the survey said build

Before the rewrite, two agents searched GitHub for an existing Canadian GPU price-alert bot. The closest large project, streetmerchant, is a stock checker driven by hand-listed product URLs in TypeScript, with a price ceiling per GPU series but no per-user price targets or price history. The Canadian projects found were tiny; most were unlicensed or covered one retailer, and the one MIT-licensed multi-retailer tracker used a headless browser and had given up on Amazon.ca. The useful finds were facts: Best Buy's endpoints, and the habit of reporting blocks separately. Those were reimplemented from the endpoints, not copied.

> **The principle:** borrow facts freely and code only under its licence; when nothing fits, build.

## 9. An audit's findings are claims too

v2.1 started from three independent audits of v2.0, by Claude, Gemini and GPT agents. Many of their claims did not hold, including some about code that is not in this repo. So a finding was kept only after a second agent had tried to refute it and failed, and arbiters settled severity disputes under one rubric (high means a wrong, missed or repeated ping, a dead loop, data loss or a security exposure in normal use). 54 issues held up: 10 medium, 44 low, none high. Each regression test was seen failing on the old code before its fix went in. Where a fix was judged worse than the defect, as a bare DESKTOP rule for prebuilt PCs was, the gap is documented in the README instead.

> **The principle:** a review is a list of hypotheses until each claim has been checked.
