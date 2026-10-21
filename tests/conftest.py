"""Shared test setup: offline tests cannot reach a retailer.

Every test not marked live runs with curl_cffi's request path (the one
gpu_alerts.retailers.http.fetch uses) replaced by a guard. A request a test
forgot to fake fails that test. The guard raises UnstubbedRequest, a
BaseException, so the `except Exception` in http.fetch, the adapters and
pricing.scan cannot turn it into an errored ScrapeResult. If something still
swallows it, the attempt is on record and the test is reported as failed anyway.
A test that fakes http.fetch, or the transport itself (http.cureq.request), works
as before: its fake answers first.

Live tests (`pytest -m live`) get the real transport.
"""

import curl_cffi.requests
import pytest

pytest_plugins = ("pytester",)   # tests/test_cli.py runs a small suite to prove the guard

_ATTEMPTS = pytest.StashKey[list]()


class UnstubbedRequest(BaseException):
    """A test reached the network. Fake http.fetch or curl_cffi in that test."""


def _blocked(attempts: list, method, url) -> UnstubbedRequest:
    attempts.append(f"{method} {url}")
    return UnstubbedRequest(
        f"{method} {url}: an offline test tried to reach a retailer. Fake http.fetch "
        "(or http.cureq.request) in this test, or mark it live."
    )


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        yield
        return
    attempts = request.node.stash.setdefault(_ATTEMPTS, [])

    def blocked_request(method, url, *args, **kwargs):
        raise _blocked(attempts, method, url)

    def blocked_session_request(self, method, url, *args, **kwargs):
        raise _blocked(attempts, method, url)

    monkeypatch.setattr(curl_cffi.requests, "request", blocked_request)
    for session in (curl_cffi.requests.Session, curl_cffi.requests.AsyncSession):
        monkeypatch.setattr(session, "request", blocked_session_request)
    yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    attempts = item.stash.get(_ATTEMPTS, None)
    if attempts:
        if report.passed:   # the UnstubbedRequest was caught and dropped somewhere
            report.outcome = "failed"
            report.longrepr = (
                "an offline test tried to reach a retailer: " + "; ".join(attempts)
                + ". Fake http.fetch (or http.cureq.request) in this test, or mark it live."
            )
        attempts.clear()
    return report
