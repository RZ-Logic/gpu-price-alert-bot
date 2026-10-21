import json
from pathlib import Path

import pytest

from gpu_alerts.retailers import canadacomputers as cc
from gpu_alerts.retailers import http
from gpu_alerts.retailers.base import ParseError, Status

FIXTURES = Path(__file__).parent / "fixtures"
PRICE_ASC = (FIXTURES / "canadacomputers_search_rtx5070_priceasc.json").read_text(encoding="utf-8")
AUDIT = (FIXTURES / "canadacomputers_search_rtx5080.json").read_text(encoding="utf-8")


def _page(products: list[dict], total: int, pages: int, page: int = 1) -> str:
    return json.dumps({
        "pagination": {"total_items": total, "pages_count": pages, "current_page": page},
        "products": products,
    })


def _row(pid: int, price: float, cat: int = 917, stock: str = "Online - Available to Ship") -> dict:
    return {
        "id": pid, "id_category_default": cat, "category_name": "Powered By Nvidia",
        "name": f"Card {pid}", "price_amount": price, "regular_price_amount": price,
        "url": f"https://www.canadacomputers.com/en/powered-by-nvidia/{pid}/card.html",
        "reference": f"VC{pid}", "condition": {"type": "new"},
        "stock_availability": {"online_availability": stock, "retail": False},
    }


def _fake_fetch(responses):
    calls = []

    def fake(url, **kw):
        calls.append(kw.get("params"))
        status, text, extra = responses[min(len(calls), len(responses)) - 1]
        return http.FetchResult(url, status, text, "chrome124", 5, **extra)

    return fake, calls


def test_parse_live_capture_price_ascending():
    rows = cc.parse(PRICE_ASC, "rtx 5070")
    # 12 rows: the Bykski water block (category 2337) is dropped
    assert len(rows) == 11
    assert not any("Water Block" in r.title for r in rows)
    assert [r.price for r in rows] == sorted(r.price for r in rows)
    assert all(r.first_party is True and r.seller is None for r in rows)
    shippable = [r for r in rows if r.in_stock]
    assert len(shippable) == 1
    windforce = shippable[0]
    assert windforce.title == "GIGABYTE GeForce RTX 5070 WINDFORCE OC SFF 12G Graphics Card"
    assert windforce.price == 1259.0
    assert windforce.sku == "VCGIG00598"
    assert windforce.condition == "new"
    assert windforce.category == "Powered By Nvidia"
    assert windforce.url.startswith("https://www.canadacomputers.com/en/powered-by-nvidia/268829/")


def test_open_box_and_pickup_only():
    rows = {r.sku: r for r in cc.parse(PRICE_ASC, "rtx 5070")}
    assert sum(r.condition == "open_box" for r in rows.values()) == 8
    # open box by item classification even though the title has no "(Open Box)"
    assert rows["VCGIG00598OB"].condition == "open_box"
    assert rows["VCMSI00477OB"].title.endswith("(Open Box)")
    sold_out = rows["VCMSI00477"]
    assert sold_out.in_stock is False and sold_out.condition == "new"
    assert sold_out.category == "Powered By Nvidia, in-store pickup only"


def test_page_info():
    assert cc.page_info(PRICE_ASC) == (12, 170, 15)
    assert cc.page_info(AUDIT) == (8, 80, 7)


def test_parse_audit_fixture_by_category_name():
    # the audit fixture was trimmed without id_category_default, so category_name gates it
    rows = cc.parse(AUDIT, "rtx 5080")
    assert len(rows) == 7
    assert not any(r.title.startswith("ARMOURY") for r in rows)
    shadow = next(r for r in rows if "SHADOW" in r.title)
    assert shadow.price == 2509.99 and shadow.in_stock is False
    assert next(r for r in rows if "RX 9070 XT" in r.title).category == "Powered By AMD"


def test_condition_from_source_and_discount():
    refurb = dict(_row(1, 800.0), condition={"type": "refurbished"}, regular_price_amount=900.0)
    backorder = _row(2, 850.0, stock="Online - Backorder")
    rows = cc.parse(_page([refurb, backorder], 2, 1), "rtx 5070")
    assert rows[0].condition == "refurbished" and rows[0].regular_price == 900.0
    assert rows[1].in_stock is False and rows[1].regular_price is None


def test_parse_errors():
    with pytest.raises(ParseError):
        cc.parse("<html>maintenance</html>", "rtx 5070")
    with pytest.raises(ParseError):
        cc.parse('{"pagination": {}}', "rtx 5070")


def _drift(edit) -> dict:
    data = json.loads(PRICE_ASC)
    for p in data["products"]:
        edit(p)
    return data


@pytest.mark.parametrize("edit", [
    lambda p: p.update(price_amount=f"${p['price_amount']:,.2f}"),
    lambda p: p.update(price=p.pop("price_amount")),
    lambda p: p.update(link_rewrite=p.pop("url")),
    lambda p: p.update(product_name=p.pop("name")),
], ids=["price-display-string", "price-renamed", "url-renamed", "name-renamed"])
def test_parse_raises_when_no_graphics_card_row_is_usable(edit):
    # Field drift must not read as a healthy EMPTY page ("no rows in the graphics card categories").
    with pytest.raises(ParseError, match="none of 11 graphics card rows"):
        cc.parse(_drift(edit), "rtx 5070")


def test_parse_unpriced_rows_are_not_drift():
    # price_amount 0 (coming soon) or null is a real unpriced row, not a changed layout
    for value in (0, None):
        assert cc.parse(_drift(lambda p: p.update(price_amount=value)), "rtx 5070") == []
    # one drifted row among usable ones costs that row only
    data = json.loads(PRICE_ASC)
    data["products"][1].pop("url")
    assert len(cc.parse(data, "rtx 5070")) == 10


def test_search_field_drift_is_a_parse_error(monkeypatch):
    fake, _ = _fake_fetch([(200, json.dumps(_drift(lambda p: p.update(price=p.pop("price_amount")))), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.PARSE_ERROR and not res.ok
    assert "none of 11 graphics card rows had a name, price and url" in res.detail
    # graphics card rows that are all unpriced are EMPTY, and the detail says why
    unpriced = _drift(lambda p: p.update(price_amount=0))
    unpriced["pagination"]["pages_count"] = 1
    fake, _ = _fake_fetch([(200, json.dumps(unpriced), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.EMPTY
    assert "11 graphics card rows, none with a price" in res.detail
    assert "no rows in the graphics card categories" not in res.detail


def test_search_reads_up_to_four_pages(monkeypatch):
    pages = [(200, _page([_row(10 * p + i, 1000.0 + p) for i in range(2)], 100, 9, p), {}) for p in range(1, 6)]
    fake, calls = _fake_fetch(pages)
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.OK
    assert [c["page"] for c in calls] == ["1", "2", "3", "4"]
    assert calls[0]["order"] == "product.price.asc" and calls[0]["s"] == "rtx 5070"
    # the server drops non graphics card rows from each page (measured live 2026-10-12)
    assert calls[0]["id_category"] == "914" and calls[0]["resultsPerPage"] == str(cc.PAGE_SIZE)
    assert len(res.listings) == 8 and res.n_raw == 8
    assert "read 4 of 9 pages" in res.detail


def test_search_reads_past_a_page_with_no_graphics_cards(monkeypatch):
    # One page of laptops between two pages of cards must not end the scan.
    fake, calls = _fake_fetch([
        (200, _page([_row(1, 900.0), _row(2, 950.0)], 100, 9, 1), {}),
        (200, _page([_row(3, 2500.0, cat=103)], 100, 9, 2), {}),  # gaming laptops
        (200, _page([_row(4, 2590.0)], 100, 9, 3), {}),
        (200, _page([], 100, 9, 4), {}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.OK and [r.sku for r in res.listings] == ["VC1", "VC2", "VC4"]
    assert [c["page"] for c in calls] == ["1", "2", "3", "4"]
    assert "read 4 of 9 pages" in res.detail and "no graphics cards left" not in res.detail


def test_search_reads_past_a_first_page_with_no_graphics_cards(monkeypatch):
    # The server-side filter sends a page of cheap accessories with no products at all.
    fake, calls = _fake_fetch([
        (200, _page([], 81, 4, 1), {}),
        (200, _page([_row(1, 399.99)], 81, 4, 2), {}),
        (200, _page([], 81, 4, 3), {}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("arc b580")
    assert res.status is Status.OK and [r.sku for r in res.listings] == ["VC1"]
    assert [c["page"] for c in calls] == ["1", "2", "3", "4"]
    # no more pages than the search reports
    fake, calls = _fake_fetch([(200, _page([], 30, 2, 1), {}), (200, _page([], 30, 2, 2), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5090")
    assert res.status is Status.EMPTY and len(calls) == 2


def test_search_flags_page_cap_without_model_rows(monkeypatch):
    def titled(pid, title):
        return dict(_row(pid, 1000.0 + pid), name=title)

    sibling_pages = [
        (200, _page([titled(p, f"MSI GeForce RTX 5070 12G card {p}")], 100, 9, p), {}) for p in range(1, 5)
    ]
    fake, _ = _fake_fetch(sibling_pages)
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rx 9070 xt")
    assert res.status is Status.OK
    assert "page cap hit with no rx 9070 rows" in res.detail

    fake, _ = _fake_fetch(sibling_pages)
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070 ti")
    assert "page cap" not in res.detail  # 5070 rows are there; the matcher judges Ti
    msi = cc.parse(_page([titled(1, "MSI G5070-12S2C GDDR7 Double Fans")], 1, 1), "rtx 5070")
    typo = cc.parse(_page([titled(1, "RTX 50700 typo")], 1, 1), "rtx 5070")
    assert cc._has_model_rows(msi, "5070") and not cc._has_model_rows(typo, "5070")


def test_search_block_on_later_page_is_flagged(monkeypatch):
    fake, _ = _fake_fetch([
        (200, _page([_row(1, 900.0)], 30, 3), {}),
        (403, "denied", {"blocked_reason": "HTTP 403"}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.OK and len(res.listings) == 1
    assert res.blocked_partway is True and res.partial is True
    assert "page 2: HTTP 403" in res.detail
    # with nothing usable from page 1 the result is BLOCKED
    fake, _ = _fake_fetch([
        (200, _page([dict(_row(1, 0.0), price_amount=None)], 30, 3), {}),
        (403, "denied", {"blocked_reason": "HTTP 403"}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.BLOCKED and res.listings == [] and res.blocked_partway


def test_search_stops_at_last_page_and_keeps_partial(monkeypatch):
    fake, calls = _fake_fetch([
        (200, _page([_row(1, 900.0)], 13, 2), {}),
        (None, "", {"error": "Timeout"}),
    ])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.OK and len(res.listings) == 1
    assert "page 2: Timeout" in res.detail
    assert res.blocked_partway is False
    assert res.partial is True


@pytest.mark.parametrize("status, text, extra, reason, blocked", [
    (None, "", {"error": "Timeout: 30 s"}, "page 2: Timeout: 30 s", False),
    (500, "oops", {}, "page 2: HTTP 500", False),
    (429, "slow down", {"blocked_reason": "HTTP 429"}, "page 2: HTTP 429", True),
    (200, "<html>maintenance</html>", {}, "page 2: response is not JSON", False),
], ids=["timeout", "http500", "http429", "not-json"])
def test_search_later_page_failure_marks_result_partial(monkeypatch, status, text, extra, reason, blocked):
    # Any later-page failure leaves a partial read, but only a block trips the run breaker.
    fake, _ = _fake_fetch([(200, _page([_row(1, 900.0)], 30, 3), {}), (status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.OK and [r.sku for r in res.listings] == ["VC1"]
    assert res.partial is True
    assert res.blocked_partway is blocked
    assert reason in res.detail
    # a complete read is not partial
    fake, _ = _fake_fetch([(200, _page([_row(1, 900.0)], 1, 1), {})])
    monkeypatch.setattr(http, "fetch", fake)
    assert cc.search("rtx 5070").partial is False


def test_search_unexpected_error_on_later_page_keeps_earlier_pages(monkeypatch):
    bad = dict(_row(2, 950.0), stock_availability="Online - Available to Ship")  # str, not an object
    fake, _ = _fake_fetch([(200, _page([_row(1, 900.0)], 30, 3), {}), (200, _page([bad], 30, 3, 2), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.OK and [r.sku for r in res.listings] == ["VC1"]
    assert res.partial is True and res.blocked_partway is False
    assert "page 2: unexpected AttributeError" in res.detail
    # the same row on page 1 is still a parse error
    fake, _ = _fake_fetch([(200, _page([bad], 30, 3), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is Status.PARSE_ERROR and res.listings == []
    assert "AttributeError" in res.detail


def test_search_only_non_gpu_rows_is_empty(monkeypatch):
    fake, _ = _fake_fetch([(200, _page([_row(1, 3000.0, cat=103)], 1, 1), {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5090")
    assert res.status is Status.EMPTY and res.n_raw == 1
    assert "no rows in the graphics card categories" in res.detail


@pytest.mark.parametrize("status, text, extra, expected", [
    (None, "", {"error": "ConnectionError: reset"}, Status.NETWORK_ERROR),
    (429, "slow down", {"blocked_reason": "HTTP 429"}, Status.BLOCKED),
    (200, "<html>not json</html>", {}, Status.PARSE_ERROR),
    (500, "oops", {}, Status.NETWORK_ERROR),
    (404, "gone", {}, Status.NETWORK_ERROR),
], ids=["network", "http429", "not-json", "http500", "http404"])
def test_search_status_mapping(monkeypatch, status, text, extra, expected):
    fake, _ = _fake_fetch([(status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = cc.search("rtx 5070")
    assert res.status is expected and res.detail and res.listings == []


@pytest.mark.live
def test_live_search_smoke():
    res = cc.search("rtx 5070")
    assert res.status in (Status.OK, Status.EMPTY, Status.BLOCKED), res.detail
    if res.status is Status.OK:
        assert res.n_raw >= len(res.listings) > 0
        assert all(r.first_party and r.url.startswith("https://www.canadacomputers.com/") for r in res.listings)
