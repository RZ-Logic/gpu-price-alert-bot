"""The eval harness: v1 baseline fidelity and the scoring protocol. Offline."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EVAL_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


baseline_v1 = _load("baseline_v1")
evaluate = _load("evaluate")
adversarial = _load("adversarial_titles")


@pytest.mark.parametrize("title,query,price,accepted", [
    # Known v1 bugs, reproduced on purpose.
    ("ASUS Prime GeForce RTX 5070 12GB GDDR7 OC Edition", "RTX 5070 TI", 1269.99, True),   # TI in EDITION
    ("GIGABYTE Gaming Radeon RX 9070 GRE 12GB GDDR6", "RX 9070", 929.99, True),            # GRE accepted
    ("ASUS Prime Radeon RX 9070 OC GDDR6 Triple Fans", "RX 9070", 1419.99, False),         # FAN is junk
    ("MSI RTX 5070 12G Ventus, DLSS 4 Frame Generation", "RTX 5070", 1299.99, False),      # RAM in FRAME
    ("GIGABYTE GV-N506TEAGLE OC-8GD GDDR7 Double Fans", "RTX 5060 TI", 740.99, False),     # code only
    ("MSI GeForce RTX 5070 12G VENTUS 2X OC", "RTX 5070", 199.0, False),                   # $200 floor
    ("MSI GeForce RTX 5070 12G VENTUS 2X OC", "RTX 5070", None, True),                     # no price: floor skipped
])
def test_baseline_v1_reproduces_v1(title, query, price, accepted):
    assert baseline_v1.accepts(title, query, price) is accepted


def test_baseline_copies_v1_lists():
    assert "FAN" in baseline_v1.JUNK_KEYWORDS and "RAM" in baseline_v1.JUNK_KEYWORDS
    assert "ARC" in baseline_v1.GPU_KEYWORDS and "OC" in baseline_v1.GPU_KEYWORDS
    assert baseline_v1.PRICE_FLOOR == 200.0


def test_query_maps_to_vram_agnostic_model_and_lowest_floor():
    model, floor = evaluate.model_for_query("RTX 5060 TI")
    assert model.key == "RTX 5060 TI" and model.vram_gb is None
    assert floor == 300            # min of the 16GB (400) and 8GB (300) floors


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "rows.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_scoring_protocol(tmp_path):
    rows = [
        {"title": "ASUS Prime GeForce RTX 5070 12GB GDDR7 OC Edition", "query": "RTX 5070", "price": 1269.99,
         "regex_label": "correct"},
        {"title": "MSI GeForce RTX 5070 Ti 16G VENTUS 3X OC", "query": "RTX 5070", "price": 1699.99,
         "regex_label": "wrong_model"},
        {"title": "ASUS Prime Radeon RX 9070 XT OC GPU + Prime 850W PSU", "query": "RX 9070 XT", "price": 1399.98,
         "regex_label": "correct_bundle"},
        {"title": "GT210 Graphics Card 1GB", "query": "RTX 5070", "price": 52.99, "regex_label": "unknown"},
        {"title": "MSI GeForce RTX 5070 12G VENTUS 2X OC", "query": "RTX 5070", "price": 99.0,
         "regex_label": "correct"},
    ]
    loaded, excluded = evaluate.load_rows(_write(tmp_path, rows), "regex_label")
    assert excluded == 1 and len(loaded) == 4
    v1, v2 = evaluate.evaluate(loaded, "regex_label")

    # v2: first row TP, 5070 Ti rejected (TN), bundle negative (TN), $99 under the 450 floor (FN).
    assert (v2.overall.tp, v2.overall.fp, v2.overall.fn, v2.overall.tn) == (1, 0, 1, 2)
    assert v2.false_negatives[0]["reason"].startswith("price")
    # v1: accepts the 5070 Ti for "RTX 5070" (FP); "PSU" junk rejects the bundle.
    assert v1.overall.fp == 1
    assert v2.per_query["RTX 5070"].precision == 1.0


def test_label_field_and_disputes(tmp_path):
    rows = [{"title": "Colorful IGame RTX 5090 D v2 Advanced OC 24GB GPU Graphics Card", "query": "RTX 5090",
             "price": 9287.99, "my_label": "correct"}]
    path = _write(tmp_path, rows)
    disputes = tmp_path / "disputes.jsonl"
    disputes.write_text(json.dumps({"title": rows[0]["title"], "query": "RTX 5090", "regex_label": "correct",
                                    "your_label": "wrong_model", "reason": "China variant"}) + "\n", encoding="utf-8")
    plain, _ = evaluate.load_rows(path, "my_label")
    relabelled, _ = evaluate.load_rows(path, "my_label", evaluate.load_disputes(disputes))
    assert plain[0]["my_label"] == "correct" and relabelled[0]["my_label"] == "wrong_model"


def _prereg_targets(text: str) -> dict[str, float]:
    """Metric -> target from the rows of PREREGISTRATION.md's Targets table."""
    section = text.split("\n## Targets", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*>=\s*([0-9.]+)\s*\|\s*$", section, re.M)
    return {metric: float(value) for metric, value in rows}


def _prereg_queries(text: str) -> tuple[str, ...]:
    section = text.split("\n## Targets", 1)[1].split("\n## ", 1)[0]
    listed = re.search(r"The (\d+) queries: ([^.]+)\.", section)
    queries = tuple(q.strip() for q in " ".join(listed.group(2).split()).split(","))
    assert int(listed.group(1)) == len(queries)
    return queries


def test_targets_are_the_preregistered_ones():
    code = {
        "Overall precision": evaluate.TARGET_PRECISION,
        "Overall recall": evaluate.TARGET_RECALL,
        "Precision on each of the 8 queries": evaluate.TARGET_PER_QUERY_PRECISION,
    }
    assert list(code.values()) == [0.97, 0.90, 0.90]
    prereg = (EVAL_DIR / "PREREGISTRATION.md").read_text(encoding="utf-8")
    assert _prereg_targets(prereg) == code
    assert _prereg_queries(prereg) == evaluate.PREREGISTERED_QUERIES
    small = re.search(r"queries\s+with fewer than (\d+) accepted rows are flagged", prereg)
    assert int(small.group(1)) == evaluate.MIN_ACCEPTED_TO_JUDGE
    # The table is what is compared: 0.97 also appears in Addendum 2, so editing only
    # the table must still show up.
    edited = prereg.replace("| Overall precision | >= 0.97 |", "| Overall precision | >= 0.80 |")
    assert edited != prereg and _prereg_targets(edited) != code


def test_cli_runs_on_dev_set(capsys):
    dev = EVAL_DIR / "dev.jsonl"
    if not dev.exists():
        pytest.skip("eval/dev.jsonl not present")
    assert evaluate.main([str(dev), "--top", "0"]) == 0
    out = capsys.readouterr().out
    assert "v1 substring filter" in out and "v2 matcher" in out


def _rows(n_correct, query="RTX 5090", title="NVIDIA GeForce RTX 5090 32GB GDDR7 Video Card", price=2899.99):
    return [{"title": title, "query": query, "price": price, "gold_label": "correct"} for _ in range(n_correct)]


def test_small_query_is_flagged_not_a_miss(tmp_path, capsys):
    # Addendum 2: one wrong accepted row on a query with fewer than 5 accepted rows is not judged.
    rows = _rows(34) + [{"title": "Intel Arc B580 12GB Graphics Card", "query": "ARC B580", "price": 359.99,
                         "gold_label": "wrong_model"}]
    path = _write(tmp_path, rows)
    loaded, _ = evaluate.load_rows(path, "gold_label")
    _, v2 = evaluate.evaluate(loaded, "gold_label")
    k = v2.per_query["ARC B580"]
    assert (k.tp, k.fp) == (0, 1) and evaluate.too_small(k)
    assert evaluate.check_targets(v2) == []
    assert evaluate.too_small_queries(v2) == ["ARC B580"]
    out_json = tmp_path / "m.json"
    assert evaluate.main([str(path), "--label-field", "gold_label", "--check", "--json-out", str(out_json)]) == 0
    out = capsys.readouterr().out
    assert "too small to judge" in out and "all met" in out
    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["too_small_to_judge"] == ["ARC B580"]
    assert doc["v2"]["per_query"]["ARC B580"]["too_small_to_judge"] is True


def test_query_with_enough_accepted_rows_still_misses():
    counts = evaluate.Counts(tp=4, fp=1)          # 5 accepted, precision 0.8
    report = evaluate.Report("v2")
    report.per_query["RTX 5080"] = counts
    report.overall = evaluate.Counts(tp=100, fp=1)
    assert not evaluate.too_small(counts)
    assert evaluate.check_targets(report) == ["RTX 5080 precision 0.800 < 0.9"]


def test_unsure_is_excluded_like_unknown(tmp_path):
    rows = _rows(2)
    rows[1]["gold_label"] = "unsure"
    rows.append({**rows[0], "gold_label": "unknown"})
    loaded, excluded = evaluate.load_rows(_write(tmp_path, rows), "gold_label")
    assert len(loaded) == 1 and excluded == 2


def test_symbol_titles_print_on_a_cp1252_console(tmp_path, monkeypatch):
    import io

    title = "ASUS TUF Gaming GeForce RTX\u2122 5090 32GB GDDR7\uff08PCIe\u00ae 5.0\uff09"
    rows = [{"title": title, "query": "RTX 5090", "price": 10999.99, "gold_label": "wrong_model"}]
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    assert evaluate.main([str(_write(tmp_path, rows)), "--label-field", "gold_label"]) == 0
    out.flush()
    assert b"GDDR7?PCIe" in out.buffer.getvalue()


# Pre-registered queries missing from a file


ONE_CARD_EACH = {   # a correct listing per pre-registered query, priced above its floor
    "RTX 5090": ("NVIDIA GeForce RTX 5090 32GB GDDR7 Video Card", 2899.99),
    "RTX 5080": ("MSI GeForce RTX 5080 16G VENTUS 3X OC Graphics Card", 1599.99),
    "RTX 5070 TI": ("ASUS TUF Gaming GeForce RTX 5070 Ti 16GB GDDR7 Graphics Card", 1199.99),
    "RTX 5070": ("MSI GeForce RTX 5070 12G VENTUS 2X OC Graphics Card", 799.99),
    "RTX 5060 TI": ("ASUS Dual GeForce RTX 5060 Ti 16GB GDDR7 Graphics Card", 599.99),
    "RX 9070 XT": ("Sapphire PULSE AMD Radeon RX 9070 XT 16GB GDDR6 Graphics Card", 999.99),
    "RX 9070": ("PowerColor Hellhound AMD Radeon RX 9070 16GB GDDR6 Graphics Card", 899.99),
    "ARC B580": ("Intel Arc B580 Limited Edition 12GB GDDR6 Graphics Card", 359.99),
}


def _one_card_each() -> list[dict]:
    return [{"title": t, "query": q, "price": p, "gold_label": "correct"} for q, (t, p) in ONE_CARD_EACH.items()]


def test_check_names_preregistered_queries_the_file_does_not_assess(tmp_path, capsys):
    # Five correct RTX 5090 rows and nothing else: the targets hold on what is there,
    # but seven of the eight queries were never scored.
    path = _write(tmp_path, _rows(5))
    out_json = tmp_path / "m.json"
    assert evaluate.main([str(path), "--label-field", "gold_label", "--check", "--json-out", str(out_json)]) == 0
    out = capsys.readouterr().out
    assert ("pre-registered targets (v2): all met, but this file has scored rows for only 1 of the 8 "
            "pre-registered queries") in out
    assert ("pre-registered queries not assessed (no scored rows in this file, not counted as a miss): "
            "RTX 5080, RTX 5070 TI, RTX 5070, RTX 5060 TI, RX 9070 XT, RX 9070, ARC B580") in out
    assert json.loads(out_json.read_text(encoding="utf-8"))["not_assessed"] == list(evaluate.PREREGISTERED_QUERIES[1:])


def test_query_whose_rows_are_all_excluded_is_not_assessed(tmp_path, capsys):
    rows = _one_card_each()
    rows[-1]["gold_label"] = "unsure"   # the ARC B580 row: excluded, so that query has none
    assert evaluate.main([str(_write(tmp_path, rows)), "--label-field", "gold_label", "--check"]) == 0
    out = capsys.readouterr().out
    assert "scored rows for only 7 of the 8 pre-registered queries" in out
    assert "not assessed (no scored rows in this file, not counted as a miss): ARC B580" in out


def test_absent_queries_do_not_hide_a_miss(tmp_path, capsys):
    rows = _rows(9) + [{"title": "MSI RTX 5090 Gaming Laptop 32GB", "query": "RTX 5090", "price": 4999.0,
                        "gold_label": "correct"}] * 3
    # Precision 1.0, recall 9/12 = 0.75 < 0.90: a miss, and still exit 1.
    assert evaluate.main([str(_write(tmp_path, rows)), "--label-field", "gold_label", "--check"]) == 1
    out = capsys.readouterr().out
    assert "pre-registered targets (v2): overall recall 0.750 < 0.9" in out and "all met" not in out
    assert "not assessed" in out


def test_complete_file_is_all_met_without_a_qualifier(tmp_path, capsys):
    assert evaluate.main([str(_write(tmp_path, _one_card_each())), "--label-field", "gold_label", "--check"]) == 0
    out = capsys.readouterr().out
    assert "pre-registered targets (v2): all met\n" in out and "not assessed" not in out


# Precision lower bound (Plow)


@pytest.mark.parametrize("accepted", [4, 5, 7, 9, 10, 28, 29, 192])
def test_lower_bound_with_no_false_positives_is_alpha_to_the_one_over_n(accepted):
    assert evaluate.precision_lower_bound(accepted, accepted) == pytest.approx(0.05 ** (1 / accepted))


def test_lower_bound_values_quoted_in_the_review():
    plow = evaluate.precision_lower_bound
    assert [round(plow(n, n), 3) for n in (5, 7, 9, 10, 19)] == [0.549, 0.652, 0.717, 0.741, 0.854]
    assert plow(28, 28) < 0.90 < plow(29, 29)   # 29 error-free rows are needed to show 0.90


@pytest.mark.parametrize("tp, accepted", [(9, 10), (418, 419), (14, 35), (156, 209)])
def test_lower_bound_with_false_positives_solves_the_binomial_tail(tp, accepted):
    from math import comb

    low = evaluate.precision_lower_bound(tp, accepted)
    tail = sum(comb(accepted, i) * low ** i * (1 - low) ** (accepted - i) for i in range(tp, accepted + 1))
    assert tail == pytest.approx(0.05, abs=1e-9)
    assert 0 < low < tp / accepted


def test_lower_bound_known_value_and_edges():
    # 9 of 10: P(X >= 9) = 10 p^9 - 9 p^10 = 0.05 at p = 0.6058 (Beta(9, 2) 5th percentile).
    assert evaluate.precision_lower_bound(9, 10) == pytest.approx(0.60584, abs=1e-5)
    assert evaluate.precision_lower_bound(0, 5) == 0.0
    assert evaluate.precision_lower_bound(0, 0) is None


def test_report_and_json_carry_the_lower_bound(tmp_path, capsys):
    path = _write(tmp_path, _rows(7))
    out_json = tmp_path / "m.json"
    evaluate.main([str(path), "--label-field", "gold_label", "--top", "0", "--json-out", str(out_json)])
    out = capsys.readouterr().out
    assert "Plow: one-sided 95% exact (Clopper-Pearson) lower bound on precision" in out
    assert "overall  P 1.000  Plow 0.652  R 1.000" in out
    assert "RTX 5090        1.000  0.652  1.000" in out
    v2 = json.loads(out_json.read_text(encoding="utf-8"))["v2"]
    assert v2["overall"]["precision_lower_95"] == pytest.approx(0.05 ** (1 / 7))
    assert v2["per_query"]["RTX 5090"]["precision_lower_95"] == pytest.approx(0.05 ** (1 / 7))


# Ratchet: the v2 numbers on every labelled set, exactly. A matcher or catalog change
# that moves any of them must change this table in the same diff, and the numbers in
# README.md and eval/README.md with it.

EVAL_SETS = {
    # file: (label field, rows scored, rows excluded, overall (TP, FP, FN, TN), per query (TP, FP))
    "dev.jsonl": ("regex_label", 881, 36, (418, 1, 8, 454), {
        "ARC B580": (8, 0), "RTX 5060 TI": (81, 0), "RTX 5070": (49, 0), "RTX 5070 TI": (68, 1),
        "RTX 5080": (79, 0), "RTX 5090": (31, 0), "RX 9070": (29, 0), "RX 9070 XT": (73, 0)}),
    "heldout.jsonl": ("gold_label", 397, 17, (192, 0, 0, 205), {
        "ARC B580": (10, 0), "RTX 5060 TI": (37, 0), "RTX 5070": (17, 0), "RTX 5070 TI": (36, 0),
        "RTX 5080": (49, 0), "RTX 5090": (7, 0), "RX 9070": (9, 0), "RX 9070 XT": (27, 0)}),
    "ood_bestbuy_memoryexpress.jsonl": ("gold_label", 869, 0, (238, 0, 0, 631), {
        "ARC B580": (4, 0), "RTX 5060 TI": (46, 0), "RTX 5070": (33, 0), "RTX 5070 TI": (43, 0),
        "RTX 5080": (52, 0), "RTX 5090": (27, 0), "RX 9070": (10, 0), "RX 9070 XT": (23, 0)}),
}


@pytest.mark.parametrize("name", list(EVAL_SETS))
def test_v2_counts_on_each_eval_set_are_pinned(name):
    label, n_rows, n_excluded, overall, per_query = EVAL_SETS[name]
    rows, excluded = evaluate.load_rows(EVAL_DIR / name, label)
    assert (len(rows), excluded) == (n_rows, n_excluded)
    _, v2 = evaluate.evaluate(rows, label)
    c = v2.overall
    assert (c.tp, c.fp, c.fn, c.tn) == overall
    assert {q: (k.tp, k.fp) for q, k in v2.per_query.items()} == per_query
    assert evaluate.absent_queries(v2) == [] and evaluate.check_targets(v2) == []


# The 11 of the code review's 100 trick titles that match() still accepts.
ADVERSARIAL_FALSE_ACCEPTS = [
    ("rtx-5080", "ASUS RTX 5080 Prime + ROG Strix B850-F Gaming WiFi"),
    ("rtx-5080", "iBUYPOWER Y40 Gaming PC Desktop, NVIDIA GeForce RTX 5080 16GB GDDR7 Graphics"),
    ("rtx-5080", "Skytech Gaming Desktop - RTX 5080 16GB GPU"),
    ("rtx-5070-ti", "CyberPowerPC Gamer Xtreme VR - RTX 5070 Ti 16GB GPU, 32GB, 2TB"),
    ("rtx-5070-ti", "Lenovo Legion Tower 5 Gen 10 with RTX 5070 Ti GPU"),
    ("rtx-5090", "Alienware Area-51 Gaming Desktop RTX 5090 32GB GDDR7"),
    ("rtx-5090", "MSI MEG Trident X2 RTX 5090 Graphics"),
    ("rtx-5090", "RTX 5090 12VHPWR 16-pin Power Connector Graphics Card"),
    ("arc-b580", "Intel B580 Motherboard"),
    ("rtx-5090", "NVIDIA GeForce RTX 5090 Founders Edition (Used)"),
    ("rtx-5090", "NVIDIA GeForce RTX 5090 Founders Edition 32GB GDDR7 - Used, Like New"),
]


def test_adversarial_false_accepts_are_pinned():
    assert len(adversarial.cases) == 100
    assert adversarial.false_accepts() == ADVERSARIAL_FALSE_ACCEPTS
