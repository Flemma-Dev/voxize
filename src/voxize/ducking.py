"""Per-app volume ducking while recording.

When recording starts, snapshot the volumes of target apps' playback streams
and set them to the configured duck volume. When recording stops, restore
the snapshot.

Uses ``pw-dump`` (to discover playback streams) and ``wpctl`` (to get and set
node volume). Both ship with PipeWire/WirePlumber on modern Linux desktops.
If either tool is missing or fails, ducking is a silent no-op and recording
continues normally.

Matching is **exact** (case-insensitive) against any of the class-like
properties a PipeWire playback node advertises — see ``_CLASS_PROPS``.
"chrome" matches a node whose ``application.process.binary`` is exactly
``chrome``, but does not match ``chromium`` or ``chromium-browser``. The
app list and target volume live in ``voxize.config`` (section ``[ducking]``).
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading

from voxize import config

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT = 2.0

# Properties from pw-dump treated as the node's "window class". We accept
# a match against any of them so users can list either the binary name
# ("chrome"), the friendly name ("Google Chrome"), or the app id
# ("org.mozilla.firefox") without worrying about which field their app
# populates.
_CLASS_PROPS = (
    "application.process.binary",
    "application.process.name",
    "application.name",
    "application.id",
    "node.name",
)


def _list_playback_streams() -> list[tuple[int, list[str]]]:
    """Return ``(node_id, candidate_class_values)`` for every playback stream.

    ``candidate_class_values`` are the lowercased non-empty values of each
    ``_CLASS_PROPS`` entry; match against them with ``==``, not substring.
    """
    try:
        out = subprocess.check_output(
            ["pw-dump"],
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("ducking: pw-dump failed", exc_info=True)
        return []
    try:
        objects = json.loads(out)
    except json.JSONDecodeError:
        logger.debug("ducking: pw-dump JSON decode failed", exc_info=True)
        return []

    streams: list[tuple[int, list[str]]] = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        node_id = obj.get("id")
        if not isinstance(node_id, int):
            continue
        candidates = [str(props[k]).lower() for k in _CLASS_PROPS if props.get(k)]
        streams.append((node_id, candidates))
    logger.debug(
        "ducking: pw-dump returned %d node(s), %d playback stream(s)",
        len(objects),
        len(streams),
    )
    return streams


def _matches(candidates: list[str], apps: list[str]) -> bool:
    targets = {a.lower() for a in apps}
    return any(c in targets for c in candidates)


def _get_volume(node_id: int) -> float | None:
    """Parse ``wpctl get-volume <id>`` output (e.g. ``Volume: 0.34``).

    Full output may include ``[MUTED]`` — we log it verbatim and only
    return the numeric volume. Mute state is intentionally preserved
    across duck/restore, so we don't touch it.
    """
    try:
        out = subprocess.check_output(
            ["wpctl", "get-volume", str(node_id)],
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("ducking: get-volume failed for %d", node_id, exc_info=True)
        return None
    logger.debug("ducking: wpctl get-volume %d -> %r", node_id, out.strip())
    try:
        return float(out.strip().split()[1])
    except (IndexError, ValueError):
        logger.debug("ducking: unexpected get-volume output for %d: %r", node_id, out)
        return None


def _set_volume(node_id: int, volume: float) -> bool:
    logger.debug("ducking: wpctl set-volume %d %.6f", node_id, volume)
    try:
        subprocess.run(
            ["wpctl", "set-volume", str(node_id), f"{volume:.6f}"],
            check=True,
            timeout=_SUBPROCESS_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        logger.debug("ducking: set-volume failed for %d", node_id, exc_info=True)
        return False


class VolumeDucker:
    """Snapshots and restores per-app volumes around recording.

    ``duck()`` and ``restore()`` are fire-and-forget — they return immediately
    and do the subprocess work on a background thread so the GTK main loop
    is never blocked. ``restore_sync()`` runs the restore on the calling
    thread, for shutdown paths where we must complete before the process
    exits and daemon threads are killed.
    """

    def __init__(
        self,
        apps: list[str] | None = None,
        duck_volume: float | None = None,
    ) -> None:
        ducking_cfg = config.CONFIG.ducking
        self._apps = list(apps) if apps is not None else list(ducking_cfg.apps)
        self._duck_volume = (
            duck_volume if duck_volume is not None else ducking_cfg.volume
        )
        self._snapshot: list[tuple[int, float]] = []
        self._active = False
        self._lock = threading.Lock()
        logger.debug(
            "ducking: VolumeDucker init apps=%s duck_volume=%s",
            self._apps,
            self._duck_volume,
        )

    def duck(self) -> None:
        logger.debug("ducking: duck() requested, spawning worker")
        threading.Thread(
            target=self._duck_blocking, daemon=True, name="voxize-duck"
        ).start()

    def restore(self) -> None:
        logger.debug("ducking: restore() requested, spawning worker")
        threading.Thread(
            target=self._restore_blocking, daemon=True, name="voxize-unduck"
        ).start()

    def restore_sync(self) -> None:
        """Synchronous restore. Use on shutdown paths (close, SIGTERM)."""
        logger.debug("ducking: restore_sync() requested, running on caller")
        self._restore_blocking()

    def _duck_blocking(self) -> None:
        logger.debug("ducking: _duck_blocking waiting for lock")
        with self._lock:
            logger.debug(
                "ducking: _duck_blocking acquired lock, active=%s", self._active
            )
            if self._active:
                logger.debug("ducking: already active, skipping duck()")
                return
            if not self._apps:
                logger.debug("ducking: app list is empty, nothing to duck")
                self._active = True
                return
            streams = _list_playback_streams()
            matched = [
                (node_id, candidates)
                for node_id, candidates in streams
                if _matches(candidates, self._apps)
            ]
            if not streams:
                logger.debug("ducking: no playback streams exist right now")
            elif not matched:
                logger.debug(
                    "ducking: none of %d playback stream(s) matched apps=%s",
                    len(streams),
                    self._apps,
                )
            snapshot: list[tuple[int, float]] = []
            for node_id, candidates in matched:
                vol = _get_volume(node_id)
                logger.debug(
                    "ducking: snapshot node=%d vol=%s class_values=%s",
                    node_id,
                    vol,
                    candidates,
                )
                if vol is not None:
                    snapshot.append((node_id, vol))
            # Commit snapshot BEFORE applying ducking — restore() works even
            # if a subsequent set-volume call fails or the thread is killed.
            self._snapshot = snapshot
            self._active = True
            for node_id, original in snapshot:
                ok = _set_volume(node_id, self._duck_volume)
                logger.debug(
                    "ducking: duck node=%d %s -> %s (ok=%s)",
                    node_id,
                    original,
                    self._duck_volume,
                    ok,
                )
            logger.debug(
                "ducking: ducked %d/%d matched streams (of %d playback)",
                len(snapshot),
                len(matched),
                len(streams),
            )

    def _restore_blocking(self) -> None:
        logger.debug("ducking: _restore_blocking waiting for lock")
        with self._lock:
            logger.debug(
                "ducking: _restore_blocking acquired lock, active=%s snapshot_len=%d",
                self._active,
                len(self._snapshot),
            )
            if not self._active:
                logger.debug("ducking: nothing to restore (not active)")
                return
            for node_id, original in self._snapshot:
                ok = _set_volume(node_id, original)
                post = _get_volume(node_id)
                logger.debug(
                    "ducking: restore node=%d -> %s (ok=%s, post=%s)",
                    node_id,
                    original,
                    ok,
                    post,
                )
            logger.debug("ducking: restored %d streams", len(self._snapshot))
            self._snapshot = []
            self._active = False
