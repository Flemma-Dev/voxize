from voxize.prompt import PromptSource, build_transcription_prompt, load_sources


def test_whisper_from_cwd_comes_first_and_is_collapsed(tmp_path):
    (tmp_path / "WHISPER.txt").write_text("Glossary:\n  Supabase,\nLuup\n")
    g = tmp_path / "Slack.txt"
    g.write_text("  sev-2, Kubo  \n")
    sources = load_sources(str(tmp_path), [str(g)])
    assert sources == [
        PromptSource(
            path=str(tmp_path / "WHISPER.txt"), content="Glossary: Supabase, Luup"
        ),
        PromptSource(path=str(g), content="sev-2, Kubo"),
    ]


def test_glossary_order_is_kept_and_missing_or_empty_skipped(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("A")
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n")
    b = tmp_path / "b.txt"
    b.write_text("B")
    paths = [str(b), str(tmp_path / "missing.txt"), str(empty), str(a)]
    assert [s.content for s in load_sources(None, paths)] == ["B", "A"]


def test_no_cwd_no_glossaries(tmp_path):
    assert load_sources(None, []) == []
    assert load_sources(str(tmp_path), []) == []  # no WHISPER.txt there


def test_sources_feed_the_single_prompt_builder(tmp_path):
    (tmp_path / "WHISPER.txt").write_text("One")
    g = tmp_path / "g.txt"
    g.write_text("Two")
    assert (
        build_transcription_prompt(load_sources(str(tmp_path), [str(g)])) == "One\nTwo"
    )
