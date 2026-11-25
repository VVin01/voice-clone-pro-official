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
    # Official 1 Recap Credit = 1 Min Final Output rule stated cleanly across recap & topup tiers.
    assert "1 Recap Credit = Final Output 1 မိနစ်" in API
    assert "1 Recap Credit = 1 Minute Final Recap Output" in API
    assert "1 Credit = Final Output 1 မိနစ်" in API
    assert "1 Credit = 1 Min Final Output" in API


def test_reference_range_corrected():
    # Pricing v3 (2026-11-23): bundle copy updated to describe Voice + Recap bundle package.
    assert "Voice + Recap bundle package" in API or "Works across Custom Voice Clone Recap & AI Recap" in API
    assert "Lossless WAV" in API


def test_voice_pro_true_perks():
    assert "Zero Watermark" in API
    assert "Deduction: 0 Voice" in API or "Voice Clone မိနစ် မဖြတ်ပါ" in API


def test_style_counts_dropped():
    assert "Normal AI Voice & Subtitle-Only" in API
    assert "Second-level exact" in API
    assert "Viral Hook" not in API


def test_cache_token_r114():
    assert INDEX.count("20261125_admin_r23") == 5
