"""Full-app polish batch (branch fix/full-app-polish, 2026-11-01).

Driven by the full-app audit (full-app-audit-2026-11-01/INVENTORY.md)
and the owner's nav decision of the same evening:

- NAV: Downloader + History leave the bottom dock (user 4 tabs, admin
  5) and move into a Home "Tools / Library" card; the panes, deep
  links and in-studio entry points are unchanged.
- A-6: Admin → Users gains server-side category filter chips
  (All / Free / Paid 👑), a 👑 VIP badge on PURCHASED rows, offset
  "Load more" paging, and a tappable Overview VIP stat card.
- A-1/A-2: admin sub-nav + queue status filters wrap instead of
  clipping. A-3/A-4: user rows become two lines with never-truncated
  badges. A-5: shorter search placeholder.
- H-1/H-2 Refer pills + wrapping link; V-1 speed label; V-2 Custom
  Voice Burmese strings; V-3 preview button / balanced descriptions;
  R-2 Micro-Zoom pills; G-1 nav scrim; G-3 heading tracking;
  D-1 downloader meta separators; Y-1/Y-2 History row wrapping.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import User
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")

# The polish rules live in the appended final section of style.css.
POLISH_CSS = CSS.split("FULL-APP POLISH (2026-11-01)", 1)[1]

ADMIN_ID = 999001


def _translations() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = """
global.localStorage = { getItem: () => null, setItem: () => {} };
global.document = { querySelectorAll: () => [], documentElement: {}, body: { classList: { toggle: () => {} } }, getElementById: () => null };
global.window = {};
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const translations = eval(src + '; translations;');
console.log(JSON.stringify(translations));
"""
    out = subprocess.run([node, "-e", script, str(STATIC / "js" / "i18n.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _pane(html: str, pane_id: str) -> str:
    return html.split(f'id="{pane_id}"', 1)[1].split('<section id="tab-', 1)[0]


def _fn(js: str, name: str) -> str:
    """Rough body slice of a top-level function for behaviour pins."""
    start = js.index(f"function {name}")
    nxt = js.find("\nfunction ", start + 10)
    return js[start:nxt if nxt != -1 else len(js)]


# ── 1 · Nav restructure ──────────────────────────────────────────────

def test_nav_has_four_user_tabs_plus_admin():
    nav = HTML.split('<nav class="bottom-nav">', 1)[1].split("</nav>", 1)[0]
    order = re.findall(r'data-tab="(tab-[a-z]+)"', nav)
    assert order == ["tab-home", "tab-voice", "tab-recap", "tab-vip", "tab-admin"]


def test_downloader_and_history_panes_survive():
    assert '<section id="tab-downloader" class="tab-pane">' in HTML
    assert '<section id="tab-history" class="tab-pane">' in HTML


def test_home_tools_library_card_entries():
    home = _pane(HTML, "tab-home")
    assert 'id="home-tools-card"' in home
    card = home.split('id="home-tools-card"', 1)[1].split("</section>", 1)[0]
    assert "switchTab('tab-downloader')" in card
    assert 'onclick="openHistoryHub()"' in card
    # the same History entry point the studios use
    assert HTML.count('onclick="openHistoryHub()"') >= 3


def test_switchtab_lights_home_for_dockless_panes():
    body = _fn(APP_JS, "switchTab")
    assert 'nav-item[data-tab="tab-home"]' in body


def test_home_tools_i18n_keys_both_locales():
    tr = _translations()
    for key in ("home_tools_title", "home_tools_dl_name", "home_tools_dl_desc",
                "home_tools_history_name", "home_tools_history_desc"):
        assert tr["my"][key] and tr["en"][key], key
    assert tr["my"]["home_tools_title"] == "🧰 ကိရိယာ နှင့် မှတ်တမ်း"
    assert tr["en"]["home_tools_title"] == "🧰 Tools & Library"


# ── 2 · A-6 paid users ───────────────────────────────────────────────

def test_users_filter_chips_markup():
    chips = HTML.split('id="adm-users-filter-chips"', 1)[1].split("</div>", 1)[0]
    cats = re.findall(r'data-cat="([A-Z]*)"', chips)
    assert cats == ["", "FREE", "PURCHASED"]
    assert chips.count("setAdminUsersCategory(") == 3
    assert 'id="adm-users-load-more"' in HTML
    assert 'onclick="loadMoreAdminUsers()"' in HTML


def test_vip_stat_card_opens_paid_users():
    seg = HTML[HTML.index('id="adm-vip-users"') - 400:HTML.index('id="adm-vip-users"')]
    assert "adm-tap" in seg and 'onclick="openAdminUsersPaid()"' in seg
    body = _fn(APP_JS, "openAdminUsersPaid")
    assert 'admUsersCategory = "PURCHASED"' in body
    assert 'showAdminSection("users")' in body


def test_users_list_is_server_filtered_and_paged():
    assert "admUsersCategoryParam" in APP_JS
    assert "&category=${encodeURIComponent(admUsersCategory)}" in APP_JS
    body = _fn(APP_JS, "loadAdminUsersRecent")
    assert "offset=${admUsersOffset}" in body
    assert "data.total" in body  # Load-more end detection
    search = _fn(APP_JS, "adminSearchUsers")
    assert "admUsersCategoryParam()" in search


def test_user_row_two_lines_with_vip_badge():
    body = _fn(APP_JS, "admUserRowHtml")
    assert "adm-user-line1" in body
    assert '<span class="badge-vip">👑 VIP</span>' in body
    assert 'u.account_category === "PURCHASED"' in body
    # username moved to line 2; grapheme-safe avatar initial
    assert '(@${escapeHtml(u.username)})' not in body
    assert "Array.from((u.first_name" in body
    rule = POLISH_CSS.split(".adm-user-line1 .badge-status {", 1)[1].split("}", 1)[0]
    assert "flex: none" in rule
    vip = POLISH_CSS.split(".badge-vip {", 1)[1].split("}", 1)[0]
    assert "flex: none" in vip and "white-space: nowrap" in vip


def test_users_filter_i18n_keys():
    tr = _translations()
    assert tr["my"]["adm_users_filter_free"] == "အခမဲ့"
    assert tr["my"]["adm_users_filter_paid"] == "ငွေပေးပြီး 👑"
    assert tr["en"]["adm_users_filter_paid"] == "Paid 👑"
    assert tr["my"]["adm_users_load_more"] and tr["en"]["adm_users_load_more"]


# ── 3–5 · Admin strips, rows, placeholder ────────────────────────────

def test_adm_nav_and_queue_filters_wrap():
    nav_rule = POLISH_CSS.split(".adm-nav {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in nav_rule and "mask-image: none" in nav_rule
    queue_rule = POLISH_CSS.split("#queue-status-filters {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in queue_rule and "mask-image: none" in queue_rule


def test_search_placeholder_shortened():
    tr = _translations()
    assert tr["my"]["admin_user_search_ph"] == "ID / @username / နာမည်"
    assert tr["en"]["admin_user_search_ph"] == "ID / @username / name"
    assert 'placeholder="ID / @username / နာမည်"' in HTML


# ── 6 · Refer card ───────────────────────────────────────────────────

def test_refer_actions_are_wide_pills_and_link_wraps():
    assert 'class="ref-actions"' in HTML
    row = POLISH_CSS.split(".ref-link-text {", 1)[1].split("}", 1)[0]
    assert "word-break: break-all" in row and "white-space: normal" in row
    actions = POLISH_CSS.split(".ref-actions .mini-chip {", 1)[1].split("}", 1)[0]
    assert "flex: 1" in actions and "white-space: nowrap" in actions


# ── 7 · Voice fixes ──────────────────────────────────────────────────

def test_speed_label_never_wraps_mid_word():
    rule = POLISH_CSS.split(".control-row .ctrl-title {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in rule and "flex: none" in rule
    tr = _translations()
    assert f'data-i18n="speed_label">{tr["my"]["speed_label"]}<' in HTML


def test_custom_voice_strings_fully_burmese():
    tr = _translations()
    assert tr["my"]["custom_ref_title"] == "🎙️ အသံနမူနာ (အသံတူ ဖန်တီးရန်)"
    assert tr["my"]["custom_ref_optional"] == "ရွေးချယ်ခွင့်"
    assert tr["my"]["ref_file_btn"] == "📁 အသံနမူနာ ဖိုင် ရွေးချယ်မည်"
    # HTML fallbacks render the same Burmese before i18n applies
    assert f'data-i18n="custom_ref_title">{tr["my"]["custom_ref_title"]}<' in HTML
    assert f'data-i18n="ref_file_btn">{tr["my"]["ref_file_btn"]}<' in HTML
    # English locale untouched
    assert "Reference Voice" in tr["en"]["custom_ref_title"]


def test_persona_preview_button_single_line_and_balanced_desc():
    label = POLISH_CSS.split(".voice-preview-btn .preview-btn-label {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in label
    desc = POLISH_CSS.split(".persona-desc {", 1)[1].split("}", 1)[0]
    assert "text-wrap: balance" in desc


# ── 8 · R-2 / G-1 / G-3 / D-1 / Y-1 / Y-2 ────────────────────────────

def test_micro_zoom_pills_one_line():
    rule = POLISH_CSS.split("#recap-zoom-picker .pill-btn {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in rule


def test_nav_scrim_and_stronger_glass():
    assert 'class="nav-scrim"' in HTML
    assert "rgba(255, 255, 255, 0.97)" in POLISH_CSS
    assert ".nav-scrim {" in POLISH_CSS


def test_heading_tracking_tightened():
    assert "letter-spacing: -0.2px" in POLISH_CSS


def test_downloader_meta_separators_only_between_parts():
    meta = HTML.split('class="dl-meta-row"', 1)[1].split("</div>", 1)[0]
    assert "<span>•</span>" not in meta
    assert ".dl-meta-row > span:empty { display: none; }" in POLISH_CSS
    assert 'span:not(:empty)::before' in POLISH_CSS


def test_history_rows_wrap():
    title = POLISH_CSS.split(".hub-row-title {", 1)[1].split("}", 1)[0]
    assert "-webkit-line-clamp: 3" in title
    meta = POLISH_CSS.split(".hub-row-meta {", 1)[1].split("}", 1)[0]
    assert "white-space: normal" in meta


# ── Server: category param behaviour ─────────────────────────────────

@pytest.fixture(autouse=True)
def _admin_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    yield


def _user(uid: int, **kw) -> User:
    return User(
        id=uid,
        first_name=kw.pop("first_name", "Test"),
        username=kw.pop("username", None),
        account_category=kw.pop("account_category", AccountCategory.FREE.value),
        language_code="my",
        **kw,
    )


async def _seed(db, *objs):
    for o in objs:
        db.add(o)
    await db.commit()


@pytest.mark.asyncio
async def test_list_users_category_filter_and_total(db_session):
    admin = _user(ADMIN_ID, created_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    free1 = _user(700111, created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    free2 = _user(700222, created_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
    paid = _user(700333, account_category=AccountCategory.PURCHASED.value,
                 created_at=datetime(2026, 9, 3, tzinfo=timezone.utc))
    await _seed(db_session, admin, free1, free2, paid)

    all_res = await api_mod.list_admin_users(30, 0, admin, db_session)
    assert all_res["total"] == 4 and all_res["count"] == 4

    paid_res = await api_mod.list_admin_users(30, 0, admin, db_session, category="PURCHASED")
    assert [u["id"] for u in paid_res["users"]] == [700333]
    assert paid_res["total"] == 1

    free_res = await api_mod.list_admin_users(30, 0, admin, db_session, category="free")
    assert {u["id"] for u in free_res["users"]} == {ADMIN_ID, 700111, 700222}
    assert free_res["total"] == 3

    # Unknown values never blank the list — they mean "no filter".
    junk = await api_mod.list_admin_users(30, 0, admin, db_session, category="GOLD")
    assert junk["total"] == 4


@pytest.mark.asyncio
async def test_search_users_respects_category(db_session):
    admin = _user(ADMIN_ID)
    free = _user(700111, first_name="Aung Free", username="aungfree")
    paid = _user(700333, first_name="Aung Paid", username="aungpaid",
                 account_category=AccountCategory.PURCHASED.value)
    await _seed(db_session, admin, free, paid)

    both = await api_mod.search_admin_users("aung", 20, admin, db_session)
    assert {u["id"] for u in both["users"]} == {700111, 700333}

    only_paid = await api_mod.search_admin_users("aung", 20, admin, db_session,
                                                 category="PURCHASED")
    assert [u["id"] for u in only_paid["users"]] == [700333]
