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
| `evaluate.py` | Scores v1 and v2 on any labelled JSONL file. |
| `baseline_v1.py` | v1's filter, copied from v1's `gpu_discord_bot.py` (removed in v2; it is in the git history at commit `ba88c4b`) so the eval never imports the old bot. |
| `PREREGISTRATION.md` | Targets and protocol, fixed before tuning. |
| `dev_label_disputes.jsonl` | Dev labels the matcher author believes are wrong, with reasons. |
| `heldout.jsonl` | The held-out split (414 rows, 397 scored), with `regex_label`, `blind_label` and the headline `gold_label`. Scored once. |
| `heldout_disagreements.jsonl` | The 5 held-out rows where the automated and blind labels disagree; excluded from the headline. |
| `ood_bestbuy_memoryexpress.jsonl` | 869 rows from Best Buy and Memory Express, retailers absent from tuning, labelled by two blind labelers (`label_1`, `label_2`, `gold_label`). Scored once. |
| `adversarial_titles.py` | The code review's 100 trick titles; prints how many the matcher still accepts. |
| `results/` | Saved outputs of the scored runs and the post-review regression run. |

The held-out and out-of-distribution sets were kept out of the matcher author's reach until they were scored, and were added here afterwards.

## How to run

From the repo root:

```
python eval/evaluate.py eval/dev.jsonl
python eval/evaluate.py eval/dev.jsonl --top 25                      # longer error lists
python eval/evaluate.py eval/dev.jsonl --json-out dev_metrics.json   # metrics as JSON
python eval/evaluate.py eval/heldout.jsonl --label-field gold_label --check   # exit 1 on a missed target
python eval/evaluate.py eval/dev.jsonl --disputes eval/dev_label_disputes.jsonl   # sensitivity only
```

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

| | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| v1 substring filter | 0.755 | 0.831 | 0.791 | 354 | 115 | 72 |
| v2 matcher | 0.998 | 0.981 | 0.989 | 418 | 1 | 8 |

v1 per query precision was 0.316 for RX 9070 (XT and GRE accepted) and 0.494 for
RTX 5070 (Ti accepted). All 9 remaining v2 disagreements are rows in
`dev_label_disputes.jsonl` (6 eGPU "AI BOX" enclosures and 2 RTX 5090 D V2 China
cards labelled `correct`, and one real 5070 Ti card labelled `accessory`). These
are dev numbers on data the matcher was tuned on, so they are optimistic. The
pre-registered claim is the held-out test result.

## Caveats in full

The headline numbers (precision and recall 1.000 on both test sets) are an upper
bound, for these reasons:

- **Near-duplicates.** The held-out split shares retailers, queries and capture
  day with the tuning data: 49% of its titles have a tuning title with at least
  80% of the same words (token Jaccard on `matching.normalise()`). The
  out-of-distribution set (Best Buy and Memory Express) exists for that reason.
- **Clean titles.** Best Buy and Memory Express titles are tidy. The two blind
  labelers, who each labelled that whole set, agreed on all 869 rows.
- **Who labelled.** On the held-out split, labels came from an automated labeler
  checked against blind labels from two Sonnet agents that split the titles
  between them and read each against a written rubric without seeing any code.
  The two label sets agreed on correct versus not correct for 397 of 402 rows;
  the 5 disagreements are excluded and listed in `heldout_disagreements.jsonl`,
  and 12 more rows are excluded as unknown or unsure. No human labelled any row,
  and the 5 disagreements still await a human decision.
- **VRAM is not in the eval.** The labels name the model, not the memory size,
  so the 8GB versus 16GB rule is covered by unit tests only.
- **Small cells.** Arc B580 had 4 accepted rows on the out-of-distribution set,
  below the pre-registered minimum of 5 for judging a model on its own.
- **The matcher changed after scoring.** A code review wrote 100 trick titles
  ("RTX 4080S", "8GB/16GB", "RTX 5070 with a 5070 Ti cooler"). 51 fooled the
  matcher on its own; after the fixes, 17 still do, mostly laptops and prebuilt
  PCs that describe their GPU like a card (`adversarial_titles.py` reruns them).
  Every adapter also filters by the retailer's own category before matching
  (Walmart's filter has a gap for sponsored rows), which removes most laptops
  and prebuilt PCs. Re-scoring the fixed matcher on the same sets gave identical
  results (`results/2026-10-12-regression-after-review-fixes.txt`), but a set it
  has already seen is a regression check, not a new measurement.
