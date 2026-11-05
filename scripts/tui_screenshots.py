"""SVG screenshots of the terminal client in every state, for review.

    uv run python scripts/tui_screenshots.py [--out DIR]
    uv run python scripts/tui_screenshots.py --hero [--out DIR]

Headless (Textual's run_test): no terminal, no backend, no clipboard. The
events are scripted the way the backend sends them. Each scene is saved at
60x12, 88x24 and 200x60 so the size cap and centring can be checked.

``--hero`` renders the README's terminal shots instead —
``terminal-recording.png`` and ``terminal-ready.png`` in assets/ (or DIR) —
with the same text, timers and costs as the desktop shots from
scripts/generate_screenshots.py, so the two sit side by side. The PNGs come
from a headless Chrome/Chromium when one is on PATH, which draws the SVG as
a browser does. ``rsvg-convert`` (librsvg, in the dev shell) is the
fallback: it draws ─ rules a few pixels short of their cells. Without
either, the SVGs stay.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from voxize.protocol import PROTOCOL_VERSION
from voxize.tui.app import VoxizeTui

_ROOT = Path(__file__).resolve().parent.parent
SIZES = [(60, 12), (88, 24), (200, 60)]
SOURCES = [{"path": "/work/WHISPER.txt", "content": "Voxize, Supabase, Luup."}]
LIVE = (
    "so the idea is that the backend owns the progress model and the "
    "frontend just draws it"
)
FINAL = (
    "So the idea is that the backend owns the progress model, and the "
    "frontend just draws it."
)


def ev(type_, **fields):
    return {"type": type_, **fields}


def st(name, error=None):
    return ev("state", state=name, error=error)


BASE = [ev("hello", protocol=PROTOCOL_VERSION), ev("context", sources=SOURCES)]
REC = [
    *BASE,
    st("WARMING"),
    st("RECORDING"),
    ev("text", stage="live", op="replace", text=LIVE),
    ev("level", dbfs=-20.0),
]
WORK = [*REC, st("TRANSCRIBING")]
CLEAN = [
    *WORK,
    ev("text", stage="batch", op="replace", text=FINAL),
    st("CLEANING"),
    ev("text", stage="batch", op="replace", text=FINAL),
    ev("progress", fraction=0.8),
]
READY = [
    *CLEAN,
    ev("text", stage="cleaned", op="replace", text=FINAL),
    ev("costs", live=0.0001, batch=0.0002, cleanup=0.0001, total=0.0004),
    st("READY"),
]
# name -> (events, key to press after them or None)
SCENES = {
    "01-warming": ([*BASE, st("WARMING"), ev("level", dbfs=-45.0)], None),
    "02-recording": (REC, None),
    "03-recording-hot": ([*REC, ev("level", dbfs=-8.0)], None),
    "04-preview-lost": ([*REC, ev("preview_lost", reason="Simulated")], None),
    "05-transcribing": ([*WORK, ev("progress", fraction=0.35)], None),
    "06-transcribing-no-estimate": ([*WORK, ev("progress", fraction=None)], None),
    "07-stalled": (
        [*WORK, ev("progress", fraction=0.6), ev("stalled", stalled=True)],
        None,
    ),
    "08-cleaning": (CLEAN, None),
    "09-ready": (READY, None),
    "10-ready-cleanup-failed": (
        [
            *CLEAN,
            ev("text", stage="batch", op="replace", text=FINAL),
            ev("failure", phase="cleanup", message="simulated", fallback="batch"),
            st("READY"),
        ],
        None,
    ),
    "11-ready-batch-failed-empty": (
        [
            *BASE,
            st("RECORDING"),
            st("TRANSCRIBING"),
            ev("failure", phase="batch", message="simulated", fallback=None),
            st("READY"),
        ],
        None,
    ),
    "12-no-speech": ([*WORK, ev("no_speech"), st("READY")], None),
    "13-error": (
        [BASE[0], st("ERROR", error="Another Voxize instance is recording")],
        None,
    ),
    "14-confirm-cancel": (REC, "escape"),
    "15-vocabulary": (REC, "v"),
}


# README hero shots. Text, timers and costs match generate_screenshots.py.
HERO_SIZE = (72, 10)
HERO_LIVE = (
    "So I need you to refactor the authentication middleware "
    "to use JWT tokens instead of session cookies."
)
HERO_CLEANED = (
    "Refactor the authentication middleware to use JWT tokens "
    "instead of session cookies. The current implementation stores "
    "session tokens in a way that doesn't meet the new compliance "
    "requirements. Update the integration tests and the API "
    "documentation."
)
HERO_REC = [
    ev("hello", protocol=PROTOCOL_VERSION),
    ev("context", sources=[{"path": "~/src/api/WHISPER.txt", "content": "JWT"}]),
    st("WARMING"),
    st("RECORDING"),
    ev("text", stage="live", op="replace", text=HERO_LIVE),
]
# name -> (events, timer seconds)
HERO = {
    "terminal-recording": ([*HERO_REC, ev("level", dbfs=-19.0)], 8),
    "terminal-ready": (
        [
            *HERO_REC,
            st("TRANSCRIBING"),
            st("CLEANING"),
            ev("text", stage="cleaned", op="replace", text=HERO_CLEANED),
            ev("costs", live=0.0007, batch=0.0031, cleanup=0.0005, total=0.0043),
            st("READY"),
        ],
        14,
    ),
}


class _NullTransport:
    def send(self, type_, **fields):
        pass

    def finish(self):
        pass

    def close_stdin(self):
        pass


class _NullClipboard:
    unavailable = False

    async def copy(self, text):
        return "none"

    async def settle(self):
        pass


def save_svg(app: VoxizeTui, out: Path, filename: str) -> Path:
    """The screen as an SVG that librsvg draws right, too.

    Without xml:space="preserve", librsvg (rsvg-convert, GNOME's image
    viewers) drops a text run's leading space: the bold "Enter" and the
    muted " Stop" of a key hint draw as "EnterStop". Browsers keep it.
    """
    svg = out / filename
    app.save_screenshot(filename=filename, path=str(out))
    svg.write_text(svg.read_text().replace("<svg ", '<svg xml:space="preserve" ', 1))
    return svg


async def shoot(out: Path, name: str, events, key, size) -> None:
    app = VoxizeTui(
        lambda on_event, on_lost: _NullTransport(),
        cwd=None,
        copier=_NullClipboard(),
        hello_timeout_s=3600,
        install_signals=False,
    )
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for event in events:
            app.on_backend_event(event)
        await pilot.pause()
        if key:
            await pilot.press(key)
            await pilot.pause()
        save_svg(app, out, f"{name}-{size[0]}x{size[1]}.svg")
    app.session_log.detach()


async def shoot_hero(out: Path, name: str, events, seconds: int) -> Path:
    app = VoxizeTui(
        lambda on_event, on_lost: _NullTransport(),
        cwd=None,
        copier=_NullClipboard(),
        hello_timeout_s=3600,
        install_signals=False,
    )
    svg = out / f"{name}.svg"
    async with app.run_test(size=HERO_SIZE) as pilot:
        await pilot.pause()
        for event in events:
            app.on_backend_event(event)
        # A frozen timer, as the desktop shots have.
        app._stop_timer()
        app.card.timer_seconds = seconds
        app._render()
        await pilot.pause()
        app.title = "voxize-tui"
        save_svg(app, out, svg.name)
    app.session_log.detach()
    return svg


_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def _chrome_png(browser: str, svg: Path, png: Path) -> None:
    viewbox = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text())
    width, height = (math.ceil(float(n)) for n in viewbox.groups())
    # A throwaway profile: never the user's own browser profile.
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--user-data-dir={profile}",
                "--hide-scrollbars",
                "--force-device-scale-factor=2",
                f"--window-size={width},{height}",
                "--default-background-color=00000000",
                f"--screenshot={png}",
                svg.as_uri(),
            ],
            check=True,
            capture_output=True,
        )


def hero(out: Path) -> None:
    browser = next(filter(None, map(shutil.which, _BROWSERS)), None)
    rsvg = shutil.which("rsvg-convert")
    for name, (events, seconds) in HERO.items():
        svg = asyncio.run(shoot_hero(out, name, events, seconds))
        png = svg.with_suffix(".png")
        # 2x, so the shots stay sharp at README widths.
        if browser is not None:
            _chrome_png(browser, svg, png)
        elif rsvg is not None:
            subprocess.run([rsvg, "-z", "2", "-o", str(png), str(svg)], check=True)
        else:
            print(f"{svg} (no Chrome/Chromium or rsvg-convert on PATH: PNG skipped)")
            continue
        svg.unlink()
        print(png)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--hero", action="store_true", help="the README's terminal shots"
    )
    args = parser.parse_args()
    if args.hero:
        out = args.out or _ROOT / "assets"
        out.mkdir(parents=True, exist_ok=True)
        hero(out)
        return
    args.out = args.out or Path("/tmp/voxize-tui-screens")
    args.out.mkdir(parents=True, exist_ok=True)
    for name, (events, key) in SCENES.items():
        for size in SIZES:
            asyncio.run(shoot(args.out, name, events, key, size))
    print(f"{len(SCENES) * len(SIZES)} screenshots in {args.out}")


if __name__ == "__main__":
    main()
