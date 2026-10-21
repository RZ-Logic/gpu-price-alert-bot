# Matcher eval

This folder measures how well the bot decides "is this listing the GPU the user
asked for". v1 used substring checks, so "TI" matched inside "EDITION", an
RTX 5070 alert accepted RTX 5070 Ti listings, and "Triple Fan" titles were thrown
away as accessories. v2 (`gpu_alerts/matching.py`) parses each title into a
listing kind, model keys and VRAM. The eval scores both on the same labelled
rows.

## Files

| File | What it is |
|---|---|
| `dev.jsonl` | 917 labelled listing rows from the 2026-10-12 live audit. The only data used for tuning. |
| `evaluate.py` | Scores v1 and v2 on any labelled JSONL file, with a 95% lower bound next to each precision. |
| `baseline_v1.py` | v1's filter, copied from v1's `gpu_discord_bot.py` (removed in v2; it is in the git history at commit `ba88c4b`) so the eval never imports the old bot. |
| `PREREGISTRATION.md` | Targets and protocol, fixed before tuning. |
| `dev_label_disputes.jsonl` | Dev labels the matcher author believes are wrong, with reasons. |
| `heldout.jsonl` | The held-out split (414 rows, 397 scored), with `regex_label`, `blind_label` and the headline `gold_label`. Scored once. |
| `heldout_disagreements.jsonl` | The 5 held-out rows where the automated and blind labels disagree; excluded from the headline. |
| `ood_bestbuy_memoryexpress.jsonl` | 869 rows (325 distinct titles) from Best Buy and Memory Express, retailers absent from tuning, labelled by two blind labelers (`label_1`, `label_2`, `gold_label`). Scored once. |
| `adversarial_titles.py` | The code review's 100 trick titles; prints how many the matcher still accepts (11). `tests/test_eval.py` pins which ones. |
| `results/` | Saved outputs of the scored runs and the post-review regression run. They predate the lower bound, so they have no `Plow` column. |

The held-out and out-of-distribution sets were kept out of the matcher author's reach until they were scored, and were added here afterwards.

## How to run

From the repo root:

```
python eval/evaluate.py eval/dev.jsonl
python eval/evaluate.py eval/dev.jsonl --top 25                      # longer error lists
python eval/evaluate.py eval/dev.jsonl --json-out dev_metrics.json   # metrics as JSON
python eval/evaluate.py eval/heldout.jsonl --label-field gold_label --check   # exit 1 on a missed target
python eval/evaluate.py eval/dev.jsonl --disputes eval/dev_label_disputes.jsonl   # sensitivity only
python eval/adversarial_titles.py
```

Each report has an overall line and a table per query. Next to each precision,
`Plow` is its one-sided 95% exact (Clopper-Pearson) lower bound, computed from
TP of the TP + FP accepted rows. It is there for reading small cells and is not
a target. With no false positives it is 0.05^(1/n): 0.652 for 7 of 7, and 29 of
29 are needed to reach 0.90. `--json-out` writes it as `precision_lower_95`,
overall and per query.

`--check` prints `pre-registered targets (v2): all met` only when the file has
scored rows for all 8 pre-registered queries. When some are missing (absent, or
every row excluded) it prints `all met, but this file has scored rows for only N
of the 8 pre-registered queries` and lists the queries it did not assess, which
the JSON also gives as `not_assessed`. A missing query is not a miss, so the exit
code is 0 unless a target on the scored rows is missed. Queries with fewer than 5
accepted rows are still flagged too small to judge.

`tests/test_eval.py` pins the v2 counts on every labelled set (rows scored, rows
excluded, TP, FP, FN and TN overall, TP and FP per query) and the 11 trick titles
the matcher still accepts. A matcher or catalog change that moves any of them
fails that test, so the change has to update the test, and the numbers here and
in the main README, in the same diff. The same file checks the Targets table in
`PREREGISTRATION.md` against the constants in `evaluate.py`.

## The protocol in plain words

Every row is one listing a retailer returned for one search, with a label that
says what the listing really is: `correct` (the searched model), `wrong_model`,
`laptop`, `prebuilt`, `accessory`, `bundle`, `other`, `correct_bundle`, `unknown` or `unsure`.
Every label except `correct` is negative; `unknown` and `unsure` are excluded.

- Only `correct` counts as a hit. A card sold together with a power supply
  (`correct_bundle`) is a miss, because its price is not the card's price.
- `unknown` rows (titles nobody could place, such as unbranded GT 210 cards) are
  left out, and the report says how many.
- The search, for example `RTX 5060 TI`, is looked up in the catalog. VRAM is
  ignored for this eval because the labels do not record it, so an 8GB and a 16GB
  5060 Ti both count as `RTX 5060 TI`. The VRAM rule is covered by unit tests
  instead (`tests/test_matching.py`).
- v2 says yes when the matcher accepts the title and the price is not below the
  catalog's sanity floor for that model.
- v1 says yes when its own old rules would have alerted.
- If a row has no price, neither version applies its price floor, so the
  comparison stays about titles.

Precision answers "when the bot pings, is it the right card?". Recall answers
"of the right cards on the page, how many did the bot see?". F1 combines them.

## Dev results (2026-10-12, `regex_label`, 881 rows scored, 36 unknown excluded)

| | Precision | Plow | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|---|
| v1 substring filter | 0.755 | 0.720 | 0.831 | 0.791 | 354 | 115 | 72 |
| v2 matcher | 0.998 | 0.989 | 0.981 | 0.989 | 418 | 1 | 8 |

v1 per query precision was 0.316 for RX 9070 (XT and GRE accepted) and 0.494 for
RTX 5070 (Ti accepted). All 9 remaining v2 disagreements are rows in
`dev_label_disputes.jsonl` (6 eGPU "AI BOX" enclosures and 2 RTX 5090 D V2 China
cards labelled `correct`, and one real 5070 Ti card labelled `accessory`). These
are dev numbers on data the matcher was tuned on, so they are optimistic. The
pre-registered claim is the held-out test result.

## Test results per query (v2, `gold_label`)

Every accepted row was correct on both test sets, so each cell's precision is
1.000 and the lower bound depends only on how many rows were accepted. Re-run on
2026-10-21 with the v2.1 matcher; the counts are the same as when each set was
scored.

| Query | Held-out accepted | Held-out Plow | Out-of-distribution accepted | OOD Plow |
|---|:-:|:-:|:-:|:-:|
| RTX 5090 | 7 | 0.652 | 27 | 0.895 |
| RTX 5080 | 49 | 0.941 | 52 | 0.944 |
| RTX 5070 TI | 36 | 0.920 | 43 | 0.933 |
| RTX 5070 | 17 | 0.838 | 33 | 0.913 |
| RTX 5060 TI | 37 | 0.922 | 46 | 0.937 |
| RX 9070 XT | 27 | 0.895 | 23 | 0.878 |
| RX 9070 | 9 | 0.717 | 10 | 0.741 |
| ARC B580 | 10 | 0.741 | 4 (too small to judge) | 0.473 |
| Overall | 192 | 0.985 | 238 | 0.987 |

Pooled over both sets, RX 9070 has 19 of 19 (Plow 0.854), ARC B580 14 of 14
(0.807), RTX 5090 34 of 34 (0.916) and all queries 430 of 430 (0.993). Every
query meets the per-query target of 0.90 as a point estimate, and only cells with
29 or more accepted rows show it at the 95% bound. v1's overall Plow is 0.692 on
the held-out split and 0.732 on the out-of-distribution set.

Sensitivity runs on the held-out split, also unchanged: against `regex_label`
alone, 403 rows scored, TP 192, FP 1, FN 5, TN 205 (Plow 0.976); against
`blind_label` alone, 413 rows scored, TP 193, FP 0, FN 0, TN 220 (Plow 0.985).
On dev with the 9 disputed labels applied: TP 419, FP 0, FN 0, TN 462 (Plow 0.993).

## Caveats in full

The headline numbers (precision and recall 1.000 on both test sets) are an upper
bound, for these reasons:

- **Near-duplicates.** The held-out split shares retailers, queries and capture
  day with the tuning data: 49% of its titles have a tuning title with at least
  80% of the same words (token Jaccard on `matching.normalise()`). The
  out-of-distribution set (Best Buy and Memory Express) exists for that reason.
- **Clean titles, repeated rows.** Best Buy and Memory Express titles are tidy.
  The two blind labelers, who each labelled that whole set, agreed on all 869
  rows. Those rows are 325 distinct titles: Best Buy returned 81 to 91 cards per
  search, most of them other models, and a title is scored once for each search
  that returned it. All 238 correct rows are distinct titles, so precision and
  recall rest on 238 different cards; the repeats are negatives. About 84% of the
  631 negatives do not contain the searched model number at all, so few of them
  test the matcher hard.
- **Who labelled.** On the held-out split, labels came from an automated labeler
  checked against blind labels from two Sonnet agents that split the titles
  between them and read each against a written rubric without seeing any code.
  The two label sets agreed on correct versus not correct for 397 of 402 rows;
  the 5 disagreements are excluded and listed in `heldout_disagreements.jsonl`,
  and 12 more rows are excluded as unknown or unsure. No human labelled any row,
  and the 5 disagreements still await a human decision.
- **VRAM is not in the eval.** The labels name the model, not the memory size,
  so the 8GB versus 16GB rule is covered by unit tests only. The catalog's
  `vram_options` check (an RX 9070 titled 12GB is a GRE) does run in the eval, as
  it does in the bot; adding it in v2.1 moved no count.
- **Small cells.** Arc B580 had 4 accepted rows on the out-of-distribution set,
  below the pre-registered minimum of 5 for judging a model on its own. Cells
  that pass the minimum can still be weak evidence: 7 of 7 (RTX 5090, held-out)
  bounds precision only at 0.652. The table above gives each cell's bound.
- **Coverage.** The 8 queries cover 9 of the 33 catalog models (the RTX 5060 Ti
  is two, 8GB and 16GB). The other 24, including the RTX 5060, RX 9070 GRE, the
  RX 9060 family, Arc B570 and the legacy cards, have no scored rows; some have
  unit tests.
- **The matcher changed after scoring, twice.** A code review wrote 100 trick
  titles ("RTX 4080S", "8GB/16GB", "RTX 5070 with a 5070 Ti cooler"). 51
  fooled the matcher on its own; after the fixes, 17 still did. The v2.1 fixes,
  from a later three-way audit, brought that to 11 of 100
  (`adversarial_titles.py` reruns them): six prebuilt PCs that describe their
  GPU like a card and name no CPU and few or no specs, a motherboard, a card
  bundled with a motherboard named without the word, a power-connector title,
  and two used
  cards, whose condition is left to the adapters. Every adapter also filters by
  the retailer's own category before matching (Walmart's filter has a gap for
  sponsored rows), which removes most laptops and prebuilt PCs. Re-scoring after
  each round of fixes gave identical counts on all three sets
  (`results/2026-10-12-regression-after-review-fixes.txt` for the first, the
  pinned counts in `tests/test_eval.py` for the second). A set the matcher has
  already seen is a regression check, not a new measurement. Under Addendum 1 of
  `PREREGISTRATION.md`, new numbers for the changed matcher need a new held-out
  set.
