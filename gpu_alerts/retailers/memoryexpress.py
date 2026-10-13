"""Memory Express adapter: the Video Cards category page, filtered server side.

/Search/Products is behind a Cloudflare challenge, but
/Category/VideoCards?Search=<query> answers 200 to curl_cffi chrome124 and
filters on the server (measured 2026-10-12). Every row on that page is in
the Graphics Cards category, so no category filter is needed beyond dropping
bundles (SKU prefix "BDL_").
"""

import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .base import Listing, ParseError, ScrapeResult, Status
from .http import fetch

RETAILER_ID = "memoryexpress"
DISPLAY_NAME = "Memory Express"

BASE_URL = "https://www.memoryexpress.com"
SEARCH_URL = f"{BASE_URL}/Category/VideoCards"
CATEGORY = "Graphics Cards"
PAGE_SIZE = 120
BUNDLE_PREFIX = "BDL_"

# Inventory badges that mean you cannot get the card now. "Special Order" keeps
# the Buy button but ships only after Memory Express orders it in.
NOT_IN_STOCK_BADGES = {"out of stock", "special order", "not available online"}

_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_CONDITION_PATTERNS = (
    ("open_box", re.compile(r"\bopen[\s-]?box\b", re.I)),
    ("refurbished", re.compile(r"\b(refurbished|renewed)\b", re.I)),
    ("used", re.compile(r"\b(used|pre-?owned)\b", re.I)),
)


def _price(text: str | None) -> float | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    price = float(m.group(1).replace(",", ""))
    return price if price > 0 else None


def _condition(title: str) -> str:
    for condition, pattern in _CONDITION_PATTERNS:
        if pattern.search(title):
            return condition
    return "new"


def _product_list(soup: BeautifulSoup) -> Tag:
    section = soup.select_one("section#AjaxProductList") or soup.select_one("section.c-cact-product-list")
    if section is None:
        raise ParseError("no product list section (section#AjaxProductList)")
    return section


def _cards(section: Tag) -> list[Tag]:
    cards = section.select("div.c-shca-icon-item")
    if not cards and "c-cact-product-list--empty" not in (section.get("class") or []):
        raise ParseError("product list has no item cards and no empty-results marker")
    return cards


def _title(card: Tag) -> str | None:
    name = card.select_one(".c-shca-icon-item__body-name")
    if name is None:
        return None
    text = " ".join(name.get_text(" ", strip=True).split())
    # The brand is only an image; its title attribute carries the name.
    brand_img = name.select_one(".c-shca-icon-item__body-name-brand img")
    brand = (brand_img.get("title") or brand_img.get("alt") or "").strip() if brand_img else ""
    if brand and not text.lower().startswith(brand.lower()):
        text = f"{brand} {text}"
    return text or None


def _current_price(card: Tag) -> float | None:
    """Price charged at checkout: the first plain span in the list block, never the after-rebate one."""
    block = card.select_one(".c-shca-icon-item__summary-list")
    if block is None:
        return None
    for span in block.find_all("span", recursive=False):
        if "c-shca-icon-item__summary-rebate" in (span.get("class") or []):
            continue
        price = _price(span.get_text(" ", strip=True))
        if price is not None:
            return price
    return None


def _in_stock(card: Tag) -> bool | None:
    badge = card.select_one(".c-shca-icon-item__body-inventory")
    if badge is not None and badge.get_text(" ", strip=True).lower() in NOT_IN_STOCK_BADGES:
        return False
    if "out of stock" in card.get_text(" ", strip=True).lower():
        return False
    buy = card.select_one("a.c-shca-icon-item__summary-buy")
    if buy is None:
        return None
    return str(buy.get("data-disabled", "")).lower() != "true"


def _listing(card: Tag) -> Listing | None:
    ref = card.select_one(".c-shca-icon-item__body-ref span")
    sku = ref.get_text(strip=True) if ref else ""
    title = _title(card)
    price = _current_price(card)
    if not sku or not title or price is None:
        return None
    regular = card.select_one(".c-shca-icon-item__summary-regular span")
    link = card.select_one(".c-shca-icon-item__body-name a[href]")
    href = link.get("href") if link else None
    return Listing(
        retailer=RETAILER_ID,
        title=title,
        price=price,
        url=urljoin(BASE_URL, href) if href else f"{BASE_URL}/Products/{sku}",
        sku=sku,
        in_stock=_in_stock(card),
        first_party=True,
        seller=DISPLAY_NAME,
        condition=_condition(title),
        regular_price=_price(regular.get_text(strip=True)) if regular else None,
        category=CATEGORY,
    )


def _parse_cards(cards: list[Tag]) -> list[Listing]:
    listings: list[Listing] = []
    malformed = 0
    for card in cards:
        listing = _listing(card)
        if listing is None:
            malformed += 1
        elif not listing.sku.upper().startswith(BUNDLE_PREFIX):
            listings.append(listing)
    if cards and malformed == len(cards):
        raise ParseError(f"none of {len(cards)} item cards had sku, title and price")
    return listings


def parse(payload: str | bytes, query: str) -> list[Listing]:
    """Category page HTML to listings. Returns [] only for a real empty-results page."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    soup = BeautifulSoup(payload, "html.parser")
    return _parse_cards(_cards(_product_list(soup)))


def search(query: str) -> ScrapeResult:
    """Fetch and parse the filtered Video Cards page. Never raises."""
    start = time.monotonic()
    result = ScrapeResult(RETAILER_ID, query, Status.NETWORK_ERROR)
    try:
        r = fetch(SEARCH_URL, params={"Search": query, "PageSize": PAGE_SIZE}, timeout=30.0)
        result.http_status = r.status_code
        if r.error:
            result.detail = r.error
            return result
        if r.blocked_reason:
            result.status, result.detail = Status.BLOCKED, r.blocked_reason
            return result
        if r.status_code != 200:
            result.detail = f"HTTP {r.status_code}"
            return result
        try:
            cards = _cards(_product_list(BeautifulSoup(r.text, "html.parser")))
            result.n_raw = len(cards)
            listings = _parse_cards(cards)
        except ParseError as exc:
            result.status, result.detail = Status.PARSE_ERROR, str(exc)
            return result
        result.listings = listings
        result.status = Status.OK if listings else Status.EMPTY
        result.detail = f"{result.n_raw} cards, {len(listings)} after dropping bundles"
        if result.n_raw >= PAGE_SIZE:
            result.detail += f"; page full at {PAGE_SIZE}, later results not fetched"
        return result
    except Exception as exc:  # never raise out of search()
        result.status = Status.PARSE_ERROR
        result.detail = f"unexpected {type(exc).__name__}: {exc}"[:300]
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
