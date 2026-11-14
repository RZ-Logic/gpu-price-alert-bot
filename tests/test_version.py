"""The package version is written in two places; pip reads one, the bot reports the other."""

import tomllib
from pathlib import Path

import gpu_alerts


def test_pyproject_version_matches_package_version():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    assert version == gpu_alerts.__version__
