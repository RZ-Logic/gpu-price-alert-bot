"""Best Buy adapter: offline parsing of saved API responses, status mapping, one live smoke test."""

import json
from pathlib import Path

import pytest

from gpu_alerts.retailers import bestbuy
from gpu_alerts.retailers.base import ParseError, Status
from gpu_alerts.retailers.http import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _by_sku(listings):
    return {l.sku: l for l in listings}


def test_parse_rtx5070ti_drops_bundles_and_keeps_marketplace_rows():
    listings = bestbuy.parse(_text("bestbuy_search_rtx5070ti.json"), "rtx 5070 ti")
    assert len(listings) == 40  # 48 products minus 8 PSU bundles with B-prefixed SKUs
    assert all(l.sku.isdigit() for l in listings)
    assert all(l.retailer == "bestbuy" and l.category == "Graphics Cards" for l in listings)
    assert all(l.url.startswith("https://www.bestbuy.ca/en-ca/product/") for l in listings)
    assert all(l.in_stock is None for l in listings)  # stock comes from the availability API
    assert sum(1 for l in listings if l.first_party is False) == 14

    shadow = _by_sku(listings)["19358759"]
    assert shadow.title == "MSI GeForce RTX 5070 Ti Shadow 3x OC 16GB GDDR7 Video Card"
    assert shadow.price == 1689.99
    assert shadow.first_party is True
    assert shadow.seller == "Best Buy"
    assert shadow.condition == "new"
    assert shadow.url.endswith("/19358759")

    aero = _by_sku(listings)["19800573"]
    assert aero.first_party is False
    assert aero.seller == "Aero Malls Inc."
    assert aero.price == 1859.99
    assert aero.regular_price == 1999.99


def test_parse_keeps_ampersand_title_that_is_not_a_bundle():
    listings = _by_sku(bestbuy.parse(_text("bestbuy_search_rtx5070ti.json"), "rtx 5070 ti"))
    assert "19828029" in listings  # "... Graphics Card - High-Performance Gaming & Creative GPU"
    assert "B0019626" not in listings  # "... Video Card & Corsair RMe Series 1000-Watt ... Power Supply"


def test_parse_rtx5080_drops_gaming_desktops():
    listings = bestbuy.parse(_text("bestbuy_search_rtx5080.json"), "rtx 5080")
    assert len(listings) == 8  # 10 products, 2 are Gaming Desktop Computers
    assert not any("Gaming PC" in l.title for l in listings)
    zotac = _by_sku(listings)["20164554"]
    assert zotac.price == 2269.99 and zotac.first_party is True


def test_parse_maps_condition_from_title():
    doc = json.loads(_text("bestbuy_search_rtx5070ti.json"))
    base = doc["products"][0]
    doc["products"] = [
        dict(base, sku="1", name="Open Box - ASUS Prime GeForce RTX 5060 OC 8GB GDDR7 Video Card"),
        dict(base, sku="2", name="（Open Box）-PNY GeForce RTX 5060 Ti 16GB"),
        dict(base, sku="3", name="Refurbished (Good) - EVGA GeForce RTX 3070 Ti 8GB GDDR6X Video Card"),
    ]
    conditions = [l.condition for l in bestbuy.parse(doc, "x")]
    assert conditions == ["open_box", "open_box", "refurbished"]


def test_parse_errors_on_missing_structure():
    with pytest.raises(ParseError):
        bestbuy.parse("<html>Access Denied</html>", "rtx 5080")
    with pytest.raises(ParseError):
        bestbuy.parse('{"total": 0}', "rtx 5080")
    with pytest.raises(ParseError):
        bestbuy.parse('{"products": [{"foo": 1}, {"bar": 2}]}', "rtx 5080")
    assert bestbuy.parse('{"products": [], "total": 0}', "zzz") == []


def test_parse_availability():
    stock = bestbuy.parse_availability(_text("bestbuy_availability_rtx5070ti.json"))
    assert len(stock) == 40
    assert sum(1 for v in stock.values() if v is True) == 27
    assert stock["19358759"] is True    # InStock
    assert stock["19590993"] is True    # InStockOnlineOnly, marketplace seller
    assert stock["19183868"] is False   # SoldOutOnline
    assert bestbuy.parse_availability(_text("bestbuy_availability.json")) == {
        "20164554": True, "19177946": True, "18934247": True, "19953290": True,
    }


def test_parse_availability_strips_bom_and_rejects_bad_shape():
    stock = bestbuy.parse_availability("﻿" + _text("bestbuy_availability.json"))
    assert stock["20164554"] is True
    with pytest.raises(ParseError):
        bestbuy.parse_availability('{"errors": []}')


def test_shipping_status_mapping():
    f = bestbuy._shipping_in_stock
    assert f({"status": "InStock", "purchasable": True}) is True
    assert f({"status": "SoldOutOnline", "purchasable": False}) is False
    assert f({"status": "Preorder", "purchasable": True}) is False
    assert f({"status": "SomethingNew", "purchasable": True}) is True
    assert f({"status": "SomethingNew"}) is None


# search() status mapping with a fake transport


def _ok(text: str) -> FetchResult:
    return FetchResult("https://www.bestbuy.ca/x", 200, text, "chrome124", 5)


def _fake_fetch(monkeypatch, *responses: FetchResult) -> list[str]:
    queue, calls = list(responses), []

    def fake(url, **kwargs):
        calls.append(url)
        return queue.pop(0)

    monkeypatch.setattr(bestbuy, "fetch", fake)
    return calls


def test_search_ok_merges_stock(monkeypatch):
    calls = _fake_fetch(
        monkeypatch,
        _ok(_text("bestbuy_search_rtx5070ti.json")),
        _ok("﻿" + _text("bestbuy_availability_rtx5070ti.json")),
    )
    r = bestbuy.search("rtx 5070 ti")
    assert r.status is Status.OK and r.http_status == 200
    assert r.n_raw == 48 and len(r.listings) == 40
    assert calls == [bestbuy.SEARCH_URL, bestbuy.AVAILABILITY_URL]
    by_sku = _by_sku(r.listings)
    assert by_sku["19358759"].in_stock is True
    assert by_sku["19183868"].in_stock is False
    assert "stock unknown" not in r.detail


@pytest.mark.parametrize("availability, status", [
    (FetchResult(bestbuy.AVAILABILITY_URL, None, "", "chrome124", 30000, error="Timeout: 30 s"),
     Status.NETWORK_ERROR),
    (FetchResult(bestbuy.AVAILABILITY_URL, 403, "Access Denied", "chrome124", 5,
                 blocked_reason="HTTP 403"), Status.BLOCKED),
    (FetchResult(bestbuy.AVAILABILITY_URL, 500, "oops", "chrome124", 5), Status.NETWORK_ERROR),
    (_ok('{"errors": []}'), Status.PARSE_ERROR),
])
def test_search_fails_when_availability_fails(monkeypatch, availability, status):
    # Stock-unknown rows would count as buyable, so SoldOutOnline SKU 19183868 at
    # $1189.95 would beat the in-stock $1689.99 card. A failed lookup is a failed scan.
    _fake_fetch(monkeypatch, _ok(_text("bestbuy_search_rtx5070ti.json")), availability)
    r = bestbuy.search("rtx 5070 ti")
    assert r.status is status
    assert r.listings == []
    assert not r.ok
    assert "availability" in r.detail


@pytest.mark.parametrize("response, status", [
    (FetchResult("u", 403, "Access Denied", "chrome124", 5, blocked_reason="HTTP 403"), Status.BLOCKED),
    (FetchResult("u", None, "", "chrome124", 5, error="ConnectionError: reset"), Status.NETWORK_ERROR),
    (FetchResult("u", 200, "<html>maintenance</html>", "chrome124", 5), Status.PARSE_ERROR),
    (FetchResult("u", 200, '{"products": [], "total": 0}', "chrome124", 5), Status.EMPTY),
])
def test_search_status_mapping(monkeypatch, response, status):
    calls = _fake_fetch(monkeypatch, response)
    r = bestbuy.search("rtx 5080")
    assert r.status is status
    assert r.listings == []
    assert len(calls) == 1  # no availability call without listings
    assert r.detail


@pytest.mark.live
def test_live_search_smoke():
    r = bestbuy.search("rtx 5080")
    assert r.status is Status.OK, r.detail
    assert r.n_raw > 0 and r.listings
    assert any(l.in_stock is not None for l in r.listings), r.detail
    assert all(l.url.startswith("https://www.bestbuy.ca/") for l in r.listings)
