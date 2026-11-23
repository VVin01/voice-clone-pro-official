"""Plans (three categories), human-only curated voices, zero legacy-engine
references — release-plans-voices-2026-10-29.

These tests pin the three items of this branch so a later price or voice
edit cannot silently leave one surface (DB seed / API / Mini App / bot)
on the old list.
"""

import pytest

from app.services.curated_voices import (
    CURATED_VOICES,
    get_curated_voice,
    get_persona_reference,
    list_curated_voices,
)
from app.services.payments import PLAN_CATALOGUE, get_plan_by_code
from app.services.queue import create_voice_clone_job

# Pricing v3 (2026-11-23): 10 official plans across combo / voice / recap (Top-Up retired).
EXPECTED = {
    "pkg_starter": ("combo", 100, 15, 14900),
    "pkg_creator": ("combo", 200, 30, 28500),
    "pkg_pro": ("combo", 500, 60, 54900),
    "pkg_business": ("combo", 1000, 180, 114900),
    "voice_200": ("voice", 200, 0, 19800),
    "voice_500": ("voice", 500, 0, 39500),
    "voice_1000": ("voice", 1200, 0, 79200),
    "recap_30": ("recap", 0, 30, 13500),
    "recap_60": ("recap", 0, 60, 25500),
    "recap_180": ("recap", 0, 180, 68400),
}


def test_plan_catalogue_matches_final_table():
    assert {p["code"] for p in PLAN_CATALOGUE} == set(EXPECTED)
    for p in PLAN_CATALOGUE:
        cat, minutes, credits, price = EXPECTED[p["code"]]
        assert (p["category"], p["minutes"], p["recap_credits"], p["price_mmk"]) == (
            cat, minutes, credits, price,
        )
    assert get_plan_by_code("no_such_plan") is None


def test_plan_categories_are_three_and_per_unit_falls():
    by_cat = {}
    for p in PLAN_CATALOGUE:
        by_cat.setdefault(p["category"], []).append(p)
    assert set(by_cat) == {"voice", "recap", "combo"}
    # Voice per-minute falls as the pack grows.
    voice = sorted(
        [p for p in by_cat["voice"] if p["minutes"]],
        key=lambda p: p["minutes"],
    )
    per_min = [p["price_mmk"] / p["minutes"] for p in voice]
    assert per_min == sorted(per_min, reverse=True)
    # Recap per-credit falls as the tier grows.
    recap = sorted(by_cat["recap"], key=lambda p: p["recap_credits"])
    per_cr = [p["price_mmk"] / p["recap_credits"] for p in recap]
    assert per_cr == sorted(per_cr, reverse=True)
    # Combo creator (200m + 30c) is cheaper than voice_200 + recap_30 bought separately.
    creator = get_plan_by_code("pkg_creator")
    separate = get_plan_by_code("voice_200")["price_mmk"] + get_plan_by_code("recap_30")["price_mmk"]
    assert creator["price_mmk"] < separate


def test_curated_count_stays_twelve_and_all_human():
    assert len(CURATED_VOICES) == 12
    assert len(list_curated_voices("my")) == 12
    assert len(list_curated_voices("en")) == 12
    banned = ("သရဲ", "Spooky", "Anime", "👻", "ghost", "cartoon")
    for vid, v in CURATED_VOICES.items():
        shown = " ".join(
            str(v.get(k, ""))
            for k in ("name_my", "name_en", "desc_my", "desc_en", "badge_my", "badge_en")
        )
        assert not any(b in shown for b in banned), vid


def test_converted_voices_have_own_references_since_batch_bf():
    # Batch BF (2026-11-06) supersedes the 2026-10-29 remap pinned
    # here before: komyat and machaw keep their slot id + name but now
    # carry their OWN AI-designed VoxCPM2 voices — no reference_id,
    # and reference audio + transcript are each slot's own.
    komyat = get_curated_voice("komyat")
    assert "reference_id" not in komyat
    assert get_curated_voice("komin") is not None
    machaw = get_curated_voice("machaw")
    assert "reference_id" not in machaw
    assert get_curated_voice("mamay") is not None
    # Reference audio + transcript come from the slot itself now.
    path, text = get_persona_reference("komyat")
    k_path, k_text = get_persona_reference("komin")
    assert path != k_path and text != k_text
    assert path.endswith("komyat.wav")
    m_path, m_text = get_persona_reference("machaw")
    y_path, y_text = get_persona_reference("mamay")
    assert m_path != y_path and m_text != y_text
    assert m_path.endswith("machaw.wav")


@pytest.mark.asyncio
async def test_queue_rejects_any_non_voxcpm_engine(db_session):
    from app.services.ledger import get_or_create_user

    user, _ = await get_or_create_user(db_session, telegram_id=7001)
    job, reason = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text="hello",
        edge_voice="my-MM-ThihaNeural",
        engine="legacy",
    )
    assert job is None
    assert reason == "LEGACY_MODEL_ENGINE_REMOVED"
