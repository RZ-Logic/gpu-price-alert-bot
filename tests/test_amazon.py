from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from gpu_alerts.retailers import amazon, http
from gpu_alerts.retailers.base import ParseError, Status

FIXTURES = Path(__file__).parent / "fixtures"
NODE = (FIXTURES / "amazon_node_rtx5080.html").read_text(encoding="utf-8")
AUDIT_CARDS = (FIXTURES / "amazon_cards_rtx5080.html").read_text(encoding="utf-8")
INTERSTITIAL = (FIXTURES / "amazon_interstitial_bmverify.html").read_text(encoding="utf-8")
ERROR_503 = (FIXTURES / "amazon_503_error_page.html").read_text(encoding="utf-8")
# Hand-written stand-in for Amazon's empty-search message; no live capture yet.
NO_RESULTS = (
    '<html><body><div class="s-no-outline"><span>No results for </span>'
    '<span class="a-text-bold">"rtx 9999 zzqxv"</span></div></body></html>'
)


def _fake_fetch(responses):
    calls = []

    def fake(url, **kw):
        calls.append(kw.get("params"))
        status, text, extra = responses[min(len(calls), len(responses)) - 1]
        return http.FetchResult(url, status, text, "chrome124", 5, **extra)

    return fake, calls


def _edit_card(html: str, asin: str, edit) -> str:
    soup = BeautifulSoup(html, "html.parser")
    edit(soup.select_one(f"div[data-asin='{asin}']"), soup)
    return str(soup)


def test_parse_node_page():
    rows = amazon.parse(NODE, "rtx 5080")
    # 10 cards: 2 sponsored and 2 without a price are skipped
    assert [r.sku for r in rows] == [
        "B0DQSLHSP2", "B0DTPG3B1N", "B0FKX6LPR6", "B096Y3TYDH", "B0DT7H5JYL", "B0F11KQQF4",
    ]
    prime = rows[1]
    assert prime.title == (
        "Asus Prime GeForce RTX 5080 16GB GDDR7 Prime-RTX5080-16G Pci-Express 5.0 Dlss 4.0 Graphics Card Gaming"
    )
    assert prime.price == 2229.99
    assert prime.url == "https://www.amazon.ca/dp/B0DTPG3B1N"
    assert prime.in_stock is True
    assert prime.first_party is True
    assert prime.seller is None
    assert prime.condition == "new"
    assert prime.regular_price is None
    assert prime.category == "Graphics Cards"
    assert all(r.retailer == "amazon" for r in rows)


def test_full_title_comes_from_h2_aria_label():
    row = amazon.parse(NODE, "rtx 5080")[0]
    assert row.title.startswith("The SFF-Ready ASUS Prime GeForce RTX™ 5080 16GB GDDR7 OC Edition")
    assert row.title.endswith("Vapor Chamber, Dual BIOS)")
    assert row.price == 2369.98


def test_third_party_merchants():
    rows = {r.sku: r for r in amazon.parse(NODE, "rtx 5080")}
    astral = rows["B0FKX6LPR6"]
    assert astral.first_party is False
    assert astral.price == 2989.99
    assert astral.seller is None  # merchant not listed in the page's Seller links
    assert rows["B096Y3TYDH"].first_party is False
    assert rows["B096Y3TYDH"].price == 3999.99


def test_seller_names_from_refinement_links():
    names = amazon._seller_names(BeautifulSoup(NODE, "html.parser"))
    assert len(names) == 7
    assert names["ADQ8PPI9HA4SX"] == "Amazon US"
    assert names["ANNSJU9W28Y9J"] == "MemoryC"
    html = NODE.replace('data-csa-c-merchant-id="ASHFC9MYCN1GN"', 'data-csa-c-merchant-id="ANNSJU9W28Y9J"')
    row = next(r for r in amazon.parse(html, "rtx 5080") if r.sku == "B0FKX6LPR6")
    assert row.first_party is False
    assert row.seller == "MemoryC"


def test_audit_fixture_skips_sponsored_and_unpriced():
    rows = amazon.parse(AUDIT_CARDS, "rtx 5080")
    assert [(r.sku, r.price) for r in rows] == [
        ("B0DQSLHSP2", 2369.98), ("B0DTPG3B1N", 2229.99), ("B0GZR4P62R", 2369.99),
    ]
    assert all(r.first_party is True and r.in_stock is True for r in rows)
    assert all(r.category is None for r in rows)  # not a node-filtered search


def test_no_add_to_cart_means_unknown_stock_and_seller():
    def strip_cart(card, soup):
        for tag in card.select("[data-csa-c-merchant-id], input[name='submit.addToCart']"):
            tag.decompose()

    html = _edit_card(NODE, "B0DTPG3B1N", strip_cart)
    row = next(r for r in amazon.parse(html, "rtx 5080") if r.sku == "B0DTPG3B1N")
    assert row.in_stock is None
    assert row.first_party is None
    assert row.price == 2229.99


def test_price_fallback_and_strike_price():
    def edit(card, soup):
        card.select_one("span.a-price span.a-offscreen").decompose()
        strike = BeautifulSoup(
            '<span class="a-price a-text-price" data-a-strike="true">'
            '<span class="a-offscreen">$2,599.99</span></span>', "html.parser",
        )
        card.select_one("span.a-price").insert_after(strike)

    html = _edit_card(NODE, "B0DTPG3B1N", edit)
    row = next(r for r in amazon.parse(html, "rtx 5080") if r.sku == "B0DTPG3B1N")
    assert row.price == 2229.99  # from a-price-whole + a-price-fraction
    assert row.regular_price == 2599.99


def test_renewed_title_is_refurbished():
    html = NODE.replace(
        'aria-label="MSI GeForce RTX 5080 16G Expert OC"',
        'aria-label="MSI GeForce RTX 5080 16G Expert OC (Renewed)"',
    )
    row = next(r for r in amazon.parse(html, "rtx 5080") if r.sku == "B096Y3TYDH")
    assert row.condition == "refurbished"
    assert amazon._condition("ZOTAC RTX 5070 Open Box") == "open_box"
    assert amazon._condition("MSI RTX 3070 (Used)") == "used"


@pytest.mark.parametrize("text, value", [
    ("$2,369.98", 2369.98),
    ("CDN$ 1,234.56", 1234.56),
    ("2 369,98\xa0$", 2369.98),
    ("$1,299", 1299.0),
    ("", None),
])
def test_money(text, value):
    assert amazon._money(text) == value


def test_page_info_and_states():
    assert amazon.page_info(NODE) == (10, 24, 136)
    assert amazon.page_state(NODE) is None
    assert amazon.page_state(INTERSTITIAL) == "blocked"
    assert amazon.page_state(NO_RESULTS) == "no_results"
    assert amazon.parse(NO_RESULTS, "rtx 9999 zzqxv") == []
    for page in (INTERSTITIAL, ERROR_503, "<html><body><div id='search'></div></body></html>"):
        with pytest.raises(ParseError):
            amazon.parse(page, "rtx 5080")


def _drift_all(html: str, selector: str, edit) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select(selector):
        edit(tag)
    return str(soup)


def _rename_class(tag, old: str, new: str) -> None:
    tag["class"] = [new if c == old else c for c in tag.get("class", [])]


@pytest.mark.parametrize("selector, edit, error", [
    ("span.a-price", lambda t: _rename_class(t, "a-price", "a-price-v2"), "none of 10 cards had span.a-price"),
    (amazon._CARD_SELECTOR, lambda t: t.attrs.pop("data-asin"), "none of 10 result cards had a data-asin"),
    (f"{amazon._CARD_SELECTOR} h2", lambda t: t.decompose(), "none of 8 organic cards had a title"),
], ids=["price-class-renamed", "asin-renamed", "h2-removed"])
def test_parse_raises_when_card_markup_drifts(selector, edit, error):
    # Layout drift must not read as a healthy EMPTY page ("cards, none organic with a price").
    with pytest.raises(ParseError, match=error):
        amazon.parse(_drift_all(NODE, selector, edit), "rtx 5080")


def test_parse_cards_without_offers_are_not_drift():
    # "No featured offers" / "See options" cards are real; with priced sponsored cards
    # still on the page, a page of only such organic cards is EMPTY, not an error.
    def drop_price(card):
        if not amazon._is_sponsored(card):
            for tag in card.select("span.a-price"):
                tag.decompose()

    assert amazon.parse(_drift_all(NODE, amazon._CARD_SELECTOR, drop_price), "rtx 5080") == []


def test_search_card_drift_is_a_parse_error(monkeypatch):
    drifted = _drift_all(NODE, "span.a-price", lambda t: _rename_class(t, "a-price", "a-price-v2"))
    fake, _ = _fake_fetch([(200, drifted, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = amazon.search("rtx 5080")
    assert res.status is Status.PARSE_ERROR and not res.ok
    assert "span.a-price" in res.detail


def test_search_reads_page2_and_dedupes(monkeypatch):
    fake, calls = _fake_fetch([(200, NODE, {}), (200, NODE, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = amazon.search("rtx 5080")
    assert res.status is Status.OK
    assert res.http_status == 200
    assert res.n_raw == 20
    assert len(res.listings) == 6
    assert calls == [
        {"k": "rtx 5080", "rh": "n:677243011"},
        {"k": "rtx 5080", "rh": "n:677243011", "page": "2"},
    ]
    assert "136 results reported" in res.detail
    assert res.partial is False


def test_search_page2_interstitial_keeps_page1(monkeypatch):
    fake, _ = _fake_fetch([(200, NODE, {}), (200, INTERSTITIAL, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = amazon.search("rtx 5080")
    assert res.status is Status.OK
    assert len(res.listings) == 6
    assert "page 2: bot-check interstitial" in res.detail
    # exposed so the run-level breaker skips Amazon for the rest of the run
    assert res.blocked_partway is True and res.partial is True


def test_search_page2_block_without_page1_listings_is_blocked(monkeypatch):
    fake, _ = _fake_fetch([(200, NODE, {}), (200, INTERSTITIAL, {})])
    monkeypatch.setattr(http, "fetch", fake)
    monkeypatch.setattr(amazon, "parse", lambda payload, query: [])
    res = amazon.search("rtx 5080")
    assert res.status is Status.BLOCKED and res.listings == [] and res.blocked_partway


def test_search_page2_timeout_is_not_a_block(monkeypatch):
    fake, _ = _fake_fetch([(200, NODE, {}), (None, "", {"error": "Timeout: 30 s"})])
    monkeypatch.setattr(http, "fetch", fake)
    res = amazon.search("rtx 5080")
    assert res.status is Status.OK and res.blocked_partway is False
    assert "page 2: Timeout" in res.detail
    # still a partial read: the page-1 listings are no proof a page-2 deal is gone
    assert res.partial is True


@pytest.mark.parametrize("status, text, extra, reason", [
    (500, "oops", {}, "page 2: HTTP 500"),
    (200, "<html><body><div id='search'></div></body></html>", {}, "page 2: no s-search-result cards"),
], ids=["http500", "no-cards"])
def test_search_page2_error_marks_result_partial(monkeypatch, status, text, extra, reason):
    fake, _ = _fake_fetch([(200, NODE, {}), (status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = amazon.search("rtx 5080")
    assert res.status is Status.OK and len(res.listings) == 6
    assert res.partial is True and res.blocked_partway is False
    assert reason in res.detail


def test_search_unexpected_error_on_page2_keeps_page1(monkeypatch):
    real_parse, pages = amazon.parse, []

    def parse(payload, query):
        pages.append(query)
        if len(pages) == 2:
            raise TypeError("unexpected markup")
        return real_parse(payload, query)

    fake, _ = _fake_fetch([(200, NODE, {}), (200, NODE, {})])
    monkeypatch.setattr(http, "fetch", fake)
    monkeypatch.setattr(amazon, "parse", parse)
    res = amazon.search("rtx 5080")
    assert res.status is Status.OK and len(res.listings) == 6
    assert res.partial is True and res.blocked_partway is False
    assert "page 2: unexpected TypeError: unexpected markup" in res.detail


def test_search_no_results_is_empty(monkeypatch):
    fake, calls = _fake_fetch([(200, NO_RESULTS, {})])
    monkeypatch.setattr(http, "fetch", fake)
    res = amazon.search("rtx 9999 zzqxv")
    assert res.status is Status.EMPTY
    assert len(calls) == 1
    assert res.detail == "no results"


@pytest.mark.parametrize("status, text, extra, expected", [
    (200, INTERSTITIAL, {}, Status.BLOCKED),
    (503, ERROR_503, {"blocked_reason": "HTTP 503"}, Status.BLOCKED),
    (200, "<html><body><div id='search'></div></body></html>", {}, Status.PARSE_ERROR),
    (404, "not found", {}, Status.NETWORK_ERROR),
    (500, "oops", {}, Status.NETWORK_ERROR),
    (None, "", {"error": "Timeout: 30 s"}, Status.NETWORK_ERROR),
], ids=["interstitial", "http503", "no-cards", "http404", "http500", "timeout"])
def test_search_failure_statuses(monkeypatch, status, text, extra, expected):
    fake, _ = _fake_fetch([(status, text, extra)])
    monkeypatch.setattr(http, "fetch", fake)
    res = amazon.search("rtx 5080")
    assert res.status is expected
    assert res.http_status == status
    assert res.listings == []
    assert res.detail


@pytest.mark.live
def test_live_search_smoke():
    res = amazon.search("rtx 5080")
    # Amazon may answer with its bot-check interstitial; that must come back as BLOCKED, never EMPTY.
    assert res.status in (Status.OK, Status.BLOCKED), res.detail
    if res.status is Status.OK:
        assert res.n_raw >= len(res.listings) > 0
        assert all(r.price > 0 and r.url.startswith("https://www.amazon.ca/dp/") for r in res.listings)
