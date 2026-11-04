import os

from voxize.gui import context


def test_slack_title_gives_channel_then_app_glossary(tmp_path):
    paths = context.glossary_paths(
        "Slack", "eng-platform (Channel) - Acme - notifications - Slack", str(tmp_path)
    )
    assert paths == [
        str(tmp_path / "Slack" / "eng-platform.txt"),
        str(tmp_path / "Slack.txt"),
    ]
    # Created empty so the user can find and fill them, as before.
    assert all(os.path.exists(p) for p in paths)


def test_app_without_extractor_gets_app_glossary_only(tmp_path):
    assert context.glossary_paths("org.gnome.Nautilus", "Home", str(tmp_path)) == [
        str(tmp_path / "org.gnome.Nautilus.txt")
    ]


def test_context_name_is_sanitized(tmp_path):
    paths = context.glossary_paths("Slack", "a/b (DM) - X - Slack", str(tmp_path))
    assert paths[0] == str(tmp_path / "Slack" / "a-b.txt")


def test_wm_class_is_sanitized(tmp_path):
    # "../Foo" → "..-Foo" → leading dots stripped → "-Foo"
    assert context.glossary_paths("../Foo", "", str(tmp_path)) == [
        str(tmp_path / "-Foo.txt")
    ]


def test_detect_context_without_a_focused_window(monkeypatch):
    monkeypatch.setattr(context, "_get_focused_window", lambda: None)
    assert context.detect_context() == (None, [])


def test_detect_context_resolves_cwd_and_glossaries(monkeypatch, tmp_path):
    monkeypatch.setattr(
        context,
        "_get_focused_window",
        lambda: {
            "pid": 42,
            "wm_class": "Slack",
            "title": "general (Channel) - X - Slack",
        },
    )
    monkeypatch.setattr(context, "_detect_cwd", lambda pid: "/work/proj")
    monkeypatch.setattr(context, "_prompts_dir", lambda: str(tmp_path))
    cwd, glossaries = context.detect_context()
    assert cwd == "/work/proj"
    assert glossaries == [
        str(tmp_path / "Slack" / "general.txt"),
        str(tmp_path / "Slack.txt"),
    ]
