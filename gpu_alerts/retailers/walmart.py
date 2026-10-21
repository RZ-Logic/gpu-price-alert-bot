"""Walmart.ca adapter: search page HTML with results in script#__NEXT_DATA__.

Plain clients get a 307 to a bot check, so every request goes through the
impersonating fetch(). Results live under
props.pageProps.initialData.searchResult.itemStacks[*].items. Almost every GPU
row is a marketplace offer (sellerType "EXTERNAL"), often from Newegg Canada.
Sponsored rows carry no catalogProductType, so the category path is the
fallback gate for them.
"""

import json
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from . import http
from .base import Listing, ParseError, ScrapeResult, Status

RETAILER_ID = "walmart"
DISPLAY_NAME = "Walmart"

BASE_URL = "https://www.walmart.ca"
SEARCH_URL = BASE_URL + "/search"
MAX_PAGES = 2
GPU_PRODUCT_TYPE = "Computer Video Cards"
GPU_CATEGORY_ID = "6000199042202"   # Electronics > ... > Computer Components > Graphic Cards & GPU
BLOCK_TEXT = ("robot or human", "px-captcha")
# sellerType to first_party. Every captured GPU row is "EXTERNAL" (a marketplace
# seller); "INTERNAL" marks an item Walmart sells itself. Other values are unknown.
SELLER_TYPES = {"INTERNAL": True, "EXTERNAL": False}

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE,
)


def _next_data(payload: str | dict) -> dict:
    """The __NEXT_DATA__ object from page HTML, its JSON text, or a saved fixture."""
    if isinstance(payload, dict):
        data = payload
    else:
        text = payload.lstrip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise ParseError(f"invalid JSON: {exc}") from exc
        else:
            m = _NEXT_DATA_RE.search(payload)
            raw = m.group(1) if m else None
            if raw is None:
                tag = BeautifulSoup(payload, "html.parser").select_one("script#__NEXT_DATA__")
                raw = tag.string if tag else None
            if not raw:
                raise ParseError("no script#__NEXT_DATA__ in page")
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise ParseError(f"__NEXT_DATA__ is not JSON: {exc}") from exc
    if isinstance(data, dict) and "__NEXT_DATA__" in data:
        data = data["__NEXT_DATA__"]
    return data


def _search_result(payload: str | dict) -> dict:
    data = _next_data(payload)
    try:
        sr = data["props"]["pageProps"]["initialData"]["searchResult"]
    except (KeyError, TypeError) as exc:
        raise ParseError(f"missing props.pageProps.initialData.searchResult ({exc})") from exc
    if not isinstance(sr, dict) or not isinstance(sr.get("itemStacks"), list):
        raise ParseError("searchResult.itemStacks missing or not a list")
    return sr


def _products(sr: dict) -> list[dict]:
    return [
        item
        for stack in sr["itemStacks"] if isinstance(stack, dict)
        for item in (stack.get("items") or [])
        if isinstance(item, dict) and item.get("__typename") == "Product"
    ]


def _primary(sr: dict) -> dict:
    """The first item stack, which holds the search results proper."""
    stacks = [s for s in sr["itemStacks"] if isinstance(s, dict)]
    return stacks[0] if stacks else {}


def _reported(stack: dict):
    """Results a stack reports: its count, else meta.totalItemCount."""
    return stack.get("count") or (stack.get("meta") or {}).get("totalItemCount") or 0


def _category(item: dict) -> tuple[bool | None, str | None]:
    """(is_gpu, category text) from Walmart's own catalog data; is_gpu None when unknown."""
    path = (item.get("category") or {}).get("path") or []
    names = [p.get("name") for p in path if isinstance(p, dict) and p.get("name")]
    leaf = names[-1] if names and names[-1] != "Default" else None
    ptype = item.get("catalogProductType")
    if ptype:
        return ptype == GPU_PRODUCT_TYPE, leaf or ptype
    path_id = str((item.get("category") or {}).get("categoryPathId") or "")
    if leaf:
        is_gpu = path_id.split(":")[-1] == GPU_CATEGORY_ID or "graphic" in leaf.lower()
        return is_gpu, leaf
    return None, None


def _price(item: dict) -> float | None:
    """Item price before shipping (item.price, else the linePrice text)."""
    value = item.get("price")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    line = (item.get("priceInfo") or {}).get("linePrice") or ""
    return _money(line)


def _shipping(item: dict) -> float:
    """Seller shipping fee: priceInfo.shippingFee when numeric, else the shipPrice text; 0 when free."""
    info = item.get("priceInfo") or {}
    fee = info.get("shippingFee")
    if isinstance(fee, (int, float)) and not isinstance(fee, bool):
        return float(fee) if fee > 0 else 0.0
    ship = info.get("shipPrice")
    return (_money(ship) or 0.0) if isinstance(ship, str) else 0.0


def _money(text: str) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    if not m:
        return None
    value = float(m.group(0).replace(",", ""))
    return value if value > 0 else None


def _condition(item: dict) -> str:
    cond = item.get("conditionV2") or item.get("preownedCondition") or ""
    if isinstance(cond, dict):
        cond = " ".join(str(v) for v in cond.values() if isinstance(v, str))
    text = f"{cond} {item.get('name') or ''}".lower()
    if "open box" in text or "open-box" in text:
        return "open_box"
    if "refurb" in text or "restored" in text or "renewed" in text:
        return "refurbished"
    if item.get("isPreowned"):
        return "used"
    return "new"


def _in_stock(item: dict) -> bool | None:
    value = (item.get("availabilityStatusV2") or {}).get("value")
    if value:
        return value == "IN_STOCK" and not item.get("isOutOfStock")
    if item.get("isOutOfStock") is not None:
        return not item["isOutOfStock"]
    return None


def _url(canonical: str) -> str:
    parts = urlsplit(urljoin(BASE_URL, canonical))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse(payload: str | dict, query: str) -> list[Listing]:
    """Listings from a Walmart.ca search page (HTML, __NEXT_DATA__ JSON, or fixture)."""
    sr = _search_result(payload)
    products = _products(sr)
    reported = _reported(_primary(sr))
    if not products and isinstance(reported, int) and reported > 0:
        # Ad placeholders sit among the items, but a real page always has Product items.
        raise ParseError(f"{reported} results reported but no Product items in itemStacks")
    out: list[Listing] = []
    seen: set[str] = set()
    malformed = 0
    for item in products:
        is_gpu, category = _category(item)
        if is_gpu is False:
            continue
        title = (item.get("name") or "").strip()
        price = _price(item)
        shipping = _shipping(item)
        canonical = item.get("canonicalUrl")
        if not title or price is None or not canonical:
            malformed += 1
            continue
        sku = item.get("usItemId") or None
        key = sku or canonical
        if key in seen:
            continue  # sponsored rows can repeat an organic row
        seen.add(key)
        seller = item.get("sellerName") or None
        # Only the known sellerType values decide; anything new falls back to the name.
        first_party = SELLER_TYPES.get(str(item.get("sellerType") or "").upper())
        if first_party is None and seller:
            first_party = seller.lower().startswith("walmart")
        was = _money((item.get("priceInfo") or {}).get("wasPrice") or "")
        # Listing.price is what the buyer pays before tax, so seller shipping is
        # added to both the price and the was-price.
        price = round(price + shipping, 2)
        was = round(was + shipping, 2) if was else None
        out.append(Listing(
            retailer=RETAILER_ID,
            title=title,
            price=price,
            url=_url(canonical),
            sku=sku,
            in_stock=_in_stock(item),
            first_party=first_party,
            seller=seller,
            condition=_condition(item),
            regular_price=was if was and was > price else None,
            category=category,
        ))
    if malformed and not out:
        raise ParseError(f"none of {malformed} graphics card rows had a name, price and url")
    return out


def page_info(payload: str | dict) -> tuple[int, int, bool]:
    """(Product rows on the page, results the primary stack reports, hasMorePages)."""
    sr = _search_result(payload)
    primary = _primary(sr)
    organic = sum(
        1 for i in (primary.get("items") or [])
        if isinstance(i, dict) and i.get("__typename") == "Product" and not i.get("isSponsoredFlag")
    )
    reported = _reported(primary)
    more = bool(sr.get("hasMorePages")) or (isinstance(reported, int) and reported > organic)
    return len(_products(sr)), int(reported or 0), more


def _blocked(res: http.FetchResult) -> str | None:
    if res.blocked_reason:
        return res.blocked_reason
    if "/blocked" in urlsplit(res.url).path:
        return "redirected to /blocked"
    if res.status_code == 200 and len(res.text) < 50_000:
        head = res.text[:20_000].lower()
        for marker in BLOCK_TEXT:
            if marker in head:
                return f"bot-check page ({marker!r})"
    marker = http.challenge_marker(res.text) if res.status_code == 200 else None
    return f"bot-check page ({marker!r})" if marker else None


def search(query: str) -> ScrapeResult:
    """Search Walmart.ca for '<query> graphics card', reading page 2 when page 1 is short."""
    start = time.monotonic()
    result = ScrapeResult(RETAILER_ID, query, Status.NETWORK_ERROR)
    listings: list[Listing] = []
    seen: set[str] = set()
    notes: list[str] = []
    try:
        for page in range(1, MAX_PAGES + 1):
            params = {"q": f"{query} graphics card"}
            if page > 1:
                params["page"] = str(page)
            res = http.fetch(SEARCH_URL, params=params)
            if page == 1:
                result.http_status = res.status_code
            reason = _blocked(res)
            failure: tuple[Status, str] | None = None
            if res.error:
                failure = (Status.NETWORK_ERROR, res.error)
            elif reason:
                failure = (Status.BLOCKED, reason)
            elif res.status_code != 200:
                failure = (Status.NETWORK_ERROR, f"HTTP {res.status_code}")
            if failure is None:
                try:
                    rows = parse(res.text, query)
                    n_page, reported, more = page_info(res.text)
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
                key = row.sku or row.url
                if key not in seen:
                    seen.add(key)
                    listings.append(row)
            if page == 1:
                notes.insert(0, f"{reported} results reported")
            if not more or n_page == 0:
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
        elif not listings:
            notes.append("no results")
        result.detail = "; ".join(notes)
        return result
    except Exception as exc:  # search() must never raise
        result.status = Status.PARSE_ERROR
        result.detail = f"{type(exc).__name__}: {exc}"[:200]
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
