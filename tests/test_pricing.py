import sys
import types
from dataclasses import dataclass

import pytest

from gpu_alerts import catalog, pricing, retailers
from gpu_alerts.pricing import Policy
from gpu_alerts.retailers.base import Listing, ScrapeResult, Status

M5080 = catalog.by_id("rtx-5080")


@dataclass
class FakeMatch:
    ok: bool
    reason: str = ""


def matcher(title, model):
    """Stand-in for matching.match: the model key must appear and no extra suffix."""
    t = title.upper()
    return FakeMatch(model.key in t and f"{model.key} TI" not in t and f"{model.key} SUPER" not in t)


def L(price, title="ASUS RTX 5080 16GB", retailer="bestbuy", **kw):
    kw.setdefault("url", f"https://example.test/{retailer}/{price}")
    return Listing(retailer=retailer, title=title, price=price, **kw)


def R(retailer, *listings, status=Status.OK):
    return ScrapeResult(retailer, "rtx 5080", status, list(listings), n_raw=len(listings))


def test_offers_filters_and_sorts():
    results = [
        R("bestbuy",
          L(1599.99, in_stock=True, first_party=True),
          L(1399.99, in_stock=False, first_party=True),                 # out of stock
          L(1499.99, condition="open_box", in_stock=True),              # open box
          L(899.0, title="RTX 5080 backplate", in_stock=True),          # under min_price
          L(1450.0, title="RTX 5070 Ti 16GB", in_stock=True)),          # wrong model
        R("walmart",
          L(1549.0, retailer="walmart", first_party=False, seller="X", in_stock=True),  # marketplace
          L(1579.0, retailer="walmart", in_stock=None)),                # unknown stock allowed
        R("newegg", L(1700.0, retailer="newegg", condition="refurbished", in_stock=True),
          L(1300.0, retailer="newegg", condition="used", in_stock=True)),
    ]
    got = pricing.offers(M5080, results, Policy(), matcher)
    assert [(o.retailer, o.price) for o in got] == [("walmart", 1579.0), ("bestbuy", 1599.99)]

    wide = pricing.offers(M5080, results, Policy(include_open_box=True, include_marketplace=True), matcher)
    assert [o.price for o in wide] == [1499.99, 1549.0, 1579.0, 1599.99, 1700.0]  # used never counts

    strict = pricing.offers(M5080, results, Policy(include_unknown_stock=False), matcher)
    assert [o.price for o in strict] == [1599.99]
    assert pricing.best(M5080, results, Policy(), matcher).price == 1579.0
    assert pricing.best(M5080, [], Policy(), matcher) is None


def test_offers_dedupes_same_listing_and_skips_matcher_errors():
    def flaky(title, model):
        if "boom" in title:
            raise RuntimeError("matcher bug")
        return matcher(title, model)

    results = [R("bestbuy", L(1600.0, sku="A"), L(1550.0, sku="A"), L(1500.0, title="RTX 5080 boom"))]
    got = pricing.offers(M5080, results, Policy(), flaky)
    assert [(o.sku, o.price) for o in got] == [("A", 1550.0)]


def test_best_per_retailer_keeps_cheapest():
    found = [L(1500, retailer="a"), L(1400, retailer="b"), L(1450, retailer="b"), L(1600, retailer="a")]
    per = pricing.best_per_retailer(found)
    assert list(per) == ["b", "a"]
    assert per["a"].price == 1500


def test_status_label():
    assert pricing.status_label(R("x", status=Status.BLOCKED), None) == "blocked"
    assert pricing.status_label(R("x", status=Status.NETWORK_ERROR), None) == "error"
    assert pricing.status_label(R("x", status=Status.PARSE_ERROR), None) == "error"
    assert pricing.status_label(R("x", status=Status.EMPTY), None) == "no match"
    assert pricing.status_label(R("x", status=Status.DISABLED), None) == "disabled"
    assert pricing.status_label(R("x"), L(1234.5, in_stock=True)) == "$1,234.50, in stock"
    label = pricing.status_label(R("x"), L(1234.5, condition="open_box", first_party=False, seller="Bob"))
    assert label == "$1,234.50, stock unknown, open box, sold by Bob"


@pytest.fixture
def fake_retailers(monkeypatch):
    """Install three fake adapter modules and point the registry at them."""
    mods = {}

    def make(rid, behaviour):
        mod = types.ModuleType(f"fake_{rid}")
        mod.RETAILER_ID = rid
        mod.DISPLAY_NAME = rid.title()
        mod.calls = []

        def search(query):
            mod.calls.append(query)
            return behaviour(query)

        mod.search = search
        monkeypatch.setitem(sys.modules, f"fake_{rid}", mod)
        mods[rid] = mod

    make("good", lambda q: ScrapeResult("good", q, Status.OK, [L(1500.0, retailer="good")], n_raw=1))
    make("crash", lambda q: (_ for _ in ()).throw(ValueError("bad json")))
    make("net", lambda q: (_ for _ in ()).throw(ConnectionError("reset")))
    make("off", lambda q: ScrapeResult("off", q, Status.OK))
    registry = {rid: f"fake_{rid}" for rid in mods}
    monkeypatch.setattr(retailers, "RETAILERS", registry)
    monkeypatch.setattr(retailers.config, "DISABLED_RETAILERS", {"off"})
    return mods


def test_scan_runs_all_and_converts_exceptions(fake_retailers):
    results = pricing.scan("rtx 5080")
    by = {r.retailer: r for r in results}
    assert [r.retailer for r in results] == ["good", "crash", "net", "off"]
    assert by["good"].status is Status.OK
    assert by["crash"].status is Status.PARSE_ERROR and "ValueError" in by["crash"].detail
    assert by["net"].status is Status.NETWORK_ERROR
    assert by["off"].status is Status.DISABLED
    assert fake_retailers["off"].calls == []


def test_scan_subset_and_unknown_id(fake_retailers):
    results = pricing.scan("rtx 5080", ["good", "nope"])
    assert [(r.retailer, r.status) for r in results] == [("good", Status.OK), ("nope", Status.DISABLED)]


def test_scan_models_dedupes_shared_queries():
    calls = []

    def scanner(query, retailer_ids):
        calls.append(query)
        return [ScrapeResult("bestbuy", query, Status.OK)]

    models = [catalog.by_id(i) for i in ("rtx-5060-ti-16gb", "rtx-5060-ti-8gb", "rtx-5080", "rtx-5080")]
    scans = pricing.scan_models(models, ["bestbuy"], scanner=scanner)
    assert calls == ["rtx 5060 ti", "rtx 5080"]
    assert set(scans) == {"rtx-5060-ti-16gb", "rtx-5060-ti-8gb", "rtx-5080"}
    assert scans["rtx-5060-ti-16gb"].results is scans["rtx-5060-ti-8gb"].results
    assert scans["rtx-5080"].n_ok == 1


def test_scan_models_skips_a_retailer_after_a_block():
    asked = []

    def scanner(query, retailer_ids):
        asked.append(list(retailer_ids))
        status = {"amazon": Status.BLOCKED, "bestbuy": Status.OK}
        return [ScrapeResult(rid, query, status[rid]) for rid in retailer_ids]

    models = [catalog.by_id(i) for i in ("rtx-5080", "rtx-5070")]
    scans = pricing.scan_models(models, ["bestbuy", "amazon"], scanner=scanner)
    assert asked == [["bestbuy", "amazon"], ["bestbuy"]]
    second = {r.retailer: r for r in scans["rtx-5070"].results}
    assert second["amazon"].status is Status.BLOCKED
    assert second["amazon"].detail.startswith(pricing.SKIPPED_PREFIX)
    assert not scans["rtx-5080"].results[1].detail.startswith(pricing.SKIPPED_PREFIX)  # the real block
    assert second["bestbuy"].status is Status.OK


def test_scan_models_skips_a_retailer_blocked_on_a_later_page():
    asked = []

    def scanner(query, retailer_ids):
        asked.append(list(retailer_ids))
        return [
            ScrapeResult(rid, query, Status.OK, blocked_partway=(rid == "amazon"))
            for rid in retailer_ids
        ]

    models = [catalog.by_id(i) for i in ("rtx-5080", "rtx-5070")]
    pricing.scan_models(models, ["bestbuy", "amazon"], scanner=scanner)
    assert asked == [["bestbuy", "amazon"], ["bestbuy"]]


def test_status_label_explains_why_nothing_is_eligible():
    model = catalog.by_id("rtx-5080")
    yes = lambda title, m: type("M", (), {"ok": True})()
    sold_out = R("bestbuy", L(1500.0, in_stock=False), L(1600.0, in_stock=False))
    market = R("bestbuy", L(1500.0, in_stock=True, first_party=False))
    open_box = R("bestbuy", L(1500.0, in_stock=True, condition="open_box"))
    assert pricing.status_label(sold_out, None, model, yes) == "sold out (2 found)"
    assert pricing.status_label(market, None, model, yes) == "marketplace only (1 found)"
    assert pricing.status_label(open_box, None, model, yes) == "open box only (1 found)"
    no = lambda title, m: type("M", (), {"ok": False})()
    assert pricing.status_label(sold_out, None, model, no) == "no match"
