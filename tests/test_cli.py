import json
import logging
from pathlib import Path

import pytest

from gpu_alerts import __main__ as cli
from gpu_alerts import bot, config, pricing
from gpu_alerts.retailers.base import Listing, ScrapeResult, Status
from gpu_alerts.store import Store

ROOT = Path(__file__).resolve().parents[1]

# Run by test_a_request_no_test_faked_fails_the_test with tests/conftest.py, in a
# fresh pytest process. The first three make requests that nothing fakes. Every
# retailer points at a reserved .invalid host, so even a broken guard reaches none.
GUARD_PROBES = '''
import curl_cffi.requests
import pytest

from gpu_alerts import __main__ as cli
from gpu_alerts import config, pricing, retailers
from gpu_alerts.retailers import http

REAL = (curl_cffi.requests.request, curl_cffi.requests.Session.request)   # before any fixture ran


@pytest.fixture(autouse=True)
def every_retailer_nowhere(monkeypatch):
    monkeypatch.setattr(config, "DISABLED_RETAILERS", set())
    for rid in retailers.RETAILERS:
        monkeypatch.setattr(retailers.get(rid), "SEARCH_URL", f"https://{rid}.invalid/search")


def test_lookup_without_a_fake():                  # the 2026-10-20 incident: `lookup` with no stub
    cli.main(["lookup", "rtx-5080"])


def test_scan_without_a_fake():                    # pricing.scan catches Exception
    pricing.scan("rtx 5080", ["newegg"])


def test_attempt_swallowed():                      # caught and dropped: still a failure
    try:
        http.fetch("https://newegg.invalid/p/pl", attempts=1)
    except BaseException:
        pass


def test_faked_transport_passes(monkeypatch):
    class Resp:
        status_code, text, url = 404, "", "https://newegg.invalid/p/pl"

    monkeypatch.setattr(http.cureq, "request", lambda *a, **kw: Resp())
    assert http.fetch("https://newegg.invalid/p/pl", attempts=1).status_code == 404


@pytest.mark.live
def test_live_tests_get_the_real_transport():      # checked, never called
    assert (curl_cffi.requests.request, curl_cffi.requests.Session.request) == REAL
'''


def test_a_request_no_test_faked_fails_the_test(pytester, monkeypatch):
    pytester.makeconftest((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makeini("[pytest]\nmarkers =\n    live: hits real retailer sites\n")
    pytester.makepyfile(test_probes=GUARD_PROBES)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))   # the child runs in a temporary directory
    for proxy in ("ALL_PROXY", "HTTPS_PROXY", "https_proxy"):   # and a closed local port as the proxy
        monkeypatch.setenv(proxy, "http://127.0.0.1:9")
    offline = pytester.runpytest_subprocess("-m", "not live", "-p", "no:cacheprovider", timeout=120)
    offline.assert_outcomes(failed=3, passed=1, deselected=1)
    offline.stdout.fnmatch_lines([
        "*UnstubbedRequest: GET https://newegg.invalid/search*",
        "*an offline test tried to reach a retailer: GET https://newegg.invalid/p/pl*",
    ])
    live = pytester.runpytest_subprocess("-m", "live", "-p", "no:cacheprovider", timeout=120)
    live.assert_outcomes(passed=1, deselected=4)


def _ok(retailer, *listings):
    return ScrapeResult(retailer, "q", Status.OK, list(listings), n_raw=len(listings))


def _listings(n):
    return [Listing(retailer="bestbuy", title=f"MSI RTX 5080 16GB #{i}", price=1400.0 + i,
                    url=f"https://example.test/{i}", in_stock=True) for i in range(n)]


@pytest.mark.parametrize("argv", [["lookup", "rtx-5080"], ["health"]])
def test_commands_name_disabled_retailer_ids_that_turn_nothing_off(monkeypatch, caplog, argv):
    monkeypatch.setattr(config, "DISABLED_RETAILERS", {"amazon.ca"})
    monkeypatch.setattr(pricing, "scan", lambda q: [_ok("bestbuy", *_listings(1))])
    with caplog.at_level(logging.WARNING):
        cli.main(argv)
    assert "'amazon.ca'" in caplog.text


def test_bot_start_names_disabled_retailer_ids_that_turn_nothing_off(monkeypatch, caplog):
    started = []
    monkeypatch.setattr(config, "DISABLED_RETAILERS", {"Walmart.ca"})
    monkeypatch.setattr(config, "discord_token", lambda: "token")
    monkeypatch.setattr(bot.GPUAlertBot, "run", lambda self, token, **kw: started.append(token))
    with caplog.at_level(logging.WARNING):
        bot.run()
    assert started == ["token"] and "'Walmart.ca'" in caplog.text


def test_lookup_fails_when_no_retailer_answered(monkeypatch, capsys):
    blocked = [ScrapeResult(r, "q", Status.BLOCKED, http_status=403) for r in ("bestbuy", "newegg")]
    monkeypatch.setattr(pricing, "scan", lambda q: blocked)
    assert cli.main(["lookup", "rtx-5080"]) == 1   # like `health`, so a script can tell
    out = capsys.readouterr().out
    assert "No retailer answered" in out and "No eligible offers" not in out
    # Retailers that answered with nothing eligible are a real market state.
    monkeypatch.setattr(pricing, "scan", lambda q: [ScrapeResult("bestbuy", "q", Status.EMPTY), blocked[1]])
    assert cli.main(["lookup", "rtx-5080"]) == 0
    assert "No eligible offers." in capsys.readouterr().out


@pytest.mark.parametrize("top", ["0", "-2", "ten"])
def test_lookup_top_must_be_a_positive_number(monkeypatch, capsys, top):
    monkeypatch.setattr(pricing, "scan", lambda q: [_ok("bestbuy", *_listings(5))])   # never the network
    with pytest.raises(SystemExit) as exc:
        cli.main(["lookup", "rtx-5080", "--top", top])
    assert exc.value.code == 2 and "--top" in capsys.readouterr().err


def test_lookup_top_limits_the_list(monkeypatch, capsys):
    monkeypatch.setattr(pricing, "scan", lambda q: [_ok("bestbuy", *_listings(5))])
    assert cli.main(["lookup", "rtx-5080", "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "Top 2 of 5 eligible offers:" in out and " 2. $" in out and " 3. $" not in out


def test_import_v1_twice_reports_existing_alerts_as_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "gpu_alerts.sqlite3")
    v1 = tmp_path / "gpu_alerts.json"
    v1.write_text(json.dumps({"111": {"RTX 5080": {"target": 1500, "channel_id": 555}}}), encoding="utf-8")
    assert cli.main(["import-v1", str(v1)]) == 0
    with Store(config.DB_PATH) as s:
        s.upsert_alert(111, "rtx-5080", 130_000, channel_id=777)   # changed in Discord since
    capsys.readouterr()
    assert cli.main(["import-v1", str(v1)]) == 0
    out = capsys.readouterr().out
    assert "skipped  111 RTX 5080: already exists" in out and "0 imported, 0 updated, 1 skipped" in out
    with Store(config.DB_PATH) as s:
        assert s.list_alerts(111)[0].target_cents == 130_000
    assert cli.main(["import-v1", "--force", str(v1)]) == 0
    assert "updated  111 RTX 5080 -> rtx-5080 at $1,500.00" in capsys.readouterr().out
    with Store(config.DB_PATH) as s:
        assert s.list_alerts(111)[0].target_cents == 150_000
