"""Batch CT — plan copy + price-source fixes (from the 2026-11-08 audit).

The audit found: (1) the Mini App buy cards still carried the pre-tiered
per-movie claims CR was meant to kill ("5 recap videos, up to 10 min
each"); (2) the bot's recap buy button labelled credits as "videos";
(3) Mini App card/drawer prices were hardcoded literals that an admin
DB price edit would silently desync.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
I18N = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APPJS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")

STALE = [
    "Recap ဗီဒီယို ၅ ကား (၁ ကား အများဆုံး ၁၀ မိနစ်)",
    "Recap ဗီဒီယို ၂၅ ကား (၁ ကား အများဆုံး ၁၀ မိနစ်)",
    "မိနစ် ၃၀ + Recap ဗီဒီယို ၅ ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
    "မိနစ် ၅၀၀ + Recap ဗီဒီယို ၆၀ ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
    "5 recap videos (up to 10 min each)",
    "25 recap videos (up to 10 min each)",
    "30 minutes + 5 recap videos (up to 10 min each)",
    "500 minutes + 60 recap videos (up to 10 min each)",
]


def test_ct_no_stale_per_movie_claims_anywhere():
    for stale in STALE:
        assert stale not in I18N, stale
        assert stale not in INDEX, stale


def test_ct_alloc_strings_state_credits_and_tiered_rate():
    # Official 1 Credit = 1 Min Final Output model copies (API & UI).
    api_py = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
    assert "1 Recap Credit = 1 Minute Final Recap Output" in api_py
    assert "1 Recap Credit = Final Output 1 မိနစ်" in api_py


def test_ct_bot_recap_button_says_credits_not_videos():
    for lang in ("my", "en"):
        d = json.loads((ROOT / f"locales/{lang}/messages.json").read_text(encoding="utf-8"))
        btn = d["pay_package_recap_btn"]
        assert "Recap Credit" in btn
        assert "Videos" not in btn and "ကား" not in btn


def test_ct_price_spans_carry_plan_codes():
    # Official storefront renders all 22 DB-driven plans dynamically across 4 category grids.
    for eid in ("vip-sub-plans", "vip-grid-voice", "vip-grid-recap", "vip-grid-combo", "vip-grid-topup"):
        assert f'id="{eid}"' in INDEX
    for fn in ("syncPlanPrices", "switchPlanCategory"):
        assert fn in APPJS


def test_ct_appjs_syncs_prices_from_api():
    assert "function syncPlanPrices(data)" in APPJS
    assert "function refreshPlanPrices()" in APPJS or "async function refreshPlanPrices()" in APPJS
    # Drawer prefers a synced API price over the inline literal.
    assert "planPriceByCode[packageCode]" in APPJS
    # VIP tab entry triggers a re-sync.
    assert 'tabId === "tab-vip"' in APPJS
    # The drawer's own plans fetch also syncs (repaint on reprice).
    assert "syncPlanPrices(data)" in APPJS
