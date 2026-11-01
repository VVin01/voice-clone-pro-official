"""Regression tests for recap Burmese spelling normalization.

Covers two failure modes found in the live recap script of job rc_1793279760
(owner report 2026-10-29):

1. The orthography normalizer itself corrupted correct text: the 'ခန' -> 'ခဏ'
   rule used a character-class lookahead whose letters included 'း', so the
   classifier noun 'ခန်း' (room) became 'ခဏ်း' and 'အခန်း' became 'အခဏ်း'.
2. Gemini's raw narration contained misspellings / wrong transliterations
   ('ဒီအန်အေ' for DNA, 'သံသဝင်', 'လူပုံအလယ်မှာ', 'အကြပ်အတည်း', 'အအပြဲ',
   'ဘွားအေ', 'လက်ဝတ်ရစား') that no post-generation check caught. The curated
   MYANMAR_SPELLING_FIXES map in normalize_burmese_orthography now fixes them;
   every recap segment passes through it before the TTS/subtitle split.

A second owner report (2026-11-01, Gemini-TTS jobs rc_1793525723 and
rc_1793526555) added the malformed 'တကြီး' adverb family ('အံ့ဩတကြီး',
'စိုးရိမ်တကြီး') and the broken construction 'အန္တရာယ်ကြားဆီ' to the map.
Foreign-name spellings (the 'လို့စ်' vs 'လို့အစ်' instability across those
two jobs) are deliberately NOT in the map: names are governed by the planner
prompt's one-consistent-form rule, not by substring replacement.
"""

from app.services.recap_engine.core import (
    MYANMAR_SPELLING_FIXES,
    canonicalize_burmese_text,
    normalize_burmese_orthography,
)

CORRECT_SENTENCES = [
    # The corrected forms of every catalogued error must pass through unchanged.
    "ဒီအဲန်အေ စစ်ဆေးမှု အမြန်လုပ်ဖို့ သူက အမိန့်ပေးလိုက်တယ်",
    "အားလုံးက သူ့ကို သံသယဝင်လာကြတော့တယ်",
    "လူပုံလယ်မှာ သူ့ကို နှိမ်ချပြောဆိုနေကြတယ်",
    "နှစ်ဦးကြား ဆက်ဆံရေးက အကျပ်အတည်းနဲ့ ရင်ဆိုင်နေရတယ်",
    "သူမက အပြင်းအထန် စိန်ခေါ်မှုတွေ ပြုလုပ်လာခဲ့တယ်",
    "အဘွားဖြစ်သူက မြေးမလေးဘက်ကနေ ဝင်ပြောပေးနေခဲ့တယ်",
    "တန်ဖိုးကြီး လက်ဝတ်ရတနာနဲ့ ကားတွေ ဝယ်ပေးမယ်လို့ ပြောတယ်",
    # Regression for the 'ခန' rule corruption: 'ခန်း' is a noun, not 'ခဏ'.
    "စာရွက်စာတမ်း ၂၀ စောင် ရှိပြီး အခန်း ၁၀ ခန်း ပါတယ်",
    "စားခန်းနဲ့ အိပ်ခန်းက သန့်ရှင်းနေတယ်",
    "ခန္ဓာကိုယ် ကျန်းမာဖို့ လေ့ကျင့်ခန်း လုပ်ရမယ်",
    "အချိန်ကို ခန့်မှန်းပြီး စီစဉ်ထားတယ်",
    # The standard written form 'အဲဒီ' (owner's exemplar style) stays untouched.
    "အဲဒီသတင်းဟာ တစ်မြို့လုံးကို ပျံ့နှံ့သွားခဲ့ပါတယ်",
    "အဲဒီနောက် သူက အဲဒီအချိန်မှာ ဖြစ်ခဲ့တာကို ပြန်စဉ်းစားနေတယ်",
    # Existing correction targets in their correct form stay untouched.
    "မေမေက တွေကို ပြောတယ်၊ သူတို့ နေတယ် ဆိုတာ သေချာတယ်",
    "ပရိသတ်ကြီး ကျေးဇူးတင်ပါတယ်၊ ခဏစောင့်ပါ",
    "သူ အရမ်း ကြိုးစားနေပြီး ပြဿနာကို ဂရုစိုက် ဖြေရှင်းနေတယ်",
    "ဥယျာဉ်ထဲမှာ ပန်းတွေ ပွင့်နေတယ်",
    # Corrected forms from the 2026-11-01 jobs pass through unchanged.
    "သူက အံ့ဩစွာ မေးမြန်းလိုက်ပါတယ်",
    "စိုးရိမ်စွာ သတိပေးလိုက်တာဖြစ်ပါတယ်",
    "သူမ အန္တရာယ်ထဲကို ရောက်သွားခဲ့ပါတယ်",
]

ERROR_FIXES = [
    ("ဒီအန်အေ စစ်ဆေးမှု အမြန်လုပ်ဖို့ ဦးဟိုးကို အမိန့်ပေးလိုက်တယ်",
     "ဒီအဲန်အေ စစ်ဆေးမှု အမြန်လုပ်ဖို့ ဦးဟိုးကို အမိန့်ပေးလိုက်တယ်"),
    ("ဒီအန်အေ စစ်ဆေးမှု ပြုလုပ်ထားကြောင်း ချင်းလန်က ထုတ်ပြောလိုက်တယ်",
     "ဒီအဲန်အေ စစ်ဆေးမှု ပြုလုပ်ထားကြောင်း ချင်းလန်က ထုတ်ပြောလိုက်တယ်"),
    ("ဒီအန်အေ အစီရင်ခံစာ ရောက်လာတဲ့အခါမှာ သူက တားမြစ်ခဲ့တယ်",
     "ဒီအဲန်အေ အစီရင်ခံစာ ရောက်လာတဲ့အခါမှာ သူက တားမြစ်ခဲ့တယ်"),
    ("တစ်စုံတစ်ရာကို သံသဝင်လာပြီး အသေးစိတ် ရှာဖွေကြည့်ခဲ့တယ်",
     "တစ်စုံတစ်ရာကို သံသယဝင်လာပြီး အသေးစိတ် ရှာဖွေကြည့်ခဲ့တယ်"),
    ("လူပုံအလယ်မှာ အခြားသူတွေရှေ့မှာ နှိမ်ချပြောဆိုနေကြတယ်",
     "လူပုံလယ်မှာ အခြားသူတွေရှေ့မှာ နှိမ်ချပြောဆိုနေကြတယ်"),
    ("နှစ်ဦးကြား ဆက်ဆံရေးက အကြပ်အတည်းနဲ့ ရင်ဆိုင်နေရတယ်",
     "နှစ်ဦးကြား ဆက်ဆံရေးက အကျပ်အတည်းနဲ့ ရင်ဆိုင်နေရတယ်"),
    ("ကားတွေပါ ဝယ်ပေးမယ်ဆိုပြီး အအပြဲ စိန်ခေါ်မှုတွေ ပြုလုပ်လာခဲ့ကြတယ်",
     "ကားတွေပါ ဝယ်ပေးမယ်ဆိုပြီး အပြင်းအထန် စိန်ခေါ်မှုတွေ ပြုလုပ်လာခဲ့ကြတယ်"),
    ("ဘွားအေဖြစ်သူကလည်း မြေးမလေးဘက်ကနေ ဝင်ပြောပေးနေခဲ့တယ်",
     "အဘွားဖြစ်သူကလည်း မြေးမလေးဘက်ကနေ ဝင်ပြောပေးနေခဲ့တယ်"),
    ("တန်ဖိုးကြီး လက်ဝတ်ရစားနဲ့ ကားတွေပါ ဝယ်ပေးမယ်ဆိုပြီး ပြောတယ်",
     "တန်ဖိုးကြီး လက်ဝတ်ရတနာနဲ့ ကားတွေပါ ဝယ်ပေးမယ်ဆိုပြီး ပြောတယ်"),
    # Colloquial 'အဲ့ဒီ' -> standard 'အဲဒီ' (the exact slip in job rc_1793503698's script).
    ("အဲ့ဒီအချိန်မှာ သတင်းထောက် လို့အစ် ရောက်ရှိနေပါတယ်",
     "အဲဒီအချိန်မှာ သတင်းထောက် လို့အစ် ရောက်ရှိနေပါတယ်"),
    ("အဲ့ဒီနောက် သူဟာ အဲ့ဒီတော့မှ ပြန်ထွက်လာခဲ့တယ်",
     "အဲဒီနောက် သူဟာ အဲဒီတော့မှ ပြန်ထွက်လာခဲ့တယ်"),
    # Malformed 'တကြီး' adverb family (jobs rc_1793525723 + rc_1793526555).
    ("သူက အံ့ဩတကြီး မေးမြန်းလိုက်ပါတယ်",
     "သူက အံ့ဩစွာ မေးမြန်းလိုက်ပါတယ်"),
    ("မကြောက်ဘူးလားလို့ စိုးရိမ်တကြီး သတိပေးလိုက်တာဖြစ်ပါတယ်",
     "မကြောက်ဘူးလားလို့ စိုးရိမ်စွာ သတိပေးလိုက်တာဖြစ်ပါတယ်"),
    # Broken construction from job rc_1793525723. The name token 'လို့စ်' is
    # intentionally left untouched: name spellings belong to the prompt rule,
    # not to this deterministic map.
    ("လို့စ်တစ်ယောက် အန္တရာယ်ကြားဆီ ရောက်ရှိသွားခဲ့ပါတယ်",
     "လို့စ်တစ်ယောက် အန္တရာယ်ထဲကို ရောက်ရှိသွားခဲ့ပါတယ်"),
]


def test_correct_sentences_pass_through_unchanged():
    for sentence in CORRECT_SENTENCES:
        assert normalize_burmese_orthography(sentence) == sentence
        assert canonicalize_burmese_text(sentence) == sentence


def test_catalogued_errors_are_fixed():
    for wrong, right in ERROR_FIXES:
        assert normalize_burmese_orthography(wrong) == right


def test_corrections_are_idempotent():
    for wrong, right in ERROR_FIXES:
        once = normalize_burmese_orthography(wrong)
        assert normalize_burmese_orthography(once) == once == right
    for sentence in CORRECT_SENTENCES:
        once = normalize_burmese_orthography(sentence)
        assert normalize_burmese_orthography(once) == once


def test_khana_misspelling_still_fixed_without_corrupting_khan_noun():
    # The intended fixes from the original rule keep working.
    assert normalize_burmese_orthography("ခနစောင့်ပါ") == "ခဏစောင့်ပါ"
    assert normalize_burmese_orthography("ခနလေး စောင့်ပါ") == "ခဏလေး စောင့်ပါ"
    assert normalize_burmese_orthography("ခနခန ဖြစ်နေတယ်") == "ခဏခဏ ဖြစ်နေတယ်"
    assert normalize_burmese_orthography("ခန အတွင်း ပြီးမယ်") == "ခဏ အတွင်း ပြီးမယ်"
    # ... while the noun 'ခန်း' (room) is never touched.
    assert normalize_burmese_orthography("အခန်း") == "အခန်း"
    assert normalize_burmese_orthography("စားခန်း") == "စားခန်း"


def test_spelling_fix_map_keys_are_not_substrings_of_their_values():
    # Guards the table against self-corruption if entries are added later.
    for wrong, right in MYANMAR_SPELLING_FIXES.items():
        assert wrong not in right
        assert normalize_burmese_orthography(right) == right
