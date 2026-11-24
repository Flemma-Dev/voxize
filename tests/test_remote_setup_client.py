"""voxize-remote setup-client end to end with fakes: a scripted adb, a scripted
console and a fake control socket."""

import json
import socket
import subprocess
import sys
import threading

import pytest

from voxize.remote import setup_client

SERIAL = "R5CW1234"
APK = "/releases/voxize-remote-0.4.1.apk"


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeDevice:
    """Answers adb the way a device would; records every call."""

    def __init__(
        self, ready=(SERIAL,), install_results=("Success",), failing=(), stuck=()
    ):
        self.ready = list(ready)
        self.install_results = list(install_results)
        # Shell commands (by prefix) that fail, adb subcommands that never finish.
        self.failing = failing
        self.stuck = stuck
        self.calls = []
        self.shell_commands = []

    def __call__(
        self, command, capture_output=True, text=True, timeout=None, check=False
    ):
        self.calls.append(command)
        arguments = command[1:]
        if arguments[:1] == ["-s"]:
            arguments = arguments[2:]
        if arguments[:1] and arguments[0] in self.stuck:
            raise subprocess.TimeoutExpired(command, timeout)
        if arguments == ["devices"]:
            lines = ["List of devices attached"] + [
                f"{serial}\tdevice" for serial in self.ready
            ]
            return Completed(stdout="\n".join(lines) + "\n")
        if arguments[:1] == ["install"]:
            result = self.install_results.pop(0) if self.install_results else "Success"
            return Completed(
                returncode=0 if result == "Success" else 1, stdout=result + "\n"
            )
        if arguments[:1] == ["shell"]:
            shell = arguments[1]
            self.shell_commands.append(shell)
            if any(shell.startswith(prefix) for prefix in self.failing):
                return Completed(
                    returncode=255, stdout="Exception occurred: SecurityException\n"
                )
            if shell.startswith("pm clear "):
                return Completed(stdout="Success\n")
        return Completed()


class FakeConsole:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.said = []
        self.asked = []

    def say(self, text=""):
        self.said.append(text)

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""


class FakeServer:
    """The control socket's pair and status commands, as voxize-remote serve speaks them."""

    def __init__(self, path, code="123 456", connects=True):
        self.code, self.connects = code, connects
        self.typed = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection, connection.makefile("rwb", buffering=0) as stream:
                command = stream.readline().strip()
                if command == b"status":
                    client = "samsung SM-A546B" if self.connects else None
                    stream.write(json.dumps({"client": client}).encode() + b"\n")
                elif command == b"pair":
                    stream.write(b'{"event": "open", "seconds": 120}\n')
                    stream.write(
                        b'{"event": "code_requested", "device": "samsung SM-A546B", "node": "a54"}\n'
                    )
                    typed = stream.readline().decode().strip()
                    self.typed.append(typed)
                    if typed.replace(" ", "") == self.code.replace(" ", ""):
                        stream.write(
                            b'{"event": "paired", "device_id": "abc", "device": "samsung SM-A546B"}\n'
                        )
                    else:
                        stream.write(b'{"event": "rejected", "reason": "wrong code"}\n')
                        stream.write(b'{"event": "expired"}\n')


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_client, "CONNECT_CHECK_SECONDS", 2)
    return str(tmp_path / "voxize-remote.sock")


def setup(device, console, control, serial=None):
    return setup_client.run(
        "/fake/adb",
        APK,
        control,
        "server.example.ts.net",
        7223,
        serial=serial,
        console=console,
        runner=device,
    )


def test_install_allow_pair_and_check(control):
    server = FakeServer(control)
    device, console = FakeDevice(), FakeConsole(answers=["123 456"])
    assert setup(device, console, control) is True
    assert server.typed == ["123 456"]
    assert ["/fake/adb", "-s", SERIAL, "install", "-r", APK] in device.calls
    shell = device.shell_commands
    for expected in (
        "pm clear dev.flemma.voxize.remote",
        "pm grant dev.flemma.voxize.remote android.permission.RECORD_AUDIO",
        "pm grant dev.flemma.voxize.remote android.permission.POST_NOTIFICATIONS",
        "appops set dev.flemma.voxize.remote SYSTEM_ALERT_WINDOW allow",
        "appops set dev.flemma.voxize.remote GET_USAGE_STATS allow",
    ):
        assert expected in shell
    # The earlier pairing is cleared before the grants (a clear would undo them).
    assert shell.index("pm clear dev.flemma.voxize.remote") < shell.index(
        "pm grant dev.flemma.voxize.remote android.permission.RECORD_AUDIO"
    )
    assert (
        "am start -n dev.flemma.voxize.remote/.MainActivity --es server "
        "server.example.ts.net --ei port 7223 --ez pair true"
    ) in shell
    assert "am start -n dev.flemma.voxize.remote/.ArmActivity" in shell
    assert "The client is connected over Tailscale." in console.said


@pytest.mark.parametrize(
    "failure, reason",
    [
        (
            "INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match",
            "was signed with another key",
        ),
        (
            "INSTALL_FAILED_VERSION_DOWNGRADE: Downgrade detected",
            "is newer than this one",
        ),
    ],
)
def test_a_client_only_an_uninstall_gets_past_is_replaced_without_asking(
    control, failure, reason
):
    # The client keeps nothing but its pairing, which setup redoes: a lost release key costs nothing.
    FakeServer(control)
    device = FakeDevice(install_results=[f"Failure [{failure}]", "Success"])
    console = FakeConsole(answers=["123 456"])
    assert setup(device, console, control) is True
    assert console.asked == ["Type the code the phone shows: "]
    assert f"The Voxize Remote on the phone {reason}: replacing it." in console.said
    uninstall = device.calls.index(
        ["/fake/adb", "-s", SERIAL, "uninstall", "dev.flemma.voxize.remote"]
    )
    assert device.calls[uninstall + 1] == ["/fake/adb", "-s", SERIAL, "install", APK]


def test_a_failed_install_says_why(control):
    device = FakeDevice(
        install_results=["Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]"]
    )
    with pytest.raises(setup_client.SetupError, match="INSUFFICIENT_STORAGE"):
        setup(device, FakeConsole(), control)


def test_a_refused_permission_stops_the_setup(control):
    device = FakeDevice(
        failing=("pm grant dev.flemma.voxize.remote android.permission.RECORD_AUDIO",)
    )
    with pytest.raises(setup_client.SetupError, match="RECORD_AUDIO"):
        setup(device, FakeConsole(), control)


def test_a_stuck_adb_command_is_a_clear_error(control):
    with pytest.raises(setup_client.SetupError) as raised:
        setup(FakeDevice(stuck=("install",)), FakeConsole(), control)
    message = str(raised.value)
    assert message.startswith("adb install -r ")
    assert "is the phone showing a question on its screen?" in message
    assert SERIAL not in message


def test_a_wrong_code_ends_in_a_clear_error(control):
    FakeServer(control)
    console = FakeConsole(answers=["000 000"])
    with pytest.raises(
        setup_client.SetupError, match="Voxize Remote's screen on the phone says why"
    ):
        setup(FakeDevice(), console, control)
    assert "Not paired: wrong code. The phone can try again." in console.said
    # Said as soon as the client is asked, so a client that never reaches the server is not two silent minutes.
    assert (
        "Asked Voxize Remote to pair; it shows a six-digit code once it reaches server.example.ts.net."
        in console.said
    )


def test_no_server_says_so(control):
    with pytest.raises(setup_client.SetupError, match="not reachable"):
        setup(FakeDevice(), FakeConsole(), control)


def test_no_connection_explains_the_split_tunnel(control):
    FakeServer(control, connects=False)
    console = FakeConsole(answers=["123 456"])
    assert setup(FakeDevice(), console, control) is False
    assert any("Split tunneling" in line for line in console.said)


@pytest.mark.parametrize(
    "ready, message", [((), "no device"), ((SERIAL, "R9OTHER"), "pass --serial")]
)
def test_the_device_must_be_one(control, ready, message):
    with pytest.raises(setup_client.SetupError, match=message):
        setup(FakeDevice(ready=ready), FakeConsole(), control)


def test_a_serial_picks_the_device(control):
    FakeServer(control)
    device = FakeDevice(ready=(SERIAL, "R9OTHER"))
    assert (
        setup(device, FakeConsole(answers=["123 456"]), control, serial="R9OTHER")
        is True
    )
    assert ["/fake/adb", "-s", "R9OTHER", "install", "-r", APK] in device.calls


def test_without_an_apk_the_command_says_where_to_get_one(control, capsys, monkeypatch):
    from voxize.remote.server import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "voxize-remote",
            "--control-socket",
            control,
            "setup-client",
            "--serial",
            SERIAL,
        ],
    )
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    assert "pass --apk or set VOXIZE_REMOTE_APK" in capsys.readouterr().err
