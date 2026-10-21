"""Golden end-to-end runs: captured retailer pages through production code, offline.

Only the transport is fake. curl_cffi answers each request with the page saved in
tests/fixtures for that retailer and search, and with HTTP 404 for a page nobody
captured (a later page of a long result list). Everything after it is the code the
bot runs: http.fetch (retries, block detection), the adapter's search(), the
retailer registry and pricing.scan, pricing's default matcher
(gpu_alerts.matching.match) and DEFAULT_POLICY. The wait between requests to one
site is recorded instead of slept, except in the two tests that run the real wait on a
fake clock: test_requests_to_one_site_wait_their_turn (one request after another) and
test_two_requests_at_once_to_one_site_still_wait_their_turn (two threads at once).

test_v1_substring_matcher_fails_the_golden_cases swaps v1's matcher
(eval/baseline_v1.py) into pricing to show that these expectations tell the two apart.
"""

import asyncio
import importlib.util
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from gpu_alerts import catalog, config, matching, pricing
from gpu_alerts.bot import GPUAlertBot
from gpu_alerts.pricing import DEFAULT_POLICY
from gpu_alerts.retailers import amazon, bestbuy, canadacomputers, http, memoryexpress, newegg, walmart
from gpu_alerts.retailers.base import Status
from gpu_alerts.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
ADAPTERS = (bestbuy, canadacomputers, memoryexpress, newegg, amazon, walmart)
HOSTS = {urlsplit(a.SEARCH_URL).hostname: a.RETAILER_ID for a in ADAPTERS}
# The request parameter that carries the search words (Walmart adds " graphics card").
QUERY_PARAM = {"bestbuy": "query", "canadacomputers": "s", "memoryexpress": "Search",
               "newegg": "d", "amazon": "k", "walmart": "q"}
REAL_WAIT_TURN = http._wait_turn


@dataclass(frozen=True)
class Capture:
    """Pages saved from one retailer's answer to one search, page 1 first."""

    retailer: str
    query: str
    pages: tuple[str, ...]
    availability: str | None = None   # Best Buy's stock lookup for the SKUs on the page


CAPTURES = {c.retailer + " " + c.query: c for c in (
    Capture("bestbuy", "rtx 5080", ("bestbuy_search_rtx5080.json",), "bestbuy_availability.json"),
    Capture("bestbuy", "rtx 5070 ti", ("bestbuy_search_rtx5070ti.json",), "bestbuy_availability_rtx5070ti.json"),
    Capture("canadacomputers", "rtx 5070", ("canadacomputers_search_rtx5070_priceasc.json",)),
    Capture("canadacomputers", "rtx 5080", ("canadacomputers_search_rtx5080.json",)),
    Capture("memoryexpress", "rtx 5070", ("memoryexpress_search_rtx5070.html",)),
    Capture("memoryexpress", "rtx 5090", ("memoryexpress_search_rtx5090.html",)),
    Capture("newegg", "rtx 5080", ("newegg_search_rtx5080_p1.html", "newegg_search_rtx5080_p2.html")),
    Capture("newegg", "rtx 4070", ("newegg_search_refurb_rtx4070.html",)),
    Capture("amazon", "rtx 5080", ("amazon_node_rtx5080.html",)),
    Capture("walmart", "rtx 5070", ("walmart_search_rtx5070.html",)),
    Capture("walmart", "rtx 5080", ("walmart_next_data_rtx5080.json",)),
)}
# Captured from a search without the Graphics Cards node, so it is not what
# amazon.search asks for; the cards are the same markup.
AMAZON_CARDS = Capture("amazon", "rtx 5080", ("amazon_cards_rtx5080.html",))


class FakeSite:
    """curl_cffi's stand-in: serves the captures given, at most one per retailer."""

    def __init__(self, captures, clock=None):
        self.captures = {c.retailer: c for c in captures}
        assert len(self.captures) == len(captures), "one capture per retailer"
        self.clock = clock
        self.requests: list[tuple[str, str | None, float | None]] = []   # (retailer, fixture served, clock)
        self.spacing: dict[str, set[float]] = {}                         # host -> min_interval asked for

    def request(self, method, url, *, params=None, **kwargs):
        rid = HOSTS[urlsplit(url).hostname]
        capture, params, name = self.captures.get(rid), params or {}, None
        if capture is not None:
            if url == bestbuy.AVAILABILITY_URL:
                name = capture.availability
            elif str(params.get(QUERY_PARAM[rid], "")).removesuffix(" graphics card") == capture.query:
                page = int(params.get("page", 1))
                name = capture.pages[page - 1] if page <= len(capture.pages) else None
        self.requests.append((rid, name, self.clock.now if self.clock else None))
        if name is None:
            return SimpleNamespace(status_code=404, text="not captured", url=url)
        return SimpleNamespace(status_code=200, text=(FIXTURES / name).read_text(encoding="utf-8"), url=url)

    def wait_turn(self, url, min_interval):
        self.spacing.setdefault(urlsplit(url).hostname, set()).add(min_interval)


@pytest.fixture
def site(monkeypatch):
    """Install a FakeSite. Request spacing is recorded, not waited for, unless a clock is given."""
    monkeypatch.setattr(config, "DISABLED_RETAILERS", set())

    def install(*captures, clock=None):
        fake = FakeSite(captures, clock)
        monkeypatch.setattr(http.cureq, "request", fake.request)
        if clock is None:
            monkeypatch.setattr(http, "_wait_turn", fake.wait_turn)
        else:
            monkeypatch.setattr(http, "_wait_turn", REAL_WAIT_TURN)
            monkeypatch.setattr(http, "_host_last", {})
            monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
        return fake

    return install


def _best(found):
    return (found[0].retailer, found[0].price, found[0].sku) if found else None


def _golden(capture: Capture, model_id: str, policy=DEFAULT_POLICY):
    """(scan result, eligible offers) for one model from one captured search."""
    (result,) = pricing.scan(capture.query, [capture.retailer])
    return result, pricing.offers(catalog.by_id(model_id), [result], policy)


MARKETPLACE = pricing.Policy(include_marketplace=True)
ANY_SELLER_OR_CONDITION = pricing.Policy(include_open_box=True, include_marketplace=True)

# (capture, model id, policy, eligible offers, best as (retailer, price, sku)). One
# capture can serve two models: RTX 5070 search pages list 5070 Ti cards and the
# other way round. The rows with a wider policy are pages whose every card is a
# marketplace or refurbished listing, where DEFAULT_POLICY alone leaves nothing to match.
GOLDEN = [
    ("bestbuy rtx 5080", "rtx-5080", DEFAULT_POLICY, 4, ("bestbuy", 2269.99, "20164554")),
    ("bestbuy rtx 5070 ti", "rtx-5070-ti", DEFAULT_POLICY, 6, ("bestbuy", 1689.99, "19358759")),
    ("bestbuy rtx 5070 ti", "rtx-5070", DEFAULT_POLICY, 3, ("bestbuy", 1259.99, "19271271")),
    ("canadacomputers rtx 5070", "rtx-5070", DEFAULT_POLICY, 1, ("canadacomputers", 1259.0, "VCGIG00598")),
    ("canadacomputers rtx 5080", "rtx-5080", DEFAULT_POLICY, 5, ("canadacomputers", 2419.99, "VCASU00601")),
    ("memoryexpress rtx 5070", "rtx-5070", DEFAULT_POLICY, 14, ("memoryexpress", 1124.99, "MX00136530")),
    ("memoryexpress rtx 5070", "rtx-5070-ti", DEFAULT_POLICY, 17, ("memoryexpress", 1609.99, "MX00135516")),
    ("memoryexpress rtx 5090", "rtx-5090", DEFAULT_POLICY, 0, None),   # every card sold out
    ("newegg rtx 5080", "rtx-5080", DEFAULT_POLICY, 17, ("newegg", 2369.99, "14-126-744")),
    ("newegg rtx 4070", "rtx-4070", DEFAULT_POLICY, 0, None),
    ("newegg rtx 4070", "rtx-4070", ANY_SELLER_OR_CONDITION, 2, ("newegg", 899.0, "9SIC3DSKNH2618")),
    ("amazon rtx 5080", "rtx-5080", DEFAULT_POLICY, 2, ("amazon", 2229.99, "B0DTPG3B1N")),
    ("walmart rtx 5070", "rtx-5070", DEFAULT_POLICY, 0, None),
    ("walmart rtx 5070", "rtx-5070", MARKETPLACE, 3, ("walmart", 1299.99, "72JN3E0NGUCN")),
    ("walmart rtx 5080", "rtx-5080", DEFAULT_POLICY, 0, None),
    ("walmart rtx 5080", "rtx-5080", MARKETPLACE, 2, ("walmart", 2369.99, "6JJBITOPURKZ")),
]
GOLDEN_IDS = [f"{c}->{m}" + ("" if p == DEFAULT_POLICY else "-wide") for c, m, p, _, _ in GOLDEN]


def test_pricing_uses_the_real_matcher_by_default():
    assert pricing._default_matcher() is matching.match


@pytest.mark.parametrize("capture, model_id, policy, count, best", GOLDEN, ids=GOLDEN_IDS)
def test_captured_search_gives_the_known_best_offer(site, capture, model_id, policy, count, best):
    site(CAPTURES[capture])
    result, found = _golden(CAPTURES[capture], model_id, policy)
    assert result.status is Status.OK
    assert (len(found), _best(found)) == (count, best)


def test_amazon_cards_from_a_search_without_the_node(site):
    site(AMAZON_CARDS)
    result, found = _golden(AMAZON_CARDS, "rtx-5080")
    assert result.status is Status.OK and len(result.listings) == 3   # sponsored accessories dropped
    assert (len(found), _best(found)) == (3, ("amazon", 2229.99, "B0DTPG3B1N"))


@pytest.mark.parametrize("query, model_id, count, best, statuses", [
    # Memory Express has no RTX 5080 capture, so it answers 404 like a failed request.
    ("rtx 5080", "rtx-5080", 28, ("amazon", 2229.99, "B0DTPG3B1N"),
     {"bestbuy": "ok", "canadacomputers": "ok", "memoryexpress": "network_error", "newegg": "ok",
      "amazon": "ok", "walmart": "ok"}),
    ("rtx 5070", "rtx-5070", 15, ("memoryexpress", 1124.99, "MX00136530"),
     {"bestbuy": "network_error", "canadacomputers": "ok", "memoryexpress": "ok", "newegg": "network_error",
      "amazon": "network_error", "walmart": "ok"}),
], ids=["rtx 5080", "rtx 5070"])
def test_one_check_across_every_retailer(site, query, model_id, count, best, statuses):
    site(*(c for c in CAPTURES.values() if c.query == query))
    scan = pricing.scan_models([catalog.by_id(model_id)])[model_id]   # the bot's scan
    assert {r.retailer: r.status.value for r in scan.results} == statuses
    found = scan.offers()
    assert (len(found), _best(found)) == (count, best)


def test_bot_without_a_matcher_pings_the_real_best_offer(site, monkeypatch):
    site(*(c for c in CAPTURES.values() if c.query == "rtx 5070"))
    client = GPUAlertBot(store=Store())   # production scan_models and matcher
    sent = []

    async def deliver(alert, embed):
        sent.append(embed)
        return True

    monkeypatch.setattr(client, "deliver", deliver)
    try:
        client.store.upsert_alert(1, "rtx-5070", 120_000, channel_id=10)
        asyncio.run(client.run_checks())
        (ping,) = sent
        assert ping.title == "RTX 5070 is under your target"
        assert ping.description.startswith("**$1,124.99** at Memory Express")
        assert ping.url == "https://www.memoryexpress.com/Products/MX00136530"
        assert {f.name: f.value for f in ping.fields}["Next best"].startswith("Canada Computers: [$1,259.00]")
        _, stored = client.store.latest_offers("rtx-5070")   # what /prices and /alert read back
        assert len(stored) == 15 and stored[0].price_cents == 112_499
    finally:
        client.store.close()


def _v1_matcher():
    spec = importlib.util.spec_from_file_location("baseline_v1_e2e", EVAL_DIR / "baseline_v1.py")
    v1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v1)
    return lambda title, model: SimpleNamespace(
        ok=v1.title_has_query(title, model.key) and v1.is_valid_gpu(title, None))


def test_v1_substring_matcher_fails_the_golden_cases(site, monkeypatch):
    # The regression these tests exist to catch: pricing wired to a v1-style matcher.
    v1 = _v1_matcher()
    monkeypatch.setattr(pricing, "_default_matcher", lambda: v1)
    wrong = []
    for row_id, (capture, model_id, policy, count, best) in zip(GOLDEN_IDS, GOLDEN):
        site(CAPTURES[capture])
        _, found = _golden(CAPTURES[capture], model_id, policy)
        if (len(found), _best(found)) != (count, best):
            wrong.append(row_id)
    assert wrong == [
        "bestbuy rtx 5070 ti->rtx-5070-ti", "bestbuy rtx 5070 ti->rtx-5070", "canadacomputers rtx 5080->rtx-5080",
        "memoryexpress rtx 5070->rtx-5070", "memoryexpress rtx 5070->rtx-5070-ti", "newegg rtx 5080->rtx-5080",
        "amazon rtx 5080->rtx-5080", "walmart rtx 5080->rtx-5080-wide",
    ]
    # The v1 bug in the README: "TI" inside "EDITION" makes a plain RTX 5070 the best 5070 Ti.
    site(CAPTURES["memoryexpress rtx 5070"])
    _, found = _golden(CAPTURES["memoryexpress rtx 5070"], "rtx-5070-ti")
    assert _best(found) == ("memoryexpress", 1124.99, "MX00136530") and "5070 Ti" not in found[0].title


# Politeness: requests to one site are spaced, 2 s apart and 10 s for Amazon.ca.


def test_each_site_is_asked_for_with_its_documented_spacing(site):
    fake = site(*(c for c in CAPTURES.values() if c.query == "rtx 5080"))
    pricing.scan("rtx 5080")
    assert fake.spacing == {
        "www.bestbuy.ca": {2.0}, "www.canadacomputers.com": {2.0}, "www.memoryexpress.com": {2.0},
        "www.newegg.ca": {2.0}, "www.amazon.ca": {10.0}, "www.walmart.ca": {2.0},
    }
    assert (http.DEFAULT_MIN_INTERVAL, amazon.MIN_INTERVAL) == (2.0, 10.0)


class Clock:
    """time.monotonic and time.sleep for the http module, without waiting."""

    def __init__(self):
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_requests_to_one_site_wait_their_turn(site):
    clock = Clock()
    fake = site(CAPTURES["newegg rtx 5080"], CAPTURES["amazon rtx 5080"], clock=clock)
    newegg.search("rtx 5080")    # two pages
    amazon.search("rtx 5080")    # page 1, then page 2 (404, not captured)
    newegg.search("rtx 5080")
    times = [(rid, t) for rid, _, t in fake.requests]
    assert times == [
        ("newegg", 1_000.0), ("newegg", 1_002.0),       # 2 s between pages
        ("amazon", 1_002.0), ("amazon", 1_012.0),       # another site goes at once; Amazon waits 10 s
        ("newegg", 1_012.0), ("newegg", 1_014.0),       # newegg's last request was 10 s ago
    ]
    assert clock.slept == [2.0, 10.0, 2.0]


class ThreadClocks:
    """time.monotonic and time.sleep where each thread keeps its own fake clock.

    A thread's clock moves only when that thread sleeps. With one shared clock, the
    second thread's 2 s sleep could move the time the first thread reads after its
    turn came, before it records it.
    """

    def __init__(self):
        self._local = threading.local()

    def monotonic(self):
        return getattr(self._local, "now", 1_000.0)

    def sleep(self, seconds):
        self._local.now = self.monotonic() + seconds


class SlowLastRequest(dict):
    """http._host_last that pauses between reading a host's last request time and
    writing the new one, so two threads that do not take turns both read the old time."""

    def get(self, *args):
        value = super().get(*args)
        time.sleep(0.02)    # really sleeps: only http.time is faked
        return value


def test_two_requests_at_once_to_one_site_still_wait_their_turn(monkeypatch):
    # The lock in http._wait_turn is what keeps two scans that reach one site at the
    # same moment 2 s apart. Without it both threads read the same last request time
    # and both go at 1,000.
    clocks = ThreadClocks()
    monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=clocks.monotonic, sleep=clocks.sleep))
    monkeypatch.setattr(http, "_host_last", SlowLastRequest())
    together, starts, errors = threading.Barrier(2), [], []

    def request():
        try:
            together.wait(timeout=5)
            http._wait_turn("https://www.newegg.ca/p/pl", 2.0)
            starts.append(clocks.monotonic())
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=request, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == [] and not any(t.is_alive() for t in threads)
    assert sorted(starts) == [1_000.0, 1_002.0]    # one goes at once, the other 2 s later
