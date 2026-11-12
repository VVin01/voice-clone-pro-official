"""Batch BF (2026-11-06): ship the owner-approved persona voices.

Eight personas get new reference voices, all designed/cloned and
approved by the owner on 2026-11-06 (build log:
~/workspace/new-voices-2026-11-06/NOTES.md):

BF-1  Six AI-designed VoxCPM voices (kozayar, komyat, kophone,
      mayoon, machaw, kothura): designed reference WAV + transcript
      sidecar (the fixed design passage) installed in BOTH reference
      locations; new sample MP3s installed.
BF-2  The 2026-10-29 reference_id remaps are removed: komyat and
      machaw carry their OWN references now (they borrowed komin's /
      mamay's before). komyat therefore takes the standard male
      studio mastering chain instead of komin's flagship exemption.
BF-3  mahnin (flagship): gains a PERSONA_REFERENCE_SOURCES entry
      pointing at a pause-edited cut of her own flagship reference
      (her sample mp3 previously doubled as the reference). komin is
      untouched and still resolves to his preview sample.
BF-4  koaung keeps his Google FLEURS reference + CC-BY 4.0
      attribution, tempo-adjusted x1.35; his transcript sidecar is
      unchanged (the audio still speaks the FLEURS passage).
BF-5  No persona's sample_text / control_instruction / cfg /
      af_filter / pitch / rate / formant changed.
BF-6  Two owner verdicts landed after the initial BF build and are
      folded into the same batch: komyat's approved sample and his
      design reference were pause-compressed (silences only, voiced
      audio untouched — sample 8.37 s -> 6.00 s, reference
      17.34 s -> 15.14 s), and kothura's FLEURS voice was replaced
      outright by a NEW AI-designed voice (v5 candidate A, bright
      news anchor; earlier v5 rounds and the v4 design were rejected
      by the owner). kothura therefore joins the AI-designed set and
      leaves the corpus-attributed personas.
"""
from __future__ import annotations

import wave
from pathlib import Path

from app.services import curated_voices as cv
from app.services.curated_voices import (
    CURATED_VOICES,
    PERSONA_REFERENCE_SOURCES,
    get_persona_reference,
)

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")

APP_DIR = ROOT / "app"
REFS_DIRS = (
    APP_DIR / "assets" / "audio" / "references",
    APP_DIR / "webapp" / "static" / "audio" / "references",
)
SAMPLES_DIR = APP_DIR / "webapp" / "static" / "audio" / "samples"

AI_DESIGNED = ("kozayar", "komyat", "kophone", "mayoon", "machaw", "kothura")
EIGHT = AI_DESIGNED + ("mahnin", "koaung")

# The fixed Burmese passage every AI-designed reference clip speaks
# (the design + prompt text of the 2026-11-06 build).
DESIGN_PASSAGE = 'မင်္ဂလာပါ။ နေ့စဉ် ပြောဆိုနေတဲ့ စကားသံတွေထဲမှာ ခံစားချက် အမျိုးမျိုး ပါဝင်နေပါတယ်။ တစ်ခါတစ်ရံ နွေးထွေးစွာ ပြောတတ်ပြီး တစ်ခါတစ်ရံ ရှင်းလင်းပြတ်သားစွာ ရှင်းပြတတ်ပါတယ်။ အသံတစ်ခုရဲ့ တန်ဖိုးကို နားထောင်သူရဲ့ ရင်ထဲ ဘယ်လောက် ထိရောက်စေနိုင်သလဲ ဆိုတာနဲ့ တိုင်းတာရမှာ ဖြစ်ပါတယ်။'


# ── Cache token ─────────────────────────────────────────────────────

def test_bf_cache_token_is_r79():
    assert HTML.count("20261104_r139") == 4
    assert "20261104_r75" not in HTML


# ── BF-1/BF-3/BF-4: reference-source bookkeeping ────────────────────

def test_bf_reference_sources_cover_the_eight():
    for pid in AI_DESIGNED:
        entry = PERSONA_REFERENCE_SOURCES[pid]
        assert entry["dataset"] == "AI-designed voice (VoxCPM), 2026-11-06", pid
        assert "AI-generated" in entry["license"], pid
    mahnin = PERSONA_REFERENCE_SOURCES["mahnin"]
    assert "pause-edited" in mahnin["dataset"]
    koaung = PERSONA_REFERENCE_SOURCES["koaung"]
    assert koaung["dataset"] == "Google FLEURS my_mm"
    assert koaung["license"] == "CC-BY 4.0"
    assert "tempo-adjusted" in koaung["note"]


def test_bf_dataset_personas_and_komin_unchanged_in_sources():
    # Only the SLR80 trio keeps a corpus attribution now (kothura
    # moved to the AI-designed set in BF-6; komin has no entry).
    for pid in ("manadi", "mamay", "mathiri"):
        assert PERSONA_REFERENCE_SOURCES[pid] == {
            "dataset": "OpenSLR SLR80 (Burmese Speech Corpus)",
            "license": "CC-BY-SA 4.0"}, pid
    assert "komin" not in PERSONA_REFERENCE_SOURCES


# ── BF-2: remaps removed, own references resolve ────────────────────

def test_bf_no_reference_id_remaps_remain():
    for pid, info in CURATED_VOICES.items():
        assert "reference_id" not in info, pid


def test_bf_design_references_resolve_with_passage_transcript():
    for pid in AI_DESIGNED:
        path, transcript = get_persona_reference(pid)
        assert path is not None and path.endswith(f"{pid}.wav"), pid
        assert transcript == DESIGN_PASSAGE, pid


def test_bf_mahnin_resolves_to_pause_edited_wav_komin_untouched():
    path, transcript = get_persona_reference("mahnin")
    assert path is not None and path.endswith("mahnin.wav")
    assert transcript == CURATED_VOICES["mahnin"]["sample_text"]
    # komin keeps the flagship fallback: preview sample + sample_text.
    k_path, k_text = get_persona_reference("komin")
    assert k_path is not None and k_path.endswith("komin.mp3")
    assert k_text == CURATED_VOICES["komin"]["sample_text"]


def test_bf_koaung_reference_resolves_with_fleurs_transcript():
    path, transcript = get_persona_reference("koaung")
    assert path is not None and path.endswith("koaung.wav")
    # The (tempo-adjusted) audio still speaks the FLEURS passage, so
    # the sidecar keeps that transcript — NOT his sample_text.
    assert "စာကြည့်တိုက်" in transcript
    assert transcript != CURATED_VOICES["koaung"]["sample_text"]


def test_bf_untouched_dataset_references_still_resolve():
    for pid in ("manadi", "mamay", "mathiri"):
        path, transcript = get_persona_reference(pid)
        assert path is not None and path.endswith(f"{pid}.wav"), pid
        assert transcript, pid


# ── Installed files: both locations, production format ──────────────

def test_bf_reference_files_both_locations_and_format():
    for pid in EIGHT:
        blobs = []
        for refs_dir in REFS_DIRS:
            wav_path = refs_dir / f"{pid}.wav"
            txt_path = refs_dir / f"{pid}.txt"
            assert wav_path.is_file() and wav_path.stat().st_size > 1000, wav_path
            assert txt_path.is_file() and txt_path.read_text(encoding="utf-8").strip(), txt_path
            blobs.append((wav_path.read_bytes(), txt_path.read_bytes()))
        assert blobs[0] == blobs[1], f"{pid}: the two reference locations drifted"
        wav_path = REFS_DIRS[0] / f"{pid}.wav"
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getframerate() == 44100, pid
            assert wf.getnchannels() == 1, pid
            assert wf.getsampwidth() == 2, pid
            assert wf.getnframes() / wf.getframerate() >= 5.0, pid


def test_bf_sample_mp3s_installed():
    for pid in EIGHT:
        mp3 = SAMPLES_DIR / f"{pid}.mp3"
        assert mp3.is_file() and mp3.stat().st_size > 10000, pid
        assert mp3.read_bytes()[:4] != b"RIFF", pid


# ── BF-5: persona parameters untouched; mastering consequence ───────

def test_bf_persona_parameters_unchanged():
    expected = {
        # pid: (cfg, pitch, rate_offset, formant_ratio)
        "kozayar": (2.2, "-3Hz", 8, 0.96),
        "komyat": (2.0, "-3Hz", 10, 0.96),
        "kophone": (2.0, "-3Hz", 13, 0.96),
        "mayoon": (1.8, "+2Hz", 7, 1.025),
        "machaw": (2.0, "+2Hz", 12, 1.025),
        "mahnin": (2.0, "+2Hz", 9, 1.025),
        "koaung": (2.0, "-3Hz", 16, 0.96),
        "kothura": (2.1, "-3Hz", 12, 0.96),
    }
    for pid, (cfg, pitch, rate_offset, formant) in expected.items():
        info = CURATED_VOICES[pid]
        assert info["cfg_value"] == cfg, pid
        assert info["pitch"] == pitch, pid
        assert info["rate_offset"] == rate_offset, pid
        assert info["formant_ratio"] == formant, pid
    assert CURATED_VOICES["komyat"]["sample_text"] == 'မင်္ဂလာပါခင်ဗျာ။ ကျွန်တော် ကိုမြတ်ပါ။ စိတ်ဝင်စားဖွယ် ဇာတ်လမ်းများကို ရှင်းလင်းနွေးထွေးစွာ ပြောပြပေးသွားပါ့မယ်။'
    assert CURATED_VOICES["machaw"]["sample_text"] == 'မင်္ဂလာပါရှင်။ ကျွန်မ မချောပါ။ ဗီဒီယိုများနှင့် လမ်းညွှန်များကို ဖော်ရွေရှင်းလင်းစွာ ပြောပြပေးသွားပါ့မယ်။'
    assert CURATED_VOICES["mahnin"]["sample_text"] == 'မင်္ဂလာပါရှင်။ နှင်းနှင်း ဖြစ်ပါတယ်။ ချိုသာနူးညံ့တဲ့ အသံလေးနဲ့ ဝတ္ထုဇာတ်လမ်းလေးတွေ ဖတ်ပြပေးပါ့မယ်နော်။'


def test_bf6_post_build_owner_verdicts():
    # komyat v2 (pause-only): the installed reference is the
    # pause-edited design cut (15.14 s), not the original 17.34 s
    # design take; the installed sample is the pause-edited master
    # (6.00 s / 97,010 bytes), not the 8.37 s / 135,044-byte v1.
    ref = REFS_DIRS[0] / "komyat.wav"
    with wave.open(str(ref), "rb") as wf:
        seconds = wf.getnframes() / wf.getframerate()
    assert 14.0 < seconds < 16.0, seconds
    sample_size = (SAMPLES_DIR / "komyat.mp3").stat().st_size
    assert 90_000 < sample_size < 110_000, sample_size
    # kothura v5 A: he now has an assets-location reference like the
    # other AI-designed personas (before BF-6 he resolved from the
    # legacy static directory only) and speaks the design passage.
    path, transcript = get_persona_reference("kothura")
    assert path is not None and "assets" in Path(path).parts, path
    assert transcript == DESIGN_PASSAGE


def test_bf_komyat_and_machaw_mastering_now_their_own():
    # With the remap gone, komyat takes the standard male studio
    # chain (previously None via komin's flagship exemption) and
    # machaw the standard female chain.
    assert cv.persona_mastering_filter("komyat") is not None
    assert cv.persona_mastering_filter("komyat") == cv.build_studio_mastering_filter("male", 0.96)
    assert cv.persona_mastering_filter("machaw") == cv.build_studio_mastering_filter("female", 1.025)
