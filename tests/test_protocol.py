import pytest

from voxize import protocol
from voxize.protocol import ProtocolError, State


def test_event_builds_a_typed_dict():
    assert protocol.event("state", state="RECORDING", error=None) == {
        "type": "state",
        "state": "RECORDING",
        "error": None,
    }


def test_event_without_fields():
    assert protocol.event("no_speech") == {"type": "no_speech"}


def test_unknown_event_type_is_rejected():
    with pytest.raises(ProtocolError):
        protocol.event("bogus")


def test_wrong_fields_are_rejected():
    with pytest.raises(ProtocolError):
        protocol.event("level")  # missing dbfs
    with pytest.raises(ProtocolError):
        protocol.event("level", dbfs=-20.0, extra=1)


def test_command_builds_a_typed_dict():
    assert protocol.command("close", trash_if_empty=True) == {
        "type": "close",
        "trash_if_empty": True,
    }
    with pytest.raises(ProtocolError):
        protocol.command("start", cwd="/tmp")  # missing glossaries


def test_encode_is_one_compact_line():
    line = protocol.encode(
        protocol.event("text", stage="live", op="append", text="é\n")
    )
    assert line.endswith("\n")
    assert line.count("\n") == 1  # the text's newline is escaped
    assert "é" in line  # non-ASCII kept raw, not escaped as \u00e9
    assert ": " not in line


def test_encode_rejects_nan():
    with pytest.raises(ValueError):
        protocol.encode(protocol.event("level", dbfs=float("nan")))


def test_decode_round_trips_str_and_bytes():
    msg = protocol.command("stop")
    assert protocol.decode(protocol.encode(msg)) == msg
    assert protocol.decode(protocol.encode(msg).encode()) == msg


def test_decode_rejects_non_objects_and_missing_type():
    for line in ("[1]", '"x"', '{"no": "type"}', '{"type": 3}', "not json"):
        with pytest.raises(ValueError):
            protocol.decode(line)


def test_decode_rejects_invalid_utf8_as_protocol_error():
    with pytest.raises(ProtocolError):
        protocol.decode(b"\xff")


def test_decode_ignores_unknown_fields():
    assert protocol.decode('{"type":"stop","later":1}') == {"type": "stop", "later": 1}


def test_state_is_shared_with_the_state_machine():
    from voxize.state import State as MachineState

    assert MachineState is State
    assert [s.name for s in State] == [
        "INITIALIZING",
        "WARMING",
        "RECORDING",
        "TRANSCRIBING",
        "CLEANING",
        "READY",
        "CANCELLED",
        "ERROR",
    ]
