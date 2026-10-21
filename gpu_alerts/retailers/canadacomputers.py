"""Canada Computers adapter: the site's AJAX search endpoint returns JSON.

The store's search matches loosely ("rtx 5070" also returns 5070 Ti cards,
laptops and prebuilts; "xt" is not required). Results are sorted by price
ascending, PAGE_SIZE per page (resultsPerPage), and up to MAX_PAGES pages are
read. id_category=914 (Graphics Cards, the parent of 916/917/918) makes the
server drop non graphics card rows from each page, which cuts the response
size; it does not narrow the ranking or the page count, so a page can come back
with no rows while a later one still holds cards. Measured live
2026-10-12: a category URL ignores the search term, the search page has no
category facet, and resultsPerPage=100 works but returns about 21 MB.

Rows are still gated on the store's own category: id_category_default
916/917/918 ("Powered By AMD/Nvidia/Intel") are the graphics card categories.
Everything is sold by Canada Computers itself. Responses are 1 to 5 MB, mostly
per-product JSON and pre-rendered HTML that is ignored.
"""

import json
import re
import time

from . import http
from .base import Listing, ParseError, ScrapeResult, Status

RETAILER_ID = "canadacomputers"
DISPLAY_NAME = "Canada Computers"

SEARCH_URL = "https://www.canadacomputers.com/en/search"
PAGE_SIZE = 24
MAX_PAGES = 4
GRAPHICS_CARDS_CATEGORY_ID = "914"
GPU_CATEGORY_IDS = {916, 917, 918}
GPU_CATEGORY_NAMES = {"powered by amd", "powered by nvidia", "powered by intel"}
CONDITION_MAP = {"new": "new", "refurbished": "refurbished", "used": "used"}


def _load(payload: str | dict) -> dict:
    if isinstance(payload, dict):
        data = payload
    else:
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise ParseError(f"response is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ParseError("no products list in response")
    return data


def _is_gpu(p: dict) -> bool:
    cat_id = p.get("id_category_default")
    if cat_id is not None:
        try:
            return int(cat_id) in GPU_CATEGORY_IDS
        except (TypeError, ValueError):
            pass
    return (p.get("category_name") or "").strip().lower() in GPU_CATEGORY_NAMES


def _condition(p: dict, title: str) -> str:
    if "(open box)" in title.lower():
        return "open_box"
    if (p.get("cc_info") or {}).get("classify") == "OB":
        return "open_box"
    cond = p.get("condition")
    ctype = cond.get("type") if isinstance(cond, dict) else cond
    return CONDITION_MAP.get(str(ctype or "new").lower(), "new")


def _amount(value) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def stock_text(p: dict) -> str:
    return ((p.get("stock_availability") or {}).get("online_availability") or "").strip()


def in_store_only(p: dict) -> bool:
    """Not shippable online but available for pickup in at least one store."""
    sa = p.get("stock_availability") or {}
    return not stock_text(p).startswith("Online - Available") and bool(sa.get("retail"))


def _unreadable_price(p: dict) -> bool:
    """price_amount is missing or not a number. Null and 0 are real unpriced rows (coming soon)."""
    if "price_amount" not in p:
        return True
    value = p["price_amount"]
    if value is None:
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return True
    return False


def parse(payload: str | dict, query: str) -> list[Listing]:
    """Listings from one page of the Canada Computers search JSON."""
    data = _load(payload)
    out: list[Listing] = []
    malformed = 0
    for p in data["products"]:
        if not isinstance(p, dict) or not _is_gpu(p):
            continue
        title = (p.get("name") or "").strip()
        price = _amount(p.get("price_amount"))
        url = p.get("url") or p.get("link") or p.get("canonical_url")
        if not title or not url or _unreadable_price(p):
            malformed += 1
            continue
        if price is None:
            continue
        stock = stock_text(p)
        regular = _amount(p.get("regular_price_amount"))
        category = p.get("category_name") or None
        if in_store_only(p):
            category = f"{category or 'Graphics card'}, in-store pickup only"
        out.append(Listing(
            retailer=RETAILER_ID,
            title=title,
            price=price,
            url=url,
            sku=p.get("reference") or (str(p["id"]) if p.get("id") else None),
            in_stock=stock.startswith("Online - Available") if stock else None,
            first_party=True,
            seller=None,
            condition=_condition(p, title),
            regular_price=regular if regular and regular > price else None,
            category=category,
        ))
    if malformed and not out:
        raise ParseError(f"none of {malformed} graphics card rows had a name, price and url")
    return out


def page_info(payload: str | dict) -> tuple[int, int, int]:
    """(rows on this page, total_items, pages_count) from the pagination block."""
    data = _load(payload)
    pg = data.get("pagination") or {}
    try:
        return len(data["products"]), int(pg.get("total_items") or 0), int(pg.get("pages_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"bad pagination block: {exc}") from exc


def _model_number(query: str) -> tuple[str, str] | None:
    """(label, number) for the query's model, e.g. ("rtx 5070", "5070"); None without one."""
    words = query.lower().split()
    for i, word in enumerate(words):
        if any(c.isdigit() for c in word):
            label = f"{words[i - 1]} {word}" if i else word
            return label, word
    return None


def _has_model_rows(listings: list[Listing], number: str) -> bool:
    pattern = re.compile(rf"(?<![0-9]){re.escape(number)}(?![0-9])", re.IGNORECASE)
    return any(pattern.search(row.title) for row in listings)


def search(query: str) -> ScrapeResult:
    """Search Canada Computers sorted by price, reading up to MAX_PAGES pages of graphics cards."""
    start = time.monotonic()
    result = ScrapeResult(RETAILER_ID, query, Status.NETWORK_ERROR)
    listings: list[Listing] = []
    seen: set[str] = set()
    notes: list[str] = []
    try:
        pages_count = 1
        page = 1
        pages_read = 0
        gpu_rows = 0
        while page <= min(pages_count, MAX_PAGES):
            res = http.fetch(SEARCH_URL, params={
                "s": query, "t": "1", "page": str(page), "ajax": "true",
                "onlyproducts": "1", "order": "product.price.asc",
                "resultsPerPage": str(PAGE_SIZE), "id_category": GRAPHICS_CARDS_CATEGORY_ID,
            })
            if page == 1:
                result.http_status = res.status_code
            failure: tuple[Status, str] | None = None
            if res.error:
                failure = (Status.NETWORK_ERROR, res.error)
            elif res.blocked_reason:
                failure = (Status.BLOCKED, res.blocked_reason)
            elif res.status_code != 200:
                failure = (Status.NETWORK_ERROR, f"HTTP {res.status_code}")
            else:
                try:
                    data = _load(res.text)
                    rows = parse(data, query)
                    n_page, total, pages_count = page_info(data)
                    n_gpu = sum(1 for p in data["products"] if isinstance(p, dict) and _is_gpu(p))
                except ParseError as exc:
                    failure = (Status.PARSE_ERROR, str(exc)[:200])
                except Exception as exc:  # an odd row on a later page must not discard earlier pages
                    failure = (Status.PARSE_ERROR, f"unexpected {type(exc).__name__}: {exc}"[:200])
            if failure:
                if page == 1:
                    result.status, result.detail = failure
                    return result
                notes.append(f"page {page}: {failure[1]}")
                result.partial = True
                result.blocked_partway = failure[0] is Status.BLOCKED
                break
            if page == 1:
                notes.append(f"{total} results over {pages_count} pages")
            pages_read += 1
            result.n_raw += n_page
            gpu_rows += n_gpu
            for row in rows:
                if row.url not in seen:
                    seen.add(row.url)
                    listings.append(row)
            page += 1
        if pages_count > pages_read:
            notes.append(f"read {pages_read} of {pages_count} pages, cheapest first")
            model = _model_number(query)
            if pages_read == MAX_PAGES and listings and model and not _has_model_rows(listings, model[1]):
                notes.append(f"page cap hit with no {model[0]} rows; results may be incomplete")
        pickup = sum(1 for row in listings if row.category and "in-store pickup only" in row.category)
        if pickup:
            notes.append(f"{pickup} in-store pickup only")
        result.listings = listings
        if listings:
            result.status = Status.OK
        elif result.blocked_partway:
            result.status = Status.BLOCKED
        else:
            result.status = Status.EMPTY
            if gpu_rows:
                notes.append(f"{gpu_rows} graphics card rows, none with a price")
            else:
                notes.append("no rows in the graphics card categories" if result.n_raw else "no results")
        result.detail = "; ".join(notes)
        return result
    except Exception as exc:  # search() must never raise
        result.status = Status.PARSE_ERROR
        result.detail = f"{type(exc).__name__}: {exc}"[:200]
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
