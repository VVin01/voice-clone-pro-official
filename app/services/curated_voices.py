"""
curated_voices.py - Studio-Mastered Curated Voice Personas for Burmese TTS & Voice Clone Pro.
Provides 12 meticulously tuned Burmese voice personas (6 Male, 6 Female)
with authentic native Burmese pronunciation, tailored vocal pitch, pacing,
and broadcast-grade acoustic studio mastering (FFmpeg EQ, dynamics & limiting).
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
        "pitch": "-2Hz",
        "rate_offset": 12,
        "af_filter": (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=120:width_type=o:w=1:g=2.5,"
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
        "pitch": "+0Hz",
        "rate_offset": 18,
        "af_filter": (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=2200:width_type=o:w=1:g=1.2,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
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
        "pitch": "+2Hz",
        "rate_offset": 24,
        "af_filter": (
            "highpass=f=80,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
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
        "pitch": "-1Hz",
        "rate_offset": 6,
        "af_filter": (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=250:width_type=o:w=1:g=1.2,"
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
        "pitch": "-2Hz",
        "rate_offset": 14,
        "af_filter": (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=200:width_type=o:w=1:g=1.8,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
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
        "rate_offset": 18,
        "af_filter": (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
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
        "pitch": "-5Hz",
        "rate_offset": 8,
        "af_filter": (
            "highpass=f=60,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "bass=g=3:f=100,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9200,loudnorm=I=-15:TP=-1.0:LRA=7"
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
        "name_my": "ကိုမြတ် (သရဲတစ္ဆေ & လျှို့ဝှက်ဆန်းကြယ်)",
        "name_en": "Ko Myat (Spooky Mystery & Ghost Stories)",
        "desc_my": "ကြက်သီးထဖွယ် ညသန်းခေါင်ယံ သရဲဇာတ်လမ်းပြောသံ",
        "desc_en": "Chilling, mysterious, atmospheric storyteller for ghost stories and dark thrillers",
        "avatar": "👻",
        "badge_my": "သရဲတစ္ဆေ 🕯️",
        "badge_en": "Spooky Dark 🕯️",
        "edge_voice": "my-MM-ThihaNeural",
        "pitch": "-3Hz",
        "rate_offset": -4,
        "af_filter": (
            "highpass=f=65,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=150:width_type=o:w=1:g=1.5,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A mysterious, chilling, suspenseful, dark Burmese male ghost story narrator. "
            "Atmospheric, brooding, captivating cadence with organic breath and natural dynamics."
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
        "pitch": "+1Hz",
        "rate_offset": 18,
        "af_filter": (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
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
        "pitch": "-1Hz",
        "rate_offset": -8,
        "af_filter": (
            "highpass=f=80,afftdn=nr=12:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-4.0,"
            "lowpass=f=9000,loudnorm=I=-16:TP=-1.5:LRA=6"
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
        "name_my": "မချော (Anime Dub & Gaming Streamer)",
        "name_en": "Ma Chaw (Anime Dub & Gaming Streamer)",
        "desc_my": "ချစ်စရာကောင်းပြီး တက်ကြွလန်းဆန်းသော အန်နီမေးနှင့် ဂိမ်းအသံ",
        "desc_en": "Youthful, high-energy, expressive anime character and gaming streamer voice",
        "avatar": "🎮",
        "badge_my": "Anime & Gaming 🎀",
        "badge_en": "Anime & Gaming 🎀",
        "edge_voice": "my-MM-NilarNeural",
        "pitch": "+4Hz",
        "rate_offset": 22,
        "af_filter": (
            "highpass=f=80,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "A high-energy, youthful, playful, vibrant Burmese female anime dubbing artist and gaming creator. "
            "Expressive, colorful intonation with bright personality and crisp acoustic presence."
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
        "pitch": "-1Hz",
        "rate_offset": 12,
        "af_filter": (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
        ),
        "control_instruction": (
            "An elegant, chic, highly sophisticated Burmese female influencer and brand ambassador. "
            "Confident, modern, silky smooth vocal texture for luxury and lifestyle with zero distortion."
        ),
        "cfg_value": 2.1,
    },
}


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
    """Calculate the combined speech rate string for Edge TTS (e.g. '+18%')."""
    extra = 0
    if isinstance(user_speed, (int, float)):
        extra = int(round((float(user_speed) - 1.0) * 100))
    elif isinstance(user_speed, str):
        s = user_speed.strip().lower()
        if s in ("fast", "1.2x", "1.2"):
            extra = 15
        elif s in ("slow", "0.8x", "0.8"):
            extra = -15
        elif s in ("normal", "1.0x", "1.0"):
            extra = 0
        elif "%" in s:
            m = re.search(r"([+-]?\d+)", s)
            if m:
                extra = int(m.group(1))
    total = max(-50, min(100, base_offset + extra))
    return f"{total:+d}%"


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
    af_filter = info.get(
        "af_filter",
        (
            "highpass=f=75,afftdn=nr=10:nf=-50:tn=1,"
            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
            "lowpass=f=9500,loudnorm=I=-15:TP=-1.0:LRA=7"
        )
    )
    af_fallback = (
        "highpass=f=75,deesser=i=0.5:m=0.5:f=0.5:s=o,"
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

    import edge_tts
    temp_mp3 = out_path.with_suffix(".persona_temp.mp3")

    async def _run_edge():
        comm = edge_tts.Communicate(clean_text, voice, rate=rate_str, pitch=pitch)
        await comm.save(str(temp_mp3))

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            fut = executor.submit(lambda: asyncio.run(_run_edge()))
            fut.result(timeout=30)
    except Exception as e_run:
        logger.warning("ThreadPoolExecutor failed for persona TTS: %s; falling back to direct run", e_run)
        asyncio.run(_run_edge())

    if not temp_mp3.is_file() or temp_mp3.stat().st_size < 1000:
        raise RuntimeError(f"Edge TTS failed to synthesize audio for persona {persona_id}")

    # Studio Broadcast Mastering via FFmpeg (44.1kHz mono PCM 16-bit WAV with exact header)
    ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    converted_cleanly = False
    if ffmpeg_bin:
        for candidate_af in (af_filter, af_fallback):
            try:
                cmd = [
                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(temp_mp3),
                    "-af", candidate_af,
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
