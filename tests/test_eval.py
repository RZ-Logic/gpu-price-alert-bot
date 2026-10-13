"""The eval harness: v1 baseline fidelity and the scoring protocol. Offline."""

import importlib.util
import json
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


def test_targets_are_the_preregistered_ones():
    assert (evaluate.TARGET_PRECISION, evaluate.TARGET_RECALL, evaluate.TARGET_PER_QUERY_PRECISION) == (0.97, 0.90, 0.90)
    prereg = (EVAL_DIR / "PREREGISTRATION.md").read_text(encoding="utf-8")
    for number in ("0.97", "0.90"):
        assert number in prereg


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
