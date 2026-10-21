"""
curated_voices.py - Pre-built Curated Voice Personas for VoxCPM Cloud AI.
Provides 6 meticulously tuned Burmese voice personas (3 Male, 3 Female)
with Gemini-optimized vocal control prompts for storytelling, movie recaps,
news documentaries, energetic TikTok/Shorts, audiobooks, and commercials.
"""

from __future__ import annotations
from typing import Dict, Any, List

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
        "control_instruction": (
            "A warm, deep, authoritative, emotionally resonant Burmese male movie recap storyteller. "
            "Calm, confident, cinematic narration with articulate Burmese pronunciation."
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
        "control_instruction": (
            "A professional, formal, crisp Burmese male documentary narrator and news anchor. "
            "Clear cadence, steady pace, articulate diction."
        ),
        "cfg_value": 2.2,
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
        "control_instruction": (
            "An enthusiastic, upbeat, vibrant young Burmese male creator. "
            "Energetic, casual, expressive, engaging rhythm ideal for TikTok and Shorts."
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
        "control_instruction": (
            "A gentle, sweet, emotionally tender Burmese female audiobook narrator. "
            "Soft, melodious tone, soothing and heartfelt delivery."
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
        "control_instruction": (
            "A dramatic, suspenseful, emotionally intense Burmese female movie recap narrator. "
            "Dynamic vocal contrast, gripping and captivating inflection."
        ),
        "cfg_value": 2.2,
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
        "control_instruction": (
            "A bright, articulate, friendly, and persuasive Burmese female commercial voice. "
            "Modern, polite, trustworthy, and cheerful."
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
        "control_instruction": (
            "A deep, bass-rich, resonant, dramatic blockbuster movie trailer narrator. "
            "Authoritative, powerful gravitas, cinematic impact with pristine Burmese pronunciation."
        ),
        "cfg_value": 2.3,
    },
    "komyat": {
        "id": "komyat",
        "gender": "male",
        "name_my": "ကိုမြတ် (သရဲတစ္ဆေ & လျှို့ဝှက်ဆန်းကြယ်)",
        "name_en": "Ko Myat (Spooky Mystery & Ghost Stories)",
        "desc_my": "ကြက်သီးထဖွယ် ညသန်းခေါင်ယံ သရဲဇာတ်လမ်းပြောသံ",
        "desc_en": "Chilling, mysterious, atmospheric storyteller for ghost stories and dark thrillers",
        "avatar": "👻",
        "badge_my": "သရဲတစ္ဆေ 🕯️",
        "badge_en": "Spooky Dark 🕯️",
        "control_instruction": (
            "A mysterious, chilling, suspenseful, dark Burmese male ghost story narrator. "
            "Atmospheric, brooding, captivating eerie cadence."
        ),
        "cfg_value": 2.1,
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
        "control_instruction": (
            "An articulate, engaging, intelligent Burmese male explainer. "
            "Clean, friendly, informative pacing ideal for technology, educational videos and tutorials."
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
        "control_instruction": (
            "An ultra-soft, serene, tranquil, whispery Burmese female voice. "
            "Extremely relaxing, gentle, calm tempo ideal for meditation, ASMR and bedtime stories."
        ),
        "cfg_value": 1.8,
    },
    "machaw": {
        "id": "machaw",
        "gender": "female",
        "name_my": "မချော (Anime Dub & Gaming Streamer)",
        "name_en": "Ma Chaw (Anime Dub & Gaming Streamer)",
        "desc_my": "ချစ်စရာကောင်းပြီး တက်ကြွလန်းဆန်းသော အန်နီမေးနှင့် ဂိမ်းအသံ",
        "desc_en": "Youthful, high-energy, expressive anime character and gaming streamer voice",
        "avatar": "🎮",
        "badge_my": "Anime & Gaming 🎀",
        "badge_en": "Anime & Gaming 🎀",
        "control_instruction": (
            "A high-energy, youthful, playful, vibrant Burmese female anime dubbing artist and gaming creator. "
            "Expressive, colorful intonation with bright personality."
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
        "control_instruction": (
            "An elegant, chic, highly sophisticated Burmese female influencer and brand ambassador. "
            "Confident, modern, silky smooth vocal texture for luxury and lifestyle."
        ),
        "cfg_value": 2.1,
    },
}


def get_curated_voice(voice_id: str) -> Dict[str, Any] | None:
    return CURATED_VOICES.get(str(voice_id).lower())


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
