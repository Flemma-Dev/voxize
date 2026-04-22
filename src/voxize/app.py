"""Voxize GTK4 application — wires state machine, UI, and providers.

Three-phase transcription:
  1. Live preview  — gpt-4o-mini-transcribe via realtime WS (throwaway)
  2. Batch         — gpt-4o-transcribe via POST /audio/transcriptions
  3. Cleanup       — gpt-5.4-nano text reformatting

Environment variables:
    VOXIZE_MOCK=1          Use mock transcription (no mic, no API)
    VOXIZE_ERROR=<ms>      Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>       Auto-stop recording after <ms>
    VOXIZE_AUTOCLOSE=<s>   Overrides [ui] autoclose_seconds from voxize.toml
                           (0 disables the auto-close timer)
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

# gi.require_version() above is executable code; all subsequent imports trigger E402.
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from voxize import clipboard, config  # noqa: E402
from voxize._trace import trace as _trace  # noqa: E402
from voxize.audio import (  # noqa: E402
    CHANNELS,
    SAMPLE_RATE,
    WAV_HEADER_SIZE,
    AudioCapture,
)
from voxize.batch import BatchTranscription  # noqa: E402
from voxize.checks import get_api_key  # noqa: E402
from voxize.cleanup import Cleanup  # noqa: E402
from voxize.ducking import VolumeDucker  # noqa: E402
from voxize.lock import MicLock, MicLockError  # noqa: E402
from voxize.mock import MockCleanup, MockTranscription  # noqa: E402
from voxize.prompt import PromptSource, detect_prompt  # noqa: E402
from voxize.recover import write_recover_script  # noqa: E402
from voxize.state import State, StateMachine  # noqa: E402
from voxize.storage import create_session_dir, prune_sessions  # noqa: E402
from voxize.transcribe import RealtimeTranscription  # noqa: E402
from voxize.ui import OverlayWindow  # noqa: E402

if TYPE_CHECKING:
    from openai import OpenAI

logger = logging.getLogger(__name__)

_MOCK = bool(os.environ.get("VOXIZE_MOCK"))

# Connection pool keepalive for the shared OpenAI client. Client-side
# upper bound; the server may idle-close sooner, which is why we also
# fire periodic warmup pings while RECORDING.
_KEEPALIVE_SECONDS = 900  # 15 minutes
_WARMUP_INTERVAL_SECONDS = 45  # safely under typical LB idle timeouts (~60-120s)

# WARMING → RECORDING thresholds. The mic stream is open but BT headsets
# switch A2DP → HFP on capture-open and may deliver silent zero-chunks
# for 200-1500ms before real audio flows. Once the level meter crosses
# the threshold for _WARMING_REQUIRED_LOUD_CHUNKS consecutive 40ms
# blocks (≈80ms of audio), we call it "really recording" and flip the
# UI. _WARMING_TIMEOUT_MS is the hard fallback — a very quiet room may
# never cross the threshold, and we still want to show RECORDING.
_WARMING_RMS_THRESHOLD_DBFS = -70.0
_WARMING_REQUIRED_LOUD_CHUNKS = 2
_WARMING_TIMEOUT_MS = 2000
_WARMING_POLL_MS = 40  # same cadence as the level-meter UI tick


def _autoclose_seconds() -> int:
    """Resolve autoclose: VOXIZE_AUTOCLOSE overrides ``[ui] autoclose_seconds``."""
    env = os.environ.get("VOXIZE_AUTOCLOSE")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            logger.debug("VOXIZE_AUTOCLOSE=%r is not an int, ignoring", env)
    return config.CONFIG.ui.autoclose_seconds


class VoxizeApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="dev.flemma.Voxize",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._machine: StateMachine | None = None
        self._ui: OverlayWindow | None = None

        # Real providers
        self._lock = None
        self._session_dir: str | None = None
        self._api_key: str | None = None
        self._prompts: list[PromptSource] = []
        self._audio = None
        self._transcription = None
        self._batch = None
        self._cleanup = None

        # Shared OpenAI client for batch + cleanup. Single httpx connection
        # pool so the TLS/HTTP1.1 keep-alive socket established during one
        # phase can be reused by the next. Built lazily in the bootstrap
        # thread so the openai SDK import does not block mic-open.
        self._client: OpenAI | None = None
        self._warmup_timer_id: int | None = None

        # Bootstrap coordination: _initialize opens the mic immediately
        # then kicks off a background thread to import openai, build the
        # client, create RealtimeTranscription, and start the WS. Stop/
        # cancel paths must wait for this Event before using self._client,
        # and set _bootstrap_cancelled to short-circuit the thread.
        self._bootstrap_done = threading.Event()
        self._bootstrap_cancelled = False

        # WARMING → RECORDING detector: polls the level meter until we
        # see real audio (or a timeout fires).
        self._warming_timer_id: int | None = None
        self._warming_start_time = 0.0
        self._warming_consecutive_loud = 0

        # Mock transcription (only in mock mode)
        self._mock_transcription: MockTranscription | None = None

        # Batch transcript (passed from TRANSCRIBING to CLEANING)
        self._batch_transcript: str = ""

        # Session cost tracking (three phases)
        self._live_usage: dict[str, int] | None = None
        self._batch_usage: dict[str, int] | None = None
        self._cleanup_usage: dict[str, int] | None = None

        # Prompt detection (set in do_activate before window present)

        # Per-app volume ducking while recording. Disabled in mock mode so
        # UI tests don't silence the user's browser tabs. Edit DUCKED_APPS
        # in voxize.ducking to change which apps are targeted.
        self._ducker = VolumeDucker(apps=[] if _MOCK else None)

        # Session-level file log handler
        self._log_handler = None

    def do_activate(self) -> None:
        _trace("do_activate entry")
        # Detect transcription prompt BEFORE presenting the window — once our
        # window takes focus, the D-Bus focused-window query would return our
        # own PID instead of the window the user was working in.
        if not _MOCK:
            self._prompts = detect_prompt()
            _trace(f"detect_prompt done (prompts={len(self._prompts)})")
            logger.debug("do_activate: prompts=%d", len(self._prompts))

        # Load CSS theme
        css = Gtk.CssProvider()
        css.load_from_string((Path(__file__).parent / "style.css").read_text())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _trace("CSS loaded")

        # Window
        win = Gtk.ApplicationWindow(application=self)
        win.set_resizable(False)
        win.set_default_size(420, -1)
        win.connect("close-request", self._on_close_request)

        # Escape key
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key, win)
        win.add_controller(ctrl)

        # State machine + UI
        self._machine = StateMachine()
        self._machine.on_change(self._on_state_change)
        self._ui = OverlayWindow(
            win, self._machine, autoclose_seconds=_autoclose_seconds()
        )
        _trace("OverlayWindow constructed")

        win.present()
        _trace("win.present() called")
        self._ui.setup_max_height()

        if self._prompts:
            self._ui.show_prompt_context(self._prompts)

        # Signal handlers — finalize WAV header before exit so the file is
        # a valid WAV (not just raw PCM with a placeholder header).
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)

        if _MOCK:
            GLib.idle_add(self._initialize_mock)
        else:
            GLib.idle_add(self._initialize)
        _trace("do_activate returning (scheduled _initialize on idle)")

    # ── Real initialization (fast startup) ──

    def _initialize(self) -> bool:
        """Acquire mic lock, start audio, enter RECORDING, then bootstrap WS.

        Fast path: only the work needed to get PCM flowing into the WAV
        runs synchronously on the GTK thread. The OpenAI client and the
        live-preview WS are built in a background "voxize-bootstrap"
        thread so the ~300ms openai SDK import does not block mic-open.

        Batch transcription on stop waits on ``self._bootstrap_done``,
        so the authoritative transcript path is never starved.
        """
        _trace("_initialize idle fired")
        # Guard: user may have cancelled during INITIALIZING before this idle fires
        if not self._machine or self._machine.state != State.INITIALIZING:
            logger.debug(
                "_initialize: skipped, state=%s",
                self._machine.state.name if self._machine else None,
            )
            return False

        logger.debug("_initialize: acquiring lock")
        try:
            self._lock = MicLock()
            self._lock.acquire()
        except MicLockError as e:
            self._machine.transition(State.ERROR, error=str(e))
            return False
        except Exception as e:
            self._machine.transition(State.ERROR, error=f"Lock failed: {e}")
            return False
        logger.debug("_initialize: lock acquired")
        _trace("mic lock acquired")

        try:
            self._session_dir = create_session_dir()
            logger.debug("_initialize: session_dir=%s", self._session_dir)
            self._api_key = get_api_key("openai")
            logger.debug("_initialize: api_key retrieved")
        except Exception as e:
            self._release_lock()
            self._machine.transition(State.ERROR, error=str(e))
            return False
        _trace("session_dir created, api_key retrieved")

        # Session folder button + recovery script
        self._ui.set_session_dir(self._session_dir)
        write_recover_script(self._session_dir)

        # Set up session-level file logging
        fh = logging.FileHandler(os.path.join(self._session_dir, "debug.log"))
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(threadName)-14s %(name)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logging.getLogger("voxize").addHandler(fh)
        logging.getLogger("voxize").setLevel(logging.DEBUG)
        # Merge httpx/httpcore/openai logs into the session debug.log so we
        # can see TLS handshakes, connection opens/closes, and connection
        # reuse events alongside our own phase_timing lines. httpcore.http11
        # and httpcore.http2 are per-frame noisy — keep them at INFO.
        for name in ("openai", "httpx", "httpcore"):
            lg = logging.getLogger(name)
            lg.addHandler(fh)
            lg.setLevel(logging.DEBUG)
        logging.getLogger("httpcore.http11").setLevel(logging.INFO)
        logging.getLogger("httpcore.http2").setLevel(logging.INFO)
        self._log_handler = fh
        logger.debug("_initialize: file logging active")
        logger.debug("_initialize: prompts=%d", len(self._prompts))

        # Open the mic stream with a no-op chunk sink — the bootstrap
        # thread swaps in ``RealtimeTranscription.send_audio`` once the
        # WS is ready. Chunks that arrive before the swap are still
        # written to the WAV, so batch transcription is never starved.
        self._audio = AudioCapture(self._session_dir)
        logger.debug("_initialize: starting audio capture")
        try:
            self._audio.start()
        except Exception as e:
            self._release_lock()
            self._machine.transition(State.ERROR, error=f"Microphone: {e}")
            return False
        _trace("audio.start() returned (mic capturing)")

        self._ui.set_level_meter(self._audio.meter)

        logger.debug("_initialize: transitioning to WARMING")
        self._machine.transition(State.WARMING)
        _trace("WARMING state entered")

        # Kick off the heavy setup in a background thread: openai SDK
        # import, client construction, RealtimeTranscription + WS, and
        # warmup scheduling. Audio capture continues uninterrupted.
        self._bootstrap_done.clear()
        self._bootstrap_cancelled = False
        threading.Thread(
            target=self._bootstrap_providers,
            daemon=True,
            name="voxize-bootstrap",
        ).start()
        _trace("bootstrap thread spawned")
        return False  # one-shot idle

    # ── WARMING → RECORDING detector ──

    def _start_warming_detector(self) -> None:
        """Watch the level meter and flip to RECORDING once audio is flowing."""
        self._stop_warming_detector()
        self._warming_start_time = time.monotonic()
        self._warming_consecutive_loud = 0
        self._warming_timer_id = GLib.timeout_add(_WARMING_POLL_MS, self._check_warming)
        logger.debug(
            "warming: detector armed threshold=%.1f dBFS timeout=%dms",
            _WARMING_RMS_THRESHOLD_DBFS,
            _WARMING_TIMEOUT_MS,
        )

    def _stop_warming_detector(self) -> None:
        if self._warming_timer_id is not None:
            GLib.source_remove(self._warming_timer_id)
            self._warming_timer_id = None

    def _mock_warming_to_recording(self) -> bool:
        """GTK idle: skip the audio-detection wait in mock mode."""
        if self._machine and self._machine.state == State.WARMING:
            self._machine.transition(State.RECORDING)
        return False

    def _check_warming(self) -> bool:
        """GLib timer: poll the level meter, transition when audio flows."""
        if not self._machine or self._machine.state != State.WARMING:
            self._warming_timer_id = None
            return False
        elapsed_ms = (time.monotonic() - self._warming_start_time) * 1000
        audio = self._audio
        level = audio.meter.level_dbfs if audio else -96.0
        if level > _WARMING_RMS_THRESHOLD_DBFS:
            self._warming_consecutive_loud += 1
            if self._warming_consecutive_loud >= _WARMING_REQUIRED_LOUD_CHUNKS:
                logger.debug(
                    "warming: audio detected level=%.1f dBFS after %dms",
                    level,
                    int(elapsed_ms),
                )
                _trace(f"warming: audio detected at {int(elapsed_ms)}ms")
                self._warming_timer_id = None
                self._machine.transition(State.RECORDING)
                return False
        else:
            self._warming_consecutive_loud = 0
        if elapsed_ms >= _WARMING_TIMEOUT_MS:
            logger.debug(
                "warming: timeout after %dms, transitioning anyway (last level=%.1f dBFS)",
                int(elapsed_ms),
                level,
            )
            _trace(f"warming: timeout at {int(elapsed_ms)}ms")
            self._warming_timer_id = None
            self._machine.transition(State.RECORDING)
            return False
        return True

    def _bootstrap_providers(self) -> None:
        """Background thread: import openai, build client, start WS.

        Runs after ``_initialize`` has entered RECORDING so the user's
        audio is already being captured while the openai SDK loads
        (~300ms warm, more on cold cache). Respects
        ``self._bootstrap_cancelled`` at checkpoints so Escape-during-
        startup doesn't leave a half-wired client behind.
        """
        _trace("bootstrap: entry")
        if self._bootstrap_cancelled:
            _trace("bootstrap: cancelled before client build")
            self._bootstrap_done.set()
            return

        client: OpenAI | None = None
        try:
            import httpx
            from openai import OpenAI as _OpenAI

            from voxize import openai_patches

            _trace("bootstrap: openai + httpx imported")
            openai_patches.install()
            _trace("bootstrap: openai_patches installed")

            http_client = httpx.Client(
                limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_SECONDS),
            )
            client = _OpenAI(api_key=self._api_key, http_client=http_client)
            logger.debug(
                "bootstrap: openai client ready keepalive_expiry=%ds",
                _KEEPALIVE_SECONDS,
            )
            _trace("bootstrap: OpenAI client built")
        except Exception:
            logger.exception("bootstrap: failed to build OpenAI client")
            self._bootstrap_done.set()
            return

        if self._bootstrap_cancelled:
            _trace("bootstrap: cancelled after client build")
            try:
                client.close()
            except Exception:
                logger.debug("bootstrap: client close failed", exc_info=True)
            self._bootstrap_done.set()
            return

        # Publish the client so the batch/cleanup/stop paths can see it.
        self._client = client

        # Create RealtimeTranscription, wire it to the audio callback,
        # and start the WS. Audio chunks already in flight are routed
        # once ``set_on_chunk`` swaps the forwarder; earlier chunks go
        # to the WAV only, which is what the live preview lost anyway
        # during the current BT/VAD startup burst.
        transcription: RealtimeTranscription | None = None
        try:
            transcription = RealtimeTranscription(
                self._api_key,
                self._session_dir,
            )
            audio = self._audio
            if audio is not None and not self._bootstrap_cancelled:
                audio.set_on_chunk(transcription.send_audio)
                logger.debug("bootstrap: audio callback swapped to WS")
            if self._bootstrap_cancelled:
                _trace("bootstrap: cancelled before WS start")
                self._bootstrap_done.set()
                return
            self._transcription = transcription
            transcription.start(
                on_delta=self._ui.append_text,
                on_error=self._on_ws_error,
                on_ready=self._on_ws_ready,
                on_speech=self._ui.on_speech,
            )
            _trace("bootstrap: transcription.start() invoked")
        except Exception:
            logger.exception("bootstrap: failed to start transcription WS")

        # Schedule warmup on the GTK thread if we're still RECORDING.
        GLib.idle_add(self._kick_warmup_after_bootstrap)
        self._bootstrap_done.set()
        _trace("bootstrap: complete")

    def _kick_warmup_after_bootstrap(self) -> bool:
        """GTK thread: start warmup pings once bootstrap has produced a client."""
        if self._machine and self._machine.state == State.RECORDING:
            self._start_warmup()
        return False

    def _on_ws_ready(self) -> None:
        """WS session configured — just bookkeeping, not a state gate."""
        logger.debug("_on_ws_ready: WS session configured")

    # ── Mock initialization ──

    def _initialize_mock(self) -> bool:
        """Mock mode: skip mic/lock/WebSocket, use mock transcription."""
        logger.debug("_initialize_mock: starting mock mode")
        self._machine.transition(State.RECORDING)

        # Schedule simulated error if requested
        error_ms = os.environ.get("VOXIZE_ERROR")
        if error_ms:
            logger.debug("_initialize_mock: scheduling error after %sms", error_ms)
            GLib.timeout_add(int(error_ms), self._mock_error)

        # Schedule auto-stop if requested
        stop_ms = os.environ.get("VOXIZE_STOP")
        if stop_ms:
            logger.debug("_initialize_mock: scheduling auto-stop after %sms", stop_ms)
            GLib.timeout_add(int(stop_ms), self._mock_stop)

        return False

    def _mock_error(self) -> bool:
        """Fire a simulated WebSocket error."""
        logger.debug(
            "_mock_error: state=%s", self._machine.state if self._machine else None
        )
        if self._machine and self._machine.state == State.RECORDING:
            self._on_ws_error("Simulated: WebSocket connection lost")
        return False

    def _mock_stop(self) -> bool:
        """Fire a simulated stop."""
        logger.debug(
            "_mock_stop: state=%s", self._machine.state if self._machine else None
        )
        if self._machine and self._machine.state == State.RECORDING:
            self._machine.transition(State.TRANSCRIBING)
        return False

    # ── WebSocket callbacks ──

    def _on_ws_error(self, message: str) -> None:
        """WebSocket error — non-fatal during RECORDING.

        Audio capture continues (WAV is the safety net). Error banner is
        shown. The user can still speak, stop, and get batch transcription.

        During INITIALIZING (before audio started): fatal, transition to ERROR.
        """
        current = self._machine.state if self._machine else None
        logger.debug("_on_ws_error: state=%s msg=%s", current, message)
        if self._machine and self._machine.state == State.RECORDING:
            # Non-fatal — keep audio, show banner
            if self._transcription:
                self._transcription.cancel()
                self._transcription = None
            self._ui.show_error_banner(message)
        elif self._machine and self._machine.state == State.INITIALIZING:
            self._teardown_async()
            self._machine.transition(State.ERROR, error=message)

    # ── State orchestration ──

    def _on_state_change(self, machine: StateMachine, old: State, new: State) -> None:
        logger.debug("_on_state_change: %s -> %s", old.name, new.name)

        # Per-app volume ducking — orthogonal to the phase-specific logic
        # below. Duck from the moment the mic opens (WARMING) so that the
        # BT A2DP→HFP transition isn't fighting music; restore once we
        # leave the capture phase for good.
        if new == State.WARMING:
            self._ducker.duck()
        elif old in (State.WARMING, State.RECORDING) and new not in (
            State.WARMING,
            State.RECORDING,
        ):
            self._ducker.restore()

        if new == State.WARMING:
            self._live_usage = None
            self._batch_usage = None
            self._cleanup_usage = None
            self._batch_transcript = ""
            if _MOCK:
                # Mock flow doesn't need real audio — transition straight
                # to RECORDING so existing mock tests behave identically.
                GLib.idle_add(self._mock_warming_to_recording)
            else:
                self._start_warming_detector()

        if old == State.WARMING and new != State.RECORDING:
            # Bail out on cancel / error / direct-to-transcribing paths.
            self._stop_warming_detector()

        if new == State.RECORDING:
            if _MOCK:
                self._mock_transcription = MockTranscription()
                self._mock_transcription.start(on_delta=self._ui.append_text)
            else:
                # Warm the shared client's HTTP pool so batch's first call
                # lands on an established TLS socket. Recurring pings keep
                # the connection alive past typical LB idle timeouts.
                self._start_warmup()

        # Leaving RECORDING — stop the warmup timer. We do this on every
        # transition out (TRANSCRIBING, CANCELLED, ERROR) so the timer
        # never outlives the recording phase.
        if old == State.RECORDING:
            self._stop_warmup()

        if new == State.TRANSCRIBING:
            # Defer to next idle so the UI repaints with TRANSCRIBING state first
            GLib.idle_add(self._begin_transcribing)

        elif new == State.CLEANING:
            # Defer to next idle so the UI repaints with CLEANING state first
            GLib.idle_add(self._begin_cleanup)

        elif new == State.CANCELLED:
            # Tell the bootstrap thread to abandon its work (no live
            # preview needed) and release any partial client it built.
            self._bootstrap_cancelled = True
            # Stop mock providers on GTK thread (instant, uses GLib timers)
            if self._mock_transcription:
                self._mock_transcription.cancel()
                self._mock_transcription = None
            if self._batch:
                self._batch.cancel()
                self._batch = None
            if self._cleanup:
                self._cleanup.cancel()
                self._cleanup = None
            # Stop audio synchronously — it's fast (stream.stop + close + WAV
            # finalize) and MUST complete before Python shutdown.
            audio = self._audio
            self._audio = None
            if audio:
                try:
                    audio.stop()
                except Exception:
                    logger.exception("Cancel: failed to stop audio")
            # Defer only the slow parts (transcription cancel, lock release)
            self._teardown_async()

        elif new == State.ERROR:
            self._bootstrap_cancelled = True
            self._teardown_async()

    # ── Connection warmup ──

    def _start_warmup(self) -> None:
        """Fire an initial warmup ping and schedule recurring pings.

        Idempotent: `_on_state_change(RECORDING)` and
        `_kick_warmup_after_bootstrap` can both call this depending on
        which of "mic warmed up" vs "bootstrap finished" lands first.
        """
        if not self._client or self._warmup_timer_id is not None:
            return
        self._schedule_warmup_ping()
        self._warmup_timer_id = GLib.timeout_add_seconds(
            _WARMUP_INTERVAL_SECONDS, self._warmup_tick
        )
        logger.debug(
            "_start_warmup: initial ping fired, timer_id=%s interval=%ds",
            self._warmup_timer_id,
            _WARMUP_INTERVAL_SECONDS,
        )

    def _stop_warmup(self) -> None:
        if self._warmup_timer_id is not None:
            GLib.source_remove(self._warmup_timer_id)
            logger.debug("_stop_warmup: timer_id=%s removed", self._warmup_timer_id)
            self._warmup_timer_id = None

    def _warmup_tick(self) -> bool:
        """GLib timer callback — fires every _WARMUP_INTERVAL_SECONDS."""
        if not self._machine or self._machine.state != State.RECORDING:
            self._warmup_timer_id = None
            return False  # stop timer
        self._schedule_warmup_ping()
        return True  # keep timer

    def _schedule_warmup_ping(self) -> None:
        """Fire a warmup ping in a daemon thread (non-blocking)."""
        threading.Thread(
            target=self._warmup_ping,
            daemon=True,
            name="voxize-warmup",
        ).start()

    def _warmup_ping(self) -> None:
        """Warm the pooled TLS socket.

        We call ``client.models.retrieve(MODEL)`` because gpt-5.4-nano does
        not participate in OpenAI's automatic prompt caching (see the
        2026-04-21 journal entry). A heavier ``responses.create`` warmup
        aimed at priming the cache was tried and empirically confirmed to
        return 0 cached tokens on every subsequent cleanup call; we
        reverted to the cheap lookup until OpenAI lights caching up for
        the gpt-5 nano/mini tier.

        If/when auto caching starts working for our model, flip this back
        to the ``responses.create``-shaped warmup (see git history for
        commit that introduced it) and re-enable the
        ``_accumulate_warmup_usage`` call that populates the Pings cost
        line.
        """
        client = self._client
        if not client:
            return
        # Lazy-import so the warmup path isn't what pulls voxize.cleanup
        # into memory; it is already resident via the bootstrap thread
        # by the time we get here.
        from voxize.cleanup import MODEL as _CLEANUP_MODEL

        t0 = time.monotonic()
        logger.debug("warmup: start")
        try:
            client.models.retrieve(_CLEANUP_MODEL)
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.debug(
                "warmup: complete duration_ms=%d status=ok kind=models.retrieve",
                duration_ms,
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("warmup: failed duration_ms=%d error=%s", duration_ms, e)

    # ── Phase 1 → Phase 2 transition ──

    def _begin_transcribing(self) -> bool:
        """Stop recording, cancel WS, start batch transcription."""
        logger.debug("_begin_transcribing: mock=%s", _MOCK)

        # Short-circuit any bootstrap still importing openai or opening
        # the WS — we no longer need the live preview, batch does the
        # authoritative pass.
        self._bootstrap_cancelled = True

        # Handle mock mode
        mock_transcript = ""
        if self._mock_transcription:
            mock_transcript = self._mock_transcription.stop()
            self._mock_transcription = None

        if _MOCK:
            self._release_lock()
            # In mock mode, skip batch — go straight to cleanup
            self._on_batch_done(mock_transcript)
            return False

        # Release mic lock immediately — recording is done
        self._release_lock()

        # Grab references and null them to prevent races
        audio = self._audio
        self._audio = None
        transcription = self._transcription
        self._transcription = None
        session_dir = self._session_dir

        threading.Thread(
            target=self._stop_and_batch,
            args=(audio, transcription, session_dir),
            daemon=True,
            name="voxize-stop",
        ).start()

        return False  # one-shot idle

    def _stop_and_batch(self, audio, transcription, session_dir) -> None:
        """Background thread: stop audio, kick off batch, tear down WS in parallel.

        Once ``audio.stop()`` has finalized the WAV, the batch POST has
        everything it needs. Firing it before ``transcription.stop()``
        overlaps the WS close/usage-wait (~100ms) with batch TTFT.
        """
        # Step 1: stop audio — must complete before batch reads the WAV.
        logger.debug("_stop_and_batch: stopping audio")
        try:
            if audio:
                audio.stop()
        except Exception:
            logger.exception("Failed to stop audio")
        logger.debug("_stop_and_batch: audio stopped")

        # Step 1b: wait for the bootstrap thread to settle so self._client
        # is populated before we dispatch batch. On a fast stop (user hit
        # Escape immediately after the window showed), openai may still
        # be importing; we block up to 10s for it. Batch can't proceed
        # without a client. Transcription stop-up below uses the local
        # ``transcription`` snapshot so a late bootstrap finishing after
        # cancellation does not leak a dangling WS.
        if not self._bootstrap_done.wait(timeout=10.0):
            logger.warning("_stop_and_batch: bootstrap did not finish within 10s")

        # Log WAV file info (cheap; do before the handoff).
        wav_path = os.path.join(session_dir, "audio.wav") if session_dir else None
        if wav_path and os.path.exists(wav_path):
            wav_size = os.path.getsize(wav_path)
            logger.debug(
                "_stop_and_batch: wav_size=%d (%.1fs audio)",
                wav_size,
                (wav_size - WAV_HEADER_SIZE) / (SAMPLE_RATE * CHANNELS * 2),
            )

        # Step 2: kick off batch now. It runs concurrently with the
        # transcription teardown that follows.
        logger.debug("_stop_and_batch: dispatching batch start")
        GLib.idle_add(self._start_batch, session_dir)

        # Step 3: cancel WS (live_transcript.stop() can wait up to 2s for
        # usage events — that's what we're parallelising away).
        logger.debug("_stop_and_batch: cancelling transcription")
        live_usage = None
        live_transcript = ""
        # Bootstrap may have populated self._transcription after our
        # parent ``_begin_transcribing`` captured a None reference —
        # grab the latest so we don't leave a dangling WS open.
        if transcription is None:
            transcription = self._transcription
            self._transcription = None
        try:
            if transcription:
                transcription.stop()
                live_usage = transcription.usage
                live_transcript = transcription.transcript
        except Exception:
            logger.exception("Failed to stop transcription")
        logger.debug(
            "_stop_and_batch: transcription cancelled, live_transcript_len=%d",
            len(live_transcript),
        )

        # Save live transcript for debugging (not the authoritative result)
        if live_transcript and session_dir:
            try:
                Path(session_dir, "live_transcript.txt").write_text(live_transcript)
            except Exception:
                logger.exception("Failed to save live_transcript.txt")

        # Step 4: hand live-preview usage back to GTK for the cost summary.
        GLib.idle_add(self._set_live_usage, live_usage)

    def _set_live_usage(self, live_usage) -> bool:
        """GTK thread: store live-preview usage once WS teardown settles."""
        logger.debug("_set_live_usage: live_usage=%s", live_usage)
        self._live_usage = live_usage
        return False

    def _start_batch(self, session_dir: str) -> bool:
        """GTK thread: start batch transcription of the WAV file."""
        logger.debug("_start_batch: session_dir=%s", session_dir)

        # Guard against stale callback (e.g., user cancelled during stop)
        if not self._machine or self._machine.state != State.TRANSCRIBING:
            return False

        wav_path = os.path.join(session_dir, "audio.wav")
        if not os.path.exists(wav_path):
            logger.error("_start_batch: audio.wav not found")
            self._ui.show_error_banner("Audio file not found")
            self._machine.transition(State.READY)
            self._show_session_costs()
            return False

        self._batch = BatchTranscription(self._client, session_dir=session_dir)
        self._batch.start(
            wav_path=wav_path,
            on_delta=self._ui.append_text,
            on_complete=self._on_batch_done,
            on_error=self._on_batch_error,
        )
        return False  # one-shot idle

    def _on_batch_done(self, transcript: str) -> None:
        """Batch transcription complete — save, clipboard, start cleanup."""
        logger.debug("_on_batch_done: transcript_len=%d", len(transcript))
        self._batch_usage = self._batch.usage if self._batch else None
        self._batch = None

        # Guard against stale callback
        if not self._machine or self._machine.state != State.TRANSCRIBING:
            return

        # Save batch transcript (the authoritative result)
        if transcript and self._session_dir:
            try:
                Path(self._session_dir, "transcription.txt").write_text(transcript)
            except Exception:
                logger.exception("Failed to save transcription.txt")

        # First clipboard write — batch transcript
        if transcript:
            clipboard.copy(transcript)

        if not transcript.strip():
            self._ui.clear_text()
            self._ui.append_text("No speech detected.")
            self._machine.transition(State.READY)
            self._show_session_costs()
            return

        # Transition to cleanup
        self._batch_transcript = transcript
        self._machine.transition(State.CLEANING)

    def _on_batch_error(self, message: str) -> None:
        """Batch failed — non-fatal. WAV + recover.sh still usable."""
        logger.error("_on_batch_error: %s", message)
        self._batch_usage = self._batch.usage if self._batch else None
        self._batch = None
        if self._machine and self._machine.state == State.TRANSCRIBING:
            self._ui.show_error_banner(message)
            self._machine.transition(State.READY)
            self._show_session_costs()

    # ── Phase 3: Cleanup ──

    def _begin_cleanup(self) -> bool:
        """Start cleanup of the batch transcript."""
        logger.debug("_begin_cleanup: mock=%s", _MOCK)

        # Guard against stale callback
        if not self._machine or self._machine.state != State.CLEANING:
            return False

        transcript = self._batch_transcript
        if not transcript.strip():
            self._machine.transition(State.READY)
            self._show_session_costs()
            return False

        # Show the batch transcript pulsing and arm the cleanup swap
        self._ui.show_transcript_for_cleanup(transcript)

        if _MOCK:
            self._cleanup = MockCleanup()
            self._cleanup.start(
                transcript=transcript,
                on_delta=self._ui.append_text,
                on_complete=self._on_cleanup_done,
            )
        else:
            self._cleanup = Cleanup(
                self._client, prompts=self._prompts, session_dir=self._session_dir
            )
            self._cleanup.start(
                transcript=transcript,
                on_delta=self._ui.append_text,
                on_complete=self._on_cleanup_done,
                on_error=self._on_cleanup_error,
            )
        return False  # one-shot idle

    def _on_cleanup_done(self, cleaned: str) -> None:
        logger.debug("_on_cleanup_done: cleaned_len=%d", len(cleaned))
        self._cleanup_usage = self._cleanup.usage if self._cleanup else None
        self._cleanup = None
        if self._machine and self._machine.state == State.CLEANING:
            # Save cleaned text and copy to clipboard (overwrites batch transcript)
            if cleaned and self._session_dir:
                try:
                    Path(self._session_dir, "cleaned.txt").write_text(cleaned)
                except Exception:
                    logger.exception("Failed to save cleaned.txt")
            if cleaned:
                clipboard.copy(cleaned)
            self._machine.transition(State.READY)
            self._show_session_costs()

    def _on_cleanup_error(self, message: str) -> None:
        """Cleanup failed — non-fatal. Batch transcript is already in clipboard."""
        logger.error("_on_cleanup_error: %s", message)
        self._cleanup_usage = self._cleanup.usage if self._cleanup else None
        self._cleanup = None
        if self._machine and self._machine.state == State.CLEANING:
            self._ui.show_error_banner(message)
            self._machine.transition(State.READY)
            self._show_session_costs()

    # ── Teardown ──

    def _teardown_async(self) -> None:
        """Move blocking teardown to a background thread."""
        logger.debug(
            "_teardown_async: audio=%s transcription=%s lock=%s",
            self._audio is not None,
            self._transcription is not None,
            self._lock is not None,
        )
        # Grab references and null them on GTK thread to prevent races
        audio = self._audio
        self._audio = None
        transcription = self._transcription
        self._transcription = None
        lock = self._lock
        self._lock = None

        if not audio and not transcription and not lock:
            logger.debug("_teardown_async: nothing to tear down")
            return  # nothing to tear down

        threading.Thread(
            target=self._teardown_blocking,
            args=(audio, transcription, lock),
            daemon=True,
            name="voxize-teardown",
        ).start()

    @staticmethod
    def _teardown_blocking(audio, transcription, lock) -> None:
        """Background thread: stop providers, release lock."""
        logger.debug(
            "_teardown_blocking: audio=%s transcription=%s lock=%s",
            audio is not None,
            transcription is not None,
            lock is not None,
        )
        try:
            if audio:
                logger.debug("_teardown_blocking: stopping audio")
                audio.stop()
        except Exception:
            logger.exception("Teardown: failed to stop audio")
        try:
            if transcription:
                logger.debug("_teardown_blocking: cancelling transcription")
                transcription.cancel()
        except Exception:
            logger.exception("Teardown: failed to cancel transcription")
        try:
            if lock:
                logger.debug("_teardown_blocking: releasing lock")
                lock.release()
        except Exception:
            logger.exception("Teardown: failed to release lock")
        logger.debug("_teardown_blocking: complete")

    def _release_lock(self) -> None:
        if self._lock:
            logger.debug("_release_lock: releasing mic lock")
            self._lock.release()
            self._lock = None

    # ── Session costs ──

    # Pricing per million tokens
    _LIVE_INPUT_PRICE = 1.25  # gpt-4o-mini-transcribe
    _LIVE_OUTPUT_PRICE = 5.00
    _BATCH_INPUT_PRICE = 2.50  # gpt-4o-transcribe
    _BATCH_OUTPUT_PRICE = 10.00
    _CLEANUP_INPUT_PRICE = 0.20  # gpt-5.4-nano
    _CLEANUP_CACHED_INPUT_PRICE = 0.02  # 10% of standard (benchlm.ai pricing guide)
    _CLEANUP_OUTPUT_PRICE = 1.25

    def _nano_cost(self, usage: dict[str, int] | None) -> float | None:
        """Cost for a nano call, splitting cached vs non-cached input tokens."""
        if not usage:
            return None
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cached = usage.get("cached_tokens", 0) or 0
        uncached = max(inp - cached, 0)
        return (
            uncached * self._CLEANUP_INPUT_PRICE
            + cached * self._CLEANUP_CACHED_INPUT_PRICE
            + out * self._CLEANUP_OUTPUT_PRICE
        ) / 1_000_000

    def _show_session_costs(self) -> None:
        """Compute dollar costs from provider usage and display in UI."""
        logger.debug(
            "_show_session_costs: live_usage=%s batch_usage=%s cleanup_usage=%s",
            self._live_usage,
            self._batch_usage,
            self._cleanup_usage,
        )

        def _cost(usage, in_price, out_price):
            if not usage:
                return None
            inp = usage["input_tokens"]
            out = usage["output_tokens"]
            return (inp * in_price + out * out_price) / 1_000_000

        l_cost = _cost(
            self._live_usage, self._LIVE_INPUT_PRICE, self._LIVE_OUTPUT_PRICE
        )
        b_cost = _cost(
            self._batch_usage, self._BATCH_INPUT_PRICE, self._BATCH_OUTPUT_PRICE
        )
        c_cost = self._nano_cost(self._cleanup_usage)
        logger.debug(
            "_show_session_costs: live=%s batch=%s cleanup=%s",
            f"{l_cost:.4f}" if l_cost is not None else "n/a",
            f"{b_cost:.4f}" if b_cost is not None else "n/a",
            f"{c_cost:.4f}" if c_cost is not None else "n/a",
        )

        if l_cost is not None or b_cost is not None or c_cost is not None:
            self._ui.show_session_costs(l_cost, b_cost, c_cost)

    # ── Window events ──

    def _on_key(self, _ctrl, keyval, _code, _mod, win) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        s = self._machine.state if self._machine else None
        logger.debug("_on_key: Escape pressed, state=%s", s)
        if s in (
            State.INITIALIZING,
            State.WARMING,
            State.RECORDING,
            State.TRANSCRIBING,
            State.CLEANING,
        ):
            self._machine.transition(State.CANCELLED)
        elif s in (State.READY, State.ERROR, None):
            win.close()
        return True

    def _on_signal(self, sig: int) -> bool:
        """Handle SIGTERM/SIGINT — finalize WAV, release lock, quit."""
        logger.debug("_on_signal: sig=%s", sig)
        logger.info("Signal %s received, shutting down", sig)
        self._bootstrap_cancelled = True
        if self._audio:
            try:
                self._audio.finalize_wav()
            except Exception:
                logger.exception("Signal handler: failed to finalize WAV")
        if self._lock:
            try:
                self._lock.release()
                self._lock = None
            except Exception:
                pass
        # Restore ducked volumes synchronously — daemon threads die with
        # the process, so fire-and-forget is not safe here.
        try:
            self._ducker.restore_sync()
        except Exception:
            logger.exception("Signal handler: failed to restore ducked volumes")
        self.quit()
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _win) -> bool:
        logger.debug("_on_close_request: entry")
        # Tell any in-flight bootstrap to stop — we're closing, no point
        # completing openai import / WS connect.
        self._bootstrap_cancelled = True
        # Restore ducked volumes synchronously — if the user closes the
        # window mid-recording we'd otherwise leave apps silent forever.
        try:
            self._ducker.restore_sync()
        except Exception:
            logger.exception("Close: failed to restore ducked volumes")
        self._stop_warmup()
        self._teardown_async()
        if self._mock_transcription:
            self._mock_transcription.cancel()
        if self._batch:
            self._batch.cancel()
        if self._cleanup:
            self._cleanup.cancel()
        if self._client:
            try:
                self._client.close()
            except Exception:
                logger.debug("Close: failed to close OpenAI client", exc_info=True)
            self._client = None
        if self._ui:
            self._ui.destroy()
        # Prune old sessions (best-effort, at termination time)
        try:
            prune_sessions()
        except Exception:
            logger.debug("_on_close_request: prune_sessions failed", exc_info=True)
        # Remove session file log handler
        if self._log_handler:
            for name in ("voxize", "openai", "httpx", "httpcore"):
                logging.getLogger(name).removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
        # Redirect stdio to /dev/null so any parent process (e.g., GNOME Shell
        # extension) sees EOF immediately, while daemon threads can still write
        # to stderr without EBADF.
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                try:  # noqa: SIM105
                    os.dup2(devnull, fd)
                except OSError:
                    pass
            os.close(devnull)
        except OSError:
            pass
        # Explicitly quit the application so the main loop exits even if
        # GLib sources (signal handlers, stale idle callbacks) are pending.
        self.quit()
        return False  # allow close
