# Pre-registration: v2 title matcher

Date: 2026-10-12

The targets and the protocol below were fixed in the v2 task specification on
2026-10-12, before the matcher in `gpu_alerts/matching.py` was written or tuned.
This file records them unchanged. They are also hard-coded in `eval/evaluate.py`
(`TARGET_*`), and `tests/test_eval.py` fails if the two drift apart.

## Targets (held-out test set, v2 matcher)

| Metric | Target |
|---|---|
| Overall precision | >= 0.97 |
| Overall recall | >= 0.90 |
| Precision on each of the 8 queries | >= 0.90 |

The 8 queries: RTX 5090, RTX 5080, RTX 5070 TI, RTX 5070, RTX 5060 TI, RX 9070 XT,
RX 9070, ARC B580.

Precision is the headline metric: a false positive is a wrong deal ping, a false
negative is a missed one.

## Data and independence

- Tuning data: `eval/dev.jsonl` only (917 rows from the 2026-10-12 live audit of
  Newegg, Amazon, Walmart and Canada Computers, page-1 search results for the 8
  queries). Its `regex_label` came from an earlier automated labeler and is
  provisional.
- Held-out test split: kept by the project owner. The matcher author did not have
  it and did not search for it.
- The old labeler's code and notes (`research/2026-10-12-audit/label.py`, the
  matching code in `fixed.py`, `norm_*.json`, any `why` fields) were not opened,
  so the matcher does not inherit its logic.
- Where a dev label looked wrong, it went into `eval/dev_label_disputes.jsonl`
  and the matcher was not bent to fit it. Disputes never change the headline
  number; `--disputes` exists only as a sensitivity run.

## Protocol

1. Each row has `title`, `query`, `price` and a label field (default
   `regex_label`, chosen with `--label-field`).
2. Positive class: label `correct`. Rows labelled `unknown` are excluded and
   their count is reported. Every other label is negative, `correct_bundle`
   included (a card plus a PSU is not the card's price).
3. Each query maps to the catalog model with that key (`GPUModel.key`), made
   VRAM-agnostic with `dataclasses.replace(model, vram_gb=None)`. For keys with
   VRAM variants (RTX 5060 TI) the price floor is the lowest of the variants'
   `min_price`.
4. v2 accepts a row when `gpu_alerts.matching.match(title, model).ok` and the
   price is at or above that floor.
5. v1 accepts a row with its own rules, re-implemented in `eval/baseline_v1.py`:
   every query word is a substring of the uppercased title, no `JUNK_KEYWORDS`
   substring, at least one `GPU_KEYWORDS` substring, price >= $200.
6. A missing price skips only the price floor, for both v1 and v2: the eval
   measures title matching, and an unknown price is not evidence about the title.
7. Reported for v1 and v2: precision, recall and F1, overall and per query,
   TP/FP/FN/TN counts, and the false positives and false negatives (cheapest
   first, since cheap false positives are the ones that fire deal alerts).

Command: `python eval/evaluate.py <test.jsonl> --label-field <field> --check`
(exits 1 if v2 misses any target).

## Addendum: which labels count (fixed 2026-10-12, before the held-out run)

The held-out split has two independent label sets: `regex_label` (the earlier
automated labeler) and `blind_label` (two Sonnet agents that read each title
against a written rubric, without seeing the regex labels or any code).

- Headline: `gold_label`, defined only where the two agree on correct versus
  not correct. Where they disagree the row is excluded from the headline and
  listed in `eval/heldout_disagreements.jsonl` for a human decision.
  `blind_label` "unsure" counts as `unknown`.
- Sensitivity runs: the same metrics against `regex_label` alone and against
  `blind_label` alone.
- The held-out run happens once. Any matcher change after it is a new version
  that needs a new held-out set before its numbers are reported.

## Addendum 2: out-of-distribution test (fixed 2026-10-12, before labeling or scoring)

The first held-out split shares retailers, queries and the capture day with the
tuning data, and 49% of its titles have a dev title with at least 80% token
overlap, so it overstates generalisation. A second test set measures the same
matcher version (no matcher changes since the first held-out run) on retailers
it never saw during tuning:

- Data: 869 listing titles collected live on 2026-10-12 from Best Buy (search
  API) and Memory Express (video-card category pages) for the same 8 queries.
  Neither retailer appears in `eval/dev.jsonl`.
- Labels: two independent blind labelers each label every row with the same
  rubric. Headline `gold_label` exists only where both agree on correct versus
  not correct; disagreements and "unsure" are excluded and listed for a human.
- Targets: the same pre-registered targets (overall precision >= 0.97, recall
  >= 0.90, per-query precision >= 0.90). Per-query precision is reported but
  queries with fewer than 5 accepted rows are flagged as too small to judge.
- Scored once, with `eval/evaluate.py --label-field gold_label --check`.
