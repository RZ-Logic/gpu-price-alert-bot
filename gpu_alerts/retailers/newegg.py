"""Newegg.ca adapter: search page HTML with results in window.__initialState__.

Plain clients get a 403 CAPTCHA page, so every request goes through the
impersonating fetch(). Newegg serves two card layouts (div.item-cell and
div.goods-container) but both embed the same state object, so the adapter reads
Products[].ItemCell and never the card markup. N=4131 is Newegg's "In Stock"
filter. Results come 36 per page in best-match order; page 2 is read when page
1 is full. A seller name on the ItemCell marks a marketplace offer.
"""

import json
import re
import time

from . import http
from .base import Listing, ParseError, ScrapeResult, Status

RETAILER_ID = "newegg"
DISPLAY_NAME = "Newegg"

BASE_URL = "https://www.newegg.ca"
SEARCH_URL = BASE_URL + "/p/pl"
IN_STOCK_FILTER = "4131"
PAGE_SIZE = 36
MAX_PAGES = 2
BLOCK_TITLE_TEXT = ("captcha", "are you a human", "access denied")

_STATE_RE = re.compile(r"window\.__initialState__\s*=\s*\{")
_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)
_NEWEGG_ITEM_RE = re.compile(r"^\d{2}-\d{3}-\d{3}$")   # "14-126-744", sold by Newegg
_OPEN_BOX_RE = re.compile(r"\bopen[\s_-]?box\b", re.IGNORECASE)
_REFURB_RE = re.compile(r"\b(refurbish\w*|recertified|renewed)\b", re.IGNORECASE)
_USED_RE = re.compile(r"(^\s*used\b|\(used\)|\bpre-?owned\b)", re.IGNORECASE)


def _state(payload: str | dict) -> dict:
    """The __initialState__ object from page HTML, its JSON text, or a saved fixture."""
    if isinstance(payload, dict):
        return payload
    text = payload.lstrip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ParseError(f"invalid JSON: {exc}") from exc
    m = _STATE_RE.search(payload)
    if not m:
        raise ParseError("no window.__initialState__ in page")
    try:
        state, _ = json.JSONDecoder().raw_decode(payload, m.end() - 1)
    except ValueError as exc:
        raise ParseError(f"__initialState__ is not JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise ParseError("__initialState__ is not an object")
    return state


def _products(state: dict) -> list[dict]:
    if "Products" not in state:
        raise ParseError("__initialState__ has no Products key")
    products = state["Products"]
    if products is None or products == []:
        # A no-results page carries Products: null with TotalItemCount 0.
        if state.get("TotalItemCount") in (0, None):
            return []
        raise ParseError(f"Products is {products!r} but TotalItemCount is {state.get('TotalItemCount')}")
    if not isinstance(products, list):
        raise ParseError("Products is not a list")
    return [p for p in products if isinstance(p, dict)]


def _is_gpu_category(text: str | None) -> bool | None:
    if not text:
        return None
    t = text.lower()
    return ("graphics card" in t or "video card" in t) and "accessor" not in t


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _prices(cell: dict) -> tuple[float | None, float | None]:
    """(price you pay now, regular price), both including shipping, before tax.

    FinalPrice = UnitCost - InstantRebateAmount + ShippingCharge, except that a
    ShippingCharge of 0.01 (first-party free shipping) is not added. Every
    fixture row checked 2026-10-12 fits that rule, so FinalPrice is used when
    present and the same sum is rebuilt when it is not. Mail-in rebates and
    promo codes are not applied.
    """
    unit = _num(cell.get("UnitCost"))
    rebate = _num(cell.get("InstantRebateAmount"))
    shipping = _num(cell.get("ShippingCharge"))
    if shipping <= 0.01:
        shipping = 0.0  # 0.01 is Newegg's marker for free shipping
    final = _num(cell.get("FinalPrice"))
    if final > 0:
        price = round(final, 2)
    elif unit > 0:
        price = round(unit - rebate + shipping, 2)
    else:
        return None, None
    if price <= 0:
        return None, None
    regular = round(unit + shipping, 2) if rebate > 0 and unit + shipping > price else None
    return price, regular


def _condition(cell: dict, title: str) -> str:
    feature = cell.get("Feature") or {}
    if _OPEN_BOX_RE.search(title) or feature.get("IsOpenBoxed"):
        return "open_box"
    if _REFURB_RE.search(title) or feature.get("IsRefurbished"):
        return "refurbished"
    if _USED_RE.search(title):
        return "used"
    return "new"


def _url(item: str, parent: str | None) -> str:
    """Product link. Newegg items use the N82E168 form; marketplace items add ?Item= to their parent."""
    if _NEWEGG_ITEM_RE.match(item):
        return f"{BASE_URL}/p/N82E168{item.replace('-', '')}"
    if parent and _NEWEGG_ITEM_RE.match(parent):
        return f"{BASE_URL}/p/N82E168{parent.replace('-', '')}?Item={item}"
    if parent and parent != item:
        # /p/<item> redirects to the parent without ?Item=, which opens the featured
        # offer (maybe another seller); /p/<parent>?Item=<item> keeps this offer.
        return f"{BASE_URL}/p/{parent}?Item={item}"
    return f"{BASE_URL}/p/{item}"


def parse(payload: str | dict, query: str) -> list[Listing]:
    """Listings from a Newegg.ca search page (HTML, __initialState__ JSON, or fixture)."""
    state = _state(payload)
    out: list[Listing] = []
    seen: set[str] = set()
    malformed = 0
    for product in _products(state):
        cell = product.get("ItemCell")
        if not isinstance(cell, dict):
            if "ComboCell" not in product:
                malformed += 1
            continue  # combo rows carry ComboCell instead
        category = (cell.get("Subcategory") or {}).get("SubcategoryDescription") or None
        if _is_gpu_category(category) is False:
            continue
        title = ((cell.get("Description") or {}).get("Title") or "").strip()
        item = str(cell.get("Item") or "").strip()
        price, regular = _prices(cell)
        if not title or not item or price is None:
            malformed += 1
            continue
        if item in seen:
            continue
        seen.add(item)
        seller = ((cell.get("Seller") or {}).get("SellerName") or "").strip() or None
        instock = cell.get("Instock")
        out.append(Listing(
            retailer=RETAILER_ID,
            title=title,
            price=price,
            url=_url(item, cell.get("ParentItem")),
            sku=item,
            in_stock=bool(instock) if instock is not None else None,
            first_party=seller is None,
            seller=seller,
            condition=_condition(cell, title),
            regular_price=regular,
            category=category,
        ))
    if malformed and not out:
        raise ParseError(f"none of {malformed} rows had a title, item number and price")
    return out


def page_info(payload: str | dict) -> tuple[int, int]:
    """(product rows on the page, TotalItemCount)."""
    state = _state(payload)
    total = state.get("TotalItemCount")
    return len(_products(state)), int(total) if isinstance(total, int) else 0


def _blocked(res: http.FetchResult) -> str | None:
    if res.blocked_reason:
        return res.blocked_reason
    if res.status_code == 200 and not _STATE_RE.search(res.text):
        m = _TITLE_RE.search(res.text[:50_000])
        title = (m.group(1) if m else "").lower()
        for marker in BLOCK_TITLE_TEXT:
            if marker in title:
                return f"bot-check page (title {marker!r})"
    return None


def search(query: str) -> ScrapeResult:
    """Search Newegg.ca in-stock results, reading page 2 when page 1 is full."""
    start = time.monotonic()
    result = ScrapeResult(RETAILER_ID, query, Status.NETWORK_ERROR)
    listings: list[Listing] = []
    seen: set[str] = set()
    notes: list[str] = []
    try:
        for page in range(1, MAX_PAGES + 1):
            params = {"d": query, "N": IN_STOCK_FILTER}
            if page > 1:
                params["page"] = str(page)
            res = http.fetch(SEARCH_URL, params=params)
            if page == 1:
                result.http_status = res.status_code
            failure: tuple[Status, str] | None = None
            reason = _blocked(res)
            if res.error:
                failure = (Status.NETWORK_ERROR, res.error)
            elif reason:
                failure = (Status.BLOCKED, reason)
            elif res.status_code != 200:
                failure = (Status.NETWORK_ERROR, f"HTTP {res.status_code}")
            if failure is None:
                try:
                    rows = parse(res.text, query)
                    n_page, total = page_info(res.text)
                except ParseError as exc:
                    failure = (Status.PARSE_ERROR, str(exc)[:200])
                except Exception as exc:  # an odd row on page 2 must not discard page 1
                    failure = (Status.PARSE_ERROR, f"unexpected {type(exc).__name__}: {exc}"[:200])
            if failure:
                if page == 1:
                    result.status, result.detail = failure
                    return result
                notes.append(f"page {page}: {failure[1]}")
                result.partial = True
                result.blocked_partway = failure[0] is Status.BLOCKED
                break
            result.n_raw += n_page
            for row in rows:
                if row.sku not in seen:
                    seen.add(row.sku)
                    listings.append(row)
            if page == 1:
                notes.append(f"{total} results reported")
            if n_page < PAGE_SIZE or total <= page * PAGE_SIZE:
                break
        result.listings = listings
        if listings:
            result.status = Status.OK
        elif result.blocked_partway:
            result.status = Status.BLOCKED
        else:
            result.status = Status.EMPTY
        if not listings and result.n_raw:
            notes.append(f"{result.n_raw} rows, none in the graphics card category")
        result.detail = "; ".join(notes)
        return result
    except Exception as exc:  # search() must never raise
        result.status = Status.PARSE_ERROR
        result.detail = f"{type(exc).__name__}: {exc}"[:200]
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
