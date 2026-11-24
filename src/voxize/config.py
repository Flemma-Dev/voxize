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
    config.CONFIG.ui.autoclose_seconds  # read anywhere, synchronously
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "voxize.toml"


@dataclass(frozen=True)
class UIConfig:
    autoclose_seconds: int = 30


@dataclass(frozen=True)
class BucketStorageConfig:
    """Fully-resolved retention values for one bucket (no inheritance)."""

    max_sessions: int
    max_age_days: int


@dataclass(frozen=True)
class StorageConfig:
    max_sessions: int = 500
    max_age_days: int = 14
    # Per-bucket overrides keyed by bucket name (e.g. "meeting"). Values
    # are already resolved against the top-level defaults at parse time,
    # so for_bucket() can return them directly.
    buckets: dict[str, BucketStorageConfig] = field(default_factory=dict)

    def for_bucket(self, name: str) -> BucketStorageConfig:
        """Resolve retention rules for a bucket. Falls back to defaults."""
        if name in self.buckets:
            return self.buckets[name]
        return BucketStorageConfig(
            max_sessions=self.max_sessions,
            max_age_days=self.max_age_days,
        )


@dataclass(frozen=True)
class Config:
    ui: UIConfig = field(default_factory=UIConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


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

[ui]
# Seconds of focused READY state before the overlay auto-closes.
# 0 disables the timer entirely. Overridden by the VOXIZE_AUTOCLOSE env
# var if it is set.
# autoclose_seconds = 30

[storage]
# Session directories live in a single flat tree but belong to "buckets"
# identified by a trailing -<tag> in the directory name. The dictation
# overlay writes the "default" bucket (no suffix); the meeting recorder
# writes the "meeting" bucket (-meeting suffix). Each app prunes only
# its own bucket at close time, so heavy dictation use won't evict
# meetings, and vice versa. The keys below set the defaults for every
# bucket; per-bucket overrides go in [storage.<bucket>] subtables.

# Maximum number of session directories per bucket. Pruned on app close
# (oldest first, based on the directory name). 0 disables the
# count-based limit.
# max_sessions = 500

# Maximum age in days for a session directory, based on its start time
# parsed from the directory name. Pruned on app close. 0 disables the
# age-based limit.
# max_age_days = 14

# [storage.meeting]
# Per-bucket override for meeting recordings. Missing keys inherit from
# the [storage] defaults above, so you can override just one if you want.
# max_sessions = 50
# max_age_days = 90
"""


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "voxize", _CONFIG_FILENAME)


def _write_template(path: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Atomic: the GUI and the backend both call load() at startup, so a
        # first run can have two writers. A reader must never see half a file.
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(_TEMPLATE)
        os.replace(tmp, path)
    except OSError:
        logger.debug("config: failed to write template at %s", path, exc_info=True)
        return False
    logger.debug("config: wrote defaults template to %s", path)
    return True


def _parse(data: dict) -> Config:
    """Build a Config from raw TOML data, per-field fallback to defaults."""
    defaults = Config()

    ui_raw = data.get("ui")
    if ui_raw is not None and not isinstance(ui_raw, dict):
        logger.debug(
            "config: [ui] is not a table (got %s), using defaults",
            type(ui_raw).__name__,
        )
        ui_raw = {}
    elif ui_raw is None:
        ui_raw = {}

    storage_raw = data.get("storage")
    if storage_raw is not None and not isinstance(storage_raw, dict):
        logger.debug(
            "config: [storage] is not a table (got %s), using defaults",
            type(storage_raw).__name__,
        )
        storage_raw = {}
    elif storage_raw is None:
        storage_raw = {}

    autoclose = ui_raw.get("autoclose_seconds")
    if autoclose is None:
        autoclose = defaults.ui.autoclose_seconds
    elif not isinstance(autoclose, int) or isinstance(autoclose, bool):
        logger.debug(
            "config: ui.autoclose_seconds is not an int (got %r), using default",
            autoclose,
        )
        autoclose = defaults.ui.autoclose_seconds

    max_sessions = _parse_nonneg_int(
        storage_raw,
        "max_sessions",
        defaults.storage.max_sessions,
        path="storage.max_sessions",
    )
    max_age_days = _parse_nonneg_int(
        storage_raw,
        "max_age_days",
        defaults.storage.max_age_days,
        path="storage.max_age_days",
    )

    # Bucket overrides — every dict-valued child of [storage] is a
    # subtable [storage.<bucket>]. Scalar keys are top-level retention
    # values (already parsed above). Missing keys per-bucket inherit from
    # the top-level defaults so a partial override is a true override.
    buckets: dict[str, BucketStorageConfig] = {}
    for bucket_name, raw in storage_raw.items():
        if not isinstance(raw, dict):
            continue
        buckets[bucket_name] = BucketStorageConfig(
            max_sessions=_parse_nonneg_int(
                raw,
                "max_sessions",
                max_sessions,
                path=f"storage.{bucket_name}.max_sessions",
            ),
            max_age_days=_parse_nonneg_int(
                raw,
                "max_age_days",
                max_age_days,
                path=f"storage.{bucket_name}.max_age_days",
            ),
        )

    return Config(
        ui=UIConfig(autoclose_seconds=int(autoclose)),
        storage=StorageConfig(
            max_sessions=max_sessions,
            max_age_days=max_age_days,
            buckets=buckets,
        ),
    )


def _parse_nonneg_int(raw: dict, key: str, default: int, *, path: str) -> int:
    """Parse a non-negative int from a TOML table; fall back on type errors."""
    val = raw.get(key)
    if val is None:
        return default
    if not isinstance(val, int) or isinstance(val, bool):
        logger.debug("config: %s is not an int (got %r), using default", path, val)
        return default
    return max(0, int(val))


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
        "config: loaded from %s (ui.autoclose_seconds=%s, "
        "storage.max_sessions=%s, storage.max_age_days=%s, storage.buckets=%s)",
        path,
        CONFIG.ui.autoclose_seconds,
        CONFIG.storage.max_sessions,
        CONFIG.storage.max_age_days,
        sorted(CONFIG.storage.buckets.keys()),
    )
