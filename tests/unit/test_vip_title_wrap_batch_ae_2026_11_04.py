"""Batch AE pins (2026-11-04): VIP tab title wrap on phones.

Owner order after the Batch AD mobile pass (screenshot verified
firsthand): at 390px the VIP pane title "💎 Creator VIP Plans &
ပိုက်ဆံအိတ်" shared a flex row with the PREMIUM badge, so the h2
wrapped with a single Burmese word ("အိတ်") stranded alone on line 2
while the badge floated mid-title — un-premium.

Fix (layout only, style.css): inside a max-width:480px media query the
VIP pane-title-row stacks — the title takes the full width and the
badge sits on its own line beneath it — and the h2 gets
text-wrap:balance so neither locale can strand an orphan word. The
rules are scoped to #tab-vip: the recap/admin pane rows and all wider
layouts keep their original side-by-side composition. Title text and
badge are unchanged in both locales. Static cache token r55 -> r57
(r56 is Batch AD's open PR #93; the merge process renumbers if needed).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")


def _ae_block() -> str:
    start = STYLE_CSS.index("Batch AE: VIP pane title stacking on phones")
    end = STYLE_CSS.index("/* ── Cards", start)
    return STYLE_CSS[start:end]


def _vip_pane_header() -> str:
    start = INDEX_HTML.index('<section id="tab-vip"')
    end = INDEX_HTML.index("<!-- User Stats Card", start)
    return INDEX_HTML[start:end]


def test_ae_media_query_stacks_vip_title_row_on_phones():
    block = _ae_block()
    assert "@media (max-width: 480px)" in block
    assert "#tab-vip .pane-title-row" in block
    assert "flex-direction: column" in block
    assert "align-items: flex-start" in block


def test_ae_balances_vip_title_wrap():
    block = _ae_block()
    assert "#tab-vip .pane-header h2" in block
    assert "text-wrap: balance" in block


def test_ae_rules_scoped_to_vip_only():
    block = _ae_block()
    # Every rule inside the AE block is scoped under #tab-vip, so the
    # recap and admin pane rows keep their original composition.
    selectors = re.findall(r"^\s{4}([^\s{][^{]*)\{", block, flags=re.M)
    assert selectors, "AE block should contain rules"
    assert all(s.strip().startswith("#tab-vip") for s in selectors)
    # The shared base row rule is untouched.
    assert ".pane-title-row { display: flex; align-items: center; gap: var(--s2); }" in STYLE_CSS


def test_vip_title_text_and_badge_unchanged():
    header = _vip_pane_header()
    assert '<h2 data-i18n="vip_title">💎 Creator VIP Plans &amp; ပိုက်ဆံအိတ်</h2>' in header
    assert header.count('<span class="badge-studio gold">PREMIUM</span>') == 1
    # Both locales keep the exact title strings.
    assert 'vip_title: "💎 Creator VIP Plans & ပိုက်ဆံအိတ်"' in I18N_JS
    assert 'vip_title: "💎 Creator VIP Plans & Wallet"' in I18N_JS


def test_cache_token_r57():
    assert INDEX_HTML.count("?v=20261104_r105") == 4
    assert "20261104_r55" not in INDEX_HTML
