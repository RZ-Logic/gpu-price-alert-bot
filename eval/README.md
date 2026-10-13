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
| `baseline_v1.py` | v1's filter, copied from `gpu_discord_bot.py` so the eval never imports the old bot. |
| `PREREGISTRATION.md` | Targets and protocol, fixed before tuning. |
| `dev_label_disputes.jsonl` | Dev labels the matcher author believes are wrong, with reasons. |

The held-out test split is not in this repo.

## How to run

From the repo root:

```
python eval/evaluate.py eval/dev.jsonl
python eval/evaluate.py eval/dev.jsonl --top 25                      # longer error lists
python eval/evaluate.py eval/dev.jsonl --json-out dev_metrics.json   # metrics as JSON
python eval/evaluate.py test.jsonl --label-field gold --check        # exit 1 on a missed target
python eval/evaluate.py eval/dev.jsonl --disputes eval/dev_label_disputes.jsonl   # sensitivity only
```

## The protocol in plain words

Every row is one listing a retailer returned for one search, with a label that
says what the listing really is: `correct` (the searched model), `wrong_model`,
`laptop`, `prebuilt`, `accessory`, `correct_bundle` or `unknown`.

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
