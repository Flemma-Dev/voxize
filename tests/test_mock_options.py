from voxize.mock import MockOptions


def test_not_mock_without_the_env_var():
    assert MockOptions.from_env({}) is None


def test_defaults_match_the_old_module_constants():
    m = MockOptions.from_env({"VOXIZE_MOCK": "1"})
    assert m == MockOptions()
    assert (
        m.warming_ms,
        m.preview_delay_ms,
        m.cleanup_delay_ms,
        m.cleanup_tick_ms,
    ) == (
        1500,
        1500,
        2500,
        60,
    )
    assert m.ttfb_ms == 1200


def test_every_env_var_is_read():
    m = MockOptions.from_env(
        {
            "VOXIZE_MOCK": "1",
            "VOXIZE_MOCK_OUTCOME": "batch-fail",
            "VOXIZE_MOCK_SHORT": "1",
            "VOXIZE_ERROR": "300",
            "VOXIZE_STOP": "1600",
            "VOXIZE_MOCK_TTFB": "50",
            "VOXIZE_MOCK_FAST": "1",
        }
    )
    assert m == MockOptions(
        outcome="batch-fail",
        short=True,
        error_ms=300,
        stop_ms=1600,
        ttfb_ms=50,
        fast=True,
    )
    assert (
        m.warming_ms,
        m.preview_delay_ms,
        m.cleanup_delay_ms,
        m.cleanup_tick_ms,
    ) == (
        50,
        50,
        50,
        5,
    )
