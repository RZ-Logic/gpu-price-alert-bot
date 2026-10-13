"""Amazon.ca adapter: search page HTML cards, narrowed to the Graphics Cards node.

Node 677243011 is Electronics > Computers & Accessories > Computer Components >
Graphics Cards on amazon.ca (checked live 2026-10-12), so accessories and
prebuilt PCs mostly drop out before parsing. Cards are
div[data-component-type='s-search-result']; sponsored cards are skipped and
cards without a price ("No featured offers", "See options") are skipped too.
The add-to-cart widget carries the buy-box merchant id; A3DWYIK6Y9EEQB is
Amazon.ca itself ("Ships from and sold by Amazon.ca"). Other merchants,
including "Amazon US" imports, count as third-party.

Amazon answers some requests with a 200 Akamai interstitial (bm-verify) that
the shared block check does not know, so search() looks for it before parsing.
"""

import re
import time
from urllib.parse import unquote

from bs4 import BeautifulSoup

from . import http
from .base import Listing, ParseError, ScrapeResult, Status

RETAILER_ID = "amazon"
DISPLAY_NAME = "Amazon.ca"

BASE_URL = "https://www.amazon.ca"
SEARCH_URL = BASE_URL + "/s"
GPU_NODE = "677243011"
GPU_CATEGORY = "Graphics Cards"
AMAZON_CA_MERCHANT = "A3DWYIK6Y9EEQB"
MAX_PAGES = 2
HEADERS = {"Accept-Language": "en-CA,en;q=0.9"}
# Amazon showed its bot check after about 5 searches in a few minutes (2026-10-12),
# so its searches are spaced well beyond the shared 2 s default.
MIN_INTERVAL = 10.0
BLOCK_TEXT = (
    "bm-verify", "/_sec/verify", "triggerinterstitialchallenge", "validatecaptcha",
    "robot check", "enter the characters you see below", "api-services-support@amazon.com",
)
NO_RESULTS_TEXT = ("no results for", "did not match any products")

_CARD_SELECTOR = "div[data-component-type='s-search-result']"
_RANGE_RE = re.compile(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(?:over\s+)?(\d[\d,]*)", re.IGNORECASE)
_SELLER_RE = re.compile(r"p_6:([A-Z0-9]{8,20})")
_SPONSORED_PREFIX_RE = re.compile(r"^\s*Sponsored\s+Ad\s*[^\w(]*\s*", re.IGNORECASE)
_REFURB_RE = re.compile(r"\b(renewed|refurbish\w*|recertified)\b", re.IGNORECASE)
_OPEN_BOX_RE = re.compile(r"\bopen[\s_-]?box\b", re.IGNORECASE)
_USED_RE = re.compile(r"(\(used\)|\bpre-?owned\b)", re.IGNORECASE)


def _soup(payload: str | BeautifulSoup) -> BeautifulSoup:
    return payload if isinstance(payload, BeautifulSoup) else BeautifulSoup(payload, "html.parser")


def _money(text: str | None) -> float | None:
    """'$2,369.98', 'CDN$ 1,234.56' or '2 369,98 $' as a float."""
    s = re.sub(r"[^\d.,]", "", (text or "").replace(" ", "").replace("\xa0", ""))
    if not s or not any(c.isdigit() for c in s):
        return None
    if "," in s and "." in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def _is_sponsored(card) -> bool:
    if "AdHolder" in (card.get("class") or []):
        return True
    if card.select_one(".puis-sponsored-label-text, .s-sponsored-label-text"):
        return True
    h2 = card.select_one("h2")
    label = (h2.get("aria-label") or "") if h2 else ""
    return label.lower().startswith("sponsored")


def _title(card) -> str:
    h2 = card.select_one("h2")
    if not h2:
        return ""
    text = h2.get("aria-label") or h2.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", _SPONSORED_PREFIX_RE.sub("", text)).strip()


def _price(card) -> float | None:
    tag = card.select_one("span.a-price:not(.a-text-price) span.a-offscreen")
    if tag:
        value = _money(tag.get_text())
        if value:
            return value
    whole = card.select_one("span.a-price:not(.a-text-price) span.a-price-whole")
    if whole:
        frac = card.select_one("span.a-price:not(.a-text-price) span.a-price-fraction")
        digits = re.sub(r"\D", "", whole.get_text())
        cents = re.sub(r"\D", "", frac.get_text()) if frac else "00"
        if digits:
            return _money(f"{digits}.{cents or '00'}")
    return None


def _regular_price(card, price: float) -> float | None:
    tag = card.select_one("span.a-price.a-text-price[data-a-strike='true'] span.a-offscreen")
    value = _money(tag.get_text()) if tag else None
    return value if value and value > price else None


def _merchant(card) -> str | None:
    tag = card.select_one("[data-csa-c-merchant-id]")
    if tag is None:
        return None
    return (tag.get("data-csa-c-merchant-id") or "").strip() or None


def _has_add_to_cart(card) -> bool:
    return bool(card.select_one(
        "input[name='submit.addToCart'], button[name='submit.addToCart'], "
        "[data-csa-c-action-name='addToCart']"
    ))


def _condition(title: str) -> str:
    if _OPEN_BOX_RE.search(title):
        return "open_box"
    if _REFURB_RE.search(title):
        return "refurbished"
    if _USED_RE.search(title):
        return "used"
    return "new"


def _seller_names(soup: BeautifulSoup) -> dict[str, str]:
    """Merchant id to name, from the 'Seller' refinement links (p_6:<id>)."""
    names: dict[str, str] = {}
    for a in soup.select("a[href*='p_6']"):
        m = _SELLER_RE.search(unquote(a.get("href") or ""))
        name = a.get_text(" ", strip=True)
        if m and name:
            names.setdefault(m.group(1), name)
    return names


def _node_filtered(html: str) -> bool:
    return f"n:{GPU_NODE}" in html or f"n%3A{GPU_NODE}" in html


def page_state(html: str) -> str | None:
    """'blocked' for a bot-check page, 'no_results' for a real empty search, else None."""
    if re.search(r"data-component-type=[\"']s-search-result", html):
        return None
    head = html[:200_000].lower()
    if any(marker in head for marker in BLOCK_TEXT):
        return "blocked"
    if any(marker in head for marker in NO_RESULTS_TEXT):
        return "no_results"
    return None


def page_info(payload: str | BeautifulSoup) -> tuple[int, int | None, int | None]:
    """(result cards on the page, last result index shown, total results reported)."""
    soup = _soup(payload)
    cards = len(soup.select(_CARD_SELECTOR))
    bar = soup.select_one("[data-component-type='s-result-info-bar']")
    m = _RANGE_RE.search(bar.get_text(" ", strip=True) if bar else "")
    if not m:
        return cards, None, None
    return cards, int(m.group(2).replace(",", "")), int(m.group(3).replace(",", ""))


def parse(payload: str, query: str) -> list[Listing]:
    """Listings from an Amazon.ca search results page."""
    soup = _soup(payload)
    cards = soup.select(_CARD_SELECTOR)
    if not cards:
        if page_state(payload) == "no_results":
            return []
        raise ParseError("no s-search-result cards in page")
    sellers = _seller_names(soup)
    category = GPU_CATEGORY if _node_filtered(payload) else None
    out: list[Listing] = []
    seen: set[str] = set()
    for card in cards:
        asin = (card.get("data-asin") or "").strip()
        if not asin or asin in seen or _is_sponsored(card):
            continue
        title = _title(card)
        price = _price(card)
        if not title or price is None:
            continue
        seen.add(asin)
        merchant = _merchant(card)
        first_party = merchant == AMAZON_CA_MERCHANT if merchant else None
        out.append(Listing(
            retailer=RETAILER_ID,
            title=title,
            price=price,
            url=f"{BASE_URL}/dp/{asin}",
            sku=asin,
            in_stock=True if _has_add_to_cart(card) else None,
            first_party=first_party,
            seller=sellers.get(merchant) if merchant and not first_party else None,
            condition=_condition(title),
            regular_price=_regular_price(card, price),
            category=category,
        ))
    return out


def search(query: str) -> ScrapeResult:
    """Search the Amazon.ca Graphics Cards node, reading page 2 when more results are reported."""
    start = time.monotonic()
    result = ScrapeResult(RETAILER_ID, query, Status.NETWORK_ERROR)
    listings: list[Listing] = []
    seen: set[str] = set()
    notes: list[str] = []
    try:
        for page in range(1, MAX_PAGES + 1):
            params = {"k": query, "rh": f"n:{GPU_NODE}"}
            if page > 1:
                params["page"] = str(page)
            res = http.fetch(SEARCH_URL, params=params, headers=HEADERS, min_interval=MIN_INTERVAL)
            if page == 1:
                result.http_status = res.status_code
            failure: tuple[Status, str] | None = None
            state = page_state(res.text) if res.status_code == 200 and not res.error else None
            if res.error:
                failure = (Status.NETWORK_ERROR, res.error)
            elif res.blocked_reason:
                failure = (Status.BLOCKED, res.blocked_reason)
            elif state == "blocked":
                failure = (Status.BLOCKED, "bot-check interstitial")
            elif res.status_code != 200:
                failure = (Status.NETWORK_ERROR, f"HTTP {res.status_code}")
            if failure is None:
                try:
                    rows = parse(res.text, query)
                    n_page, shown, total = page_info(res.text)
                except ParseError as exc:
                    failure = (Status.PARSE_ERROR, str(exc)[:200])
            if failure:
                if page == 1:
                    result.status, result.detail = failure
                    return result
                notes.append(f"page {page}: {failure[1]}")
                result.blocked_partway = failure[0] is Status.BLOCKED
                break
            result.n_raw += n_page
            for row in rows:
                if row.sku not in seen:
                    seen.add(row.sku)
                    listings.append(row)
            if page == 1:
                notes.append("no results" if state == "no_results" else f"{total} results reported")
            if not shown or not total or shown >= total:
                break
        result.listings = listings
        if listings:
            result.status = Status.OK
        elif result.blocked_partway:
            result.status = Status.BLOCKED
        else:
            result.status = Status.EMPTY
        if not listings and result.n_raw:
            notes.append(f"{result.n_raw} cards, none organic with a price")
        result.detail = "; ".join(notes)
        return result
    except Exception as exc:  # search() must never raise
        result.status = Status.PARSE_ERROR
        result.detail = f"{type(exc).__name__}: {exc}"[:200]
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
