"""Central user configuration, loaded once at startup from XDG config.

On first launch, writes ``$XDG_CONFIG_HOME/voxize/voxize.toml`` containing
every default key commented out. The user uncomments a line to override
the default; anything missing falls back to the value in ``Config``.

Test-only env vars (``VOXIZE_MOCK``, ``VOXIZE_ERROR``, ``VOXIZE_STOP``) are
not part of this config. ``VOXIZE_AUTOCLOSE`` is a user preference that
lives here but can still be overridden by the env var when set.

Usage::

    from voxize import config

    config.load()                     # once, at startup
    config.CONFIG.ducking.apps        # read anywhere, synchronously
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "voxize.toml"


@dataclass(frozen=True)
class DuckingConfig:
    apps: list[str] = field(
        default_factory=lambda: [
            "chrome",
            "chromium",
            "brave",
            "firefox",
        ]
    )
    volume: float = 0.2


@dataclass(frozen=True)
class UIConfig:
    autoclose_seconds: int = 30


@dataclass(frozen=True)
class Config:
    ducking: DuckingConfig = field(default_factory=DuckingConfig)
    ui: UIConfig = field(default_factory=UIConfig)


# Replaced by load(). Modules that read at import time see the in-code
# defaults; after load() runs, CONFIG reflects what's on disk.
CONFIG: Config = Config()


# Template written on first launch. Every line that sets a default is
# commented so uncommenting is a pure "override". Keep in sync with the
# Config dataclass defaults above — there is no automated drift check
# because existing user files are never rewritten.
_TEMPLATE = """\
# Voxize configuration. Uncomment and edit any line to override the default.
# Delete this file to regenerate it with current defaults.

[ducking]
# Apps whose playback is silenced while Voxize is recording. Compared
# case-insensitively as a full match (not substring) against
# application.process.binary, application.process.name, application.name,
# application.id, and node.name from pw-dump. Inspect `pw-dump` while an
# app is playing to find the exact value it advertises.
# apps = ["chrome", "chromium", "brave", "firefox"]

# Target volume during ducking. 0.0 = silent, 1.0 = 100%.
# volume = 0.2

[ui]
# Seconds of focused READY state before the overlay auto-closes.
# 0 disables the timer entirely. Overridden by the VOXIZE_AUTOCLOSE env
# var if it is set.
# autoclose_seconds = 30
"""


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "voxize", _CONFIG_FILENAME)


def _write_template(path: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(_TEMPLATE)
    except OSError:
        logger.debug("config: failed to write template at %s", path, exc_info=True)
        return False
    logger.debug("config: wrote defaults template to %s", path)
    return True


def _parse(data: dict) -> Config:
    """Build a Config from raw TOML data, per-field fallback to defaults."""
    defaults = Config()

    ducking_raw = data.get("ducking")
    if ducking_raw is not None and not isinstance(ducking_raw, dict):
        logger.debug(
            "config: [ducking] is not a table (got %s), using defaults",
            type(ducking_raw).__name__,
        )
        ducking_raw = {}
    elif ducking_raw is None:
        ducking_raw = {}

    ui_raw = data.get("ui")
    if ui_raw is not None and not isinstance(ui_raw, dict):
        logger.debug(
            "config: [ui] is not a table (got %s), using defaults",
            type(ui_raw).__name__,
        )
        ui_raw = {}
    elif ui_raw is None:
        ui_raw = {}

    apps_raw = ducking_raw.get("apps")
    if apps_raw is None:
        apps = defaults.ducking.apps
    elif isinstance(apps_raw, list):
        apps = [str(x) for x in apps_raw]
    else:
        logger.debug(
            "config: ducking.apps is not a list (got %s), using default",
            type(apps_raw).__name__,
        )
        apps = defaults.ducking.apps

    volume = ducking_raw.get("volume")
    if volume is None:
        volume = defaults.ducking.volume
    elif not isinstance(volume, int | float) or isinstance(volume, bool):
        logger.debug(
            "config: ducking.volume is not numeric (got %r), using default",
            volume,
        )
        volume = defaults.ducking.volume

    autoclose = ui_raw.get("autoclose_seconds")
    if autoclose is None:
        autoclose = defaults.ui.autoclose_seconds
    elif not isinstance(autoclose, int) or isinstance(autoclose, bool):
        logger.debug(
            "config: ui.autoclose_seconds is not an int (got %r), using default",
            autoclose,
        )
        autoclose = defaults.ui.autoclose_seconds

    return Config(
        ducking=DuckingConfig(apps=apps, volume=float(volume)),
        ui=UIConfig(autoclose_seconds=int(autoclose)),
    )


def load() -> None:
    """Populate ``CONFIG`` from the XDG config file. Call once at startup.

    Creates the file with commented defaults on first run. Any failure
    (unreadable, unwritable, malformed TOML) falls back silently to the
    in-code defaults — config is never fatal.
    """
    global CONFIG
    path = _config_path()
    logger.debug("config: load() starting, path=%s", path)

    if os.path.exists(path):
        logger.debug("config: existing file found at %s", path)
    else:
        logger.debug("config: file not present, writing defaults template")
        if not _write_template(path):
            logger.debug("config: template write failed, using in-code defaults only")
            CONFIG = Config()
            return

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("config: failed to read/parse %s", path, exc_info=True)
        CONFIG = Config()
        return

    CONFIG = _parse(data)
    logger.debug(
        "config: loaded from %s (ducking.apps=%s, ducking.volume=%s, "
        "ui.autoclose_seconds=%s)",
        path,
        CONFIG.ducking.apps,
        CONFIG.ducking.volume,
        CONFIG.ui.autoclose_seconds,
    )
