import json
from pathlib import Path

import pytest

from gpu_alerts.retailers import http, walmart
from gpu_alerts.retailers.base import ParseError, Status

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "walmart_search_rtx5070.html").read_text(encoding="utf-8")
AUDIT_JSON = (FIXTURES / "walmart_next_data_rtx5080.json").read_text(encoding="utf-8")


def _page(items: list[dict], count: int | None = None, more: bool = False) -> str:
    stack = {"count": len(items) if count is None else count, "items": items}
    data = {"props": {"pageProps": {"initialData": {"searchResult": {
        "hasMorePages": more, "itemStacks": [stack],
    }}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>' + " " * 60_000


def _fake_fetch(responses):
    calls = []

    def fake(url, **kw):
        calls.append(kw.get("params"))
        status, text, extra = responses[min(len(calls), len(responses)) - 1]
        return http.FetchResult(url, status, text, "chrome124", 5, **extra)

    return fake, calls


def test_parse_live_capture_rtx5070():
    rows = walmart.parse(SEARCH_HTML, "rtx 5070")
    # 8 rows in the fixture: an ad placeholder and a sponsored motherboard are dropped
    assert len(rows) == 6
    titles = [r.title for r in rows]
    assert not any("Motherboard" in t for t in titles)
    aero = next(r for r in rows if r.sku == "72JN3E0NGUCN")
    assert aero.title == "GIGABYTE GVN5070AERO OC12GD GeForce RTX 5070 AERO OC 12GB Graphics Card"
    assert aero.price == 1299.99
    assert aero.in_stock is True
    assert aero.first_party is False
    assert aero.seller == "Newegg Canada Inc."
    assert aero.condition == "new"
    assert aero.regular_price is None
    assert aero.category == "Graphic Cards & GPU"
    assert aero.url == (
        "https://www.walmart.ca/en/ip/GIGABYTE-GVN5070AERO-OC12GD-GeForce-RTX-5070-AERO-OC-12GB-Graphics-Card/72JN3E0NGUCN"
    )
    assert all(r.retailer == "walmart" and r.url.startswith("https://www.walmart.ca/en/ip/") for r in rows)


def test_parse_was_price_and_sponsored_gpu_kept():
    rows = {r.sku: r for r in walmart.parse(SEARCH_HTML, "rtx 5070")}
    colorful = rows["60J7DS8A4WYR"]
    assert colorful.price == 1529.99
    assert colorful.regular_price == 1699.99
    assert colorful.seller == "ASPIRING-ELECTRONICS"
    # sponsored row with no catalogProductType, kept through its category path
    sponsored = rows["3LWOR8MAWABO"]
    assert sponsored.category == "Graphic Cards & GPU"
    # rows from the second "other options" stack are read too
    assert "79O3RP01XQDK" in rows and rows["79O3RP01XQDK"].price == 3599.99
    # category path "Default" falls back to catalogProductType
    assert rows["7DAM1LBL2J1H"].category == "Computer Video Cards"


def test_page_info_live_capture():
    n_products, reported, more = walmart.page_info(SEARCH_HTML)
    assert (n_products, reported) == (7, 27)
    assert more is True  # trimmed fixture shows fewer organic rows than the 27 reported


def test_parse_audit_fixture():
    rows = walmart.parse(AUDIT_JSON, "rtx 5080")
    assert len(rows) == 6
    prime = next(r for r in rows if r.title.startswith("ASUS PRIME GeForce RTX 5080"))
    assert prime.price == 2369.99
    assert prime.category == "Computer Video Cards"
    assert prime.in_stock is True and prime.first_party is False
    assert {r.seller for r in rows} == {"Newegg Canada Inc.", "stricklands distribution inc."}


def test_price_includes_seller_shipping_fee():
    # stricklands lists the WINDFORCE SFF at 2499.99 with priceInfo.shippingFee 69.99
    rows = walmart.parse(AUDIT_JSON, "rtx 5080")
    sff = next(r for r in rows if "WINDFORCE SFF" in r.title)
    assert sff.seller == "stricklands distribution inc."
    assert sff.price == 2569.98


def test_shipping_fee_applies_to_was_price_too():
    item = {
        "__typename": "Product", "usItemId": "S1", "name": "RTX 5070 card", "price": 1000.0,
        "canonicalUrl": "/en/ip/s/S1", "catalogProductType": "Computer Video Cards",
        "priceInfo": {"wasPrice": "$1,200.00", "shippingFee": 25.5},
    }
    text_fee = dict(item, usItemId="S2", canonicalUrl="/en/ip/s/S2",
                    priceInfo={"shipPrice": "$15.00", "wasPrice": ""})
    free = dict(item, usItemId="S3", canonicalUrl="/en/ip/s/S3",
                priceInfo={"shippingFee": 0, "shipPrice": ""})
    rows = {r.sku: r for r in walmart.parse(_page([item, text_fee, free]), "rtx 5070")}
    assert (rows["S1"].price, rows["S1"].regular_price) == (1025.5, 1225.5)
    assert (rows["S2"].price, rows["S2"].regular_price) == (1015.0, None)
    assert (rows["S3"].price, rows["S3"].regular_price) == (1000.0, None)


def test_condition_and_stock_flags():
    base = {
        "__typename": "Product", "usItemId": "X1", "name": "Refurbished MSI RTX 5070",
        "price": 900.0, "canonicalUrl": "/en/ip/x/X1?classType=REGULAR",
        "sellerName": "Walmart", "sellerType": "INTERNAL", "catalogProductType": "Computer Video Cards",
        "availabilityStatusV2": {"value": "OUT_OF_STOCK"}, "isOutOfStock": True,
    }
    used = dict(base, usItemId="X2", name="MSI RTX 5070", isPreowned=True)
    other = dict(base, usItemId="X3", name="PC case", catalogProductType="PC Cases")
    rows = walmart.parse(_page([base, used, other]), "rtx 5070")
    assert [r.sku for r in rows] == ["X1", "X2"]
    assert rows[0].condition == "refurbished" and rows[1].condition == "used"
    assert rows[0].in_stock is False and rows[0].first_party is True
    assert rows[0].url == "https://www.walmart.ca/en/ip/x/X1"


@pytest.mark.parametrize("seller_type, seller, first_party", [
    ("EXTERNAL", "Newegg Canada Inc.", False),   # every captured GPU row
    ("INTERNAL", "Walmart", True),
    ("external", "Newegg Canada Inc.", False),
    ("MARKETPLACE", "Newegg Canada Inc.", False),  # unknown value: decided by the seller name
    ("SELLER", "Walmart.ca", True),
    ("MARKETPLACE", None, None),
    (None, "stricklands distribution inc.", False),
    (None, None, None),
], ids=["external", "internal", "lowercase", "unknown-reseller", "unknown-walmart", "unknown-no-name",
        "missing-type", "missing-both"])
def test_seller_type_only_known_values_decide_first_party(seller_type, seller, first_party):
    # Any sellerType other than "EXTERNAL" used to count as sold by Walmart, letting
    # marketplace offers past a default alert after a schema change.
    item = {
        "__typename": "Product", "usItemId": "S", "name": "RTX 5080 card", "price": 2369.99,
        "canonicalUrl": "/en/ip/s/S", "catalogProductType": "Computer Video Cards",
        "sellerType": seller_type, "sellerName": seller,
    }
    assert walmart.parse(_page([item]), "rtx 5080")[0].first_party is first_party


def test_parse_errors():
    with pytest.raises(ParseError):
        walmart.parse("<html><body>no data</body></html>", "rtx 5070")
    with pytest.raises(ParseError):
        walmart.parse('{"props": {"pageProps": {}}}', "rtx 5070")


def _drift(edit) -> dict:
    data = json.loads(AUDIT_JSON)
    for stack in walmart._search_result(data)["itemStacks"]:
        for item in stack["items"]:
            edit(item)
    return data


@pytest.mark.parametrize("edit, error", [
    (lambda i: (i.pop("price"), i["priceInfo"].pop("linePrice")), "none of 6 graphics card rows had"),
    (lambda i: i.update(canonicalUrl=None), "none of 6 graphics card rows had"),
    (lambda i: i.update(__typename="SearchProduct"), "results reported but no Product items"),
], ids=["price-renamed", "url-missing", "typename-renamed"])
def test_parse_raises_when_no_row_is_usable(edit, error):
    # Field drift must not read as a healthy EMPTY page.
    data = _drift(edit)
    walmart._search_result(data)["itemStacks"][0]["count"] = 6
    with pytest.raises(ParseError, match=error):
        walmart.parse(data, "rtx 5080")


def test_parse_ad_placeholders_and_category_rejects_are_not_drift():
    # The live page mixes an AdPlaceholder into the Product items; that is not drift.
    assert len(walmart.parse(SEARCH_HTML, "rtx 5070")) == 6
    boards = _drift(lambda i: i.update(catalogProductType="Motherboards"))
    walmart._search_result(boards)["itemStacks"][0]["count"] = 6
    assert walmart.parse(boards, "rtx 5080") == []
    assert walmart.parse(_page([], count=0), "arc b580") == []


def test_search_field_drift_is_a_parse_error(monkeypatch):
    drifted = _page([{
        "__typename": "Product", "usItemId": "A", "name": "RTX 5070 card", "salePrice": 999.0,
        "canonicalUrl": "/en/ip/a/A", "catalogProductType": "Computer Video Cards",
    }], count=27)
    fake, _ = _fake_fetch([(200, drifted, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.PARSE_ERROR and not res.ok
    assert "none of 1 graphics card rows had a name, price and url" in res.detail


def test_search_category_rejects_name_the_cause(monkeypatch):
    board = {
        "__typename": "Product", "usItemId": "M", "name": "B850 motherboard", "price": 299.0,
        "canonicalUrl": "/en/ip/m/M", "catalogProductType": "Motherboards",
    }
    fake, _ = _fake_fetch([(200, _page([board]), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.EMPTY and "1 rows, none in the graphics card category" in res.detail
    fake, _ = _fake_fetch([(200, _page([], count=0), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("arc b580")
    assert res.status is Status.EMPTY and "no results" in res.detail
    assert "none in the graphics card category" not in res.detail


def test_search_reads_page_two_and_dedupes(monkeypatch):
    gpu = {
        "__typename": "Product", "usItemId": "A", "name": "RTX 5070 card", "price": 999.0,
        "canonicalUrl": "/en/ip/a/A", "sellerName": "Newegg Canada Inc.", "sellerType": "EXTERNAL",
        "catalogProductType": "Computer Video Cards",
    }
    page2 = dict(gpu, usItemId="B", canonicalUrl="/en/ip/b/B", price=1099.0)
    fake, calls = _fake_fetch([
        (200, _page([gpu], count=41), {}),
        (200, _page([gpu, page2], count=41), {}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.OK
    assert len(calls) == 2 and calls[1]["page"] == "2"
    assert calls[0]["q"] == "rtx 5070 graphics card"
    assert [r.sku for r in res.listings] == ["A", "B"]
    assert res.n_raw == 3 and res.http_status == 200
    assert res.blocked_partway is False and res.partial is False


@pytest.mark.parametrize("status, text, extra, reason", [
    (None, "", {"error": "Timeout: 30 s"}, "page 2: Timeout: 30 s"),
    (502, "bad gateway", {}, "page 2: HTTP 502"),
    (200, "<html>" + "x" * 60_000 + "</html>", {}, "page 2: no script#__NEXT_DATA__ in page"),
], ids=["timeout", "http502", "no-next-data"])
def test_search_page_two_error_marks_result_partial(monkeypatch, status, text, extra, reason):
    # Not a block, so the run breaker is left alone, but the read is still partial.
    gpu = {
        "__typename": "Product", "usItemId": "A", "name": "RTX 5070 card", "price": 999.0,
        "canonicalUrl": "/en/ip/a/A", "catalogProductType": "Computer Video Cards",
    }
    fake, _ = _fake_fetch([(200, _page([gpu], count=41), {}), (status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.OK and [r.sku for r in res.listings] == ["A"]
    assert res.partial is True and res.blocked_partway is False
    assert reason in res.detail


def test_search_unexpected_error_on_page_two_keeps_page_one(monkeypatch):
    gpu = {
        "__typename": "Product", "usItemId": "A", "name": "RTX 5070 card", "price": 999.0,
        "canonicalUrl": "/en/ip/a/A", "catalogProductType": "Computer Video Cards",
    }
    bad = dict(gpu, usItemId="B", canonicalUrl="/en/ip/b/B", priceInfo=["$15.00"])  # list, not an object
    fake, _ = _fake_fetch([(200, _page([gpu], count=41), {}), (200, _page([bad], count=41), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.OK and [r.sku for r in res.listings] == ["A"]
    assert res.partial is True and res.blocked_partway is False
    assert "page 2: unexpected AttributeError" in res.detail
    # the same row on page 1 is still a parse error
    fake, _ = _fake_fetch([(200, _page([bad], count=41), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.PARSE_ERROR and res.listings == []
    assert "AttributeError" in res.detail


def test_search_page_two_block_is_flagged(monkeypatch):
    gpu = {
        "__typename": "Product", "usItemId": "A", "name": "RTX 5070 card", "price": 999.0,
        "canonicalUrl": "/en/ip/a/A", "catalogProductType": "Computer Video Cards",
    }
    fake, _ = _fake_fetch([
        (200, _page([gpu], count=41), {}),
        (200, "<html>Robot or human? Activate and hold</html>", {}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.OK and [r.sku for r in res.listings] == ["A"]
    assert res.blocked_partway is True and "page 2: bot-check page" in res.detail
    assert res.partial is True
    # nothing usable on page 1: the result is BLOCKED
    board = dict(gpu, catalogProductType="Motherboards")
    fake, _ = _fake_fetch([
        (200, _page([board], count=41), {}),
        (200, "<html>Robot or human? Activate and hold</html>", {}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is Status.BLOCKED and res.listings == []


@pytest.mark.parametrize("status, text, extra, expected", [
    (None, "", {"error": "ConnectionError: reset"}, Status.NETWORK_ERROR),
    (403, "denied", {"blocked_reason": "HTTP 403"}, Status.BLOCKED),
    (200, "<html>Robot or human? Activate and hold</html>", {}, Status.BLOCKED),
    (200, "<html>" + "x" * 60_000 + "</html>", {}, Status.PARSE_ERROR),
    (404, "not found", {}, Status.NETWORK_ERROR),
    (502, "bad gateway", {}, Status.NETWORK_ERROR),
], ids=["network", "http403", "robot-page", "no-next-data", "http404", "http502"])
def test_search_status_mapping(monkeypatch, status, text, extra, expected):
    fake, _ = _fake_fetch([(status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("rtx 5070")
    assert res.status is expected
    assert res.detail and res.listings == []


def test_search_empty(monkeypatch):
    fake, calls = _fake_fetch([(200, _page([], count=0), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = walmart.search("arc b580")
    assert res.status is Status.EMPTY and len(calls) == 1 and res.n_raw == 0


@pytest.mark.live
def test_live_search_smoke():
    res = walmart.search("rtx 5070")
    assert res.status in (Status.OK, Status.EMPTY, Status.BLOCKED), res.detail
    if res.status is Status.OK:
        assert res.n_raw >= len(res.listings) > 0
        assert all(r.price > 0 and r.url.startswith("https://www.walmart.ca/") for r in res.listings)
