import sys
import types
from dataclasses import dataclass

import pytest

from gpu_alerts import catalog, pricing, retailers
from gpu_alerts.pricing import Policy
from gpu_alerts.retailers.base import Listing, PickupStore, ScrapeResult, Status

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


def test_scan_models_keeps_a_retailer_whose_later_page_timed_out():
    # A partial read without a block (timeout, HTTP or parse error on page 2) must not
    # skip the retailer for the rest of the run; only block evidence trips the breaker.
    asked = []

    def scanner(query, retailer_ids):
        asked.append(list(retailer_ids))
        return [ScrapeResult(rid, query, Status.OK, partial=(rid == "amazon")) for rid in retailer_ids]

    models = [catalog.by_id(i) for i in ("rtx-5080", "rtx-5070")]
    scans = pricing.scan_models(models, ["bestbuy", "amazon"], scanner=scanner)
    assert asked == [["bestbuy", "amazon"], ["bestbuy", "amazon"]]
    assert scans["rtx-5080"].results[1].partial is True


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


# In-store pickup (v2.2). Canada Computers rows sold out online but held by a branch.

TORONTO = PickupStore("ON", "Toronto Downtown", "1")
NORTH_YORK = PickupStore("ON", "North York Centerpoint Mall", "1")
MISSISSAUGA = PickupStore("ON", "Mississauga", "1")
RICHMOND_BC = PickupStore("BC", "Richmond", "2")


def CC(price, *stores, in_stock=False, **kw):
    """A Canada Computers listing, sold out online unless in_stock says so, held by the given branches."""
    kw.setdefault("first_party", True)
    return L(price, retailer="canadacomputers", in_stock=in_stock, pickup_stores=tuple(stores), **kw)


def test_pickup_key_folds_case_dots_spaces_and_accents():
    assert pricing.pickup_key("  St.  Catharines ") == "st catharines"
    assert pricing.pickup_key("MONTRÉAL") == "montreal"
    assert pricing.pickup_key("Québec City") == "quebec city"


def test_parse_pickup():
    assert pricing.parse_pickup("ON") == (frozenset({"on"}), [])
    assert pricing.parse_pickup("Oakville, burlington") == (frozenset({"oakville", "burlington"}), [])
    assert pricing.parse_pickup("on, BC, Mississauga") == (frozenset({"on", "bc", "mississauga"}), [])
    assert pricing.parse_pickup("St. Catharines") == (frozenset({"st catharines"}), [])
    assert pricing.parse_pickup("north york   centerpoint mall") == (frozenset({"north york centerpoint mall"}), [])
    assert pricing.parse_pickup("Ontario, Québec") == (frozenset({"on", "qc"}), [])
    assert pricing.parse_pickup("Marché Central") == (frozenset({"marche central"}), [])
    # Richmond (BC) and Richmond Hill (ON) are two branches.
    assert pricing.parse_pickup("Richmond, Richmond Hill") == (frozenset({"richmond", "richmond hill"}), [])


def test_parse_pickup_blank_means_off_and_unknown_words_come_back_as_typed():
    for blank in (None, "", "   ", ",", " , ,"):
        assert pricing.parse_pickup(blank) == (frozenset(), [])
    tokens, unknown = pricing.parse_pickup("ON, Winnipeg , Online, *, nowhere")
    assert tokens == frozenset({"on"})
    assert unknown == ["Winnipeg", "Online", "*", "nowhere"]


def test_every_branch_name_parses_to_itself():
    from gpu_alerts.retailers.canadacomputers import BRANCHES

    for province, names in BRANCHES.items():
        assert pricing.parse_pickup(province) == (frozenset({province.lower()}), [])
        for name in names:
            assert pricing.parse_pickup(name) == (frozenset({pricing.pickup_key(name)}), [])


def test_pickup_matches():
    li = CC(9499.0, TORONTO, MISSISSAUGA, RICHMOND_BC)
    assert Policy().pickup_matches(li) == ()
    assert Policy(pickup=frozenset({"on"})).pickup_matches(li) == (TORONTO, MISSISSAUGA)
    assert Policy(pickup=frozenset({"bc"})).pickup_matches(li) == (RICHMOND_BC,)
    assert Policy(pickup=frozenset({"mississauga"})).pickup_matches(li) == (MISSISSAUGA,)
    assert Policy(pickup=frozenset({"richmond hill"})).pickup_matches(li) == ()
    assert Policy(pickup=frozenset({"oakville", "qc"})).pickup_matches(li) == ()
    assert Policy(pickup=frozenset({pricing.ANY_PICKUP})).pickup_matches(li) == li.pickup_stores
    assert Policy(pickup=frozenset({"on"})).pickup_matches(CC(9499.0)) == ()


def test_pickup_off_is_the_default():
    assert pricing.DEFAULT_POLICY.pickup == frozenset()
    assert pricing.DEFAULT_POLICY == Policy()
    assert pricing.RECORD_POLICY.pickup == frozenset({pricing.ANY_PICKUP})


def test_offers_keeps_pickup_only_rows_only_when_the_policy_asks():
    results = [R("canadacomputers",
                 CC(9499.0, TORONTO, NORTH_YORK, MISSISSAUGA, title="PNY RTX 5080 Triple Fan"),
                 CC(9300.0, title="ASUS RTX 5080 sold out everywhere"),
                 CC(9999.0, RICHMOND_BC, title="ZOTAC RTX 5080 AIO"))]

    def prices(policy):
        return [o.price for o in pricing.offers(M5080, results, policy, matcher)]

    assert prices(Policy()) == []
    assert prices(Policy(pickup=frozenset({"on"}))) == [9499.0]
    assert prices(Policy(pickup=frozenset({"bc"}))) == [9999.0]
    assert prices(Policy(pickup=frozenset({"mississauga"}))) == [9499.0]
    assert prices(Policy(pickup=frozenset({"oakville"}))) == []
    assert prices(Policy(pickup=frozenset({"on", "bc"}))) == [9499.0, 9999.0]
    # RECORD_POLICY stores every pickup-only row; a row no branch holds stays out.
    assert prices(pricing.RECORD_POLICY) == [9499.0, 9999.0]


def test_pickup_rows_still_pass_the_other_filters():
    results = [R("canadacomputers",
                 CC(1500.0, TORONTO, condition="open_box"),
                 CC(800.0, TORONTO),                                   # under min_price
                 CC(1450.0, TORONTO, title="RTX 5080 Ti"),             # wrong model
                 CC(1550.0, TORONTO, first_party=False, seller="X"),   # marketplace
                 CC(1600.0, TORONTO, in_stock=None),                   # unknown stock, not pickup-gated
                 CC(1700.0, MISSISSAUGA, in_stock=True))]              # buyable online anyway

    def prices(policy):
        return [o.price for o in pricing.offers(M5080, results, policy, matcher)]

    assert prices(Policy()) == [1600.0, 1700.0]
    assert prices(Policy(pickup=frozenset({"on"}))) == [1600.0, 1700.0]
    assert prices(Policy(include_open_box=True, pickup=frozenset({"on"}))) == [1500.0, 1600.0, 1700.0]
    assert prices(Policy(include_marketplace=True, pickup=frozenset({"on"}))) == [1550.0, 1600.0, 1700.0]
    assert prices(Policy(include_unknown_stock=False, pickup=frozenset({"on"}))) == [1700.0]


def test_offers_dedup_keeps_the_cheaper_row_with_pickup():
    results = [R("canadacomputers", CC(1600.0, TORONTO, sku="A")),
               R("canadacomputers", CC(1550.0, MISSISSAUGA, sku="A"), CC(1500.0, RICHMOND_BC, sku="A"))]
    (got,) = pricing.offers(M5080, results, Policy(pickup=frozenset({"on"})), matcher)
    assert (got.price, got.pickup_stores) == (1550.0, (MISSISSAUGA,))
    (wide,) = pricing.offers(M5080, results, pricing.RECORD_POLICY, matcher)
    assert (wide.price, wide.pickup_stores) == (1500.0, (RICHMOND_BC,))


def test_scan_models_passes_pickup_rows_through_unchanged():
    row = CC(9499.0, TORONTO)

    def scanner(query, retailer_ids):
        return [ScrapeResult("canadacomputers", query, Status.OK, [row], n_raw=1)]

    scan = pricing.scan_models([M5080], ["canadacomputers"], scanner=scanner)["rtx-5080"]
    assert scan.results[0].listings == [row]
    assert scan.offers(matcher=matcher) == []
    assert scan.offers(Policy(pickup=frozenset({"on"})), matcher) == [row]
    assert scan.best(Policy(pickup=frozenset({"toronto downtown"})), matcher) is row


def test_pickup_summary():
    assert pricing.pickup_summary([]) == ""
    assert pricing.pickup_summary([MISSISSAUGA, TORONTO, NORTH_YORK]) == (
        "Mississauga (1), Toronto Downtown (1), North York Centerpoint Mall (1)")
    assert pricing.pickup_summary([PickupStore("ON", "Oakville")]) == "Oakville"
    six = [PickupStore("ON", f"Branch {i}", "10+") for i in range(6)]
    assert pricing.pickup_summary(six) == (
        "Branch 0 (10+), Branch 1 (10+), Branch 2 (10+), Branch 3 (10+), +2 more")
    assert pricing.pickup_summary(six, limit=5) == (
        "Branch 0 (10+), Branch 1 (10+), Branch 2 (10+), Branch 3 (10+), Branch 4 (10+), +1 more")
    assert pricing.pickup_summary(six[:4]) == "Branch 0 (10+), Branch 1 (10+), Branch 2 (10+), Branch 3 (10+)"
    assert pricing.pickup_summary(iter(six), limit=0).count("(10+)") == 6


def test_status_label_names_pickup_branches_only_with_a_pickup_policy():
    offer = CC(9499.0, MISSISSAUGA, TORONTO, NORTH_YORK, RICHMOND_BC)
    on = Policy(pickup=frozenset({"on"}))
    result = R("canadacomputers")
    assert pricing.status_label(result, offer) == "$9,499.00, out of stock"
    assert pricing.status_label(result, offer, policy=Policy()) == "$9,499.00, out of stock"
    assert pricing.status_label(result, offer, policy=on) == (
        "$9,499.00, in-store pickup: Mississauga (1), Toronto Downtown (1), North York Centerpoint Mall (1)")
    assert pricing.status_label(result, offer, policy=Policy(pickup=frozenset({"qc"}))) == "$9,499.00, out of stock"
    assert pricing.status_label(result, CC(9499.0, MISSISSAUGA, in_stock=True), policy=on) == "$9,499.00, in stock"
    open_box = CC(1500.0, MISSISSAUGA, condition="open_box")
    assert pricing.status_label(result, open_box, policy=on) == "$1,500.00, open box, in-store pickup: Mississauga (1)"


def test_why_none_says_pickup_only_when_a_branch_has_the_card():
    model = catalog.by_id("rtx-5080")
    yes = lambda title, m: type("M", (), {"ok": True})()
    pickup = R("canadacomputers", CC(9499.0, TORONTO, MISSISSAUGA), CC(9999.0, RICHMOND_BC), CC(9300.0))
    one = R("canadacomputers", CC(9499.0, TORONTO), CC(9599.0, TORONTO))
    qc = Policy(pickup=frozenset({"qc"}))
    assert pricing.why_none(model, pickup, yes, policy=qc) == "pickup only (3 branches), none on your list"
    assert pricing.status_label(pickup, None, model, yes, policy=qc) == "pickup only (3 branches), none on your list"
    assert pricing.why_none(model, one, yes, policy=qc) == "pickup only (1 branch), none on your list"
    # A pickup row in the policy's branches counts as available, so the reason moves on.
    market = R("canadacomputers", CC(9499.0, TORONTO, first_party=False, seller="X"))
    assert pricing.why_none(model, market, yes) == "sold out (1 found)"
    assert pricing.why_none(model, market, yes, policy=qc) == "pickup only (1 branch), none on your list"
    assert pricing.why_none(model, market, yes, policy=Policy(pickup=frozenset({"on"}))) == (
        "marketplace only (1 found)")
    # Without pickup_stores (every pre-v2.2 row) the old reasons stand.
    sold_out = R("bestbuy", L(1500.0, in_stock=False), L(1600.0, in_stock=False))
    assert pricing.why_none(model, sold_out, yes) == "sold out (2 found)"
    assert pricing.why_none(model, sold_out, yes, policy=Policy(pickup=frozenset({"on"}))) == "sold out (2 found)"


def test_why_none_keeps_the_v21_label_with_pickup_off():
    # Rule 1: with no pickup policy (/lookup, CLI without --pickup) the text and
    # the count stay as in v2.1, even when the rows name branches.
    model = catalog.by_id("rtx-5080")
    yes = lambda title, m: type("M", (), {"ok": True})()
    pickup = R("canadacomputers", CC(9499.0, TORONTO, MISSISSAUGA), CC(9999.0, RICHMOND_BC), CC(9300.0))
    one = R("canadacomputers", CC(9499.0, MISSISSAUGA))
    for policy in (None, Policy(), pricing.DEFAULT_POLICY):
        assert pricing.why_none(model, pickup, yes, policy=policy) == "sold out (3 found)"
        assert pricing.status_label(pickup, None, model, yes, policy=policy) == "sold out (3 found)"
        assert pricing.why_none(model, one, yes, policy=policy) == "sold out (1 found)"
