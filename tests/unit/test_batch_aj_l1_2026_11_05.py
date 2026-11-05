"""Batch AJ / L1 — ASS override tags in cue text are escaped.

write_synced_ass_file interpolated cue text verbatim into Dialogue
lines. The .ass ships as a downloadable side file, and libass players
honour {...} override tags — a cue containing {\\pos(0,0)} or
{\\fs999} could reposition / resize / hide the subtitle in the
player. The writer now escapes backslashes and braces in CUE text
(the writer's own \\pos tag for explicit sub_x placement is
generated separately and must stay intact).
"""
from __future__ import annotations

from app.services.recap_engine.core import write_synced_ass_file


def _dialogue_lines(path):
    text = path.read_text(encoding="utf-8")
    return [l for l in text.splitlines() if l.startswith("Dialogue:")]


def test_ass_cue_override_tags_are_escaped(tmp_path):
    out = write_synced_ass_file(
        [(0.0, 3.0, r"hello {\pos(0,0)}world {\fs999}big")],
        tmp_path / "s.ass", 1280, 720,
    )
    (line,) = _dialogue_lines(out)
    assert r"{\pos(0,0)}" not in line
    assert r"{\fs999}" not in line
    # The cue text itself survives, in escaped form.
    assert r"\{" in line and r"\}" in line
    assert "hello" in line and "world" in line and "big" in line


def test_ass_cue_literal_backslash_sequences_are_escaped(tmp_path):
    out = write_synced_ass_file(
        [(0.0, 3.0, r"path C:\new and a fake \N break")],
        tmp_path / "s.ass", 1280, 720,
    )
    (line,) = _dialogue_lines(out)
    # A literal backslash from the cue must not reach the file as a
    # bare ASS escape introducer (\N = hard line break to libass).
    assert r"C:\\new" in line
    assert r"fake \\N break" in line


def test_ass_newlines_still_become_ass_breaks(tmp_path):
    out = write_synced_ass_file(
        [(0.0, 3.0, "line one\nline two")],
        tmp_path / "s.ass", 1280, 720, sub_lines=2,
    )
    (line,) = _dialogue_lines(out)
    assert r"line one\Nline two" in line


def test_ass_writer_pos_tag_survives_escaping(tmp_path):
    # Batch AG: an explicit sub_x generates the writer's OWN {\pos}
    # tag — escaping cue text must not mangle that tag.
    layout = {"pw": 1280, "ph": 720, "ox": 0, "oy": 0, "sw": 1280, "sh": 720}
    out = write_synced_ass_file(
        [(0.0, 3.0, "normal cue")],
        tmp_path / "s.ass", 1280, 720, sub_x=0.25, layout=layout,
    )
    (line,) = _dialogue_lines(out)
    assert r"{\pos(" in line
    assert "normal cue" in line
