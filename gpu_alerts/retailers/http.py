"""Shared HTTP layer: pinned TLS impersonation, retries, politeness, block detection.

curl_cffi's bare "chrome" alias follows the library version (0.16.3 maps it to
chrome150, which Amazon.ca answers with 503), so targets are pinned and tried
in order. Measured 2026-10-12: chrome124 and safari18_0 load Newegg.ca and
Amazon.ca; chrome110, edge101 and chrome150 do not.
"""

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi import requests as cureq

IMPERSONATE_ORDER = ("chrome124", "safari18_0", "chrome120", "chrome136")
BLOCK_STATUSES = {403, 418, 429, 503}
BLOCK_MARKERS = (
    "captcha", "just a moment", "are you a human", "robot check",
    "access denied", "something went wrong", "verify you are human", "cf-chl",
    "robot or human", "px-captcha",                              # Walmart (PerimeterX)
    "bm-verify", "/_sec/verify", "triggerinterstitialchallenge",  # Amazon (Akamai interstitial)
)
# Markers only a challenge page carries, looked for in bodies of any size: a
# challenge can be large too (inline fonts and styles). Generic words stay with
# BLOCK_MARKERS, and "challenge-platform" is left out because Cloudflare loads
# its detection script from that path on ordinary pages as well.
CHALLENGE_MARKERS = (
    "<title>just a moment", "_cf_chl_opt",          # Cloudflare managed challenge
    "bm-verify", "triggerinterstitialchallenge",    # Akamai interstitial
    "<title>robot or human",                        # PerimeterX (Walmart)
)
CHALLENGE_WINDOW = 200_000
RETRY_STATUSES = BLOCK_STATUSES | {500, 502, 504}
DEFAULT_MIN_INTERVAL = 2.0  # seconds between requests to the same host

_host_lock = threading.Lock()
_host_last: dict[str, float] = {}


@dataclass
class FetchResult:
    url: str
    status_code: int | None
    text: str
    impersonate: str | None
    elapsed_ms: int
    error: str | None = None
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and not self.blocked_reason and not self.error


def block_reason(status_code: int | None, text: str, url: str = "") -> str | None:
    """Why a response looks like a block page, or None if it looks real."""
    if status_code in BLOCK_STATUSES:
        return f"HTTP {status_code}"
    if "/blocked" in (urlsplit(url).path or "").lower():
        return "redirected to a block page"
    # Real result pages are large; most bot-check pages are small. Generic words are
    # only looked for in small bodies so one inside a big page's scripts is not a
    # false alarm; challenge-only markers are looked for at any size.
    if status_code == 200 and len(text) < 50_000:
        head = text[:20_000].lower()
        for marker in BLOCK_MARKERS:
            if marker in head:
                return f"bot-check page ({marker!r})"
    marker = challenge_marker(text) if status_code == 200 else None
    return f"bot-check page ({marker!r})" if marker else None


def challenge_marker(text: str) -> str | None:
    """The first CHALLENGE_MARKERS entry in the top CHALLENGE_WINDOW chars of a body, or None."""
    head = text[:CHALLENGE_WINDOW].lower()
    return next((marker for marker in CHALLENGE_MARKERS if marker in head), None)


def _wait_turn(url: str, min_interval: float) -> None:
    host = urlsplit(url).hostname or ""
    while True:
        with _host_lock:
            now = time.monotonic()
            ready_at = _host_last.get(host, 0.0) + min_interval
            if now >= ready_at:
                _host_last[host] = now
                return
            delay = ready_at - now
        time.sleep(delay)


def fetch(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    method: str = "GET",
    json_body: dict | None = None,
    impersonate_order: tuple[str, ...] = IMPERSONATE_ORDER,
    attempts: int = 3,
    timeout: float = 30.0,
    min_interval: float = DEFAULT_MIN_INTERVAL,
) -> FetchResult:
    """GET or POST with impersonation fallback. Never raises.

    Each attempt uses the next impersonation target. Retries only on block
    statuses, transient 5xx and network errors, with a short backoff. Returns the last
    response (or error) when every attempt fails, except that a block seen on any
    attempt is returned instead, so a 429 followed by resets still reads as BLOCKED
    and the run-level breaker skips the retailer.
    """
    last = FetchResult(url, None, "", None, 0, error="no attempt made")
    blocked: FetchResult | None = None
    for i in range(max(1, attempts)):
        target = impersonate_order[i % len(impersonate_order)]
        _wait_turn(url, min_interval)
        start = time.monotonic()
        try:
            resp = cureq.request(
                method, url, params=params, headers=headers, json=json_body,
                impersonate=target, timeout=timeout,
            )
            text = resp.text
            last = FetchResult(
                url=str(resp.url), status_code=resp.status_code, text=text,
                impersonate=target, elapsed_ms=int((time.monotonic() - start) * 1000),
                blocked_reason=block_reason(resp.status_code, text, str(resp.url)),
            )
            if last.status_code == 200 and not last.blocked_reason:
                return last
            if last.blocked_reason and blocked is None:
                blocked = last
            if last.status_code not in RETRY_STATUSES and not last.blocked_reason:
                break  # e.g. 404: retrying with another fingerprint will not help
        except Exception as exc:  # curl_cffi raises its own error types
            last = FetchResult(
                url, None, "", target, int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
        if i < max(1, attempts) - 1:
            time.sleep(1.5 * (i + 1))  # back off only when another attempt follows
    return blocked or last
