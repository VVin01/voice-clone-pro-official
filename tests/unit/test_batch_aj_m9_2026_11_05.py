"""Batch AJ / M9 - Group AI prompt-injection hardening (2026-11-05).

Pins:
* parse_ai_decision output guard: length cap, instruction / fact-sheet
  echo refusal, smuggled protocol tokens anywhere in the reply, and
  markup stripping.
* detect_sensitive_topic normalisation: zero-width characters, repeated
  whitespace and spacing tricks must not slip the deterministic floor,
  while ordinary price / how-to-pay questions stay non-sensitive.
"""

import pytest

from app.services import group_ai_admin as gai

ZW = chr(0x200B)  # zero-width space
ZWNJ = chr(0x200C)  # zero-width non-joiner
ZWJ = chr(0x200D)  # zero-width joiner
BOM = chr(0xFEFF)  # zero-width no-break space / BOM


def test_reply_over_length_cap_is_void():
    long_reply = "X" * (gai.REPLY_MAX_CHARS + 1)
    assert gai.parse_ai_decision("REPLY: " + long_reply) == (gai.DECISION_NO_REPLY, None)


def test_reply_at_length_cap_still_passes():
    ok_reply = "X" * gai.REPLY_MAX_CHARS
    decision, reply = gai.parse_ai_decision("REPLY: " + ok_reply)
    assert decision == gai.DECISION_REPLY
    assert reply == ok_reply


def test_normal_short_reply_unaffected():
    decision, reply = gai.parse_ai_decision("REPLY: hello, you can start in the Mini App")
    assert decision == gai.DECISION_REPLY
    assert reply == "hello, you can start in the Mini App"


def test_reply_echoing_system_instructions_is_void():
    first_line = gai.SYSTEM_INSTRUCTIONS.strip().splitlines()[0]
    leaked = "REPLY: " + first_line
    assert gai.parse_ai_decision(leaked) == (gai.DECISION_NO_REPLY, None)


def test_reply_echoing_fact_sheet_heading_is_void():
    leaked = "REPLY: === KNOWLEDGE SHEET === Voice Pro price list follows"
    assert gai.parse_ai_decision(leaked) == (gai.DECISION_NO_REPLY, None)


def test_reply_echoing_fact_sheet_section_brackets_is_void():
    leaked = "REPLY: " + chr(0x3010) + "section" + chr(0x3011) + " prices follow"
    assert gai.parse_ai_decision(leaked) == (gai.DECISION_NO_REPLY, None)


def test_reply_with_smuggled_protocol_token_mid_text_is_void():
    assert gai.parse_ai_decision("REPLY: ok NO_REPLY") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision("REPLY: ok REPLY: second") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision("REPLY: please ESCALATE this") == (gai.DECISION_NO_REPLY, None)


def test_reply_html_markup_is_stripped_not_posted_raw():
    decision, reply = gai.parse_ai_decision(
        "REPLY: <b>ok</b> <a href=\"https://evil.example\">here</a> tap"
    )
    assert decision == gai.DECISION_REPLY
    assert "<" not in reply and ">" not in reply
    assert "evil.example" not in reply
    assert "ok" in reply


def test_reply_markdown_link_is_neutralised():
    decision, reply = gai.parse_ai_decision("REPLY: [here](https://evil.example/phish) look")
    assert decision == gai.DECISION_REPLY
    assert "evil.example" not in reply
    assert "](" not in reply


@pytest.mark.parametrize(
    "text",
    [
        "ref" + ZW + "und please",
        "ref" + ZWNJ + "und please",
        BOM + "refund" + ZWJ + " please",
        "REFUND please",
        "i    paid but minutes not credited",
        "paid   but not approved",
        "not    credited please",
    ],
)
def test_sensitive_topic_survives_ascii_obfuscation(text):
    assert gai.detect_sensitive_topic(text)


def test_sensitive_topic_survives_zw_in_burmese_pattern():
    # Build a sensitive Burmese pattern at runtime from the module list
    # is not possible (patterns are private); use the known refund
    # phrase with zero-width chars inserted between characters.
    phrase = gai._SENSITIVE_PATTERNS[2]  # a Burmese money-back phrase
    obfuscated = ZW.join(list(phrase))
    assert gai.detect_sensitive_topic(obfuscated)


def test_sensitive_topic_survives_spaced_burmese_pattern():
    phrase = gai._SENSITIVE_PATTERNS[2]
    spaced = " ".join(list(phrase.replace(" ", "")))
    if len(spaced) < 4:
        pytest.skip("pattern too short for spacing test")
    assert gai.detect_sensitive_topic(spaced)


@pytest.mark.parametrize(
    "text",
    [
        "how much is Voice Pro",
        "how to pay, please tell me",
        "how much free do I get",
        "how do I start",
    ],
)
def test_sensitive_topic_stays_not_overbroad_en(text):
    assert not gai.detect_sensitive_topic(text)
