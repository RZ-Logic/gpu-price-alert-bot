"""Shared HTTP layer: block evidence across retries and challenge-page detection, offline."""

from pathlib import Path

import pytest

from gpu_alerts import catalog, pricing, retailers
from gpu_alerts.retailers import bestbuy, canadacomputers, http, memoryexpress, walmart
from gpu_alerts.retailers.base import Status

FIXTURES = Path(__file__).parent / "fixtures"
BLOCK_PAGES = {"amazon_503_error_page.html", "amazon_interstitial_bmverify.html", "newegg_403_captcha_head.html"}
URL = "https://www.memoryexpress.com/Category/VideoCards"

# Challenge pages padded past the 50,000-char gate, as inline fonts and styles do.
CLOUDFLARE_CHALLENGE = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
    "<style>" + "body{margin:0}" * 5_000 + "</style></head><body>"
    '<noscript><span id="challenge-error-text">Enable JavaScript and cookies to continue</span></noscript>'
    "<script>(function(){window._cf_chl_opt={cvId: '3',cZone: 'www.memoryexpress.com',cType: 'managed'};"
    "var cpo=document.createElement('script');"
    "cpo.src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=8f00';"
    "document.getElementsByTagName('head')[0].appendChild(cpo);}());</script></body></html>"
)
PERIMETERX_CHALLENGE = (
    '<html lang="en"><head><title>Robot or human?</title>'
    "<style>@font-face{src:url(data:font/woff2;base64," + "A" * 80_000 + ")}</style></head>"
    '<body><div id="px-captcha-wrapper"><div id="px-captcha"></div></div>'
    "<p>Activate and hold the button to confirm that you're human.</p></body></html>"
)


class _Resp:
    def __init__(self, status: int, text: str, url: str):
        self.status_code, self.text, self.url = status, text, url


def _transport(monkeypatch, *outcomes) -> list[str]:
    """Replace curl_cffi with outcomes taken in turn (cycling): a status, (status, text) or an exception."""
    calls: list[str] = []

    def request(method, url, **kw):
        outcome = outcomes[len(calls) % len(outcomes)]
        calls.append(url)
        if isinstance(outcome, BaseException):
            raise outcome
        status, text = outcome if isinstance(outcome, tuple) else (outcome, "")
        return _Resp(status, text, url)

    monkeypatch.setattr(http.cureq, "request", request)
    monkeypatch.setattr(http, "_wait_turn", lambda url, interval: None)
    monkeypatch.setattr(http.time, "sleep", lambda seconds: None)
    return calls


# Block evidence across attempts


@pytest.mark.parametrize("outcomes, reason, n_calls", [
    ((429, ConnectionError("Recv failure: Connection was reset"), ConnectionError("reset")), "HTTP 429", 3),
    ((429, 502, 502), "HTTP 429", 3),
    ((403, TimeoutError("timed out"), TimeoutError("timed out")), "HTTP 403", 3),
    ((429, 404), "HTTP 429", 2),
    (((200, "<title>Just a moment...</title>"), 500, 502), "bot-check page ('just a moment')", 3),
], ids=["429-reset-reset", "429-502-502", "403-timeouts", "429-404", "challenge-500-502"])
def test_block_followed_by_other_failures_is_still_a_block(monkeypatch, outcomes, reason, n_calls):
    calls = _transport(monkeypatch, *outcomes)
    res = http.fetch(URL, attempts=3)
    assert res.blocked_reason == reason
    assert not res.ok and res.error is None
    assert len(calls) == n_calls


def test_block_followed_by_success_is_ok(monkeypatch):
    _transport(monkeypatch, 429, (200, "<html>real page</html>"))
    res = http.fetch(URL, attempts=3)
    assert res.ok and res.status_code == 200 and res.blocked_reason is None


def test_failures_without_a_block_stay_network_errors(monkeypatch):
    _transport(monkeypatch, 502)
    res = http.fetch(URL, attempts=3)
    assert res.status_code == 502 and res.blocked_reason is None
    _transport(monkeypatch, ConnectionError("reset"))
    res = http.fetch(URL, attempts=3)
    assert res.error == "ConnectionError: reset" and res.blocked_reason is None


def test_block_then_reset_trips_the_run_breaker(monkeypatch):
    # Memory Express answers 429 then resets: the run must skip it for later queries.
    calls = _transport(monkeypatch, 429, ConnectionError("reset"), ConnectionError("reset"))
    monkeypatch.setattr(retailers.config, "DISABLED_RETAILERS", set())
    models = [catalog.by_id(i) for i in ("rtx-5080", "rtx-5070")]
    scans = pricing.scan_models(models, ["memoryexpress"])
    first, second = scans["rtx-5080"].results[0], scans["rtx-5070"].results[0]
    assert first.status is Status.BLOCKED and first.detail == "HTTP 429"
    assert second.status is Status.BLOCKED and second.detail.startswith(pricing.SKIPPED_PREFIX)
    assert len(calls) == 3


# Challenge pages of any size


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.iterdir() if p.name not in BLOCK_PAGES))
def test_real_fixtures_are_not_block_pages(name):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    assert http.block_reason(200, text) is None
    # padded past the small-body gate, as a full live page is
    assert http.block_reason(200, text + " " * 60_000) is None


def test_generic_words_in_a_large_real_page_are_not_a_block():
    # A Cloudflare-fronted page always loads challenge-platform's detection script,
    # and a big page can mention "captcha" or "access denied" in its scripts.
    page = (FIXTURES / "memoryexpress_search_rtx5070.html").read_text(encoding="utf-8")
    page += (
        "<script>window.__CF$cv$params={r:'8f00',t:'MTc2'};var a=document.createElement('script');"
        "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>"
        "<script>var captchaEnabled=false; function onAccessDenied(){/* access denied */}</script>"
    )
    assert len(page) > 50_000
    assert http.block_reason(200, page) is None


@pytest.mark.parametrize("page, marker", [
    (CLOUDFLARE_CHALLENGE, "<title>just a moment"),
    (PERIMETERX_CHALLENGE, "<title>robot or human"),
    ((FIXTURES / "amazon_interstitial_bmverify.html").read_text(encoding="utf-8") + " " * 60_000, "bm-verify"),
], ids=["cloudflare", "perimeterx", "akamai-bm-verify"])
def test_large_challenge_pages_are_blocks(page, marker):
    assert len(page) > 50_000
    assert http.block_reason(200, page) == f"bot-check page ({marker!r})"


def test_cloudflare_script_marker_alone_is_a_block():
    page = "<html><head><title>&nbsp;</title></head><body>" + "<p>.</p>" * 10_000 + CLOUDFLARE_CHALLENGE[-400:]
    assert http.block_reason(200, page) == "bot-check page ('_cf_chl_opt')"


@pytest.mark.parametrize("search, query", [
    (memoryexpress.search, "rtx 5090"),
    (canadacomputers.search, "rtx 5070"),
    (bestbuy.search, "rtx 5080"),
], ids=["memoryexpress", "canadacomputers", "bestbuy"])
def test_large_challenge_page_is_blocked_not_a_parse_error(monkeypatch, search, query):
    calls = _transport(monkeypatch, (200, CLOUDFLARE_CHALLENGE))
    res = search(query)
    assert res.status is Status.BLOCKED and res.listings == []
    assert "just a moment" in res.detail
    assert len(calls) == 3  # retried like any block, then reported


def test_walmart_large_robot_page_is_blocked(monkeypatch):
    res = http.FetchResult("https://www.walmart.ca/search", 200, PERIMETERX_CHALLENGE, "chrome124", 5)
    assert walmart._blocked(res) == "bot-check page ('<title>robot or human')"
    monkeypatch.setattr(http, "fetch", lambda url, **kw: res)
    out = walmart.search("rtx 5070")
    assert out.status is Status.BLOCKED and out.listings == []
