"""Best Buy Canada adapter: keyless JSON search API plus the availability API.

The HTML search page answers 403 (Akamai), but the JSON endpoints the site's
own front end calls are open. Search is narrowed to the Graphics Cards
category (id 20397, verified 2026-10-12 through each product's categoryIds),
then the SKUs that survive filtering go to the availability API in one batch
for online stock. A failed availability call fails the whole scan (its own
BLOCKED, NETWORK_ERROR or PARSE_ERROR, no listings), because stock-unknown
rows would let sold-out cards become the best offer. For the same reason a SKU
the answer leaves out, or gives no usable status, counts as sold out and marks
the result partial; an answer with no usable status for any SKU fails the scan.
"""

import json
import re
import time
from dataclasses import replace

from .. import config
from .base import Listing, ParseError, ScrapeResult, Status
from .http import FetchResult, fetch

RETAILER_ID = "bestbuy"
DISPLAY_NAME = "Best Buy"

BASE_URL = "https://www.bestbuy.ca"
SEARCH_URL = f"{BASE_URL}/api/v2/json/search"
AVAILABILITY_URL = f"{BASE_URL}/ecomm-api/availability/products"
GRAPHICS_CARDS_CATEGORY_ID = "20397"
GRAPHICS_CARDS_CATEGORY = "Graphics Cards"
PAGE_SIZE = 100  # the API caps a category search at 100 rows; 48 cut close for "rtx 5070"
AVAILABILITY_BATCH = 100
TIMEOUT = 30.0  # the availability API sometimes takes 8 to 10 s

# shipping.status values. Seen live 2026-10-12: InStock, InStockOnlineOnly,
# SoldOutOnline; the rest are assumed. Anything else falls back to
# shipping.purchasable.
IN_STOCK_STATUSES = {"InStock", "InStockOnlineOnly", "Limited", "LimitedQuantity"}
OUT_OF_STOCK_STATUSES = {
    "SoldOutOnline", "OutOfStock", "NotAvailable", "Preorder", "PreOrder",
    "BackOrder", "Backorder", "ComingSoon",
}

_BUNDLE_TITLE_RE = re.compile(r"\b(card|gpu)\s+(&|\+)\s+\S|\bbundle\b", re.I)
_HEADERS = {"Accept": "application/json", "Accept-Language": "en-CA"}
_CONDITION_PATTERNS = (
    ("open_box", re.compile(r"\bopen[\s-]?box\b", re.I)),
    ("refurbished", re.compile(r"\b(refurbished|renewed)\b", re.I)),
    ("used", re.compile(r"\b(used|pre-?owned)\b", re.I)),
)


def _load_json(payload: str | bytes | dict) -> dict:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    try:
        doc = json.loads(payload.lstrip("﻿"))
    except (TypeError, ValueError) as exc:
        raise ParseError(f"not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ParseError("JSON root is not an object")
    return doc


def _products(doc: dict) -> list[dict]:
    products = doc.get("products")
    if not isinstance(products, list):
        raise ParseError("search JSON has no 'products' list")
    return products


def _price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _condition(title: str) -> str:
    for condition, pattern in _CONDITION_PATTERNS:
        if pattern.search(title):
            return condition
    return "new"


def _is_bundle(sku: str, name: str) -> bool:
    # Bundles carry SKUs like "B0019601" and titles like "Video Card & Corsair ... PSU".
    # A bare " & " is not enough: one marketplace card is "... Gaming & Creative GPU".
    return not sku.isdigit() or bool(_BUNDLE_TITLE_RE.search(name))


def _is_graphics_card(product: dict) -> bool:
    ids = product.get("categoryIds")
    if isinstance(ids, list) and ids:
        return GRAPHICS_CARDS_CATEGORY_ID in {str(i) for i in ids}
    return product.get("categoryName") == GRAPHICS_CARDS_CATEGORY


def parse(payload: str | bytes | dict, query: str) -> list[Listing]:
    """Search JSON to listings. Drops non graphics card rows and bundles; in_stock stays None."""
    products = _products(_load_json(payload))
    listings: list[Listing] = []
    malformed = 0
    for p in products:
        if not isinstance(p, dict):
            malformed += 1
            continue
        sku = str(p.get("sku") or "").strip()
        name = " ".join(str(p.get("name") or "").split())
        path = p.get("productUrl")
        sale = _price(p.get("salePrice"))
        regular = _price(p.get("regularPrice"))
        if not sku or not name or not path or (sale is None and regular is None):
            malformed += 1
            continue
        if not _is_graphics_card(p) or _is_bundle(sku, name):
            continue
        ehf = _price(p.get("ehf")) or 0.0  # environmental fee, charged before tax
        price = round((sale if sale is not None else regular) + ehf, 2)
        marketplace = bool(p.get("isMarketplace"))
        seller_info = p.get("seller") if isinstance(p.get("seller"), dict) else {}
        seller = seller_info.get("name") if marketplace else DISPLAY_NAME
        url = path if str(path).startswith("http") else BASE_URL + str(path)
        listings.append(Listing(
            retailer=RETAILER_ID,
            title=name,
            price=price,
            url=url,
            sku=sku,
            in_stock=False if p.get("isPreorderable") else None,
            first_party=not marketplace,
            seller=seller or None,
            condition=_condition(name),
            regular_price=round(regular + ehf, 2) if regular is not None else None,
            category=p.get("categoryName") or None,
        ))
    if products and malformed == len(products):
        raise ParseError(f"none of {len(products)} products had sku, name, url and price")
    return listings


def _shipping_in_stock(shipping: dict) -> bool | None:
    status = str(shipping.get("status") or "")
    if status in IN_STOCK_STATUSES:
        return True
    if status in OUT_OF_STOCK_STATUSES:
        return False
    purchasable = shipping.get("purchasable")
    return purchasable if isinstance(purchasable, bool) else None


def parse_availability(payload: str | bytes | dict) -> dict[str, bool | None]:
    """Availability JSON to {sku: in_stock}, using online shipping status."""
    doc = _load_json(payload)
    rows = doc.get("availabilities")
    if not isinstance(rows, list):
        raise ParseError("availability JSON has no 'availabilities' list")
    out: dict[str, bool | None] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("sku"):
            continue
        shipping = row.get("shipping")
        out[str(row["sku"])] = _shipping_in_stock(shipping) if isinstance(shipping, dict) else None
    return out


def _fetch_failure(r: FetchResult) -> tuple[Status, str] | None:
    """Status and reason when a fetch did not return a usable 200, else None."""
    if r.error:
        return Status.NETWORK_ERROR, r.error
    if r.blocked_reason:
        return Status.BLOCKED, r.blocked_reason
    if r.status_code != 200:
        return Status.NETWORK_ERROR, f"HTTP {r.status_code}"
    return None


def _availability(skus: list[str]) -> tuple[dict[str, bool | None], tuple[Status, str] | None]:
    """Stock for each SKU, plus (status, reason) for the first batch that failed.

    Any failure fails the whole scan: rows the API did not answer would be stock
    unknown, and pricing counts unknown stock as buyable, so a sold-out card could
    become the best offer.
    """
    stock: dict[str, bool | None] = {}
    for i in range(0, len(skus), AVAILABILITY_BATCH):
        batch = skus[i:i + AVAILABILITY_BATCH]
        r = fetch(
            AVAILABILITY_URL,
            params={
                "accept": "application/vnd.bestbuy.standardproduct.v1+json",
                "accept-language": "en-CA",
                "locations": "",
                "postalCode": config.BESTBUY_POSTAL_CODE,
                "skus": "|".join(batch),
            },
            headers=_HEADERS,
            timeout=TIMEOUT,
        )
        failure = _fetch_failure(r)
        if failure:
            return stock, (failure[0], f"availability: {failure[1]}")
        try:
            stock.update(parse_availability(r.text))
        except ParseError as exc:
            return stock, (Status.PARSE_ERROR, f"availability: {exc}")
    return stock, None


def search(query: str) -> ScrapeResult:
    """Search Graphics Cards for query, then look up online stock. Never raises."""
    start = time.monotonic()
    result = ScrapeResult(RETAILER_ID, query, Status.NETWORK_ERROR)
    try:
        r = fetch(
            SEARCH_URL,
            params={
                "query": query, "lang": "en-CA", "page": 1, "pageSize": PAGE_SIZE,
                "categoryid": GRAPHICS_CARDS_CATEGORY_ID,
            },
            headers=_HEADERS,
            timeout=TIMEOUT,
        )
        result.http_status = r.status_code
        failure = _fetch_failure(r)
        if failure:
            result.status, result.detail = failure
            return result
        try:
            doc = _load_json(r.text)
            result.n_raw = len(_products(doc))
            listings = parse(doc, query)
        except ParseError as exc:
            result.status, result.detail = Status.PARSE_ERROR, str(exc)
            return result

        detail = f"{result.n_raw} rows, {len(listings)} graphics cards"
        if listings:
            stock, failure = _availability([l.sku for l in listings if l.sku])
            if failure:
                result.status = failure[0]
                result.detail = f"{detail}; {failure[1]}"
                return result
            unknown = sum(1 for l in listings if stock.get(l.sku) is None)
            if unknown == len(listings):
                result.status = Status.PARSE_ERROR
                result.detail = f"{detail}; availability: no usable stock status for any of {unknown} SKUs"
                return result
            if unknown:
                # Fail closed: a SKU the API left out, or answered with no usable
                # status, counts as sold out. It may still be the deal, so the read
                # is partial.
                detail += f"; {unknown} stock unknown, counted as sold out"
                result.partial = True
            listings = [
                replace(l, in_stock=stock[l.sku] if stock.get(l.sku) is not None else False)
                for l in listings
            ]
        result.listings = listings
        result.status = Status.OK if listings else Status.EMPTY
        result.detail = detail
        return result
    except Exception as exc:  # never raise out of search()
        result.status = Status.PARSE_ERROR
        result.detail = f"unexpected {type(exc).__name__}: {exc}"[:300]
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
