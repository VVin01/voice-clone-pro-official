"""i18n completion (batch F, 2026-11-03) — symmetry & no-hardcoded guards.

Before this batch, three classes of user-visible strings bypassed the
translation system entirely:

- ~30 hardcoded Myanmar literals in app.js (bilingual ternaries,
  ``t(key) || "…my fallback…"`` leftovers, admin-panel templates) —
  English users saw Myanmar, and two recap status lines branched on
  the never-assigned ``state.lang`` so they rendered English for
  everyone;
- ~78 unwired Myanmar texts in index.html (subtitle-toolbar labels,
  Gemini voice options, curated voice cards, crop pills, tooltips);
- English-only server strings in api.py (the ``"5m ago"`` relative
  time at three call sites, the recap queue/status progress labels).

These tests pin the completed state:

(a) i18n.js my/en key sets are identical;
(b) locales/{my,en}/messages.json key sets are identical;
(c) no string literal containing Myanmar text remains in app.js
    outside the documented allowlist below;
(d) every i18n key referenced from index.html (data-i18n /
    data-i18n-ph / data-i18n-title) or app.js (t()/tf() calls,
    including ternary-passed keys and ``"prefix" + …`` families)
    exists in i18n.js;
(e) index.html carries no unwired Myanmar text outside the
    documented exceptions;
(f) the server-side relative-time helper returns localized strings;
(g) the static-asset cache-bust version is the current r45.

The allowlist in (c) is deliberate and each entry is a NON-UI string:

- ``ပယ်ဖျက်မည်`` / ``သဘောတူ`` — emergency fallbacks in the confirm
  modal, used only when i18n.js itself failed to load (they mirror
  the ``confirm_cancel`` / ``confirm_ok`` values);
- ``စုစုပေါင်း``, ``credits လိုအပ်ပါတယ်``, ``credit မလုံလောက်တော့ပါ``,
  ``ကန့်သတ်ချက် (အများဆုံး``, ``အပိုင်း`` — match needles tested
  against Burmese error copy SENT BY THE SERVER (translating them
  would break error classification);
- ``ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်`` /
  ``ဆွဲယူခြင်းကို ငြင်းပယ်ထားပါသည်`` — same, for the downloader's
  classified source-error copy;
- the four tutorial default titles — data values that must match
  the server-side defaults byte-for-byte (display goes through
  ``tutorialDisplayTitle()`` → i18n); also used as admin-settings
  default values.

Myanmar detection range: U+1000–U+103F plus the Burmese punctuation
U+104A/U+104B. Myanmar DIGITS (U+1040–U+1049) are intentionally not
flagged: numeral strings are data (e.g. the recap ETA renders
"၁.၅ - ၂.၅" for my and converts to Latin digits for en).

Everything here is offline file parsing — no network, no browser.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
MESSAGES_MY = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
MESSAGES_EN = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))

MYANMAR_RE = re.compile(r"[က-ဿ၊။]")

# ── batch F key inventory ────────────────────────────────────────────
UI_KEYS = [
    "alert_btn_view_result", "recap_tier_vip_text", "recap_tier_free_text",
    "tier_free_label", "text_too_long_vip_note", "recap_over_max_vip",
    "recap_over_max_free", "recap_over_limit_label", "recap_stage1_status",
    "recap_eta_estimate", "dl_open_in_browser_tab", "admin_media_gone_title",
    "admin_media_gone_desc", "admin_media_gone_regen",
    "admin_media_duration_badge", "admin_retry_starting",
    "admin_retry_queued_note", "admin_retry_status_waiting",
    "admin_retry_status_running", "admin_retry_status_done",
    "tut_guide_title", "btn_play_audio_long", "tut_btn_watch",
    "preview_drag_hint", "tb_tab_mask", "tb_tab_style",
    "sub_seek_check_label", "btn_seek_back_5", "btn_seek_fwd_5",
    "btn_canvas_preview", "btn_reset_layout", "btn_reset_layout_title",
    "crop_preset_side_toolbar",
    "gem_voice_charon", "gem_voice_algenib", "gem_voice_orus",
    "gem_voice_iapetus", "gem_voice_puck", "gem_voice_kore",
    "gem_voice_leda", "gem_voice_sulafat", "gem_voice_gacrux",
    "gem_voice_achernar",
    "voice_komin_name", "voice_komin_desc", "voice_kozayar_name",
    "voice_kozayar_desc", "voice_koaung_name", "voice_koaung_desc",
    "voice_mahnin_name", "voice_mahnin_desc", "voice_kothura_name",
    "voice_kothura_desc", "voice_komyat_name", "voice_komyat_desc",
    "voice_kophone_name", "voice_kophone_desc", "voice_manadi_name",
    "voice_manadi_desc", "voice_mamay_name", "voice_mamay_desc",
    "voice_mayoon_name", "voice_mayoon_desc", "voice_machaw_name",
    "voice_machaw_desc", "voice_mathiri_name", "voice_mathiri_desc",
    "voice_preview_title", "btn_sync_crop_title", "btn_use_in_recap",
    "alert_default_title", "alert_default_desc", "alert_btn_view",
]
SERVER_KEYS = [
    "time_ago_minutes", "time_ago_hours", "recap_progress_picking_up",
    "recap_progress_preparing", "recap_progress_requeued",
    "webapp_status_completed", "webapp_status_processing",
    "webapp_status_failed",
]

APP_JS_LITERAL_ALLOWLIST = {
    # confirm-modal emergency fallbacks (i18n.js failed to load)
    "ပယ်ဖျက်မည်", "သဘောတူ",
    # server-error match needles (error classification)
    "စုစုပေါင်း", "credits လိုအပ်ပါတယ်", "credit မလုံလောက်တော့ပါ",
    "ကန့်သတ်ချက် (အများဆုံး", "အပိုင်း",
    "ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်",
    "ဆွဲယူခြင်းကို ငြင်းပယ်ထားပါသည်",
    # tutorial default titles — server-mirrored data values
    "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
    "🎙️ Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
    "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
    "🎬 Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
}

# index.html exceptions: the language switcher labels itself, and the
# two admin tutorial-title inputs whose PLACEHOLDER shows a server-side
# data default (a stored title value, not UI copy).
# Batch BA (2026-11-06): "lang-btn-text" is the switcher's own label
# span inside btn-lang-toggle, split out so the ≤430px header
# compaction can hide it whole — same self-labelling exception.
HTML_UNWIRED_ALLOW_IDS = {"btn-lang-toggle", "lang-btn-text", "adm-tut-voice-title", "adm-tut-recap-title"}


# ── i18n.js parsing (same block approach as test_upload_ux) ─────────

def _locale_block(lang: str) -> str:
    marker = "    " + lang + ": {"
    start = I18N_JS.index(marker) + len(marker)
    end = I18N_JS.index("\n    }", start)
    return I18N_JS[start:end]


def _keys(lang: str) -> set[str]:
    return set(re.findall(r"(?m)^        (\w+):", _locale_block(lang)))


# ── minimal JS string scanner ────────────────────────────────────────
# Stack-based tokenizer: skips comments and regex literals, yields
# (line, text) for every '…' / "…" literal and every static chunk of
# a template literal (the parts outside ${…}).

def _scan_js_strings(src: str):
    results = []
    i, n, line = 0, len(src), 1
    prev_sig = ""

    def regex_allowed():
        return prev_sig in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?",
                            "{", "}", ";", "+", "-", "*", "%", "<", ">",
                            "^", "~", "return", "case", "typeof")

    def read_quoted(j, q, line):
        buf = []
        start = line
        while j < n:
            ch = src[j]
            if ch == "\\":
                buf.append(src[j:j + 2]); j += 2; continue
            if ch == q or ch == "\n":
                break
            buf.append(ch); j += 1
        return "".join(buf), j + 1, start

    def read_template(j, line):
        chunks = []
        buf = []
        chunk_line = line
        while j < n:
            ch = src[j]
            if ch == "\\":
                buf.append(src[j:j + 2]); j += 2; continue
            if ch == "\n":
                line += 1; buf.append(ch); j += 1; continue
            if ch == "`":
                chunks.append((chunk_line, "".join(buf)))
                return chunks, j + 1, line
            if ch == "$" and j + 1 < n and src[j + 1] == "{":
                chunks.append((chunk_line, "".join(buf)))
                buf = []
                depth = 1
                j += 2
                while j < n and depth > 0:
                    e = src[j]
                    if e == "\n":
                        line += 1; j += 1; continue
                    if e == "/" and j + 1 < n and src[j + 1] == "/":
                        while j < n and src[j] != "\n":
                            j += 1
                        continue
                    if e == "/" and j + 1 < n and src[j + 1] == "*":
                        j += 2
                        while j + 1 < n and not (src[j] == "*" and src[j + 1] == "/"):
                            if src[j] == "\n":
                                line += 1
                            j += 1
                        j += 2; continue
                    if e in "'\"":
                        text, j, sline = read_quoted(j + 1, e, line)
                        results.append((sline, text))
                        continue
                    if e == "`":
                        sub, j, line = read_template(j + 1, line)
                        results.extend(sub)
                        continue
                    if e == "{":
                        depth += 1
                    elif e == "}":
                        depth -= 1
                    j += 1
                chunk_line = line
                continue
            buf.append(ch); j += 1
        chunks.append((chunk_line, "".join(buf)))
        return chunks, j, line

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1; i += 1; continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    line += 1
                i += 1
            i += 2; continue
        if c == "/" and regex_allowed():
            j = i + 1
            in_class = False
            ok = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2; continue
                if ch == "\n":
                    break
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    ok = True; break
                j += 1
            if ok:
                i = j + 1
                while i < n and src[i].isalpha():
                    i += 1
                prev_sig = "literal"
                continue
            prev_sig = "/"; i += 1; continue
        if c in "'\"":
            text, i, sline = read_quoted(i + 1, c, line)
            results.append((sline, text))
            prev_sig = "literal"
            continue
        if c == "`":
            chunks, i, line = read_template(i + 1, line)
            results.extend(chunks)
            prev_sig = "literal"
            continue
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            word = src[i:j]
            prev_sig = word if word in ("return", "case", "typeof") else "ident"
            i = j; continue
        if c.isdigit():
            j = i
            while j < n and (src[j].isalnum() or src[j] in "._"):
                j += 1
            prev_sig = "literal"; i = j; continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return results


# ── (a) / (b) symmetry ───────────────────────────────────────────────

def test_i18n_js_key_sets_symmetric():
    assert _keys("my") == _keys("en")


def test_messages_json_key_sets_symmetric():
    assert set(MESSAGES_MY) == set(MESSAGES_EN)


def test_batch_f_keys_present_in_all_four_files():
    for key in UI_KEYS:
        assert key in _keys("my"), f"{key} missing from i18n.js my"
        assert key in _keys("en"), f"{key} missing from i18n.js en"
        assert key in MESSAGES_MY, f"{key} missing from messages.json my"
        assert key in MESSAGES_EN, f"{key} missing from messages.json en"
    for key in SERVER_KEYS:
        assert key in MESSAGES_MY, f"{key} missing from messages.json my"
        assert key in MESSAGES_EN, f"{key} missing from messages.json en"


# ── (c) no hardcoded Myanmar literals in app.js ─────────────────────

def test_app_js_no_hardcoded_myanmar_literals():
    offenders = [
        (line, text) for line, text in _scan_js_strings(APP_JS)
        if MYANMAR_RE.search(text) and text not in APP_JS_LITERAL_ALLOWLIST
    ]
    assert not offenders, (
        "hardcoded Myanmar literals in app.js (key them via t()/tf() "
        f"or document an allowlist exception): {offenders[:5]}"
    )


def test_app_js_allowlist_entries_still_present():
    # If a needle/fallback is removed, its allowlist entry must go too —
    # a stale allowlist would silently re-admit the string as UI copy.
    present = {text for _, text in _scan_js_strings(APP_JS)}
    stale = APP_JS_LITERAL_ALLOWLIST - present
    assert not stale, f"allowlist entries no longer in app.js: {stale}"


# ── (d) referenced keys exist ────────────────────────────────────────

def test_referenced_keys_exist():
    keys = _keys("my")
    refs = set(re.findall(r'data-i18n(?:-ph|-title)?="([A-Za-z0-9_]+)"', INDEX_HTML))
    missing = refs - keys
    assert not missing, f"index.html references missing i18n keys: {missing}"

    # "prefix" + … dynamic families (e.g. t("recap_stage_" + stage)):
    # the family must exist; prefixes are not themselves keys.
    prefixes = set(re.findall(r'\btf?\(\s*"([A-Za-z0-9_]+)"\s*\+', APP_JS))
    for prefix in prefixes:
        assert any(k.startswith(prefix) for k in keys), \
            f"no i18n keys in dynamic family {prefix!r}"

    # t("key") / tf("key", …) literal calls
    direct = set(re.findall(r'\btf?\(\s*"([A-Za-z0-9_]+)"', APP_JS)) - prefixes
    missing = direct - keys
    assert not missing, f"app.js t()/tf() references missing keys: {missing}"

    # Keys passed via ternaries inside t()/tf() calls, e.g.
    # t(cond ? "key_a" : "key_b"): scan each call's argument span for
    # key-shaped literals.
    key_shape = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")
    for m in re.finditer(r"\btf?\(", APP_JS):
        depth = 1
        j = m.end()
        while j < len(APP_JS) and depth > 0:
            if APP_JS[j] == "(":
                depth += 1
            elif APP_JS[j] == ")":
                depth -= 1
            j += 1
        span = APP_JS[m.end():j]
        for lit in re.findall(r'"([^"]+)"', span):
            if key_shape.match(lit) and lit not in prefixes:
                assert lit in keys, \
                    f"key-shaped literal {lit!r} in a t()/tf() call is not in i18n.js"


# ── (e) index.html carries no unwired Myanmar text ──────────────────

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img",
              "input", "link", "meta", "source", "track", "wbr"}


class _UnwiredFinder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.unwired = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for attr in ("title", "placeholder"):
            if attr in d and MYANMAR_RE.search(d[attr]):
                wired_attr = "data-i18n-ph" if attr == "placeholder" else "data-i18n-title"
                if wired_attr not in d and d.get("id") not in HTML_UNWIRED_ALLOW_IDS:
                    self.unwired.append((tag, attr, d.get("id"), d[attr][:40]))
        if tag not in _VOID_TAGS:
            self.stack.append((tag, d, []))

    def handle_data(self, data):
        if self.stack and MYANMAR_RE.search(data):
            self.stack[-1][2].append(data)

    def handle_endtag(self, tag):
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx][0] == tag:
                t, d, parts = self.stack[idx]
                text = "".join(parts).strip()
                if text and MYANMAR_RE.search(text):
                    if "data-i18n" not in d and d.get("id") not in HTML_UNWIRED_ALLOW_IDS:
                        self.unwired.append((t, "text", d.get("id"), text[:40]))
                del self.stack[idx:]
                break


def test_index_html_no_unwired_myanmar_text():
    finder = _UnwiredFinder()
    finder.feed(INDEX_HTML)
    assert not finder.unwired, (
        "Myanmar text in index.html without data-i18n wiring: "
        f"{finder.unwired[:5]}"
    )


# ── (f) server-side localization ─────────────────────────────────────

def test_server_time_ago_helper_is_localized():
    from app.webapp import api as api_mod

    assert api_mod._time_ago_str(5, "en") == "5m ago"
    assert api_mod._time_ago_str(5, "my") == "5 မိနစ်က"
    assert api_mod._time_ago_str(125, "en") == "2h ago"
    assert api_mod._time_ago_str(125, "my") == "2 နာရီက"
    # Unknown language falls back to Myanmar (product is Burmese-first).
    assert api_mod._time_ago_str(5, "xx") == "5 မိနစ်က"


def test_server_progress_labels_use_locales():
    api_src = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
    for literal in ('"⏳ Picking up from queue..."',
                    '"⏳ ပြင်ဆင်နေပါသည်…"',
                    '"Processing in background..."',
                    '"🔄 Re-queued by Administrator..."'):
        assert literal not in api_src, f"hardcoded progress label remains: {literal}"
    for key in SERVER_KEYS:
        assert f't("{key}"' in api_src, f"api.py does not use server key {key}"


# ── (g) cache-bust version ───────────────────────────────────────────

def test_cache_bust_version_is_r45():
    assert INDEX_HTML.count("?v=20261104_r116") == 4
    assert "20261103_r41" not in INDEX_HTML
