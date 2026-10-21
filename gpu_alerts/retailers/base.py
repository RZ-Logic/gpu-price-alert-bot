"""Shared types for retailer adapters.

Every adapter module in this package exposes:

    RETAILER_ID: str        short id, e.g. "bestbuy"
    DISPLAY_NAME: str       shown in Discord, e.g. "Best Buy"
    search(query) -> ScrapeResult
        Fetch live results for a search string (catalog GPUModel.query).
        Never raises: network errors, blocks and parse failures come back
        as a ScrapeResult with the matching Status.
    parse(payload, query) -> list[Listing]
        Pure function from a raw response body to listings, so it can be
        tested offline against tests/fixtures. Raises ParseError when the
        expected structure is missing.

Adapters return every plausible graphics-card listing they see. They may drop
rows using the retailer's own category data, but deciding whether a title is
the requested model is the job of gpu_alerts.matching, not the adapter.
"""

from dataclasses import dataclass, field
from enum import Enum

CONDITIONS = ("new", "open_box", "refurbished", "used")


class Status(str, Enum):
    OK = "ok"                        # fetched and parsed, at least one listing
    EMPTY = "empty"                  # fetched and parsed, the page really had no results
    BLOCKED = "blocked"              # 403/418/429/503, CAPTCHA or bot-check page
    PARSE_ERROR = "parse_error"      # page loaded but the expected structure is missing
    NETWORK_ERROR = "network_error"  # timeout, DNS, connection reset
    DISABLED = "disabled"            # turned off in config


class ParseError(Exception):
    """The response did not have the structure the parser expects."""


@dataclass(frozen=True)
class Listing:
    retailer: str                       # RETAILER_ID
    title: str
    price: float                        # CAD you pay now, before tax
    url: str
    sku: str | None = None
    in_stock: bool | None = None        # None means the source does not say
    first_party: bool | None = None     # sold by the retailer itself; None means unknown
    seller: str | None = None           # marketplace seller name when third-party
    condition: str = "new"              # one of CONDITIONS
    regular_price: float | None = None  # pre-sale or pre-rebate price when shown
    category: str | None = None         # retailer category text, if the source gives one


@dataclass
class ScrapeResult:
    retailer: str
    query: str
    status: Status
    listings: list[Listing] = field(default_factory=list)
    http_status: int | None = None
    n_raw: int = 0                      # rows seen before adapter-level filtering
    detail: str = ""                    # short human-readable reason
    elapsed_ms: int = 0
    # A later page hit a block (bot check) after page 1 succeeded. Status stays
    # OK with the earlier pages' listings, so a run-level breaker should treat
    # this like BLOCKED and skip the retailer for the rest of the run. When no
    # listings were collected at all, the status itself is BLOCKED.
    blocked_partway: bool = False
    # The read is incomplete: a later page failed for any reason (block, network
    # or HTTP error, parse error) after page 1 succeeded, or some rows could not
    # be checked. The listings are real offers, but a deal that was not read may
    # still exist, so this result is no evidence that an earlier deal is gone.
    partial: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (Status.OK, Status.EMPTY)
