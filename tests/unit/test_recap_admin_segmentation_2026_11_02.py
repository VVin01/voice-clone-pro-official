"""Admin segmented recap — owner ruling 2026-11-02.

The owner ruled: "admin က အားလုံး ကန့်သတ်ချက် လွတ်တယ်" — admin is
exempt from ALL limits, so a VIP-only feature gate must not exclude
admin from segmented recap. Before this fix, per-part segmentation
could never engage for an admin: ``_recap_should_segment()`` returned
False for admins outright, the worker's split block skipped admins at
the door, and the submit decision's "segment" verdict was unreachable
behind the 86400 s admin cap.

Pinned behaviour after the fix:

* admin + split ON + source over the standard recap cap (600 s)
  segments, exactly as for a VIP, using the admin's chosen part
  length (``split_minutes`` against the admin cap — unclamped);
* admin + split OFF, or under the trigger, stays a single job (the
  admin cap exemption as a LIMIT is unchanged: 86400 s);
* admin + split + script review still does NOT segment (script
  review edits one whole-video script — same constraint as VIP);
* admin is NEVER charged per-part top-ups: the worker skips
  ``_top_up_recap_segment_charge`` for admins (only ``parts_count``
  is recorded), so the job's credit bookkeeping stays at 0 and the
  segmented split_note claims no credit cost;
* VIP behaviour is byte-identical: same gate, same target clamp,
  same per-part charging; free/other users unchanged.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ── Worker gate: admin engages like a VIP ────────────────────────────

def test_should_segment_admin_matrix():
    from app.webapp.api import _recap_should_segment

    # Admin + split + over the standard cap → segments (the ruling).
    assert _recap_should_segment(
        is_vip=True, is_admin=True, split_video=True, duration_seconds=754.0
    ) is True
    # The admin flag alone suffices (the endpoint also passes
    # is_vip=True for admins, but the gate must not depend on it).
    assert _recap_should_segment(
        is_vip=False, is_admin=True, split_video=True, duration_seconds=754.0
    ) is True
    # Under the trigger → single job, exactly like a VIP under cap.
    assert _recap_should_segment(
        is_vip=True, is_admin=True, split_video=True, duration_seconds=500.0
    ) is False
    # Split OFF → never segments (admin's single-job exemption intact).
    assert _recap_should_segment(
        is_vip=True, is_admin=True, split_video=False, duration_seconds=754.0
    ) is False
    assert _recap_should_segment(
        is_vip=True, is_admin=True, split_video=False, duration_seconds=3600.0
    ) is False


def test_should_segment_vip_and_free_unchanged():
    from app.webapp.api import _recap_should_segment

    assert _recap_should_segment(
        is_vip=True, is_admin=False, split_video=True, duration_seconds=754.0
    ) is True
    assert _recap_should_segment(
        is_vip=True, is_admin=False, split_video=True, duration_seconds=500.0
    ) is False
    assert _recap_should_segment(
        is_vip=True, is_admin=False, split_video=False, duration_seconds=754.0
    ) is False
    assert _recap_should_segment(
        is_vip=False, is_admin=False, split_video=True, duration_seconds=754.0
    ) is False


# ── Submit decision mirrors the worker gate ──────────────────────────

def test_submit_decision_admin_matrix():
    from app.webapp.api import _recap_submit_duration_decision

    # Split ON + over the standard cap → "segment" (was unreachable:
    # the 86400 s admin cap made every admin source "ok").
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=True, split_video=True, script_review=False,
        duration_seconds=754.0,
    ) == "segment"
    # Split ON + under the trigger → single job.
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=True, split_video=True, script_review=False,
        duration_seconds=500.0,
    ) == "ok"
    # Split ON + script review → single job (script review edits ONE
    # whole-video script; the VIP constraint applies to admin too).
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=True, split_video=True, script_review=True,
        duration_seconds=754.0,
    ) == "ok"
    # Split OFF → single job at any length (limit exemption unchanged).
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=True, split_video=False, script_review=False,
        duration_seconds=3600.0,
    ) == "ok"
    # Even beyond the admin cap, split ON segments — never "reject".
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=True, split_video=True, script_review=False,
        duration_seconds=90000.0,
    ) == "segment"


def test_submit_decision_vip_and_free_unchanged():
    from app.webapp.api import _recap_submit_duration_decision

    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=False, split_video=True, script_review=False,
        duration_seconds=754.0,
    ) == "segment"
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=False, split_video=False, script_review=False,
        duration_seconds=754.0,
    ) == "reject"
    assert _recap_submit_duration_decision(
        is_vip=False, is_admin=False, split_video=True, script_review=False,
        duration_seconds=754.0,
    ) == "reject"


# ── Part length: the admin's chosen minutes, unclamped ───────────────

def test_admin_part_length_is_the_chosen_minutes():
    from app.webapp.api import (
        _recap_estimate_part_count,
        _recap_max_duration_allowed,
        _recap_segment_target_seconds,
    )

    admin_cap = _recap_max_duration_allowed(True, True)
    assert admin_cap == 86400  # limit exemption itself is unchanged
    # Against the admin cap the target IS the chosen part length…
    assert _recap_segment_target_seconds(5, admin_cap) == 300.0
    assert _recap_segment_target_seconds(3, admin_cap) == 180.0
    assert _recap_segment_target_seconds(10, admin_cap) == 600.0
    # …while the VIP clamp (cap − 30 s) is untouched.
    assert _recap_segment_target_seconds(10, 600) == 570.0
    # Estimate parity with the worker's admin parameters.
    assert _recap_estimate_part_count(754.0, 5.0, float(admin_cap)) == 3


# ── Admin is never charged: worker wiring contracts ──────────────────

def test_worker_admin_segmentation_wiring():
    api_py = _read("app/webapp/api.py")
    # The worker split block no longer excludes admins at the door.
    assert 'if phase == "full" and req.split_video:' in api_py
    assert "req.split_video and not is_admin_user" not in api_py
    # Inside the segmented block, the per-part top-up sits behind the
    # non-admin branch; the admin branch records only the part count.
    block = api_py.split("segmented_mode = len(segment_files) >= 2", 1)[1]
    block = block.split("canonical_source_url = local_source", 1)[0]
    assert "if is_admin_user:" in block
    assert block.index("if is_admin_user:") < block.index(
        "_top_up_recap_segment_charge(user_id, entry, total_parts)"
    )
    assert 'entry["parts_count"] = total_parts' in block
    # The segmented split_note must be able to state no charge was made
    # (admin) instead of the per-part credit line.
    assert "Credit မကောက်ခံထားပါ" in api_py


# ── Functional: the admin parameter set really cuts parts ────────────

def test_admin_parameters_cut_source_into_parts(tmp_path):
    """Gate-open admin parameters (target from the admin cap = the
    chosen 3-minute part length) cut a 12-minute source into parts —
    the same segmenter the worker calls, driven exactly as the
    worker drives it for an admin."""
    import shutil
    import subprocess

    import pytest

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    from app.services.recap_engine.core import probe_duration
    from app.services.recap_engine.video_splitter import (
        split_source_into_segments,
    )
    from app.webapp.api import (
        _recap_max_duration_allowed,
        _recap_segment_target_seconds,
        _recap_should_segment,
    )

    assert _recap_should_segment(
        is_vip=True, is_admin=True, split_video=True, duration_seconds=720.0
    ) is True
    target = _recap_segment_target_seconds(3, _recap_max_duration_allowed(True, True))
    assert target == 180.0

    src = tmp_path / "long_source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:r=2:d=720",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "720", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
        ],
        check=True, timeout=240,
    )
    ffprobe = shutil.which("ffprobe")
    segments = split_source_into_segments(
        str(src), tmp_path / "segments", part_seconds=target,
        ffmpeg="ffmpeg", ffprobe=ffprobe,
    )
    assert len(segments) == 4
    for seg in segments:
        assert Path(seg["path"]).is_file()
        assert probe_duration(ffprobe, Path(seg["path"])) <= 210.0
