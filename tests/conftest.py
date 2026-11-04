"""Shared test setup: own XDG dirs, a clean VOXIZE_* env, a clean voxize logger."""

import logging
import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    # Session close prunes the state dir — never let a test touch Stan's.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in [k for k in os.environ if k.startswith("VOXIZE_")]:
        monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def _restore_voxize_logger():
    # Frontends attach a SessionLog to logger "voxize" and set it to DEBUG;
    # undo both so no test writes into another's log.
    root = logging.getLogger("voxize")
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
