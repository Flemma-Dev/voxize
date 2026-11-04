from voxize.backend.presenter import Transcript


def test_first_text_of_each_stage_replaces():
    t = Transcript()
    assert t.add("live", "Hello") == "replace"
    assert t.add("live", " world") == "append"
    assert t.add("batch", "Hello") == "replace"
    assert t.add("batch", ", world.") == "append"
    assert t.add("cleaned", "Hello, world.") == "replace"


def test_after_a_whole_replace_a_new_stage_replaces_and_the_same_stage_appends():
    t = Transcript()
    t.replace("batch", "Full batch transcript.")
    assert t.stage == "batch"
    assert t.add("batch", " More") == "append"
    assert t.add("cleaned", "Full") == "replace"


def test_only_live_text_is_kept_for_the_fallback():
    t = Transcript()
    t.add("live", "said ")
    t.add("live", "this")
    t.add("batch", "Said this.")
    assert t.live_text == "said this"
    assert t.batch_fallback() == "said this"


def test_blank_live_text_has_no_fallback():
    t = Transcript()
    assert t.batch_fallback() is None
    t.add("live", "   ")
    assert t.batch_fallback() is None


def test_had_content_is_sticky():
    t = Transcript()
    assert t.had_content is False
    t.add("live", "x")
    assert t.had_content is True


def test_no_speech_counts_as_content():
    # The old window read "had first text" to decide whether Ctrl+M trashes
    # the session; "No speech detected." set it.
    t = Transcript()
    t.mark_no_speech()
    assert t.had_content is True
