"""Batch CR — VIP plan copy refresh.

The /api/vip/plans presentation copy must match current product facts:
tiered recap credits (1 credit per 3-minute band), the 10-30s reference
sample range, no blanket 1080p claim, no style counts, no Auto-Mirror.
Prices/minutes/credits are NOT touched by CR (catalogue + DB own them).
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
INDEX = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")

STALE = [
    "၃-၃၀", "3-30s", "Full HD 1080p", "Clean 1080p", "Auto-Mirror",
    "ဗီဒီယို (၅) ကား", "ဗီဒီယို (၂၅) ကား", "ဗီဒီယို ၅ ကား", "ဗီဒီယို ၆၀ ကား",
    "ဇာတ်လမ်းစတိုင် ၆ မျိုး", "စာတန်းစတိုင် ၇ မျိုး",
    "5 Recap Videos", "60 Full HD Videos",
]


def test_no_stale_copy_anywhere_in_api():
    for frag in STALE:
        assert frag not in API, frag


def test_tiered_rate_stated_for_every_recap_plan():
    # my + en for starter/pro/combined/master allocations+features
    assert API.count("၃ မိနစ်ကို ၁ credit နှုန်း") >= 6
    assert API.count("1 credit per 3 minutes") >= 4
    assert "၁၀ မိနစ်ဆို ၄ credit" in API
    assert "a 10-minute video = 4 credits" in API


def test_reference_range_corrected():
    assert API.count("၁၀-၃၀ စက္ကန့်") >= 4
    assert "10-30s sample" in API


def test_voice_pro_true_perks():
    assert "TTS အသံ တစ်ရက် ၆၀ မိနစ်" in API
    assert "Up to 60 TTS minutes per day" in API


def test_style_counts_dropped():
    assert "ဇာတ်လမ်းစတိုင် အမျိုးမျိုး" in API
    assert "Multiple narration styles (Viral Hook, Drama, Comedy & more)" in API


def test_cache_token_r114():
    assert INDEX.count("20261104_r114") == 4
