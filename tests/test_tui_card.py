"""The TUI card model: the GTK window's presentation rules, without a toolkit."""

import pytest

from voxize.protocol import State
from voxize.tui import card as c
from voxize.tui.card import Card


def walk(card, *states):
    for s in states:
        card.on_state(s)
    return card


def recording():
    return walk(Card(), State.WARMING, State.RECORDING)


def test_initial_card():
    card = Card()
    assert (card.status, card.tone, card.dot) == ("Starting…", "idle", "○")
    assert card.strip is None and not card.timer_visible
    assert card.keys == [("Esc", "Cancel")]
    assert card.deliverable is None


def test_warming_shows_placeholder_level_and_dim_timer():
    card = walk(Card(), State.WARMING)
    assert (card.status, card.tone, card.dot) == ("Warming up…", "idle", "○")
    assert card.text == c.PLACEHOLDER and card.dimmed and not card.copyable
    assert card.strip == "level"
    assert card.timer_visible and card.timer_dim and card.timer_seconds == 0
    assert card.keys == [("Enter", "Stop"), ("Esc", "Cancel")]


def test_recording_listens_with_a_live_timer():
    card = recording()
    assert (card.status, card.tone, card.dot) == ("Listening", "recording", "●")
    assert card.timer_visible and not card.timer_dim
    assert card.text == c.PLACEHOLDER and card.dimmed


def test_live_text_replaces_the_placeholder_then_appends():
    card = recording()
    card.on_text("live", "replace", "Fix the")
    card.on_text("live", "append", " typo")
    assert card.text == "Fix the typo"
    assert not card.dimmed and card.copyable


def test_mock_mode_skips_warming_and_still_starts_clean():
    card = walk(Card(), State.RECORDING)
    assert card.text == c.PLACEHOLDER and card.timer_seconds == 0


def test_timer_ticks_only_while_recording():
    card = recording()
    card.tick()
    card.tick()
    assert card.timer_seconds == 2
    card.on_state(State.TRANSCRIBING)
    card.tick()
    assert card.timer_seconds == 2 and card.timer_dim


@pytest.mark.parametrize(
    ("dbfs", "zone"),
    [
        (-60.0, "quiet"),
        (-41.0, "quiet"),
        (-30.0, "good"),
        (-10.0, "hot"),
        (-2.0, "clip"),
    ],
)
def test_level_maps_dbfs_to_the_gui_zones(dbfs, zone):
    card = recording()
    card.on_level(dbfs)
    assert card.level == pytest.approx(c.dbfs_to_fraction(dbfs))
    assert card.zone == zone


def test_level_is_ignored_outside_capture_and_reset_after_it():
    card = recording()
    card.on_level(-10.0)
    card.on_state(State.TRANSCRIBING)
    assert (card.level, card.zone) == (0.0, "quiet")
    card.on_level(-10.0)
    assert card.level == 0.0


def test_transcribing_dims_the_live_text_and_shows_progress():
    card = recording()
    card.on_text("live", "replace", "fix the typo")
    card.on_state(State.TRANSCRIBING)
    assert (card.status, card.tone) == ("Working…", "working")
    assert card.dimmed and card.strip == "progress" and card.progress == 0.0
    assert card.keys == [("Esc", "Cancel")]
    card.on_text("live", "replace", "fix the typo")
    assert card.dimmed  # live is about to be superseded
    card.on_text("batch", "replace", "Fix the typo.")
    assert not card.dimmed and card.text == "Fix the typo."


def test_cleaning_dims_the_batch_until_cleaned_text_arrives():
    card = walk(recording(), State.TRANSCRIBING, State.CLEANING)
    card.on_text("batch", "replace", "fix it")
    assert card.dimmed
    card.on_text("cleaned", "replace", "Fix")
    card.on_text("cleaned", "append", " it.")
    assert card.text == "Fix it." and not card.dimmed


def test_progress_none_means_no_estimate_and_only_counts_while_working():
    card = recording()
    card.on_progress(0.5)
    assert card.progress == 0.0
    card.on_state(State.TRANSCRIBING)
    card.on_progress(None)
    assert card.progress is None
    card.on_progress(0.4)
    assert card.progress == 0.4


def test_stall_swaps_the_label_and_survives_into_cleaning():
    card = walk(recording(), State.TRANSCRIBING)
    card.on_stalled(True)
    assert card.status == "Still working…"
    card.on_state(State.CLEANING)
    assert card.status == "Still working…"
    card.on_stalled(False)
    assert card.status == "Working…"
    card.on_stalled(True)
    card.on_state(State.READY)
    assert not card.stalled and card.status == "Ready"


def test_ready_undims_copyable_text_and_offers_copy_and_exit():
    card = walk(recording(), State.TRANSCRIBING, State.CLEANING)
    card.on_text("cleaned", "replace", "Fix it.")
    card.on_state(State.READY)
    assert (card.status, card.tone) == ("Ready", "ready")
    assert not card.dimmed and card.strip is None and card.timer_dim
    assert card.deliverable == "Fix it."
    assert card.keys == [("Enter", "Copy & Exit"), ("C", "Copy"), ("Q", "Exit")]


def test_no_speech_leaves_nothing_to_deliver():
    card = walk(recording(), State.TRANSCRIBING)
    card.on_no_speech()
    card.on_state(State.READY)
    assert card.text == c.NO_SPEECH and card.dimmed and not card.copyable
    assert card.deliverable is None
    assert card.keys == [("Enter", "Exit"), ("Q", "Exit")]


def test_batch_failure_falls_back_to_the_live_preview():
    card = recording()
    card.on_text("live", "replace", "fix the typo")
    card.on_state(State.TRANSCRIBING)
    card.on_text("live", "replace", "fix the typo")
    card.on_failure("batch", "simulated", "live")
    card.on_state(State.READY)
    assert card.notice == c.NOTICE_BATCH_FAILED
    assert not card.dimmed and card.deliverable == "fix the typo"


def test_batch_failure_without_live_text():
    card = walk(recording(), State.TRANSCRIBING)
    card.on_failure("batch", "simulated", None)
    card.on_state(State.READY)
    assert card.text == c.NO_PREVIEW and card.dimmed
    assert card.notice == c.NOTICE_BATCH_FAILED_EMPTY
    assert card.deliverable is None


def test_cleanup_failure_shows_the_full_batch_transcript():
    card = walk(recording(), State.TRANSCRIBING, State.CLEANING)
    card.on_text("cleaned", "replace", "Fi")
    card.on_text("batch", "replace", "Fix it.")
    card.on_failure("cleanup", "simulated", "batch")
    card.on_state(State.READY)
    assert card.notice == c.NOTICE_CLEANUP_FAILED
    assert card.text == "Fix it." and not card.dimmed
    assert card.deliverable == "Fix it."


def test_preview_lost_while_recording_lasts_until_capture_ends():
    card = recording()
    card.on_preview_lost("ws closed")
    assert card.notice == c.NOTICE_PREVIEW_LOST and card.dimmed
    card.on_state(State.TRANSCRIBING)
    assert card.notice == ""


def test_preview_lost_while_warming_is_still_shown_at_recording():
    card = walk(Card(), State.WARMING)
    card.on_preview_lost("ws closed")
    assert card.notice == c.NOTICE_PREVIEW_LOST
    card.on_state(State.RECORDING)
    assert card.notice == c.NOTICE_PREVIEW_LOST and card.dimmed


def test_error_shows_the_message_and_nothing_else():
    card = Card()
    card.on_context([{"path": "/w/WHISPER.txt", "content": "Luup"}])
    assert card.vocab_visible
    card.on_state(State.ERROR, "Another Voxize instance is recording")
    assert (card.status, card.tone, card.dot) == ("Error", "error", "●")
    assert card.text == "Another Voxize instance is recording"
    assert not card.dimmed and not card.copyable and card.deliverable is None
    assert not card.timer_visible and card.strip is None and not card.vocab_visible
    assert card.keys == [("Enter", "Exit"), ("Q", "Exit")]


def test_costs_sum_the_known_parts():
    card = Card()
    card.on_costs(0.0001, None, 0.0003)
    assert card.cost == pytest.approx(0.0004)
    card.on_costs(None, None, None)
    assert card.cost is None


def test_cost_parts_keep_the_known_ones_in_order():
    card = Card()
    card.on_costs(0.0001, None, 0.0003)
    assert card.cost_parts == [("Live", 0.0001), ("Cleanup", 0.0003)]
    card.on_costs(0.0001, 0.0002, 0.0003)
    assert [label for label, _ in card.cost_parts] == ["Live", "Batch", "Cleanup"]
    card.on_costs(None, None, None)
    assert card.cost_parts == []


def test_costs_are_visible_only_at_ready_with_a_cost():
    card = recording()
    card.on_costs(0.0001, 0.0002, 0.0001)
    assert not card.costs_visible
    walk(card, State.TRANSCRIBING, State.CLEANING)
    assert not card.costs_visible
    card.on_state(State.READY)
    assert card.costs_visible
    card.on_costs(None, None, None)
    assert not card.costs_visible


def test_costs_key_follows_the_ready_keys_and_precedes_vocabulary():
    card = recording()
    card.on_text("live", "replace", "Fix it.")
    card.on_context([{"path": "/w/WHISPER.txt", "content": "Luup"}])
    card.on_costs(0.0001, 0.0002, 0.0001)
    assert ("$", "Costs") not in card.keys
    walk(card, State.TRANSCRIBING, State.CLEANING, State.READY)
    assert card.keys == [
        ("Enter", "Copy & Exit"),
        ("C", "Copy"),
        ("Q", "Exit"),
        ("$", "Costs"),
        ("V", "Vocabulary"),
    ]


def test_vocabulary_key_is_offered_when_sources_were_loaded():
    card = recording()
    card.on_context([{"path": "/w/WHISPER.txt", "content": "Luup"}])
    assert card.keys[-1] == ("V", "Vocabulary")


def test_clipboard_unavailable_notice():
    card = Card()
    card.set_clipboard_unavailable()
    assert card.clipboard_notice == c.NOTICE_NO_CLIPBOARD


@pytest.mark.parametrize(
    ("seconds", "shown"), [(0, "00:00"), (61, "01:01"), (3600, "1:00:00")]
)
def test_format_timer(seconds, shown):
    assert c.format_timer(seconds) == shown
