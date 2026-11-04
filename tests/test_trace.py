import os
import subprocess
import sys


def test_tagged_trace_and_exported_origin():
    code = (
        "import os, sys; from voxize import _trace; "
        "_trace.set_tag('gui'); _trace.trace('hello'); "
        "_trace.export_origin(); print(os.environ['VOXIZE_TRACE_T0'], file=sys.stdout)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "VOXIZE_TRACE": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[voxize-trace] [gui] t+" in out.stderr
    assert "hello" in out.stderr
    assert float(out.stdout.strip()) > 0


def test_a_sink_receives_the_lines_instead_of_stderr():
    code = (
        "from voxize import _trace; seen = []; _trace.set_sink(seen.append); "
        "_trace.set_tag('tui'); _trace.trace('hello'); print(seen[0])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "VOXIZE_TRACE": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[voxize-trace] [tui] t+" in out.stdout and "hello" in out.stdout
    assert out.stderr == ""
