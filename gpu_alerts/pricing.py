"""Run retailer searches and turn raw listings into eligible offers for one model.

Adapters and the matcher are imported at call time, so a broken or missing
module costs one retailer (or one lookup), never the whole bot.
"""

import logging
import math
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalog import GPUModel
from .retailers.base import Listing, ScrapeResult, Status

log = logging.getLogger(__name__)

# (title, model) -> object with a boolean .ok, like matching.match
Matcher = Callable[[str, GPUModel], object]

_NETWORK_EXCEPTIONS = (OSError, TimeoutError, ConnectionError)

# Detail prefix of the placeholder result scan_models makes for a retailer it
# skipped after a block. No request was sent, so store.record_runs drops these.
SKIPPED_PREFIX = "skipped:"
SKIPPED_DETAIL = f"{SKIPPED_PREFIX} blocked earlier in this run"


@dataclass(frozen=True)
class Policy:
    """Which listings count as a buyable offer."""

    include_open_box: bool = False
    include_marketplace: bool = False
    include_unknown_stock: bool = True

    def conditions(self) -> frozenset[str]:
        if self.include_open_box:
            return frozenset({"new", "open_box", "refurbished"})
        return frozenset({"new"})


DEFAULT_POLICY = Policy()
# What gets stored per scan: every matched, possibly buyable listing. Narrower
# policies are applied again when reading history.
RECORD_POLICY = Policy(include_open_box=True, include_marketplace=True, include_unknown_stock=True)


@dataclass
class ModelScan:
    model: GPUModel
    results: list[ScrapeResult]
    scanned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def n_ok(self) -> int:
        return sum(1 for r in self.results if r.ok)

    def offers(self, policy: Policy = DEFAULT_POLICY, matcher: Matcher | None = None) -> list[Listing]:
        return offers(self.model, self.results, policy, matcher)

    def best(self, policy: Policy = DEFAULT_POLICY, matcher: Matcher | None = None) -> Listing | None:
        return best(self.model, self.results, policy, matcher)


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".splitlines()[0] if str(exc) else type(exc).__name__
    return text[:160]


def _error_result(rid: str, query: str, exc: BaseException, elapsed_ms: int) -> ScrapeResult:
    status = Status.NETWORK_ERROR if isinstance(exc, _NETWORK_EXCEPTIONS) else Status.PARSE_ERROR
    name = type(exc).__module__ or ""
    if name.startswith("curl_cffi"):
        status = Status.NETWORK_ERROR
    return ScrapeResult(rid, query, status, detail=_short(exc), elapsed_ms=elapsed_ms)


def _search_one(rid: str, query: str) -> ScrapeResult:
    from . import retailers

    start = time.monotonic()
    try:
        result = retailers.get(rid).search(query)
    except Exception as exc:
        log.warning("%s search for %r raised %s", rid, query, _short(exc))
        return _error_result(rid, query, exc, int((time.monotonic() - start) * 1000))
    if not isinstance(result, ScrapeResult):
        return ScrapeResult(
            rid, query, Status.PARSE_ERROR, detail=f"adapter returned {type(result).__name__}",
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
    return result


def scan(query: str, retailer_ids: Iterable[str] | None = None) -> list[ScrapeResult]:
    """Search every requested retailer at once. One result per id, in order. Never raises."""
    from . import retailers

    ids = list(retailer_ids) if retailer_ids is not None else list(retailers.RETAILERS)
    enabled = set(retailers.enabled_ids())
    results: dict[str, ScrapeResult] = {}
    todo: list[str] = []
    for rid in ids:
        if rid not in retailers.RETAILERS:
            results[rid] = ScrapeResult(rid, query, Status.DISABLED, detail="unknown retailer")
        elif rid not in enabled:
            results[rid] = ScrapeResult(rid, query, Status.DISABLED, detail="disabled in config")
        else:
            todo.append(rid)
    if todo:
        with ThreadPoolExecutor(max_workers=len(todo), thread_name_prefix="scan") as pool:
            futures = {rid: pool.submit(_search_one, rid, query) for rid in todo}
            for rid, fut in futures.items():
                try:
                    results[rid] = fut.result()
                except Exception as exc:  # _search_one already catches; belt and braces
                    results[rid] = _error_result(rid, query, exc, 0)
    return [results[rid] for rid in ids]


def _default_matcher() -> Matcher:
    from . import matching

    return matching.match


def offers(
    model: GPUModel,
    results: Iterable[ScrapeResult],
    policy: Policy = DEFAULT_POLICY,
    matcher: Matcher | None = None,
) -> list[Listing]:
    """Listings that are this model and buyable under the policy, cheapest first."""
    match = matcher or _default_matcher()
    conditions = policy.conditions()
    seen: dict[tuple[str, str], Listing] = {}
    for result in results:
        for listing in result.listings:
            price = listing.price
            if not isinstance(price, (int, float)) or not math.isfinite(price):
                continue
            if price < model.min_price:
                continue
            if listing.condition not in conditions:
                continue
            if listing.first_party is False and not policy.include_marketplace:
                continue
            if listing.in_stock is False:
                continue
            if listing.in_stock is None and not policy.include_unknown_stock:
                continue
            try:
                ok = bool(getattr(match(listing.title, model), "ok", False))
            except Exception as exc:
                log.warning("matcher failed on %r: %s", listing.title[:80], _short(exc))
                continue
            if not ok:
                continue
            key = (listing.retailer, listing.sku or listing.url)
            if key not in seen or price < seen[key].price:
                seen[key] = listing
    return sorted(seen.values(), key=lambda li: (li.price, li.retailer, li.title))


def best(
    model: GPUModel,
    results: Iterable[ScrapeResult],
    policy: Policy = DEFAULT_POLICY,
    matcher: Matcher | None = None,
) -> Listing | None:
    found = offers(model, results, policy, matcher)
    return found[0] if found else None


def best_per_retailer(found: Iterable[Listing]) -> dict[str, Listing]:
    """Cheapest offer per retailer, keeping the cheapest-first order."""
    out: dict[str, Listing] = {}
    for listing in sorted(found, key=lambda li: li.price):
        out.setdefault(listing.retailer, listing)
    return out


def why_none(model: GPUModel, result: ScrapeResult, matcher: Matcher | None = None) -> str:
    """Why a retailer that answered has no eligible offer for the model."""
    match = matcher or _default_matcher()
    found = []
    for li in result.listings:
        try:
            if li.price >= model.min_price and getattr(match(li.title, model), "ok", False):
                found.append(li)
        except Exception:
            continue
    if not found:
        return "no match"
    n = len(found)
    available = [li for li in found if li.in_stock is not False]
    if not available:
        return f"sold out ({n} found)"
    if all(li.first_party is False for li in available):
        return f"marketplace only ({n} found)"
    if all(li.condition != "new" for li in available):
        return f"open box only ({n} found)"
    return f"{n} found, none eligible"


def status_label(
    result: ScrapeResult,
    offer: Listing | None,
    model: GPUModel | None = None,
    matcher: Matcher | None = None,
) -> str:
    """One word or phrase for a retailer's outcome: a price line, or why there is none.

    With the model, a retailer that answered but has nothing eligible says why
    (sold out, marketplace only, open box only) instead of just "no match".
    """
    status = Status(result.status)
    if offer is not None:
        stock = {True: "in stock", False: "out of stock", None: "stock unknown"}[offer.in_stock]
        extra = "" if offer.condition == "new" else f", {offer.condition.replace('_', ' ')}"
        if offer.first_party is False:
            extra += f", sold by {offer.seller or 'a marketplace seller'}"
        return f"${offer.price:,.2f}, {stock}{extra}"
    if status in (Status.OK, Status.EMPTY):
        return why_none(model, result, matcher) if model is not None else "no match"
    if status is Status.BLOCKED:
        return "blocked"
    if status is Status.DISABLED:
        return "disabled"
    return "error"


def _query_key(query: str) -> str:
    return " ".join(query.lower().split())


def scan_models(
    models: Iterable[GPUModel],
    retailer_ids: Iterable[str] | None = None,
    scanner: Callable[..., list[ScrapeResult]] | None = None,
) -> dict[str, ModelScan]:
    """Scan each distinct query once and share the results between models.

    RTX 5060 Ti 8GB and 16GB both search "rtx 5060 ti", so they cost one scan.
    A retailer that answers with a block page is skipped for the rest of the
    run, so one bot check does not turn into a burst of further requests.
    """
    from . import retailers

    run = scanner or scan
    ids = list(retailer_ids) if retailer_ids is not None else list(retailers.RETAILERS)
    blocked: set[str] = set()
    by_query: dict[str, tuple[list[ScrapeResult], datetime]] = {}
    out: dict[str, ModelScan] = {}
    for model in models:
        if model.id in out:
            continue
        key = _query_key(model.query)
        if key not in by_query:
            live = [rid for rid in ids if rid not in blocked]
            fresh = {r.retailer: r for r in run(model.query, live)} if live else {}
            results = [
                fresh[rid] if rid in fresh else ScrapeResult(
                    rid, model.query, Status.BLOCKED, detail=SKIPPED_DETAIL
                )
                for rid in ids if rid in fresh or rid in blocked
            ]
            # Only block evidence trips the breaker: a partial read after a timeout
            # or parse error on a later page does not skip the retailer.
            blocked |= {
                r.retailer for r in results
                if Status(r.status) is Status.BLOCKED or getattr(r, "blocked_partway", False)
            }
            by_query[key] = (results, datetime.now(timezone.utc))
        results, at = by_query[key]
        out[model.id] = ModelScan(model, results, at)
    return out
