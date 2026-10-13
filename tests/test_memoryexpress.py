"""Memory Express adapter: offline parsing of saved category pages, status mapping, one live smoke test."""

from pathlib import Path

import pytest

from gpu_alerts.retailers import memoryexpress
from gpu_alerts.retailers.base import ParseError, Status
from gpu_alerts.retailers.http import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _by_sku(listings):
    return {l.sku: l for l in listings}


def test_parse_rtx5070_page():
    listings = memoryexpress.parse(_text("memoryexpress_search_rtx5070.html"), "rtx 5070")
    assert len(listings) == 41
    assert sum(1 for l in listings if l.in_stock) == 31
    assert all(l.first_party is True and l.seller == "Memory Express" for l in listings)
    assert all(l.condition == "new" and l.category == "Graphics Cards" for l in listings)
    by_sku = _by_sku(listings)

    prime = by_sku["MX00132604"]
    assert prime.title == "Asus GeForce RTX 5070 PRIME 12GB PCI-E w/ HDMI, Triple DP"  # brand from the logo
    assert prime.price == 1239.99 and prime.regular_price == 1239.99
    assert prime.in_stock is True
    assert prime.url == "https://www.memoryexpress.com/Products/MX00132604"

    assert by_sku["MX00134885"].in_stock is False  # "Out of Stock", buy button disabled
    assert by_sku["MX00135862"].in_stock is False  # "Special Order"
    assert by_sku["MX00136530"].in_stock is True   # "While Supplies Last"


def test_parse_rtx5090_drops_bundle_and_reads_stock():
    listings = memoryexpress.parse(_text("memoryexpress_search_rtx5090.html"), "rtx 5090")
    assert len(listings) == 13  # 14 cards, one BDL_ bundle
    assert not any(l.sku.startswith("BDL_") for l in listings)
    assert all(l.in_stock is False for l in listings)
    gaming = _by_sku(listings)["MX00132477"]
    assert gaming.title == "Gigabyte GeForce RTX 5090 GAMING OC 32GB PCI-E w/ HDMI, Triple DP"
    assert gaming.price == 7199.99


def test_sale_price_is_checkout_price_and_reg_is_regular():
    # The only sale card on the page is a bundle; relabel it to parse it as a card.
    html = _text("memoryexpress_search_rtx5090.html").replace("BDL_MM00005852", "MX99999999")
    sale = _by_sku(memoryexpress.parse(html, "rtx 5090"))["MX99999999"]
    assert sale.price == 6999.99
    assert sale.regular_price == 8429.98


def test_mail_in_rebate_price_is_ignored():
    html = _text("memoryexpress_search_rtx5070.html").replace(
        "$1,239.99 After Rebate", "$1,139.99 After Rebate", 1,
    )
    assert _by_sku(memoryexpress.parse(html, "rtx 5070"))["MX00132604"].price == 1239.99


def test_empty_results_page():
    assert memoryexpress.parse(_text("memoryexpress_empty.html"), "zzqx") == []


def test_parse_errors_on_missing_structure():
    bare_cards = _text("memoryexpress_cards_rtx5080.html")  # audit fixture: cards without the list section
    with pytest.raises(ParseError):
        memoryexpress.parse(bare_cards, "rtx 5080")
    with pytest.raises(ParseError):
        memoryexpress.parse('<section id="AjaxProductList" class="c-cact-product-list"></section>', "rtx 5080")
    with pytest.raises(ParseError):
        memoryexpress.parse(
            '<section id="AjaxProductList" class="c-cact-product-list">'
            '<div class="c-shca-icon-item"><div>no fields</div></div></section>', "rtx 5080",
        )


def test_audit_cards_inside_a_list_section():
    html = ('<section id="AjaxProductList" class="c-cact-product-list">'
            + _text("memoryexpress_cards_rtx5080.html") + "</section>")
    listings = memoryexpress.parse(html, "rtx 5080")
    assert len(listings) == 4
    by_sku = _by_sku(listings)
    assert by_sku["MX00132529"].title == "Asus PRIME GeForce RTX 5080 16GB PCIe w/ Triple DP, HDMI"
    assert by_sku["MX00132529"].price == 2279.99
    assert by_sku["MX00135617"].in_stock is False
    assert by_sku["MX00132521"].in_stock is True


# search() status mapping with a fake transport


def _fake_fetch(monkeypatch, response: FetchResult) -> None:
    monkeypatch.setattr(memoryexpress, "fetch", lambda url, **kwargs: response)


@pytest.mark.parametrize("response, status, n_raw", [
    (FetchResult("u", 200, _text("memoryexpress_search_rtx5090.html"), "chrome124", 5), Status.OK, 14),
    (FetchResult("u", 200, _text("memoryexpress_empty.html"), "chrome124", 5), Status.EMPTY, 0),
    (FetchResult("u", 403, "<title>Just a moment...</title>", "chrome124", 5, blocked_reason="HTTP 403"),
     Status.BLOCKED, 0),
    (FetchResult("u", None, "", "chrome124", 5, error="Timeout: 30 s"), Status.NETWORK_ERROR, 0),
    (FetchResult("u", 200, "<html><body>new layout</body></html>", "chrome124", 5), Status.PARSE_ERROR, 0),
])
def test_search_status_mapping(monkeypatch, response, status, n_raw):
    _fake_fetch(monkeypatch, response)
    r = memoryexpress.search("rtx 5090")
    assert r.status is status
    assert r.n_raw == n_raw
    assert r.http_status == response.status_code
    assert r.detail
    assert len(r.listings) == (13 if status is Status.OK else 0)


@pytest.mark.live
def test_live_search_smoke():
    r = memoryexpress.search("rtx 5080")
    assert r.status is Status.OK, r.detail
    assert r.n_raw > 0 and r.listings
    assert all(l.url.startswith("https://www.memoryexpress.com/Products/") for l in r.listings)
    assert all(l.price > 0 for l in r.listings)
