# Voxize Implementation Journal

### 2026-03-27 — Session 1: Phase 0 Bootstrap

**Phase**: 0 — Bootstrap
**Status**: in progress

Scaffolded the project:

- `pyproject.toml` — uv/hatchling build, PyGObject dependency, `src/voxize/` layout. Fixed build backend (`hatchling.build`, not `hatchling.backends`).
- `flake.nix` + `shell.nix` — follows the flemma.nvim pattern (flake imports shell.nix, shell.nix has `pkgs` fallback). System deps: gtk4, gobject-introspection, portaudio, pkg-config, wl-clipboard, libsecret. `shellHook` exports `LD_LIBRARY_PATH` via `lib.makeLibraryPath`. Flake pinned to `nixos-25.11`.
- `src/voxize/checks.py` — startup validation: GTK4 importable, wl-copy and secret-tool on PATH, API keys present in GNOME Keyring via secret-tool. Reports all failures at once (not fail-fast) with actionable messages.
- `src/voxize/__main__.py` — entry point. Runs checks first (exits non-zero on failure). Creates a GTK4 `ApplicationWindow` with `set_decorated(False)`, `set_resizable(False)`, dark translucent CSS, Escape key closes the window.

Verified: `uv run python -m voxize` launches a GTK4 window cleanly (only harmless Mesa GPU driver messages).

> [!IMPORTANT]
> **Major design deviation — dropped gtk4-layer-shell, adopted dual frontend architecture.** The design doc specified `gtk4-layer-shell` for overlay behaviour. Testing on GNOME Shell 49.2 revealed the `zwlr_layer_shell_v1` protocol is wlroots-only — GNOME has never implemented it. After investigating GTK4 Wayland APIs (`GdkToplevel`, `GdkToplevelLayout`) and GNOME Shell D-Bus (`Shell.Eval` disabled in GNOME 45+), we confirmed that window positioning and always-on-top are not controllable from a GTK4 client on GNOME/Wayland. The only path to these capabilities is a GNOME Shell extension. This led to a broader architectural discussion and decision documented in full at **[`docs/architecture-decision-layer-shell-and-dual-frontend.md`](architecture-decision-layer-shell-and-dual-frontend.md)**.

Summary of the decision:
- **GTK4 frontend (now):** Complete working product. Backend runs in-process. Accepts GNOME's window placement limitations — still a massive improvement over the current `notify-send` setup.
- **GNOME Shell extension frontend (future, out of scope):** Would spawn the Python backend as a subprocess, communicate via stdin/stdout JSON Lines, render UI in St/Clutter widgets, and handle hotkey/positioning/stacking natively. Not part of this project's phases.
- **Backend stays GTK-free:** `state.py`, `audio.py`, `transcribe.py`, `cleanup.py`, `storage.py`, `lock.py`, `clipboard.py` have no UI imports. Only `app.py` and `ui.py` touch GTK. This is the natural seam for a future JSON Lines boundary.
- **No over-engineering now:** No IPC protocol, no abstraction layer. The state machine callback interface is sufficient separation. JSON Lines wrapping is trivial to add later if needed.

Phase 0 done criteria met in prior session. Moving to Phase 1.

---

### 2026-03-27 — Session 2: Phase 1 — UI/UX with Mocks

**Phase**: 1 — UI/UX with Mocks
**Status**: in progress

Implemented the full Phase 1 deliverables:

**State machine (`state.py`)** — Pure logic, no GTK imports. States: INITIALIZING → RECORDING → CLEANING → READY, with CANCELLED and ERROR reachable from active states. Validates transitions and notifies listeners synchronously. This is the contract that real providers (Phase 2-3) will plug into.

**CSS theme (`style.css`)** — Extracted from inline string in `__main__.py` to a standalone file. Full dark translucent theme per design doc §5: window styling, status dot colors (red/amber/green/grey) with pulse animation via `transition: opacity`, header separator, text view with transparent background, primary/secondary button styles overriding Adwaita defaults.

**UI (`ui.py`)** — Header bar with status dot (●), label, timer, and contextual buttons. Text area uses `Gtk.TextView` (non-editable, word-wrap, selectable) inside `Gtk.ScrolledWindow` with `POLICY_EXTERNAL` (no scrollbar, programmatic scroll-to-bottom). Text area hidden initially, shown when first text arrives — keeps window compact. Fade-out gradient at top of text area via `Gtk.DrawingArea` overlay drawing a `cairo.LinearGradient` from window bg to transparent (32px). Max text area height computed from monitor geometry (screen_height / 4 - 50px). Pulsing dot via GLib timer toggling a `.dim` CSS class every 600ms with 400ms CSS transition.

**Mock providers (`mock.py`)** — `MockTranscription` emits predefined text word-by-word at ~220ms intervals (~4.5 words/sec). `MockCleanup` emits cleaned text at ~60ms intervals (~16 words/sec, simulating fast API streaming). Cleaned text has minor formatting improvements over raw transcript (added commas, removed filler words). Same start/stop/cancel interface that real providers will implement.

**App wiring (`app.py`)** — `VoxizeApp` orchestrates: loads CSS from file, creates window, creates state machine + UI, transitions to RECORDING on activate. State change listener starts/stops mock providers. Escape key cancels during RECORDING/CLEANING, closes during READY/ERROR. `close-request` handler cleans up timers and providers.

**Entry point (`__main__.py`)** — Simplified to: run checks → import app → run.

Implementation decisions:
- **`Gtk.TextView` over `Gtk.Label`** for text display — gives auto-scrolling via marks, selection, and word-wrap out of the box. Labels don't support scroll-to-end.
- **`Gtk.Overlay` + `Gtk.DrawingArea`** for top fade — GTK4 CSS doesn't support `mask-image`. Cairo gradient with `set_can_target(False)` for click-through works well.
- **`POLICY_EXTERNAL`** on ScrolledWindow — hides scrollbar but allows programmatic `scroll_mark_onscreen()`. Design spec says no scrollbar.
- **Text area starts hidden** (`_overlay.set_visible(False)`) — the window shows only the header bar initially, grows when text arrives. Matches design: "Initial state: window shows just the header bar."
- Needed `gi.require_version('Gdk', '4.0')` in every module importing Gdk, not just the entry point. PyGObject warns otherwise.

Verified: `uv run python -m voxize` launches, runs for 3+ seconds without errors (only harmless Mesa GPU driver warnings).

Visual testing and iterative polish followed. Changes made during testing:

- **Scroll jitter fix** — `scroll_mark_onscreen` was racing ScrolledWindow height reallocation on line wraps. Fixed by deferring scroll to `GLib.idle_add` and using `vadjustment.set_value` directly for deterministic bottom-pinning. Debounced with `_scroll_pending` flag.
- **Fade gradient conditional** — Driven by `vadjustment.changed` signal instead of checking in the scroll callback. Shows only when `upper > page_size + 1`.
- **`Gtk.HeaderBar` as titlebar** — Replaced manual header `Gtk.Box` with `Gtk.HeaderBar` + `set_titlebar()`. Gives drag-to-move for free. `set_show_title_buttons(False)` suppresses default window buttons. Removed `set_decorated(False)` — CSD handles chrome. Removed the manual `Gtk.Separator` — headerbar provides its own bottom border via CSS.
- **System font inheritance** — Removed all `px`-based font sizes and `font-family: monospace` from window. Sizing now uses `em` units throughout so the UI scales with the system font. Monospace only on the text view via `set_monospace(True)` (adds `.monospace` class, libadwaita handles `--monospace-font-family`).
- **CSS variables over `@define-color`** — Discovered libadwaita uses CSS custom properties (`var(--name)`), not just `@define-color`. Migrated entire palette to `--vox-*` variables on `window`. This is how you override libadwaita's own styling — e.g., `--view-fg-color`, `--destructive-bg-color`.
- **Adwaita button classes** — Cancel uses `destructive-action` (overridden to muted red via `--destructive-bg-color`). Stop is a regular button. Close uses `flat`. Removed all custom `.primary`/`.secondary` button CSS.
- **Spinner loading state** — `Gtk.Spinner` shown during startup delay (before first transcription word) and before cleanup starts. Dismissed when first text token arrives via `append_text`. Mock providers have configurable delay (1.5s transcription, 2.5s cleanup).
- **Text pulse during cleanup** — Instead of clearing transcript and showing spinner, the transcript stays visible and pulses (opacity breathes between 1.0 and 0.5 via CSS `transition` + `.processing` class toggle). User can review what they said while waiting. First cleanup token clears and replaces the pulsing text.
- **Enter confirms, Escape cancels** — `set_default_widget(action_btn)` during RECORDING/READY/ERROR so Enter activates Stop/Close. Cleared during CLEANING. Escape cancels during RECORDING/CLEANING, closes during READY/ERROR.
- **Backdrop text dimming** — `notify::is-active` signal toggles `.backdrop` class on the text view. CSS overrides `--view-fg-color` to `--vox-fg-dim`. Had to use CSS variables because libadwaita's `textview > text { color: var(--view-fg-color) }` rule was unbeatable via normal selectors.
- **`lookup_color` fix** — PyGObject returns `(found, Gdk.RGBA)` tuple, not C-style output parameter. Wrong call signature silently killed the fade gradient draw callback.
- **ScrolledWindow overshoot/undershoot** — Disabled via CSS to prevent Adwaita's built-in scroll edge shadows.

> [!IMPORTANT]
> **Key GTK4/libadwaita CSS finding:** `@define-color` / `@name` is the legacy mechanism. Libadwaita uses CSS custom properties (`--name` / `var(--name)`) throughout. To override libadwaita styling, set its own variables (`--view-fg-color`, `--view-bg-color`, `--destructive-bg-color`, `--monospace-font-family`, etc.) on your widgets. This avoids specificity fights entirely. The `.view` class on `GtkTextView` with its `textview > text` selector is effectively unbeatable via normal CSS — override `--view-fg-color` instead.

#### Next
- Phase 1 done criteria check: all states render, timer works, scrolling works, buttons work, full visual flow feels complete
- Begin Phase 2: Audio Capture + OpenAI Realtime Transcription
