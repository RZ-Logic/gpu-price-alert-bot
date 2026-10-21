import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gpu_alerts import config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_env(monkeypatch):
    """Test keys unset, and whatever the loader writes undone afterwards."""
    for key in ("GPU_ALERTS_TEST_KEY", "GPU_ALERTS_TEST_OTHER"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def test_dotenv_key_set_twice_warns_and_the_first_value_stays(tmp_path, clean_env, caplog):
    # .env.example already has the line; a user who appends their own instead of editing it.
    path = tmp_path / ".env"
    path.write_text("GPU_ALERTS_TEST_KEY=\nGPU_ALERTS_TEST_OTHER=1\nGPU_ALERTS_TEST_KEY=amazon,walmart\n",
                    encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gpu_alerts.config"):
        config._load_dotenv(path)
    assert os.environ["GPU_ALERTS_TEST_KEY"] == "" and os.environ["GPU_ALERTS_TEST_OTHER"] == "1"
    assert "line 3 sets GPU_ALERTS_TEST_KEY again; the value on line 1 is used" in caplog.text


def test_dotenv_accepts_an_export_prefix(tmp_path, clean_env):
    path = tmp_path / ".env"
    path.write_text("export GPU_ALERTS_TEST_KEY=amazon\nexport\tGPU_ALERTS_TEST_OTHER = 2\n", encoding="utf-8")
    config._load_dotenv(path)
    assert os.environ["GPU_ALERTS_TEST_KEY"] == "amazon" and os.environ["GPU_ALERTS_TEST_OTHER"] == "2"
    assert "export GPU_ALERTS_TEST_KEY" not in os.environ


def test_unknown_disabled_retailer_ids_are_named(monkeypatch, caplog):
    # The README tables show display names; only the ids turn a retailer off.
    monkeypatch.setattr(config, "DISABLED_RETAILERS", {"amazon.ca", "walmart", "best buy"})
    with caplog.at_level(logging.WARNING, logger="gpu_alerts.config"):
        config.warn_unknown_retailers()
    (record,) = caplog.records
    assert "'amazon.ca', 'best buy'" in record.getMessage() and "'walmart'" not in record.getMessage()
    assert "bestbuy, canadacomputers" in record.getMessage()
    caplog.clear()
    monkeypatch.setattr(config, "DISABLED_RETAILERS", {"amazon"})
    config.warn_unknown_retailers()
    assert caplog.records == []


@pytest.mark.parametrize("value", ["0", "-5"])
def test_check_interval_under_a_minute_warns_and_uses_the_default(monkeypatch, caplog, value):
    monkeypatch.setenv("GPU_ALERTS_TEST_INT", value)
    with caplog.at_level(logging.WARNING, logger="gpu_alerts.config"):
        assert config._int_setting("GPU_ALERTS_TEST_INT", 60, minimum=1) == 60
    assert f"GPU_ALERTS_TEST_INT={value!r} is below 1; using 60" in caplog.text
    # Not a back-to-back scan loop: the bot reads the setting the same way.
    env = {**os.environ, "CHECK_INTERVAL_MINUTES": value}
    out = subprocess.run(
        [sys.executable, "-c", "from gpu_alerts import config; print(config.CHECK_INTERVAL_MINUTES)"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.stdout.strip() == "60" and "below 1" in out.stderr


def test_empty_data_dir_and_postal_code_use_the_defaults():
    env = {**os.environ, "GPU_ALERTS_DATA_DIR": "", "BESTBUY_POSTAL_CODE": " "}
    code = "from gpu_alerts import config; print(config.DB_PATH); print(config.BESTBUY_POSTAL_CODE)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    db_path, postal = out.stdout.split("\n")[:2]
    assert Path(db_path) == ROOT / "data" / "gpu_alerts.sqlite3" and postal == "M5G2C3"


def test_allowed_guild_ids_skip_what_is_not_an_id(monkeypatch, caplog):
    monkeypatch.setenv("GPU_ALERTS_TEST_IDS", " 111, 222 ,,my server ")
    with caplog.at_level(logging.WARNING, logger="gpu_alerts.config"):
        assert config._id_set("GPU_ALERTS_TEST_IDS") == {111, 222}
    assert "'my server'" in caplog.text
    monkeypatch.setenv("GPU_ALERTS_TEST_IDS", "")
    assert config._id_set("GPU_ALERTS_TEST_IDS") == frozenset()


def test_requirements_raise_certifi_past_the_compromised_ca_advisory():
    # curl_cffi checks retailer certificates against certifi's bundle (GHSA-6q48-6c7w-9gw2).
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("certifi")] == ["certifi>=2026.10.19"]
