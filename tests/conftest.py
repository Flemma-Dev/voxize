"""Shared test setup: every test gets its own XDG dirs and a clean VOXIZE_* env."""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    # Session close prunes the state dir — never let a test touch Stan's.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in [k for k in os.environ if k.startswith("VOXIZE_")]:
        monkeypatch.delenv(key)
