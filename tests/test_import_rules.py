import importlib.util
import json
import subprocess
import sys

BACKEND_MODULES = {
    "voxize.audio",
    "voxize.batch",
    "voxize.cleanup",
    "voxize.transcribe",
    "voxize.mock",
    "voxize.prompt",
    "voxize.recover",
    "voxize.ducking",
    "voxize.storage",
    "voxize.lock",
    "voxize.checks",
    "voxize.state",
    "voxize.opus",
    "voxize.openai_patches",
    "voxize._audio_const",
}


def modules_after(statement: str) -> set[str]:
    code = f"{statement}; import sys, json; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_gui_imports_no_backend_module():
    mods = modules_after("import voxize.gui.app")
    assert "gi.repository.Gtk" in mods  # sanity: it really is the GTK app
    leaked = sorted(
        m for m in mods if m in BACKEND_MODULES or m.startswith("voxize.backend")
    )
    assert leaked == []


def test_old_in_process_modules_are_gone():
    mods = modules_after("import voxize.gui.app, voxize.gui.ui")
    assert "voxize.app" not in mods
    assert "voxize.ui" not in mods
    assert importlib.util.find_spec("voxize.app") is None
    assert importlib.util.find_spec("voxize.ui") is None
