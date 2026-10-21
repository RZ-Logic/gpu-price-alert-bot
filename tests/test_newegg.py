import json
from pathlib import Path

import pytest

from gpu_alerts.retailers import http, newegg
from gpu_alerts.retailers.base import ParseError, Status

FIXTURES = Path(__file__).parent / "fixtures"
P1 = (FIXTURES / "newegg_search_rtx5080_p1.html").read_text(encoding="utf-8")
P2 = (FIXTURES / "newegg_search_rtx5080_p2.html").read_text(encoding="utf-8")
NO_RESULTS = (FIXTURES / "newegg_search_noresults.html").read_text(encoding="utf-8")
REFURB = (FIXTURES / "newegg_search_refurb_rtx4070.html").read_text(encoding="utf-8")
AUDIT_STATE = (FIXTURES / "newegg_layoutB_state_rtx5080.json").read_text(encoding="utf-8")
LAYOUT_A_CARDS = (FIXTURES / "newegg_layoutA_cards_rtx5070.html").read_text(encoding="utf-8")
CAPTCHA = (FIXTURES / "newegg_403_captcha_head.html").read_text(encoding="utf-8")


def _fake_fetch(responses):
    calls = []

    def fake(url, **kw):
        calls.append(kw.get("params"))
        status, text, extra = responses[min(len(calls), len(responses)) - 1]
        return http.FetchResult(url, status, text, "chrome124", 5, **extra)

    return fake, calls


def test_parse_page1_first_party_row():
    rows = newegg.parse(P1, "rtx 5080")
    assert len(rows) == 36
    prime = rows[0]
    assert prime.title == (
        "ASUS PRIME GeForce RTX 5080 16GB GDDR7 OC Edition PRIME-RTX5080-O16G PCI-Express 5.0 DLSS 4.0 Graphics Card"
    )
    assert prime.price == 2369.99
    assert prime.sku == "14-126-744"
    assert prime.url == "https://www.newegg.ca/p/N82E16814126744"
    assert prime.in_stock is True
    assert prime.first_party is True
    assert prime.seller is None
    assert prime.condition == "new"
    assert prime.regular_price is None
    assert prime.category == "GPUs / Video Graphics Cards"
    assert all(r.retailer == "newegg" and r.url.startswith("https://www.newegg.ca/p/") for r in rows)


def test_parse_marketplace_rows_and_counts():
    rows = {r.sku: r for r in newegg.parse(P1, "rtx 5080")}
    assert sum(r.first_party for r in rows.values()) == 17
    assert sum(not r.first_party for r in rows.values()) == 19
    speed = rows["9SIC3DSKNH4195"]
    assert speed.first_party is False
    assert speed.seller == "Speed Electronics"
    assert speed.price == 3390.0
    # marketplace offers link to the parent product with the offer selected
    assert speed.url == "https://www.newegg.ca/p/N82E16814126776?Item=9SIC3DSKNH4195"
    # other parent forms keep ?Item= too; /p/<item> would redirect to the featured offer
    assert rows["9SIC3DSKNH4168"].url == "https://www.newegg.ca/p/1FT-000Y-00BR9?Item=9SIC3DSKNH4168"
    assert newegg._url("9SIABC", None) == "https://www.newegg.ca/p/9SIABC"


def test_price_includes_real_shipping():
    # FinalPrice 3568.38 = UnitCost 3542.44 + 25.94 shipping; the buyer pays 3,568.38
    row = next(r for r in newegg.parse(P1, "rtx 5080") if r.sku == "9SIADGEKKE5428")
    assert row.price == 3568.38
    assert row.seller == "Deal Targets"


def test_first_party_penny_shipping_is_free():
    # First-party rows carry ShippingCharge 0.01 with FinalPrice == UnitCost
    state = newegg._state(P1)
    cell = next(p["ItemCell"] for p in state["Products"] if p["ItemCell"]["Item"] == "14-126-744")
    assert cell["ShippingCharge"] == 0.01 and cell["FinalPrice"] == cell["UnitCost"]
    assert newegg._prices(cell) == (2369.99, None)
    # without FinalPrice the same sum is rebuilt, and 0.01 still does not leak in
    assert newegg._prices({"UnitCost": 2369.99, "ShippingCharge": 0.01}) == (2369.99, None)
    assert newegg._prices({"UnitCost": 1305.0, "ShippingCharge": 60.0}) == (1365.0, None)
    assert newegg._prices({"UnitCost": 0, "FinalPrice": 0}) == (None, None)


def test_refurbished_flag_from_feature_data():
    # title has no "Refurbished" text; ItemCell.Feature.IsRefurbished says so
    row = next(r for r in newegg.parse(P1, "rtx 5080") if r.sku == "14-985-071")
    assert row.title == "PNY ARGB OC GeForce RTX 5080 Graphics Card"
    assert row.condition == "refurbished"
    assert row.first_party is True


def test_page2_drops_desktop_pcs_by_subcategory():
    rows = newegg.parse(P2, "rtx 5080")
    assert newegg.page_info(P2) == (36, 196)
    assert len(rows) == 18
    assert {r.category for r in rows} == {"GPUs / Video Graphics Cards"}
    assert not any("Desktop" in r.title or "Gaming PC" in r.title for r in rows)


def test_instant_rebate_sets_regular_price():
    state = newegg._state(P2)
    cell = next(p["ItemCell"] for p in state["Products"] if p["ItemCell"]["Item"] == "83-420-054")
    assert cell["FinalPrice"] == 4469.98  # 4999.99 - 600 rebate + 69.99 shipping
    assert newegg._prices(cell) == (4469.98, 5069.98)  # both on the delivered basis


def test_refurb_search_keeps_gpus_only():
    rows = newegg.parse(REFURB, "rtx 4070")
    assert [r.sku for r in rows] == ["9SIC3DSKNH2618", "9SIC3VJM4F6895", "9SIC3VJM4C3158"]
    assert {r.condition for r in rows} == {"refurbished"}
    assert rows[1].seller == "soft_9"
    assert rows[1].price == 1365.0  # UnitCost 1305 plus 60 shipping


def test_condition_from_title():
    assert newegg._condition({}, "Open_Box - HP OMEN Transcend 16") == "open_box"
    assert newegg._condition({}, "MSI RTX 5070 VENTUS 3X OC (Open Box)") == "open_box"
    assert newegg._condition({"Feature": {"IsOpenBoxed": True}}, "MSI RTX 5070") == "open_box"
    assert newegg._condition({}, "Refurbished ASUS RTX 4070") == "refurbished"
    assert newegg._condition({}, "Used - ASUS RTX 4070") == "used"
    assert newegg._condition({"Feature": {"IsNew": True}}, "ASUS RTX 4070 Used Look") == "new"


def test_audit_state_json_fixture():
    rows = newegg.parse(json.loads(AUDIT_STATE), "rtx 5080")
    assert len(rows) == 6
    assert sum(r.first_party is False for r in rows) == 4
    assert {r.seller for r in rows if not r.first_party} == {
        "Speed Electronics", "Aero Malls Inc.", "OneDealOutlet Canada", "TECH EDGE",
    }


def test_no_results_page_is_empty_not_error():
    assert newegg.parse(NO_RESULTS, "rtx 9999 zzqxv") == []
    assert newegg.page_info(NO_RESULTS) == (0, 0)


def _drift(edit) -> dict:
    state = newegg._state(P1)
    for product in state["Products"]:
        edit(product)
    return state


def _rename(d: dict, old: str, new: str) -> None:
    d[new] = d.pop(old)


@pytest.mark.parametrize("edit", [
    lambda p: (_rename(p["ItemCell"], "FinalPrice", "Final"), _rename(p["ItemCell"], "UnitCost", "Cost")),
    lambda p: _rename(p["ItemCell"]["Description"], "Title", "Name"),
    lambda p: _rename(p, "ItemCell", "ProductCell"),
], ids=["price-renamed", "title-renamed", "itemcell-renamed"])
def test_parse_raises_when_no_row_is_usable(edit):
    # Field drift must not read as a healthy EMPTY page.
    with pytest.raises(ParseError, match="none of 36 rows had a title, item number and price"):
        newegg.parse(_drift(edit), "rtx 5080")


def test_parse_category_rejects_are_not_drift():
    def not_a_card(product):
        product["ItemCell"]["Subcategory"]["SubcategoryDescription"] = "Gaming Desktops"

    assert newegg.parse(_drift(not_a_card), "rtx 5080") == []


def test_empty_products_with_results_reported_raises():
    # Products: null with results reported is already an error; an empty list must be too.
    state = newegg._state(P1)
    state["Products"] = []
    with pytest.raises(ParseError, match="TotalItemCount is 196"):
        newegg.parse(state, "rtx 5080")
    state["TotalItemCount"] = 0
    assert newegg.parse(state, "x") == []


def test_search_field_drift_is_a_parse_error(monkeypatch):
    drifted = json.dumps(_drift(lambda p: _rename(p["ItemCell"]["Description"], "Title", "Name")))
    fake, _ = _fake_fetch([(200, drifted, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 5080")
    assert res.status is Status.PARSE_ERROR and not res.ok
    assert "none of 36 rows had a title, item number and price" in res.detail


def test_card_markup_without_state_raises():
    with pytest.raises(ParseError):
        newegg.parse(LAYOUT_A_CARDS, "rtx 5070")
    with pytest.raises(ParseError):
        newegg.parse('<html><script>window.__initialState__ = {"Foo": 1}</script></html>', "x")


def test_search_reads_two_pages(monkeypatch):
    fake, calls = _fake_fetch([(200, P1, {}), (200, P2, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 5080")
    assert res.status is Status.OK
    assert res.http_status == 200
    assert res.n_raw == 72
    assert len(res.listings) == 54
    assert calls == [{"d": "rtx 5080", "N": "4131"}, {"d": "rtx 5080", "N": "4131", "page": "2"}]
    assert "196 results reported" in res.detail
    assert res.partial is False


def test_search_stops_after_short_page(monkeypatch):
    fake, calls = _fake_fetch([(200, REFURB, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 4070")
    assert len(calls) == 1
    assert res.status is Status.OK
    assert res.n_raw == 5


def test_search_page2_failure_keeps_page1(monkeypatch):
    fake, _ = _fake_fetch([(200, P1, {}), (403, CAPTCHA, {"blocked_reason": "HTTP 403"})])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 5080")
    assert res.status is Status.OK
    assert len(res.listings) == 36
    assert "page 2: HTTP 403" in res.detail
    assert res.blocked_partway is True and res.partial is True


@pytest.mark.parametrize("status, text, extra, reason, blocked", [
    (None, "", {"error": "Timeout: 30 s"}, "page 2: Timeout: 30 s", False),
    (500, "oops", {}, "page 2: HTTP 500", False),
    (200, LAYOUT_A_CARDS, {}, "page 2: no window.__initialState__ in page", False),
    (200, "<html><head><title>CAPTCHA page</title></head></html>", {}, "page 2: bot-check page", True),
], ids=["timeout", "http500", "no-state", "captcha-title"])
def test_search_page2_failure_marks_result_partial(monkeypatch, status, text, extra, reason, blocked):
    # Any page-2 failure leaves a partial read, but only a block trips the run breaker.
    fake, _ = _fake_fetch([(200, P1, {}), (status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 5080")
    assert res.status is Status.OK and len(res.listings) == 36
    assert res.partial is True
    assert res.blocked_partway is blocked
    assert reason in res.detail


def test_search_unexpected_error_on_page2_keeps_page1(monkeypatch):
    state = newegg._state(P2)
    state["Products"][0]["ItemCell"]["Description"] = ["not", "an", "object"]
    fake, _ = _fake_fetch([(200, P1, {}), (200, json.dumps(state), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 5080")
    assert res.status is Status.OK and len(res.listings) == 36
    assert res.partial is True and res.blocked_partway is False
    assert "page 2: unexpected AttributeError" in res.detail
    # the same row on page 1 is still a parse error
    fake, _ = _fake_fetch([(200, json.dumps(state), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 5080")
    assert res.status is Status.PARSE_ERROR and res.listings == []
    assert "AttributeError" in res.detail


def test_http_fetch_does_not_sleep_after_last_attempt(monkeypatch):
    class Resp:
        status_code, text, url = 503, "busy", "https://www.newegg.ca/p/pl"

    targets, sleeps = [], []
    monkeypatch.setattr(http, "_wait_turn", lambda url, interval: None)
    monkeypatch.setattr(http.cureq, "request", lambda *a, impersonate, **kw: targets.append(impersonate) or Resp())
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    res = http.fetch("https://www.newegg.ca/p/pl", attempts=3)
    assert res.status_code == 503 and res.blocked_reason == "HTTP 503"
    assert targets == ["chrome124", "safari18_0", "chrome120"]
    assert sleeps == [1.5, 3.0]  # backoff between attempts only


def test_search_no_results_is_empty(monkeypatch):
    fake, _ = _fake_fetch([(200, NO_RESULTS, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 9999 zzqxv")
    assert res.status is Status.EMPTY
    assert res.listings == []


@pytest.mark.parametrize("status, text, extra, expected", [
    (403, CAPTCHA, {"blocked_reason": "HTTP 403"}, Status.BLOCKED),
    (200, "<html><head><title>CAPTCHA page</title></head></html>", {}, Status.BLOCKED),
    (200, LAYOUT_A_CARDS, {}, Status.PARSE_ERROR),
    (404, "not found", {}, Status.NETWORK_ERROR),
    (None, "", {"error": "ConnectionError: reset"}, Status.NETWORK_ERROR),
], ids=["http403", "captcha-title", "no-state", "http404", "reset"])
def test_search_failure_statuses(monkeypatch, status, text, extra, expected):
    fake, _ = _fake_fetch([(status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = newegg.search("rtx 5080")
    assert res.status is expected
    assert res.http_status == status
    assert res.listings == []
    assert res.detail


@pytest.mark.live
def test_live_search_smoke():
    res = newegg.search("rtx 5080")
    assert res.status in (Status.OK, Status.EMPTY), res.detail
    if res.status is Status.OK:
        assert res.n_raw >= len(res.listings) > 0
        assert all(r.price > 0 and r.url.startswith("https://www.newegg.ca/p/") for r in res.listings)
