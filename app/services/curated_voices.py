"""
curated_voices.py - Studio-Mastered Curated Voice Personas for Burmese TTS & Voice Clone Pro.
Provides 12 meticulously tuned Burmese voice personas (6 Male, 6 Female)
with authentic native Burmese pronunciation, tailored vocal pitch, pacing,
and broadcast-grade acoustic studio mastering (FFmpeg EQ, dynamics & limiting).

Human-only rule (2026-10-29): every curated slot must be a natural human
voice. Two slots were previously presented as non-human characters and
were CONVERTED (not deleted) — same slot id and name, underlying voice
remapped to an existing human persona:
  - komyat (was: ghost-story character) -> human voice of komin
    (male human storyteller, same Edge base ThihaNeural).
  - machaw (was: anime/cartoon character) -> human voice of mamay
    (female human friendly guide, same Edge base NilarNeural).
The remap is done with "reference_id": VoxCPM2 reference audio and
reference transcript come from the human persona, the control
instruction and pitch/formant are neutral human values, and the
display name/description/badge/avatar are human. No new audio file
or model id was created, and nothing was re-recorded or re-trained —
this is a remap of existing human voices onto the same slots.
The total count stays 12.
"""

from __future__ import annotations

import os
import re
import shutil
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)

CURATED_VOICES: Dict[str, Dict[str, Any]] = {
    "komin": {
        "id": "komin",
        "gender": "male",
        "name_my": "မင်းမင်း (ရုပ်ရှင်ဇာတ်လမ်းပြောသူ)",
        "name_en": "Ko Min (Movie Recap Storyteller)",
        "desc_my": "နွေးထွေးတည်ကြည်ပြီး ဇာတ်လမ်းပြောရာတွင် အကောင်းဆုံး ရုပ်ရှင်အသံ",
        "desc_en": "Deep, warm, cinematic narrator ideal for movie recaps and docudramas",
        "avatar": "👨‍💼",
        "badge_my": "လူကြိုက်အများဆုံး 🔥",
        "badge_en": "Most Popular 🔥",
        "edge_voice": "my-MM-ThihaNeural",
        "pitch": "-3Hz",
        "rate_offset": 10,
        "formant_ratio": 0.96,
        "sample_text": "မင်္ဂလာပါခင်ဗျာ။ ကျွန်တော်ကတော့ မင်းမင်းပါ။ ရုပ်ရှင်ဇာတ်လမ်းကောင်းတွေကို စိတ်ဝင်စားဖွယ် တင်ဆက်ပေးသွားမှာ ဖြစ်ပါတယ်။",
        "af_filter": (
            "asetrate=42336,aresample=44100,atempo=1.0417,"
            "highpass=f=70,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=130:width_type=o:w=1:g=3.0,"
            "equalizer=f=3200:width_type=o:w=1:g=1.5,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A warm, deep, authoritative, emotionally resonant Burmese male movie recap storyteller. "
            "Natural human breathing, velvety cadence, warm acoustic studio presence with pristine Burmese pronunciation, zero robotic artifact."
        ),
        "cfg_value": 2.0,
    },
    "kothura": {
        "id": "kothura",
        "gender": "male",
        "name_my": "ကိုသူရ (သတင်းနှင့် Documentary)",
        "name_en": "Ko Thura (News & Documentary)",
        "desc_my": "သန့်ရှင်းပြတ်သားပြီး ခိုင်မာသော သတင်းကြေညာသူ စတိုင်လ်",
        "desc_en": "Crisp, formal, authoritative anchor tone for educational & news content",
        "avatar": "🎙️",
        "badge_my": "သန့်ရှင်းပြတ်သား ⚡",
        "badge_en": "Formal Anchor ⚡",
        "edge_voice": "my-MM-ThihaNeural",
        "pitch": "-3Hz",
        "rate_offset": 12,
        "formant_ratio": 0.96,
        "sample_text": "မင်္ဂလာပါခင်ဗျာ။ ဒီကနေ့ သတင်းနှင့် မှတ်တမ်းတင် အကြောင်းအရာများကို ရှင်းလင်းပြတ်သားစွာ တင်ဆက်ပေးသွားပါမည်။",
        "af_filter": (
            "asetrate=42336,aresample=44100,atempo=1.0417,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=130:width_type=o:w=1:g=3.0,equalizer=f=3200:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A professional, formal, crisp Burmese male documentary narrator and news anchor. "
            "Clear natural cadence, warm studio acoustic presence, articulate diction with zero metallic noise."
        ),
        "cfg_value": 2.1,
    },
    "koaung": {
        "id": "koaung",
        "gender": "male",
        "name_my": "အောင်အောင် (ခေတ်မီ Viral Creator)",
        "name_en": "Ko Aung (Energetic Viral Creator)",
        "desc_my": "သွက်လက်တက်ကြွပြီး TikTok/Shorts နှင့် လိုက်ဖက်သော လူငယ်အသံ",
        "desc_en": "Fast-paced, vibrant, casual youth creator voice for TikTok and Shorts",
        "avatar": "⚡",
        "badge_my": "TikTok & Shorts 🚀",
        "badge_en": "TikTok & Shorts 🚀",
        "edge_voice": "my-MM-ThihaNeural",
        "pitch": "-3Hz",
        "rate_offset": 16,
        "formant_ratio": 0.96,
        "sample_text": "ဟေး အားလုံးပဲ မင်္ဂလာပါဗျာ။ အောင်အောင်ပါ၊ ခေတ်မီတဲ့ အကြောင်းအရာသစ်တွေကို အတူတူ လေ့လာကြရအောင်!",
        "af_filter": (
            "asetrate=42336,aresample=44100,atempo=1.0417,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=130:width_type=o:w=1:g=3.0,equalizer=f=3200:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "An enthusiastic, upbeat, vibrant young Burmese male creator. "
            "Natural conversational rhythm, expressive organic inflection for TikTok and Shorts, clear studio clarity without distortion."
        ),
        "cfg_value": 2.0,
    },
    "mahnin": {
        "id": "mahnin",
        "gender": "female",
        "name_my": "နှင်းနှင်း (အချစ်ဇာတ်လမ်း & Audiobook)",
        "name_en": "Ma Hnin (Sweet Romance & Audiobook)",
        "desc_my": "နူးညံ့ချိုသာပြီး ရင်ထဲထိရှစေသော ဝတ္ထုစာအုပ်ဖတ်ပြသံ",
        "desc_en": "Gentle, melodious, heartfelt female voice for audiobooks & soft storytelling",
        "avatar": "👩‍🦰",
        "badge_my": "နူးညံ့ချိုသာ 🌸",
        "badge_en": "Gentle Melody 🌸",
        "edge_voice": "my-MM-NilarNeural",
        "pitch": "+2Hz",
        "rate_offset": 9,
        "formant_ratio": 1.025,
        "sample_text": "မင်္ဂလာပါရှင်။ နှင်းနှင်း ဖြစ်ပါတယ်။ ချိုသာနူးညံ့တဲ့ အသံလေးနဲ့ ဝတ္ထုဇာတ်လမ်းလေးတွေ ဖတ်ပြပေးပါ့မယ်နော်။",
        "af_filter": (
            "asetrate=45203,aresample=44100,atempo=0.9756,"
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=240:width_type=o:w=1:g=1.8,"
            "equalizer=f=3500:width_type=o:w=1:g=1.5,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A gentle, sweet, emotionally tender Burmese female audiobook narrator. "
            "Soft, melodious tone, soothing organic delivery, warm intimate presence with no robotic buzz."
        ),
        "cfg_value": 2.0,
    },
    "manadi": {
        "id": "manadi",
        "gender": "female",
        "name_my": "နဒီ (သည်းထိတ်ရင်ဖို & Movie Drama)",
        "name_en": "Ma Nadi (Dramatic Thriller Narrator)",
        "desc_my": "သည်းထိတ်ရင်ဖိုနှင့် စိတ်ဝင်စားဖွယ် ဇာတ်ဝင်ခန်းများကို ပေါ်လွင်စေသော အသံ",
        "desc_en": "Suspenseful, dramatic, expressive female voice for thrilling narratives",
        "avatar": "🎭",
        "badge_my": "သည်းထိတ်ရင်ဖို 🎬",
        "badge_en": "Thriller Drama 🎬",
        "edge_voice": "my-MM-NilarNeural",
        "pitch": "+2Hz",
        "rate_offset": 11,
        "formant_ratio": 1.025,
        "sample_text": "သည်းထိတ်ရင်ဖိုနဲ့ လျှို့ဝှက်ဆန်းကြယ်တဲ့ အဖြစ်အပျက်တွေကို နဒီနဲ့အတူ အဆုံးထိ ရင်ခုန်စွာ နားဆင်လိုက်ကြရအောင်။",
        "af_filter": (
            "asetrate=45203,aresample=44100,atempo=0.9756,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=240:width_type=o:w=1:g=1.8,equalizer=f=3500:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A dramatic, suspenseful, emotionally intense Burmese female movie recap narrator. "
            "Dynamic vocal contrast, natural cinematic inflection, zero digital clipping or harsh sibilance."
        ),
        "cfg_value": 2.1,
    },
    "mamay": {
        "id": "mamay",
        "gender": "female",
        "name_my": "မေမေ (ကြော်ငြာ & စီးပွားရေး လမ်းညွှန်)",
        "name_en": "Ma May (Commercial & Friendly Guide)",
        "desc_my": "ယဉ်ကျေးဖော်ရွေပြီး ယုံကြည်စိတ်ချရသော စီးပွားရေးကြော်ငြာသံ",
        "desc_en": "Articulate, polite, trustworthy, bright commercial & explainer voice",
        "avatar": "🌟",
        "badge_my": "ကြော်ငြာ & ပရိုမိုးရှင်း 💎",
        "badge_en": "Commercial Pro 💎",
        "edge_voice": "my-MM-NilarNeural",
        "pitch": "+2Hz",
        "rate_offset": 13,
        "formant_ratio": 1.025,
        "sample_text": "မင်္ဂလာပါရှင်။ သင့်လုပ်ငန်းနှင့် ပရိုမိုးရှင်းများကို ပိုမိုဆွဲဆောင်မှုရှိစေဖို့ မေမေက အကောင်းဆုံး ကူညီပေးပါရစေရှင်။",
        "af_filter": (
            "asetrate=45203,aresample=44100,atempo=0.9756,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=240:width_type=o:w=1:g=1.8,equalizer=f=3500:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A bright, articulate, friendly, and persuasive Burmese female commercial voice. "
            "Polite, trustworthy, modern clear studio broadcast timbre with organic smoothness."
        ),
        "cfg_value": 2.0,
    },
    "kozayar": {
        "id": "kozayar",
        "gender": "male",
        "name_my": "ကိုဇေယျာ (အက်ရှင်ဇာတ်ကားကြီး & Trailer)",
        "name_en": "Ko Zayar (Epic Action & Movie Trailer)",
        "desc_my": "ဩဇာတိက္ကမကြီးမားပြီး ရုပ်ရှင်ကားကြီးများ၏ အဖွင့်ကြေညာသံ",
        "desc_en": "Deep, bass-rich, resonant blockbuster movie trailer voice with powerful impact",
        "avatar": "🎬",
        "badge_my": "Epic Cinema 👑",
        "badge_en": "Epic Cinema 👑",
        "edge_voice": "my-MM-ThihaNeural",
        "pitch": "-3Hz",
        "rate_offset": 8,
        "formant_ratio": 0.96,
        "sample_text": "မင်္ဂလာပါ။ ရင်ခုန်စိတ်လှုပ်ရှားဖွယ် အက်ရှင်ဇာတ်ကားကြီးများ၏ အကောင်းဆုံး ဇာတ်ဝင်ခန်းများကို ခံစားကြည့်ရှုလိုက်ပါ။",
        "af_filter": (
            "asetrate=42336,aresample=44100,atempo=1.0417,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=130:width_type=o:w=1:g=3.0,equalizer=f=3200:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A deep, bass-rich, resonant, dramatic blockbuster movie trailer narrator. "
            "Authoritative gravitas, cinematic impact, velvety low-end warmth with pristine Burmese pronunciation."
        ),
        "cfg_value": 2.2,
    },
    "komyat": {
        "id": "komyat",
        "gender": "male",
        "name_my": "ကိုမြတ် (လူသားဇာတ်လမ်းပြောသူ)",
        "name_en": "Ko Myat (Human Storyteller)",
        "desc_my": "နွေးထွေးပြီး ရှင်းလင်းသော လူသားဇာတ်လမ်းပြောသံ — ဇာတ်လမ်းနှင့် စာအုပ်များအတွက်",
        "desc_en": "Warm, clear, natural human storyteller for stories and audiobooks",
        "avatar": "🎙️",
        "badge_my": "ဇာတ်လမ်းပြောသူ 🎙️",
        "badge_en": "Storyteller 🎙️",
        "edge_voice": "my-MM-ThihaNeural",
        "pitch": "-3Hz",
        "rate_offset": 10,
        "formant_ratio": 0.96,
        # Human-voice remap (2026-10-29): this slot was a ghost-story
        # character; its VoxCPM2 reference audio + transcript now come
        # from komin, an existing male human storyteller. Slot id and
        # name are kept; no new audio was recorded.
        "reference_id": "komin",
        "sample_text": "မင်္ဂလာပါခင်ဗျာ။ ကျွန်တော် ကိုမြတ်ပါ။ စိတ်ဝင်စားဖွယ် ဇာတ်လမ်းများကို ရှင်းလင်းနွေးထွေးစွာ ပြောပြပေးသွားပါ့မယ်။",
        "af_filter": (
            "asetrate=42336,aresample=44100,atempo=1.0417,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=130:width_type=o:w=1:g=3.0,equalizer=f=3200:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A warm, clear, natural Burmese male human storyteller. "
            "Conversational narration with natural breathing and human cadence, no character effects."
        ),
        "cfg_value": 2.0,
    },
    "kophone": {
        "id": "kophone",
        "gender": "male",
        "name_my": "ကိုဘုန်း (နည်းပညာ & ဗဟုသုတ)",
        "name_en": "Ko Phone (Tech & Educational Explainer)",
        "desc_my": "တိကျရှင်းလင်းပြီး သိပ္ပံနှင့် နည်းပညာ ဗဟုသုတ ဝေမျှရာတွင် အကောင်းဆုံး",
        "desc_en": "Clear, engaging, articulate explainer voice for tech tutorials, gadgets and science",
        "avatar": "💻",
        "badge_my": "Tech & Science 💡",
        "badge_en": "Tech & Science 💡",
        "edge_voice": "my-MM-ThihaNeural",
        "pitch": "-3Hz",
        "rate_offset": 13,
        "formant_ratio": 0.96,
        "sample_text": "နည်းပညာနှင့် ဗဟုသုတဆိုင်ရာ အကြောင်းအရာများကို လွယ်ကူရှင်းလင်းစွာ မျှဝေပေးနေတဲ့ ကိုဘုန်း ဖြစ်ပါတယ်။",
        "af_filter": (
            "asetrate=42336,aresample=44100,atempo=1.0417,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=130:width_type=o:w=1:g=3.0,equalizer=f=3200:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "An articulate, engaging, intelligent Burmese male explainer. "
            "Clean, friendly, informative pacing ideal for technology and educational tutorials with zero robotic noise."
        ),
        "cfg_value": 2.0,
    },
    "mayoon": {
        "id": "mayoon",
        "gender": "female",
        "name_my": "မယွန်း (အိပ်ဆေးပုံပြင် & ASMR)",
        "name_en": "Ma Yoon (Calm Meditation & ASMR)",
        "desc_my": "စိတ်ကို အေးချမ်းစေသော တရားထိုင်နှင့် အိပ်ဆေးပုံပြင်သံ",
        "desc_en": "Ultra-soft, tranquil voice designed for guided meditation and sleep relaxation",
        "avatar": "🌙",
        "badge_my": "အေးချမ်း ASMR ✨",
        "badge_en": "Peaceful ASMR ✨",
        "edge_voice": "my-MM-NilarNeural",
        "pitch": "+2Hz",
        "rate_offset": 7,
        "formant_ratio": 1.025,
        "sample_text": "တစ်နေ့တာ ပင်ပန်းသမျှတွေကို မေ့ဖျောက်ပြီး စိတ်အေးချမ်းစွာ အိပ်စက်အနားယူနိုင်ဖို့ မယွန်းက ကူညီပေးပါ့မယ်နော်။",
        "af_filter": (
            "asetrate=45203,aresample=44100,atempo=0.9756,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=240:width_type=o:w=1:g=1.8,equalizer=f=3500:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "An ultra-soft, serene, tranquil, whispery Burmese female voice. "
            "Extremely relaxing, gentle, calm tempo ideal for meditation, ASMR and bedtime stories, pristine natural warmth."
        ),
        "cfg_value": 1.8,
    },
    "machaw": {
        "id": "machaw",
        "gender": "female",
        "name_my": "မချော (လူသား Creator & Guide)",
        "name_en": "Ma Chaw (Human Creator & Guide)",
        "desc_my": "ဖော်ရွေပြီး ရှင်းလင်းသော လူသား Creator အသံ — ဗီဒီယိုနှင့် လမ်းညွှန်များအတွက်",
        "desc_en": "Friendly, clear, natural human creator voice for videos and guides",
        "avatar": "🌟",
        "badge_my": "Creator & Guide 🌟",
        "badge_en": "Creator & Guide 🌟",
        "edge_voice": "my-MM-NilarNeural",
        "pitch": "+2Hz",
        "rate_offset": 12,
        "formant_ratio": 1.025,
        # Human-voice remap (2026-10-29): this slot was an anime/cartoon
        # character; its VoxCPM2 reference audio + transcript now come
        # from mamay, an existing female human friendly guide. Slot id
        # and name are kept; no new audio was recorded.
        "reference_id": "mamay",
        "sample_text": "မင်္ဂလာပါရှင်။ ကျွန်မ မချောပါ။ ဗီဒီယိုများနှင့် လမ်းညွှန်များကို ဖော်ရွေရှင်းလင်းစွာ ပြောပြပေးသွားပါ့မယ်။",
        "af_filter": (
            "asetrate=45203,aresample=44100,atempo=0.9756,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=240:width_type=o:w=1:g=1.8,equalizer=f=3500:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A friendly, clear, natural Burmese female human creator and guide. "
            "Warm, articulate delivery with natural human intonation, no character effects."
        ),
        "cfg_value": 2.0,
    },
    "mathiri": {
        "id": "mathiri",
        "gender": "female",
        "name_my": "မသီရိ (Luxury Brand & Lifestyle)",
        "name_en": "Ma Thiri (Luxury Brand & Lifestyle)",
        "desc_my": "ခေတ်မီဆန်းပြားပြီး အဆင့်အတန်းမြင့်သော Lifestyle ကြော်ငြာသံ",
        "desc_en": "Chic, sophisticated, stylish influencer tone for fashion, beauty and luxury brands",
        "avatar": "💎",
        "badge_my": "Luxury Lifestyle 👑",
        "badge_en": "Luxury Lifestyle 👑",
        "edge_voice": "my-MM-NilarNeural",
        "pitch": "+2Hz",
        "rate_offset": 11,
        "formant_ratio": 1.025,
        "sample_text": "ခေတ်မီဆန်းပြားပြီး အဆင့်အတန်းမြင့်မားတဲ့ လူနေမှုဘဝပုံစံ အကြောင်းအရာများကို မသီရိက တင်ဆက်ပေးသွားမှာ ဖြစ်ပါတယ်ရှင်။",
        "af_filter": (
            "asetrate=45203,aresample=44100,atempo=0.9756,deesser=i=0.5:m=0.5:f=0.5:s=o,highpass=f=70,equalizer=f=240:width_type=o:w=1:g=1.8,equalizer=f=3500:width_type=o:w=1:g=1.5,equalizer=f=7200:width_type=q:w=2:g=-3.5,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "An elegant, chic, highly sophisticated Burmese female influencer and brand ambassador. "
            "Confident, modern, silky smooth vocal texture for luxury and lifestyle with zero distortion."
        ),
        "cfg_value": 2.1,
    },
}


# Real human reference clips (2026-10-29 rebuild): personas listed here
# were rebuilt on openly licensed corpus recordings (see
# PERSONA_REFERENCE_SOURCES). When a persona has an entry and the wav +
# transcript sidecar exist, job cloning uses the REAL reference instead
# of the preview sample mp3. komin/mahnin have no entry (owner-approved,
# unchanged); komyat resolves to komin (unchanged) and machaw resolves
# to mamay, so machaw follows mamay's real reference automatically.
PERSONA_REFERENCE_SOURCES: Dict[str, Dict[str, str]] = {
    "kothura": {"dataset": "Google FLEURS my_mm", "license": "CC-BY 4.0"},
    "koaung": {"dataset": "Google FLEURS my_mm", "license": "CC-BY 4.0"},
    "kozayar": {"dataset": "Google FLEURS my_mm", "license": "CC-BY 4.0"},
    "kophone": {"dataset": "Google FLEURS my_mm", "license": "CC-BY 4.0"},
    "manadi": {"dataset": "OpenSLR SLR80 (Burmese Speech Corpus)", "license": "CC-BY-SA 4.0"},
    "mamay": {"dataset": "OpenSLR SLR80 (Burmese Speech Corpus)", "license": "CC-BY-SA 4.0"},
    "mayoon": {"dataset": "OpenSLR SLR80 (Burmese Speech Corpus)", "license": "CC-BY-SA 4.0"},
    "mathiri": {"dataset": "OpenSLR SLR80 (Burmese Speech Corpus)", "license": "CC-BY-SA 4.0"},
}


# ---------------------------------------------------------------------------
# Studio mastering spec (2026-10-30): the exact pipeline that made the
# flagship voices komin / mahnin sound like real studio recordings,
# applied to every other persona. The flagship parameter sets themselves
# are owner-approved and are NOT touched by this unification.
#
# Spec (per gender):
#   pitch -3Hz (male) / +2Hz (female); formant ratio 0.96 / 1.025
#   (formant is realised as asetrate=<44100*ratio> + atempo compensation,
#   exactly as in the flagship af_filters).
#   DSP order: deesser -> highpass 70Hz -> body EQ (male 130Hz +3.0dB,
#   female 240Hz +1.8dB) -> presence EQ (male 3200Hz, female 3500Hz,
#   +1.5dB) -> 7200Hz -3.5dB metallic-treble cut ->
#   loudnorm I=-15:TP=-1.0:LRA=7.
# The sample-rate fix stays first: callers resample to 44100 Hz before
# this chain (the chain's own leading asetrate assumes a 44100 Hz input).
STUDIO_SPEC_MALE_PROMPT = (
    "A warm, deep, authoritative, emotionally resonant Burmese male movie recap storyteller. "
    "Natural human breathing, velvety cadence, warm acoustic studio presence with pristine Burmese pronunciation, zero robotic artifact."
)
STUDIO_SPEC_FEMALE_SWEET_PROMPT = (
    "A gentle, sweet, emotionally tender Burmese female audiobook narrator. "
    "Soft, melodious tone, soothing organic delivery, warm intimate presence with no robotic buzz."
)
STUDIO_SPEC_FEMALE_WARM_PROMPT = (
    "A warm, natural, gentle Burmese female narrator with a soft melodious voice. "
    "Soothing organic delivery, warm intimate studio presence, pristine Burmese pronunciation with no robotic buzz."
)

FLAGSHIP_PERSONA_IDS = frozenset({"komin", "mahnin"})


def build_studio_mastering_filter(gender: str, formant_ratio: float) -> str:
    """Build the studio-spec mastering chain for one persona.

    The returned chain starts with the formant prefix (asetrate shift +
    aresample + atempo compensation) and assumes the caller has already
    resampled the input to 44100 Hz (see the 2026-10-29 sample-rate fix
    in synthesize_curated_persona / apply_persona_mastering).
    """
    ratio = float(formant_ratio or 1.0)
    sr_shift = int(round(44100 * ratio + 1e-6))
    atempo_comp = 1.0 / ratio
    prefix = f"asetrate={sr_shift},aresample=44100,atempo={atempo_comp:.4f},"
    if str(gender or "").strip().lower() == "female":
        body_eq = "equalizer=f=240:width_type=o:w=1:g=1.8"
        presence_eq = "equalizer=f=3500:width_type=o:w=1:g=1.5"
    else:
        body_eq = "equalizer=f=130:width_type=o:w=1:g=3.0"
        presence_eq = "equalizer=f=3200:width_type=o:w=1:g=1.5"
    return (
        f"{prefix}"
        "deesser=i=0.5:m=0.5:f=0.5:s=o,"
        "highpass=f=70,"
        f"{body_eq},{presence_eq},"
        "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
        "loudnorm=I=-15:TP=-1.0:LRA=7"
    )


def persona_mastering_filter(persona_id: str) -> Optional[str]:
    """Studio-spec mastering chain for *persona_id*, or None.

    Returns None for the flagship personas (komin/mahnin — their
    owner-approved processing stays exactly as authored) and for
    unknown ids, so callers fall back to their previous behaviour.
    Remapped personas (komyat -> komin, machaw -> mamay) resolve to
    their human reference persona first, like get_persona_reference().
    """
    info = get_curated_voice(persona_id)
    if not info:
        return None
    ref_id = info.get("reference_id")
    if ref_id:
        ref_info = get_curated_voice(ref_id)
        if ref_info:
            info = ref_info
    if info.get("id") in FLAGSHIP_PERSONA_IDS:
        return None
    return build_studio_mastering_filter(
        str(info.get("gender") or "male"),
        float(info.get("formant_ratio", 1.0) or 1.0),
    )


def apply_persona_mastering(
    src_path: Path | str,
    dst_path: Path | str,
    persona_id: str,
    *,
    prefix: str = "",
) -> bool:
    """Master *src_path* into *dst_path* with the persona's studio chain.

    The 44100 Hz sample-rate fix is applied first, then *prefix* (e.g.
    a tempo stage), then the persona chain. Returns False (leaving the
    caller to use its generic mastering) for flagship/unknown personas
    or when ffmpeg is unavailable or fails.
    """
    chain = persona_mastering_filter(persona_id)
    if not chain:
        return False
    ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    if not ffmpeg_bin:
        return False
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(src_path),
                "-af", f"aresample=44100,{prefix}{chain}",
                "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
                str(dst),
            ],
            check=True, capture_output=True, timeout=60,
        )
    except Exception as e_ff:
        logger.debug("Persona studio mastering failed for %s: %s", persona_id, e_ff)
        return False
    return dst.is_file() and dst.stat().st_size > 1000


def get_persona_reference(voice_id: str) -> tuple[Optional[str], str]:
    """Return (sample_audio_path, sample_transcript) for the given curated persona.

    If the persona was remapped to a human voice (``reference_id``), the
    reference audio AND transcript come from that human persona, so the
    transcript always matches the reference clip used for cloning.

    Personas rebuilt on a real human reference clip (see
    ``PERSONA_REFERENCE_SOURCES``) return that wav + its exact transcript
    when both files are present; otherwise the preview sample is used,
    exactly as before.
    """
    info = get_curated_voice(voice_id)
    if not info:
        return None, ""
    ref_id = info.get("reference_id")
    if ref_id:
        ref_info = get_curated_voice(ref_id)
        if ref_info:
            info = ref_info
    pid = info["id"]
    if pid in PERSONA_REFERENCE_SOURCES:
        refs_dir = Path(__file__).resolve().parent.parent / "webapp" / "static" / "audio" / "references"
        ref_wav = refs_dir / f"{pid}.wav"
        ref_txt = refs_dir / f"{pid}.txt"
        if ref_wav.is_file() and ref_wav.stat().st_size > 1000 and ref_txt.is_file():
            transcript = ref_txt.read_text(encoding="utf-8").strip()
            if transcript:
                return str(ref_wav.resolve()), transcript
    sample_text = str(info.get("sample_text") or "").strip()
    samples_dir = Path(__file__).resolve().parent.parent / "webapp" / "static" / "audio" / "samples"
    cand = samples_dir / f"{pid}.mp3"
    if cand.is_file() and cand.stat().st_size > 1000:
        return str(cand.resolve()), sample_text
    return None, sample_text


def get_curated_voice(voice_id: str) -> Dict[str, Any] | None:
    if not voice_id:
        return None
    slug = str(voice_id).strip().lower()
    if slug.startswith("voxcpm_"):
        slug = slug.replace("voxcpm_", "")
    return CURATED_VOICES.get(slug)


def list_curated_voices(lang: str = "my") -> List[Dict[str, Any]]:
    result = []
    is_my = (lang == "my")
    for vid, v in CURATED_VOICES.items():
        result.append({
            "id": v["id"],
            "gender": v["gender"],
            "name": v["name_my"] if is_my else v["name_en"],
            "description": v["desc_my"] if is_my else v["desc_en"],
            "badge": v["badge_my"] if is_my else v["badge_en"],
            "avatar": v["avatar"],
            "sample_audio_url": f"/api/voices/sample/{v['id']}",
        })
    return result


def compute_curated_rate(base_offset: int, user_speed: float | str | None) -> str:
    """Calculate the combined speech rate string for Edge TTS (e.g. '+20%')."""
    extra = 8
    if isinstance(user_speed, (int, float)):
        extra = int(round((float(user_speed) - 1.0) * 100))
    elif isinstance(user_speed, str):
        s = user_speed.strip().lower()
        if s in ("fast", "1.2x", "1.2"):
            extra = 18
        elif s in ("slow", "0.8x", "0.8"):
            extra = -8
        elif s in ("normal", "1.0x", "1.0"):
            extra = 8
        elif "%" in s:
            m = re.search(r"([+-]?\d+)", s)
            if m:
                extra = int(m.group(1))
    total = max(-20, min(100, base_offset + extra))
    return f"{total:+d}%"


# Edge TTS requests are kept short on purpose: one request per chunk, and
# the caller concatenates every chunk in order, so a long text can never
# be silently shortened to whatever a single request happened to return.
# 800 Burmese characters are ~2400 UTF-8 bytes, safely under the
# edge_tts library's own 4096-byte internal split.
_PERSONA_TTS_CHUNK_CHARS = 800


def _split_persona_text(text: str, max_chars: int = _PERSONA_TTS_CHUNK_CHARS) -> List[str]:
    """Split *text* into ordered, lossless chunks of at most *max_chars*.

    Boundaries prefer Burmese sentence ends (။), then clause commas (၊),
    then whitespace. ``"".join(chunks)`` always reproduces the input
    exactly — no character is dropped, added, or reordered.
    """
    clean = str(text or "")
    if len(clean) <= max_chars:
        return [clean] if clean else []

    # Sentence-sized units, delimiter kept attached to its unit.
    units: List[str] = []
    start = 0
    for m in re.finditer(r"။|\n", clean):
        units.append(clean[start:m.end()])
        start = m.end()
    if start < len(clean):
        units.append(clean[start:])

    # Hard-split any unit that is still too long.
    pieces: List[str] = []
    for unit in units:
        while len(unit) > max_chars:
            cut = unit.rfind("၊", 0, max_chars)
            if cut < max_chars // 2:
                cut = unit.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            else:
                cut += 1  # keep the delimiter with the left piece
            pieces.append(unit[:cut])
            unit = unit[cut:]
        if unit:
            pieces.append(unit)

    # Pack pieces greedily into chunks without splitting them further.
    chunks: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks


def _edge_tts_to_mp3(text: str, voice: str, rate_str: str, pitch: str, dest_mp3: Path) -> None:
    """Run one Edge TTS request for *text* and save the MP3 to *dest_mp3*."""
    import edge_tts

    async def _run_edge():
        comm = edge_tts.Communicate(text, voice, rate=rate_str, pitch=pitch)
        await comm.save(str(dest_mp3))

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            fut = executor.submit(lambda: asyncio.run(_run_edge()))
            fut.result(timeout=30)
    except Exception as e_run:
        logger.warning("ThreadPoolExecutor failed for persona TTS: %s; falling back to direct run", e_run)
        asyncio.run(_run_edge())

    if not dest_mp3.is_file() or dest_mp3.stat().st_size < 200:
        raise RuntimeError("Edge TTS failed to synthesize an audio chunk")


def synthesize_curated_persona(
    text: str,
    persona_id: str,
    output_wav: Path | str,
    speed: float | str | None = "normal",
    log_fn: Optional[Callable[[str], None]] = None,
) -> Path:
    """
    Synthesize authentic native Burmese speech with persona-specific pitch,
    cadence, and broadcast studio audio mastering.
    """
    out_path = Path(output_wav).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("Cannot synthesize empty text.")

    info = get_curated_voice(persona_id) or CURATED_VOICES["komin"]
    voice = info.get("edge_voice", "my-MM-ThihaNeural")
    pitch = str(info.get("pitch") or "+0Hz").strip()
    if not (pitch.startswith("+") or pitch.startswith("-")):
        pitch = f"+{pitch}"
    rate_offset = int(info.get("rate_offset", 10))
    rate_str = compute_curated_rate(rate_offset, speed)
    formant_ratio = float(info.get("formant_ratio", 1.0) or 1.0)
    formant_prefix = ""
    if abs(formant_ratio - 1.0) > 0.005:
        sr_shift = int(round(44100 * formant_ratio))
        atempo_comp = 1.0 / formant_ratio
        formant_prefix = f"asetrate={sr_shift},aresample=44100,atempo={atempo_comp:.4f},"
    af_filter = info.get(
        "af_filter",
        (
            f"{formant_prefix}highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
        )
    )
    af_fallback = (
        f"{formant_prefix}highpass=f=75,deesser=i=0.5:m=0.5:f=0.5:s=o,"
        "equalizer=f=7200:width_type=q:w=2:g=-3.5,lowpass=f=9500,"
        "acompressor=threshold=-18dB:ratio=2.2:attack=20:release=120:makeup=1.2,alimiter=limit=0.92"
    )

    msg = f"🎙️ Curated Persona [{info['id']}] ({info['name_en']}): voice={voice}, rate={rate_str}, pitch={pitch}"
    logger.info(msg)
    if log_fn:
        try:
            log_fn(msg)
        except Exception:
            pass

    temp_mp3 = out_path.with_suffix(".persona_temp.mp3")
    temp_mp3.unlink(missing_ok=True)

    # Synthesize EVERY chunk of the text and concatenate the parts in
    # order (fix 2026-10-29): long input is never entrusted to a single
    # Edge request, so the delivered audio always covers the full text.
    text_chunks = _split_persona_text(clean_text)
    part_mp3s: List[Path] = []
    try:
        for idx, chunk_text in enumerate(text_chunks):
            part = out_path.with_suffix(f".persona_part{idx}.mp3")
            _edge_tts_to_mp3(chunk_text, voice, rate_str, pitch, part)
            part_mp3s.append(part)

        if len(part_mp3s) == 1:
            shutil.copy2(str(part_mp3s[0]), str(temp_mp3))
        else:
            ffmpeg_join = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
            if not ffmpeg_join:
                raise RuntimeError("FFmpeg is required to join persona TTS chunks")
            list_file = out_path.with_suffix(".persona_concat.txt")
            list_file.write_text(
                "".join(
                    "file '{}'\n".format(str(p.resolve()).replace("'", "'\\''"))
                    for p in part_mp3s
                ),
                encoding="utf-8",
            )
            try:
                subprocess.run(
                    [
                        ffmpeg_join, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "concat", "-safe", "0", "-i", str(list_file),
                        "-c:a", "libmp3lame", "-ar", "24000", "-ac", "1",
                        str(temp_mp3),
                    ],
                    check=True, capture_output=True, timeout=60,
                )
            finally:
                list_file.unlink(missing_ok=True)
    finally:
        for part in part_mp3s:
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass

    if not temp_mp3.is_file() or temp_mp3.stat().st_size < 1000:
        raise RuntimeError(f"Edge TTS failed to synthesize audio for persona {persona_id}")

    # Studio Broadcast Mastering via FFmpeg (44.1kHz mono PCM 16-bit WAV with exact header)
    #
    # Sample-rate fix (2026-10-29): edge_tts always delivers a 24000 Hz
    # MP3, but the persona af_filters were authored for a 44100 Hz input
    # — their leading asetrate=<44100 * formant_ratio> only makes sense
    # on a 44100 Hz stream. Fed the raw 24 kHz file, asetrate played it
    # ~1.76x too fast and too high (a ~42 s narration came out as
    # ~23 s at double pitch; live job 457f294a). Resampling to 44100 Hz
    # FIRST restores the authored behaviour: pitch shifts by exactly
    # formant_ratio and the duration is preserved.
    ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    converted_cleanly = False
    if ffmpeg_bin:
        for candidate_af in (af_filter, af_fallback):
            try:
                cmd = [
                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(temp_mp3),
                    "-af", f"aresample=44100,{candidate_af}",
                    "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
                    str(out_path)
                ]
                subprocess.run(cmd, check=True, capture_output=True, timeout=30)
                if out_path.is_file() and out_path.stat().st_size > 1000:
                    converted_cleanly = True
                    break
            except Exception as e_ff:
                logger.debug("FFmpeg persona mastering candidate failed: %s", e_ff)

    if not converted_cleanly:
        shutil.copy2(str(temp_mp3), str(out_path))

    try:
        temp_mp3.unlink(missing_ok=True)
    except Exception:
        pass

    return out_path
