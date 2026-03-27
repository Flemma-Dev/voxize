# Architecture Decision: Layer Shell, GNOME Shell, and Dual Frontend

**Date**: 2026-03-27 (Phase 0)
**Status**: Decided
**Referenced from**: `docs/journal.md`, Session 1

## Context

The design document specified `gtk4-layer-shell` for the overlay window — always-on-top, top-right anchoring, no focus steal. During Phase 0 implementation, we discovered this doesn't work on GNOME Shell.

## The Layer Shell Problem

### What happened

After scaffolding the project, `uv run python -m voxize` produced:

```
Failed to initialize layer surface, GTK4 Layer Shell may have been linked after libwayland.
```

We fixed the link order via `LD_PRELOAD` in `shell.nix`. The library loaded, but:

```
it appears your Wayland compositor doesn't support Layer Shell
GtkWindow is not a layer surface. Make sure you called gtk_layer_init_for_window()
```

### Why it failed

`gtk4-layer-shell` implements the `zwlr_layer_shell_v1` Wayland protocol. This is a **wlroots-specific protocol** — it works on Sway, Hyprland, and other wlroots-based compositors. GNOME Shell / Mutter does not implement it. Verified on GNOME Shell 49.2:

```python
Gtk4LayerShell.is_supported()  # → False
```

This isn't a bug or a version issue — GNOME has never supported this protocol and has no plans to. The design document's choice of `gtk4-layer-shell` was based on incorrect assumptions about protocol availability on GNOME.

### Resolution

Removed `gtk4-layer-shell` entirely — from `shell.nix` (package + `LD_PRELOAD` + `GI_TYPELIB_PATH`), from `checks.py` (import check), and from `__main__.py` (all `Gtk4LayerShell` calls). The window now launches as a standard GTK4 `ApplicationWindow`.

## What GTK4 Cannot Do on Wayland/GNOME

With Layer Shell gone, we investigated what GTK4 can and cannot control on Wayland:

| Capability | GTK4 on Wayland | Notes |
|---|---|---|
| Remove title bar | **Yes** | `window.set_decorated(False)` |
| Set window size | **Yes** | `window.set_default_size(420, -1)` |
| Position window | **No** | Compositor decides placement. GTK4 removed `window.move()` for Wayland |
| Always on top | **No** | `GdkToplevel` has no keep-above API. GTK3's `set_keep_above()` was removed |
| Prevent focus steal | **Partial** | `set_focus_on_click(False)` helps, but the compositor controls initial focus |

We also checked `GdkToplevel` and `GdkToplevelLayout` APIs — they only expose fullscreen and maximized states, nothing for stacking or positioning.

### GNOME Shell D-Bus (Eval)

Investigated using `org.gnome.Shell.Eval` to run JS inside the compositor (e.g., `global.display.focus_window.make_above()`). The method exists but returns `(false, '')` — GNOME 45+ disabled `Shell.Eval` unless `unsafe_mode` is true, which it isn't by default and we can't require users to enable it.

## The GNOME Shell Extension Question

### What an extension provides

We researched GNOME Shell extension APIs to understand what becomes possible:

| Capability | Available | API |
|---|---|---|
| Global hotkey | Yes | `Main.wm.addKeybinding()` |
| Position window | Yes | `Meta.Window.move_resize_frame(bool, x, y, w, h)` |
| Always on top | Yes (workarounds) | `Meta.Window.make_above()` — not fully exposed to JS, but extensions like "Window on Top" demonstrate working approaches |
| Spawn processes | Yes | `Gio.Subprocess` with stdin/stdout access |
| Match windows | Yes | WM_CLASS matching |
| Custom UI widgets | Yes | St.Button, St.Label, St.BoxLayout, St.ScrollView (Shell Toolkit) |

### Should the extension replace the GTK4 UI?

**No. The extension should be a companion, not a replacement.**

Arguments against moving the UI into the extension:
- Extensions run inside the GNOME Shell compositor process. A crash in extension JS **crashes the entire desktop**. Audio capture, WebSocket streaming, and API calls are crash-prone I/O operations that should never run inside the compositor.
- The UI is built with a completely different toolkit (St/Clutter widgets, not GTK4). Zero code transfers between them.
- Extensions require GNOME Shell and tie you to specific Shell versions. A GTK4 app works on any Wayland compositor.

### Should we build a full extension UI?

We discussed whether to build the UI in GNOME Shell's St/Clutter toolkit instead of GTK4. The widget APIs are entirely different:

| | GTK4 (Python) | GNOME Shell (JS) |
|---|---|---|
| Buttons | `Gtk.Button()` | `St.Button()` |
| Labels | `Gtk.Label()` | `St.Label()` |
| Layout | `Gtk.Box()` | `St.BoxLayout()` |
| Scrolling | `Gtk.ScrolledWindow()` | `St.ScrollView()` |
| Styling | `Gtk.CssProvider` | Shell CSS (similar syntax, different engine) |
| Language | Python | JavaScript (GJS) |

No UI code is transferrable. But the UI is small (~150-200 lines in either toolkit), so building it twice isn't a significant cost.

## Decision: Dual Frontend Architecture

### The architecture

Two frontends sharing the same Python backend:

**GTK4 frontend (in-process):** The backend and UI run in the same Python process. The UI calls the state machine directly. This is simpler, works today, and runs on any Wayland compositor.

```
Single Python process
┌──────────────────────────────────┐
│ Gtk.ApplicationWindow UI         │
│ State machine                    │
│ Audio, WebSocket, Anthropic, etc │
└──────────────────────────────────┘
```

**GNOME Shell extension frontend (future, out of scope):** The extension spawns the Python backend as a subprocess. Communication is via stdin/stdout using JSON Lines. The extension renders its own UI in St/Clutter widgets and manages window positioning, stacking, and hotkeys natively.

```
GNOME Shell extension (JS)          Python subprocess
┌──────────────────────┐            ┌─────────────────────┐
│ Hotkey registration  │            │ Audio capture       │
│ St.Widget UI         │◄─ stdio ──►│ OpenAI WebSocket    │
│ Window positioning   │  (jsonl)   │ Anthropic cleanup   │
│ Always-on-top        │            │ WAV writer          │
│ Render state         │            │ Clipboard, storage  │
└──────────────────────┘            │ State machine       │
                                    └─────────────────────┘
```

### Why stdin/stdout, not D-Bus

- The extension spawns exactly one Python process per session — 1:1 communication, not pub/sub
- The data is simple: state changes, text deltas, two commands (stop/cancel)
- stdin/stdout is trivial on both sides — JSON Lines, one object per line
- `Gio.Subprocess` already provides stdin/stdout handles
- No schema registration, no bus names, no dbus daemon dependency
- D-Bus would only make sense for a long-running daemon or multiple consumers — neither applies

### What this means for implementation

1. **Build the GTK4 version now as planned.** It's a complete, working product.
2. **Keep the backend free of GTK imports.** The design doc already structures this: `state.py`, `audio.py`, `transcribe.py`, `cleanup.py`, `storage.py`, `lock.py`, `clipboard.py` have no UI dependency. Only `app.py` and `ui.py` touch GTK.
3. **Don't over-engineer the separation.** No JSON Lines protocol now, no IPC abstraction layer. The state machine's callback interface (already in the design) is the natural seam. When/if the extension is built, wrapping those callbacks in JSON Lines serialization is straightforward.
4. **The GNOME Shell extension is out of scope for this project.** It's a future enhancement, not a Phase 5.

### Risk assessment

**Over-engineering the separation:** Largely solved by choosing stdin/stdout over D-Bus. The backend just emits JSON Lines. Whether it's Python spawning Python or a Shell extension spawning a script, the protocol is trivial.

**GTK4 version feels limited on GNOME:** The window can't auto-position or stay on top. But the baseline comparison is `notify-send` (the author's current VoxInput setup) — a GTK4 window with live streaming transcription, buttons, and clipboard integration is a massive improvement regardless of window placement.

**Shell extension never gets built:** That's fine. If the GTK4 version works well enough, there's no need. No wasted work — the GTK4 UI is the product, not a stepping stone.

**Two UIs to maintain:** The UI is small and self-contained. With a clear JSON Lines protocol, changes to the backend don't affect either frontend. UI changes are local to each frontend's ~150 lines of widget code.
