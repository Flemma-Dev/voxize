"""``voxize-remote setup-client``: the Android client installed, allowed, paired and
checked, over adb.

The APK is a release's ``voxize-remote-VERSION.apk`` (``--apk``, or
``VOXIZE_REMOTE_APK``), signed with the release key. A client signed with another key,
or newer than this one, is replaced without asking: the client keeps nothing but its
pairing, and this redoes the pairing anyway.

Exit status: 0 once the client is connected, 2 when it is paired but did not connect
(the message says what to fix), 1 when a step failed.

Every outside effect goes through Adb, Console and the control socket, so the tests
drive the whole flow with fakes.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time

CLIENT_PACKAGE = "dev.flemma.voxize.remote"
GRANTED_PERMISSIONS = (
    "android.permission.RECORD_AUDIO",
    "android.permission.POST_NOTIFICATIONS",
)
# appops: "Display over other apps" for the floating button, Usage access so it shows
# only over Termux.
ALLOWED_OPERATIONS = ("SYSTEM_ALERT_WINDOW", "GET_USAGE_STATS")
CONNECT_CHECK_SECONDS = 20.0
APK_VARIABLE = "VOXIZE_REMOTE_APK"


class SetupError(Exception):
    """A step that cannot go on; the message says what to do."""


class Console:
    """Stan's terminal. Lines are flushed; answers are read with anything typed ahead
    thrown away."""

    def __init__(self, stream=sys.stdout, keyboard=sys.stdin):
        self.stream = stream
        self.keyboard = keyboard

    def say(self, text=""):
        print(text, file=self.stream, flush=True)

    def ask(self, prompt):
        if self.keyboard.isatty():
            import termios

            termios.tcflush(self.keyboard, termios.TCIFLUSH)
        self.stream.write(prompt)
        self.stream.flush()
        line = self.keyboard.readline()
        if not line:
            raise SetupError("the terminal has no more input to answer with")
        return line.strip()


class Adb:
    def __init__(self, executable, serial=None, runner=subprocess.run):
        self.executable = executable
        self.serial = serial
        self.runner = runner

    def run(self, *arguments, timeout=120):
        command = (
            [self.executable]
            + (["-s", self.serial] if self.serial else [])
            + list(arguments)
        )
        completed = self.runner(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def shell(self, command, timeout=60):
        """One device shell command line (adb joins its arguments into one anyway)."""
        return self.run("shell", command, timeout=timeout)

    def ready_devices(self):
        """Serials of the devices adb can use now (state "device")."""
        status, output = Adb(self.executable, runner=self.runner).run(
            "devices", timeout=15
        )
        if status != 0:
            raise SetupError(f"adb cannot list devices: {output.strip()}")
        return [
            parts[0]
            for parts in (line.split() for line in output.splitlines()[1:])
            if len(parts) >= 2 and parts[1] == "device"
        ]

    def checked_shell(self, command, what):
        """A device command that must work; SetupError saying ``what`` failed otherwise."""
        status, output = self.shell(command)
        if status != 0:
            raise SetupError(f"could not {what}: {output.strip() or f'exit {status}'}")
        return output


def pick_device(adb):
    """The one device adb can use; with none or several, say how to choose."""
    ready = adb.ready_devices()
    if len(ready) == 1:
        return ready[0]
    if not ready:
        raise SetupError(
            "no device: connect the phone with USB debugging allowed (or Wireless "
            "debugging), then try again"
        )
    raise SetupError(f"several devices ({', '.join(ready)}): pass --serial")


def install_client(adb, apk, console):
    """Installs ``apk``; a client only an uninstall gets past is replaced."""
    status, output = adb.run("install", "-r", apk, timeout=180)
    if status == 0 and "Success" in output:
        return
    reasons = {
        "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "was signed with another key",
        "INSTALL_FAILED_VERSION_DOWNGRADE": "is newer than this one",
    }
    reason = next((text for code, text in reasons.items() if code in output), None)
    if reason is not None:
        console.say(f"The Voxize Remote on the phone {reason}: replacing it.")
        adb.run("uninstall", CLIENT_PACKAGE, timeout=60)
        status, output = adb.run("install", apk, timeout=180)
        if status == 0 and "Success" in output:
            return
    raise SetupError(f"could not install the client: {output.strip()}")


def grant_client(adb):
    for permission in GRANTED_PERMISSIONS:
        adb.checked_shell(
            f"pm grant {CLIENT_PACKAGE} {permission}",
            f"grant Voxize Remote {permission}",
        )
    for operation in ALLOWED_OPERATIONS:
        adb.checked_shell(
            f"appops set {CLIENT_PACKAGE} {operation} allow",
            f"allow Voxize Remote {operation}",
        )


def pair_client(adb, console, control_path, server_name, port):
    """A pairing window on the server, the client asked to pair over adb, the code
    typed here."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        try:
            connection.connect(control_path)
        except OSError as error:
            raise SetupError(
                f"the Voxize Remote server is not reachable at {control_path} "
                f"({error}); is its service set up on this machine?"
            ) from None
        stream = connection.makefile("rwb", buffering=0)
        stream.write(b"pair\n")
        for line in stream:
            event = json.loads(line)
            kind = event.get("event")
            if kind == "busy":
                raise SetupError(
                    "another voxize-remote pair is already waiting on this machine"
                )
            if kind == "open":
                adb.shell(
                    f"am start -n {CLIENT_PACKAGE}/.MainActivity "
                    f"--es server {shlex.quote(server_name)} --ei port {int(port)} "
                    "--ez pair true"
                )
                console.say(
                    "Asked Voxize Remote to pair; it shows a six-digit code once it "
                    f"reaches {server_name}."
                )
            elif kind == "code_requested":
                console.say(f"{event['device']} ({event['node']}) asks to pair.")
                typed = console.ask("Type the code the phone shows: ")
                stream.write(f"{typed}\n".encode())
            elif kind == "paired":
                console.say(f"Paired {event['device']}.")
                return
            elif kind == "rejected":
                console.say(
                    f"Not paired: {event.get('reason', 'refused')}. "
                    "The phone can try again."
                )
            elif kind == "expired":
                raise SetupError(
                    "the pairing window closed before the client paired; Voxize "
                    "Remote's screen on the phone says why (most often Tailscale's "
                    "split tunneling leaves it out)"
                )
    raise SetupError("the Voxize Remote server closed the pairing")


def client_status(control_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(control_path)
        connection.sendall(b"status\n")
        reply = b""
        while chunk := connection.recv(65536):
            reply += chunk
    return json.loads(reply)


def check_connection(adb, console, control_path):
    """Arms the client as ``ssh cc`` will, and waits for it to connect; says what to
    fix when it does not."""
    adb.shell(f"am start -n {CLIENT_PACKAGE}/.ArmActivity")
    deadline = time.monotonic() + CONNECT_CHECK_SECONDS
    while time.monotonic() < deadline:
        try:
            if client_status(control_path).get("client"):
                console.say("The client is connected over Tailscale.")
                return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    console.say(
        "The client did not connect. Most likely Tailscale's split tunneling leaves "
        "Voxize Remote out:"
    )
    console.say(
        "in the Tailscale app, Settings → Split tunneling, include Voxize Remote. "
        "Its screen also says why."
    )
    return False


def command_text(command):
    """A failed command for a message: the tool's name, then its arguments without
    the device serial."""
    arguments = [str(part) for part in command[1:]]
    if arguments[:1] == ["-s"]:
        arguments = arguments[2:]
    return " ".join([os.path.basename(str(command[0])), *arguments])


def run(
    adb_executable,
    apk,
    control_path,
    server_name,
    port,
    serial=None,
    console=None,
    runner=subprocess.run,
):
    """The whole setup; True once the client is connected. SetupError says what went
    wrong."""
    console = console or Console()
    try:
        adb = Adb(adb_executable, runner=runner)
        adb = Adb(adb_executable, serial=serial or pick_device(adb), runner=runner)
        install_client(adb, apk, console)
        # A fresh start: the client forgets an earlier pairing (the server replaces its
        # own when this one completes).
        adb.shell(f"am force-stop {CLIENT_PACKAGE}")
        cleared = adb.checked_shell(
            f"pm clear {CLIENT_PACKAGE}", "clear Voxize Remote's earlier pairing"
        )
        if "Success" not in cleared:
            raise SetupError(
                "could not clear Voxize Remote's earlier pairing (pm clear did not "
                "succeed)"
            )
        grant_client(adb)
        console.say(
            "Installed, with the microphone, notifications, the floating button and "
            "Usage access allowed."
        )
        pair_client(adb, console, control_path, server_name, port)
        return check_connection(adb, console, control_path)
    except subprocess.TimeoutExpired as error:
        raise SetupError(
            f"{command_text(error.cmd)} did not finish in {error.timeout:.0f} seconds; "
            "is the phone showing a question on its screen?"
        ) from None
    except OSError as error:
        raise SetupError(str(error)) from None


def this_machines_name():
    """The name clients dial: this machine's MagicDNS name, or its Tailscale address."""
    from voxize.remote.server import tailscale_json

    try:
        own = tailscale_json("status")["Self"]
    except Exception as error:
        raise SetupError(
            f"cannot tell this machine's Tailscale name ({error}); pass --server"
        ) from None
    name = (own.get("DNSName") or "").rstrip(".")
    addresses = own.get("TailscaleIPs") or []
    if name or addresses:
        return name or addresses[0]
    raise SetupError("Tailscale reports no name for this machine; pass --server")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    from voxize.remote.server import DEFAULT_PORT

    parser.add_argument(
        "--serial", help="adb's serial for the device (default: the one connected)"
    )
    parser.add_argument(
        "--server",
        help="the name the client dials (default: this machine's Tailscale name)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--apk",
        default=os.environ.get(APK_VARIABLE),
        help=f"the client (default: ${APK_VARIABLE})",
    )
    parser.add_argument(
        "--adb", default="adb", help="the adb to use (default: adb on PATH)"
    )


def main(arguments: argparse.Namespace, control_path: str) -> int:
    console = Console()
    try:
        if not arguments.apk:
            raise SetupError(
                f"no client to install: pass --apk or set {APK_VARIABLE} (a release's "
                "voxize-remote-VERSION.apk)"
            )
        server_name = arguments.server or this_machines_name()
        connected = run(
            arguments.adb,
            arguments.apk,
            control_path,
            server_name,
            arguments.port,
            serial=arguments.serial,
            console=console,
        )
    except SetupError as error:
        print(f"voxize-remote setup-client: {error}", file=sys.stderr, flush=True)
        return 1
    return 0 if connected else 2
