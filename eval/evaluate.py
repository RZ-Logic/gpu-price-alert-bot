"""Score the v1 substring filter and the v2 matcher on a labelled JSONL file.

Usage (from the repo root):

    python eval/evaluate.py eval/dev.jsonl
    python eval/evaluate.py eval/dev.jsonl --label-field regex_label --top 15
    python eval/evaluate.py test.jsonl --check      # exit 1 if v2 misses a pre-registered target

Each row needs title, query (a catalog key such as "RTX 5060 TI"), price (number
or null) and a label field. Positive class: label "correct". Rows labelled
"unknown" or "unsure" are excluded and counted. Every other label,
"correct_bundle" included, is negative. Per-query precision on fewer than 5
accepted rows is reported but flagged too small to judge and is not a target
miss (Addendum 2). A pre-registered query with no scored rows in the file is
listed as not assessed; it is not a miss either, so "all met" then says for how
many of the 8 queries the file has scored rows. Next to each precision, Plow is its one-sided 95%
exact (Clopper-Pearson) lower bound, for reading small cells; it is reported,
not a target. See eval/README.md and eval/PREREGISTRATION.md.
"""

import argparse
import dataclasses
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import baseline_v1  # noqa: E402
from gpu_alerts import catalog  # noqa: E402
from gpu_alerts.catalog import GPUModel  # noqa: E402
from gpu_alerts.matching import match  # noqa: E402

POSITIVE = "correct"
EXCLUDED = "unknown"
EXCLUDED_LABELS = frozenset({EXCLUDED, "unsure"})   # Addendum 2: "unsure" counts as unknown

# Pre-registered on 2026-10-12 (eval/PREREGISTRATION.md). Do not edit.
TARGET_PRECISION = 0.97
TARGET_RECALL = 0.90
TARGET_PER_QUERY_PRECISION = 0.90
MIN_ACCEPTED_TO_JUDGE = 5      # Addendum 2: fewer accepted rows per query is too small to judge
TOO_SMALL = "too small to judge"
PREREGISTERED_QUERIES = ("RTX 5090", "RTX 5080", "RTX 5070 TI", "RTX 5070", "RTX 5060 TI",
                         "RX 9070 XT", "RX 9070", "ARC B580")
NOT_ASSESSED = "not assessed"
LOWER_BOUND_CONFIDENCE = 0.95  # for Plow, which is reported, not a target


def _at_least(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p) with 0 < p < 1, summed in log space."""
    log_p, log_q = math.log(p), math.log1p(-p)
    log_n = math.lgamma(n + 1)
    return sum(
        math.exp(log_n - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q)
        for i in range(k, n + 1)
    )


def precision_lower_bound(tp: int, accepted: int, confidence: float = LOWER_BOUND_CONFIDENCE) -> float | None:
    """One-sided exact (Clopper-Pearson) lower confidence bound on precision.

    The lowest true precision under which tp or more of the accepted rows would be
    correct with probability at least 1 - confidence. With no false positives it is
    (1 - confidence) ** (1 / accepted): 0.652 for 7 of 7, and 29 of 29 are needed
    to reach 0.90. None when nothing was accepted.
    """
    if accepted <= 0:
        return None
    alpha = 1.0 - confidence
    if tp <= 0:
        return 0.0
    if tp >= accepted:
        return alpha ** (1.0 / accepted)
    lo, hi = 0.0, 1.0
    for _ in range(60):   # P(X >= tp) rises with p; bisect for the p where it equals alpha
        mid = (lo + hi) / 2
        if _at_least(tp, accepted, mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, predicted: bool, actual: bool) -> None:
        if predicted and actual:
            self.tp += 1
        elif predicted:
            self.fp += 1
        elif actual:
            self.fn += 1
        else:
            self.tn += 1

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def precision_lower(self) -> float | None:
        return precision_lower_bound(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)


@dataclass
class Report:
    name: str
    overall: Counts = field(default_factory=Counts)
    per_query: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    false_positives: list[dict] = field(default_factory=list)
    false_negatives: list[dict] = field(default_factory=list)


def model_for_query(query: str) -> tuple[GPUModel, float]:
    """VRAM-agnostic catalog model for a query key, and the lowest min_price among its VRAM variants."""
    key = query.upper().strip()
    hits = [m for m in catalog.load() if m.key == key]
    if not hits:
        raise SystemExit(f"query {query!r} is not a catalog model key")
    return dataclasses.replace(hits[0], vram_gb=None), min(m.min_price for m in hits)


def v2_accepts(title: str, model: GPUModel, floor: float, price: float | None) -> tuple[bool, str]:
    result = match(title, model)
    if not result.ok:
        return False, result.reason
    if price is not None and price < floor:
        return False, f"price {price:.2f} < floor {floor:.0f}"
    return True, "match"


def v1_accepts(title: str, query: str, price: float | None) -> tuple[bool, str]:
    if not baseline_v1.title_has_query(title, query):
        return False, "query words missing"
    if not baseline_v1.is_valid_gpu(title, price):
        return False, "junk / no gpu keyword / under $200"
    return True, "accepted"


def load_disputes(path: Path) -> dict[tuple[str, str], str]:
    """(title, QUERY) -> your_label from a dev_label_disputes.jsonl file."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[(d["title"], d["query"].upper().strip())] = d["your_label"]
    return out


def load_rows(path: Path, label_field: str,
              disputes: dict[tuple[str, str], str] | None = None) -> tuple[list[dict], int]:
    """Rows to score and the number excluded as unknown or unsure. Disputed rows take the disputed label."""
    rows, excluded = [], 0
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            for k in ("title", "query", label_field):
                if k not in row:
                    raise SystemExit(f"{path}:{n}: missing field {k!r}")
            if disputes:
                row[label_field] = disputes.get((row["title"], row["query"].upper().strip()), row[label_field])
            if row[label_field] in EXCLUDED_LABELS:
                excluded += 1
                continue
            rows.append(row)
    return rows, excluded


def evaluate(rows: list[dict], label_field: str) -> tuple[Report, Report]:
    v1, v2 = Report("v1 substring filter"), Report("v2 matcher")
    models: dict[str, tuple[GPUModel, float]] = {}
    for row in rows:
        query = row["query"].upper().strip()
        if query not in models:
            models[query] = model_for_query(query)
        model, floor = models[query]
        actual = row[label_field] == POSITIVE
        price = row.get("price")
        for report, (predicted, reason) in (
            (v1, v1_accepts(row["title"], query, price)),
            (v2, v2_accepts(row["title"], model, floor, price)),
        ):
            report.overall.add(predicted, actual)
            report.per_query[query].add(predicted, actual)
            item = {"query": query, "price": price, "label": row[label_field],
                    "reason": reason, "title": row["title"]}
            if predicted and not actual:
                report.false_positives.append(item)
            elif actual and not predicted:
                report.false_negatives.append(item)
    return v1, v2


def _fmt(x: float | None) -> str:
    return "  n/a" if x is None else f"{x:.3f}"


def _price_key(item: dict) -> float:
    return item["price"] if item["price"] is not None else float("inf")


def print_report(r: Report, top: int) -> None:
    c = r.overall
    print(f"== {r.name} ==")
    print(f"overall  P {_fmt(c.precision)}  Plow {_fmt(c.precision_lower)}  R {_fmt(c.recall)}  "
          f"F1 {_fmt(c.f1)}  TP {c.tp}  FP {c.fp}  FN {c.fn}  TN {c.tn}")
    print(f"{'query':<14}{'P':>7}{'Plow':>7}{'R':>7}{'F1':>7}{'TP':>5}{'FP':>5}{'FN':>5}{'TN':>5}")
    for q in sorted(r.per_query):
        k = r.per_query[q]
        flag = f"  ({TOO_SMALL}: {k.tp + k.fp} accepted)" if too_small(k) else ""
        print(f"{q:<14}{_fmt(k.precision):>7}{_fmt(k.precision_lower):>7}{_fmt(k.recall):>7}{_fmt(k.f1):>7}"
              f"{k.tp:>5}{k.fp:>5}{k.fn:>5}{k.tn:>5}{flag}")
    for name, items in (("false positives (cheapest first)", r.false_positives),
                        ("false negatives (cheapest first)", r.false_negatives)):
        print(f"-- top {min(top, len(items))} of {len(items)} {name}")
        for it in sorted(items, key=_price_key)[:top]:
            price = "   n/a" if it["price"] is None else f"{it['price']:>8.2f}"
            print(f"  [{it['query']}] {price}  {it['label']:<14} {it['reason']:<34} {it['title'][:110]}")
    print()


def too_small(k: Counts) -> bool:
    """Addendum 2: per-query precision on fewer than 5 accepted rows is not judged."""
    return k.tp + k.fp < MIN_ACCEPTED_TO_JUDGE


def too_small_queries(r: Report) -> list[str]:
    return [q for q, k in sorted(r.per_query.items()) if too_small(k)]


def absent_queries(r: Report) -> list[str]:
    """Pre-registered queries with no scored row in the file (absent, or every row excluded)."""
    return [q for q in PREREGISTERED_QUERIES if q not in r.per_query]


def check_targets(r: Report) -> list[str]:
    """Pre-registered targets the v2 report misses (empty list means all met).

    Queries with too few accepted rows to judge are left out (see too_small_queries).
    """
    misses = []
    p, rec = r.overall.precision or 0.0, r.overall.recall or 0.0
    if p < TARGET_PRECISION:
        misses.append(f"overall precision {p:.3f} < {TARGET_PRECISION}")
    if rec < TARGET_RECALL:
        misses.append(f"overall recall {rec:.3f} < {TARGET_RECALL}")
    for q, k in sorted(r.per_query.items()):
        if k.precision is not None and not too_small(k) and k.precision < TARGET_PER_QUERY_PRECISION:
            misses.append(f"{q} precision {k.precision:.3f} < {TARGET_PER_QUERY_PRECISION}")
    return misses


def to_json(r: Report) -> dict:
    def counts(k: Counts) -> dict:
        return {"precision": k.precision, "precision_lower_95": k.precision_lower,
                "recall": k.recall, "f1": k.f1, "tp": k.tp, "fp": k.fp, "fn": k.fn, "tn": k.tn}

    def per_query(k: Counts) -> dict:
        return {**counts(k), "too_small_to_judge": too_small(k)}
    return {"name": r.name, "overall": counts(r.overall),
            "per_query": {q: per_query(k) for q, k in sorted(r.per_query.items())}}


def main(argv: list[str] | None = None) -> int:
    # Titles carry symbols such as trademark signs and fullwidth punctuation; a
    # cp1252 console (or a redirect on Windows) must not crash the run on them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path, help="labelled JSONL file")
    ap.add_argument("--label-field", default="regex_label")
    ap.add_argument("--top", type=int, default=10, help="false positives / negatives to list")
    ap.add_argument("--json-out", type=Path, help="also write the metrics as JSON")
    ap.add_argument("--check", action="store_true", help="exit 1 if v2 misses a pre-registered target")
    ap.add_argument("--disputes", type=Path,
                    help="sensitivity run: relabel rows listed in a disputes JSONL (never for the headline number)")
    args = ap.parse_args(argv)

    disputes = load_disputes(args.disputes) if args.disputes else None
    rows, excluded = load_rows(args.path, args.label_field, disputes)
    v1, v2 = evaluate(rows, args.label_field)
    print(f"{args.path}: {len(rows)} rows scored, {excluded} excluded as unknown or unsure, "
          f"label field {args.label_field!r}"
          + (f", {len(disputes)} disputed labels applied from {args.disputes}" if disputes else ""))
    print(f"Plow: one-sided {LOWER_BOUND_CONFIDENCE:.0%} exact (Clopper-Pearson) lower bound on precision, "
          "from TP of the TP + FP accepted rows\n")
    print_report(v1, args.top)
    print_report(v2, args.top)

    misses = check_targets(v2)
    small = too_small_queries(v2)
    absent = absent_queries(v2)
    n_queries = len(PREREGISTERED_QUERIES)
    if misses:
        summary = "; ".join(misses)
    elif absent:
        summary = (f"all met, but this file has scored rows for only {n_queries - len(absent)} "
                   f"of the {n_queries} pre-registered queries")
    else:
        summary = "all met"
    print("pre-registered targets (v2): " + summary)
    if small:
        print(f"per-query precision {TOO_SMALL} (fewer than {MIN_ACCEPTED_TO_JUDGE} accepted rows, "
              f"not counted as a miss): " + ", ".join(small))
    if absent:
        print(f"pre-registered queries {NOT_ASSESSED} (no scored rows in this file, not counted as a miss): "
              + ", ".join(absent))
    if args.json_out:
        args.json_out.write_text(json.dumps(
            {"path": str(args.path), "rows": len(rows), "excluded_unknown": excluded,
             "v1": to_json(v1), "v2": to_json(v2), "target_misses": misses,
             "too_small_to_judge": small, "not_assessed": absent}, indent=2), encoding="utf-8")
    return 1 if args.check and misses else 0


if __name__ == "__main__":
    sys.exit(main())
