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

#### Next
- Verify Escape closes the window and missing-dependency errors render correctly
- Continue Phase 0 done criteria: translucent overlay with "Voxize" text, Escape to close, clear errors on missing deps
