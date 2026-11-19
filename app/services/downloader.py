"""
downloader.py - Core Downloader Engine
Supports:
- YouTube (Videos, Shorts, Music)
- TikTok (Videos, Audio, Watermark-free where available)
- Short URLs (bit.ly, tinyurl, youtu.be, vt.tiktok.com, vm.tiktok.com, etc.)
- Outputs: Video (MP4), Audio (MP3), Subtitles (SRT)
"""

import os
import re
import shutil
import tempfile
import threading
import time
import logging
import subprocess
import json
import uuid
import requests
import yt_dlp
from typing import Dict, Any, Optional, List, Tuple, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MEDIA_VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv', '.wmv', '.m4v', '.ts'}
MEDIA_AUDIO_EXTS = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.opus', '.wma'}
MEDIA_EXTS = MEDIA_VIDEO_EXTS | MEDIA_AUDIO_EXTS

# Final-audit batch M: every local-media ffmpeg call below is bounded.
# A wedged decoder on a malformed/hostile upload used to hang the
# calling job forever (these calls run before the recap engine's own
# per-call timeouts apply). Same scale as recap_engine.core's
# ffmpeg_timeout: a floor plus wall-clock per second of media; the
# thumbnail grab is a single frame and gets a short fixed cap.
LOCAL_MEDIA_TIMEOUT_FLOOR_SECONDS = 600
LOCAL_MEDIA_TIMEOUT_PER_SECOND = 30.0
THUMBNAIL_TIMEOUT_SECONDS = 60

# Batch AJ / L21: every ffprobe/ffmpeg invocation in this module may
# only open local files and pipes — never network protocols. The
# hunt confirmed (crafted ffconcat file with a video extension,
# probed through probe_local_media below) that playlist/concat-style
# inputs can make the demuxer follow embedded references; with no
# whitelist ffmpeg's protocol surface was unrestricted. `file` is
# required for every legitimate local input and `pipe` for the
# engine's pipe workflows; http/https/tcp/udp/... are refused.
MEDIA_PROTOCOL_WHITELIST = "file,pipe"

# Batch AJ / M10a: TOTAL wall-clock budget for one logical media
# download (all client attempts of it combined). The base yt-dlp
# opts carry retries:10 / fragment_retries:10 and a 20 s
# socket_timeout that only fires on an IDLE socket — a slow-drip
# host that keeps sending bytes could hold a download, and the
# recap slot behind it, indefinitely. The budget is enforced by a
# progress hook shared by every attempt of the download (see
# _attempt_plans): yt-dlp calls it as bytes arrive, and it raises
# MediaDownloadTimeoutError once the budget is spent.
MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS = 1800


class MediaDownloadTimeoutError(RuntimeError):
    """A media download exceeded its total wall-clock budget."""


def _media_deadline_progress_hook(*, started: float, budget_seconds: float,
                                  clock: Callable[[], float] = time.monotonic):
    """yt-dlp progress hook enforcing a total download budget.

    ``started``/``clock`` are injectable for tests. The hook raises
    MediaDownloadTimeoutError as soon as the elapsed wall clock
    exceeds the budget, aborting the in-flight download.
    """
    def _hook(_status: Dict[str, Any]) -> None:
        if clock() - started >= budget_seconds:
            raise MediaDownloadTimeoutError(
                f"Media download exceeded its total time budget of "
                f"{int(budget_seconds)}s — aborting instead of holding "
                "the download slot indefinitely."
            )
    return _hook


def _default_download_dir() -> str:
    """Batch AJ / L7: the default download directory lives under
    settings.storage_root (covered by the storage sweeps), not in
    os.getcwd()/downloads where nothing ever cleaned it. Falls back
    to the old cwd location only when the app settings package is
    unavailable (standalone engine use)."""
    try:
        from app.config import settings

        return str(settings.storage_root / "downloads")
    except Exception:
        return os.path.join(os.getcwd(), "downloads")


def _local_media_timeout(duration_seconds) -> int:
    """Wall-clock timeout (seconds) for an ffmpeg pass over this media."""
    try:
        duration = float(duration_seconds or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return int(max(LOCAL_MEDIA_TIMEOUT_FLOOR_SECONDS, LOCAL_MEDIA_TIMEOUT_PER_SECOND * max(0.0, duration)))

def is_local_media(path_or_url: str) -> bool:
    """Check if the provided string is a path to an existing local media file or media filename."""
    if not path_or_url:
        return False
    clean = str(path_or_url).strip('\'"')
    if clean.startswith(("http://", "https://")):
        return False
    if os.path.exists(clean):
        return True
    ext = os.path.splitext(clean)[1].lower()
    return ext in MEDIA_EXTS

def probe_local_media(file_path: str) -> Dict[str, Any]:
    """Inspect duration, streams, codec, resolution, and filesize via ffprobe."""
    file_path = str(file_path).strip('\'"')
    info: Dict[str, Any] = {
        "duration": 0.0,
        "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
        "width": None,
        "height": None,
        "vcodec": None,
        "acodec": None,
        "has_video": False,
        "has_audio": False,
    }
    try:
        cmd = [
            "ffprobe", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            try:
                info["duration"] = float(fmt.get("duration", 0.0))
            except (ValueError, TypeError):
                info["duration"] = 0.0
            try:
                info["size"] = int(fmt.get("size", info["size"]))
            except (ValueError, TypeError):
                pass
            streams = data.get("streams", [])
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
            if video_stream:
                info["has_video"] = True
                w = video_stream.get("width")
                h = video_stream.get("height")
                rotation = 0
                for side in video_stream.get("side_data_list") or []:
                    if "rotation" in side:
                        try:
                            rotation = int(round(float(side["rotation"])))
                        except (TypeError, ValueError):
                            pass
                if not rotation:
                    try:
                        rotation = int(round(float((video_stream.get("tags") or {}).get("rotate", 0) or 0)))
                    except (TypeError, ValueError):
                        rotation = 0
                if abs(rotation) % 180 == 90 and w and h:
                    w, h = h, w
                info["width"] = w
                info["height"] = h
                info["vcodec"] = video_stream.get("codec_name")
                if not info["duration"] and "duration" in video_stream:
                    try:
                        info["duration"] = float(video_stream["duration"])
                    except Exception:
                        pass
            if audio_stream:
                info["has_audio"] = True
                info["acodec"] = audio_stream.get("codec_name")
                if not info["duration"] and "duration" in audio_stream:
                    try:
                        info["duration"] = float(audio_stream["duration"])
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"ffprobe error on {file_path}: {e}")
    return info

def extract_thumbnail_from_media(file_path: str, output_path: str, timestamp_sec: float = 1.0) -> Optional[str]:
    """Capture a snapshot image from a video file using ffmpeg."""
    file_path = str(file_path).strip('\'"')
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        cmd = [
            "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-y",
            "-ss", str(max(0.1, timestamp_sec)),
            "-i", file_path,
            "-vf", "scale='min(720,iw)':-2",
            "-vframes", "1",
            "-q:v", "6",
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=THUMBNAIL_TIMEOUT_SECONDS)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception as e:
        logger.warning(f"Thumbnail extraction failed for {file_path}: {e}")
    return None

try:
    from app.services.transcriber import transcribe_to_srt
except ImportError:
    try:
        from transcriber import transcribe_to_srt
    except ImportError:
        transcribe_to_srt = None

try:
    from app.services.recap_engine.recap_pipeline import run_recap_pipeline, generate_recap_preview
except ImportError:
    try:
        from recap_engine.recap_pipeline import run_recap_pipeline, generate_recap_preview
    except ImportError as e:
        logger.error(f"Failed to import recap_engine: {e}")
        run_recap_pipeline = None
        generate_recap_preview = None

# Regex to detect URLs in a message
URL_REGEX = re.compile(
    r'(https?:\/\/(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'www\.[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'https?:\/\/[a-zA-Z0-9]+\.[^\s]{2,})'
)

def extract_first_url(text: str) -> Optional[str]:
    """Find and return the first valid HTTP/HTTPS URL from a string."""
    match = URL_REGEX.search(text)
    if match:
        url = match.group(0)
        # Strip trailing punctuation often caught in chat messages
        url = url.rstrip('.,!?:;)]}')
        return url
    return None

def unshorten_url(url: str) -> str:
    """
    Expands shortened URLs (bit.ly, tinyurl, vt.tiktok.com, v.douyin.com, youtu.be, etc.)
    to their canonical target URL. Handles Douyin (抖音) mobile share links and redirects.
    """
    clean_url = extract_first_url(url) or str(url).strip()

    # YouTube Shorts and youtu.be normalization to canonical watch?v= URL
    yt_match = re.search(r'(?:youtube\.com/shorts/|youtu\.be/)([a-zA-Z0-9_-]{11})', clean_url)
    if yt_match:
        return f"https://www.youtube.com/watch?v={yt_match.group(1)}"

    # Douyin (抖音) specific resolution
    clean_lower = clean_url.lower()
    if "douyin.com" in clean_lower or "iesdouyin.com" in clean_lower:
        # Check if already a full video/note URL
        vid_match = re.search(r'/(?:video|note)/(\d+)', clean_url)
        if vid_match:
            return f"https://www.douyin.com/video/{vid_match.group(1)}"

        # Follow Douyin short link (v.douyin.com), validating every
        # redirect hop as a public URL (SSRF prevention). A blocked hop
        # propagates so callers reject the URL instead of fetching it.
        try:
            from app.services.url_safety import SSRFBlockedError, follow_redirects_safely

            final_url = follow_redirects_safely(clean_url)
            vid_match2 = re.search(r'/(?:video|note|share/video)/(\d+)', final_url)
            if vid_match2:
                return f"https://www.douyin.com/video/{vid_match2.group(1)}"
            return final_url
        except SSRFBlockedError:
            raise
        except Exception as e:
            logger.warning(f"Douyin link expansion error for {clean_url}: {e}")
            return clean_url

    try:
        from app.services.url_safety import SSRFBlockedError, follow_redirects_safely

        # Redirects are followed hop-by-hop with each target validated
        # as public (SSRF prevention); no body is downloaded.
        return follow_redirects_safely(clean_url)
    except SSRFBlockedError:
        raise
    except Exception:
        # If redirect resolution fails, return original URL (yt-dlp will try to resolve it anyway)
        return clean_url

def detect_platform(url: str) -> str:
    """Identify the platform name from URL or local media for display."""
    if is_local_media(url):
        clean = str(url).strip('\'"')
        ext = os.path.splitext(clean)[1].lower()
        if ext in MEDIA_AUDIO_EXTS:
            return "Uploaded Audio"
        return "Uploaded Video"
    url_lower = url.lower()
    if "tiktok.com" in url_lower:
        return "TikTok"
    elif "douyin.com" in url_lower or "iesdouyin.com" in url_lower:
        return "Douyin (抖音)"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "YouTube"
    elif "facebook.com" in url_lower or "fb.watch" in url_lower:
        return "Facebook"
    elif "instagram.com" in url_lower:
        return "Instagram"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "X (Twitter)"
    else:
        return "Web Media"

def _is_direct_media_url(url: str) -> bool:
    """True when the URL itself points at a media file (direct-host
    link), as opposed to a platform page/share link."""
    try:
        path = urlparse(str(url or "")).path.lower()
    except Exception:
        return False
    return os.path.splitext(path)[1] in MEDIA_EXTS



# R40 deck K1: ONE shared Douyin message (was three near-identical
# copies in get_info / download_video / download_audio).
DOUYIN_BLOCKED_MESSAGE = (
    "⚠️ Douyin လင့်ခ်များကို တိုက်ရိုက် ဒေါင်းလုဒ် လုပ်၍ မရပါ — Douyin က ပြင်ပ ဒေါင်းလုဒ်ကို ပိတ်ထားပါသည်။ "
    "ဗီဒီယိုဖိုင်ကို ဖုန်း/ကွန်ပျူတာမှ တိုက်ရိုက် တင်၍ အသုံးပြုနိုင်ပါသည်။"
)

# R40 deck K2 fallback (used only if classify_source_error has no
# branch for the error): the classified message is the primary path.
YOUTUBE_BOT_WALL_MESSAGE = (
    "⚠️ YouTube က ကျွန်တော်တို့ ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်။ "
    "ဗီဒီယိုဖိုင်ကို တိုက်ရိုက် Upload တင်၍ သုံးနိုင်ပါသည်၊ သို့မဟုတ် TikTok / Facebook လင့်ခ်ဖြင့် စမ်းကြည့်ပါ။"
)


def recap_over_limit_message(duration_sec: float, max_duration_sec: float, is_vip: bool) -> str:
    """R40 deck K3: the ONE shared recap over-duration message.

    Was implemented three times (downloader recap path, api.py script
    phase, api.py generate) with drifting wording. Callers append
    their own context suffix (split hint / VIP upsell) as needed.
    """
    m = int(float(duration_sec) // 60)
    sec = int(float(duration_sec) % 60)
    max_m = int(float(max_duration_sec) // 60)
    tier_label = "VIP" if is_vip else "အခမဲ့ (Free)"
    return (
        f"ဗီဒီယို ကြာချိန် ({m:02d}:{sec:02d}) သည် {tier_label} အသုံးပြုသူ "
        f"ကန့်သတ်ချက် (အများဆုံး {max_m} မိနစ်) ထက် ကျော်လွန်နေပါသည်။"
    )


# Batch DF (2026-11-10): the one message every no-audio surface uses —
# the failure classifier below, the submit guard, and (mirrored copy)
# the Mini App upload warning. A recap source with NO audio track can
# never be recapped: the planner's first step extracts the speech
# track. On 2026-11-10 a new user retried the same silent file four
# times in six minutes because every failure hid behind the generic
# technical-error line.
NO_AUDIO_TRACK_MESSAGE = (
    "⚠️ ဒီ ဗီဒီယိုမှာ အသံ လုံးဝ မပါဝင်ပါ။ Recap လုပ်ဖို့ အသံပါတဲ့ "
    "မူရင်း ဗီဒီယို လိုပါတယ် — အသံပါတဲ့ ဖိုင်ဖြင့် ပြန်စမ်းကြည့်ပါ ခင်ဗျာ။"
)


def classify_source_error(err_msg: str, url: str) -> Optional[str]:
    """Map a raw yt-dlp / extractor error to a curated user-facing message.

    Hunt #6: until now only two failure classes had human messages
    (Douyin anti-bot, YouTube sign-in). Everything else — a TikTok
    "No video formats found!", a share-site HTTP 403, a removed or
    private video — reached the user as the raw English extractor
    string (stored verbatim on the job and shown in the bot failure
    notice and the Mini App job detail), with no hint of what to do
    differently. This classifier covers the recurring source-side
    classes; the raw text always stays in the logs.

    Returns None for unrecognised errors so callers keep their
    existing behaviour. Every returned message starts with "⚠️" — the
    convention the API layer uses to recognise an already user-safe
    detail string (the same convention the Douyin/YouTube messages
    below already follow).
    """
    low = str(err_msg or "").lower()
    platform = detect_platform(url or "")
    specific = platform not in ("Web Media", "Uploaded Video", "Uploaded Audio")
    subj = platform if specific else "ဒီ ဝက်ဘ်ဆိုက်"
    video_ref = f"{platform} ဗီဒီယို" if specific else "ဗီဒီယို"
    upload_hint = (
        "ဗီဒီယိုဖိုင်ကို Mini App ရှိ '📁 Video Upload' မှ တိုက်ရိုက် တင်၍ "
        "ပြုလုပ်နိုင်ပါသည် ခင်ဗျာ။"
    )

    if "no space left on device" in low or "errno 28" in low:
        # Server storage exhausted (Batch W, 2026-11-04): every recap
        # failure that day died on the full persistent disk — "[Errno
        # 28] No space left on device" raised by yt-dlp's file write
        # or by the job-dir mkdir — and matched no needle, so users
        # saw the generic technical-error line for a problem that is
        # neither their link's nor the source's fault. Errno 28 IS
        # ENOSPC by definition, so this needle cannot overreach onto
        # source-side errors. NOTE (merge coordination): Batch V adds
        # its whisper needles in this same function — the two blocks
        # are independent and can land in either order.
        return (
            "⚠️ ဆာဗာ သိုလှောင်နေရာ ပြည့်နေပါသဖြင့် ဒီ အလုပ်ကို လုပ်ဆောင်၍ မရပါ။ "
            "ပြဿနာကို ပြုပြင်နေပါသည် — ခဏစောင့်ပြီး ပြန်စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
    if "unsupported url" in low:
        return (
            "⚠️ ဒီ လင့်ခ် အမျိုးအစား ကို မထောက်ပံ့သေးပါ။ "
            "YouTube၊ TikTok၊ Facebook လင့်ခ်များဖြင့် စမ်းကြည့်ပါ။ " + upload_hint
        )
    if "matches no streams" in low or "failed to set value '0:a" in low:
        # Batch DF (2026-11-10): the source has NO audio stream, so
        # the planner's Whisper-prep ffmpeg (-map 0:a:0) aborts with
        # "Stream map '' matches no streams." — the raw RuntimeError
        # matched no needle and users saw the generic line, then
        # retried the same silent file (four attempts in six minutes,
        # jobs rc_1794268862_*). Retrying cannot help; the user needs
        # a file that actually carries sound.
        return NO_AUDIO_TRACK_MESSAGE
    if (
        "could not download the whisper model" in low
        or "local whisper backend" in low
    ):
        # Batch V (2026-11-04): the local-Whisper transcription
        # fallback could not obtain its model ("Could not download
        # the Whisper model from huggingface.co …" after 3 attempts)
        # or found no local backend ("Local Whisper backend မတွေ့ပါ …").
        # Neither string matched a needle, so recap jobs reaching the
        # fallback died at the transcription stage behind the generic
        # technical-error message. This is a server-side transient:
        # same remedy shape as the network class — wait and retry.
        # The needles are the exceptions' own distinctive openings,
        # so unrelated download/whisper wording stays unclassified.
        # (Placed early: the download error embeds the underlying
        # network exception text, which broader needles below could
        # otherwise claim first.)
        return (
            "⚠️ စာသားပြောင်းစနစ် ခေတ္တ အလုပ်များနေပါသည်။ "
            "ခဏစောင့်ပြီး ပြန်စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
    if "no_usable_speech" in low or "music_only_or_transcript_too_thin" in low:
        # Batch V (2026-11-04): the source was fetched correctly but
        # holds no usable speech — whisper_service raises
        # NoUsableSpeechError with these codes (no audio stream,
        # 0 usable transcript segments, or a music/noise-only rescue
        # the quality guard rejected). Retrying the same source
        # cannot change the outcome; the user needs a source with
        # clear speech.
        return (
            "⚠️ ဒီ ဗီဒီယို၏ အသံတွင် စကားပြောသံ မပါဝင်ပါ (သို့မဟုတ်) "
            "အသုံးပြု၍ ရအောင် ရှင်းလင်းမှု မရှိပါ။ စကားပြောသံ ရှင်းလင်းသော "
            "အခြား ဗီဒီယိုဖြင့် စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
    if (
        ("http error 403" in low or "http error 401" in low
         or ("403" in low and "forbidden" in low)
         or ("401" in low and "unauthorized" in low))
        and _is_direct_media_url(url or "")
    ):
        # Direct-host refusal (R39): the URL is a media FILE and its
        # host answered 403/401 — e.g. Google's GTV sample bucket now
        # refuses datacenter egress wholesale (measured 2026-11-02:
        # bare curl gets the same 403 from every datacenter IP). That
        # is NOT an "invalid link" and NOT a platform block: the file
        # exists, its host will not serve OUR server. Distinct copy:
        # name the host refusal and point at a different source or a
        # direct upload. (The client recognises this sentence as a
        # blocked-source marker — see blockedLinkInfo in app.js.)
        return (
            "⚠️ ဒီ ဗီဒီယိုဖိုင်၏ မူရင်း ဆာဗာ (host) က ကျွန်တော်တို့ ဆာဗာမှ ဆွဲယူခြင်းကို "
            "ငြင်းပယ်ထားပါသည်။ အခြား အရင်းအမြစ် လင့်ခ် ဖြင့် စမ်းကြည့်ပါ။ " + upload_hint
        )
    if (
        ("403" in low and ("forbidden" in low or "http error" in low))
        # Per-item IP block (TikTok: "Your IP address is blocked from
        # accessing this post"). The wording carries no 403/forbidden,
        # so this branch used to miss it and the raw yt-dlp string was
        # stored on the job and rendered verbatim in History (jobs
        # rc_1793505308_08cd6b and rc_1793511624_d4c5a1, 2026-11-01 —
        # the same post, refused identically on every attempt while
        # neighbouring TikTok posts extracted fine from the same
        # server). The user meaning is the same as a 403 wall — the
        # source refuses OUR server's fetch of this item — so it
        # shares this branch's copy (the client recognises that exact
        # sentence as the blocked-source marker, see blockedLinkInfo).
        or "ip address is blocked" in low
        or "ip is blocked" in low
        or "blocked from accessing" in low
    ):
        return f"⚠️ {subj} က ကျွန်တော်တို့ ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်။ " + upload_hint
    if (
        "sign in" in low
        or "log in" in low
        or "login required" in low
        or "fresh cookies" in low
        or "use --cookies" in low
    ):
        return (
            f"⚠️ {subj} က ဒီ ဗီဒီယို ကို ကြည့်ရန် အကောင့် ဝင်ရန် တောင်းဆိုနေပါသည်။ "
            "အခြား လင့်ခ် ဖြင့် စမ်းကြည့်ပါ။ " + upload_hint
        )
    if (
        "no video formats found" in low
        or "video unavailable" in low
        or "private video" in low
        or "no longer available" in low
        or "has been removed" in low
        or "could not extract media info" in low
    ):
        return (
            f"⚠️ ဒီ {video_ref} ကို ဖွင့်၍ မရပါ — ဖျက်ထားခြင်း သို့မဟုတ် private "
            "ဖြစ်နေခြင်း ဖြစ်နိုင်ပါသည်။ လင့်ခ် မှန်မမှန် ပြန်စစ်ပါ။ "
            "ဗီဒီယို ရှိနေသေးရင် ဖိုင်ကို တိုက်ရိုက် တင်၍ စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
    if "failed to extract" in low or "unable to extract" in low:
        # Generic yt-dlp extraction failure — canonically YouTube's
        # "Failed to extract any player response; please report this
        # issue on https://github.com/yt-dlp/yt-dlp/issues …". The raw
        # string (GitHub-issues tail included) reached the user
        # verbatim in the bot-chat recap failure notice (journey audit
        # 2026-11-02). The user meaning is the blocked family's: the
        # source would not let our server extract this item, so it
        # shares that copy (and its upload guidance) exactly.
        # "Unable to extract …" is the same failure in the wording
        # other extractors use (audit fix 2026-11-03).
        return f"⚠️ {subj} က ကျွန်တော်တို့ ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်။ " + upload_hint
    if "http error 429" in low or "too many requests" in low:
        # Rate limited by the source (audit fix 2026-11-03): this
        # class matched no needle, so the raw yt-dlp string reached
        # the user. The remedy is simply to wait and retry, and the
        # copy says exactly that.
        return (
            f"⚠️ {subj} က တောင်းဆိုမှု များလွန်းသဖြင့် ယာယီ ကန့်သတ်"
            "ထားပါသည်။ ခဏစောင့်ပြီး ပြန်စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
    if (
        "timed out" in low
        or "timeout" in low
        or "connection reset" in low
        or "connection aborted" in low
        or "connection refused" in low
        or "temporary failure" in low
        or "urlopen error" in low
        or "eof occurred" in low
        or "http error 5" in low
    ):
        return (
            "⚠️ ကွန်ရက် ချိတ်ဆက်မှု ယာယီ ပြဿနာ ဖြစ်သွားပါသည်။ "
            "ခဏစောင့်ပြီး ပြန်စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
    if low.startswith("error: ["):
        # Last-resort catch for any other raw yt-dlp "ERROR:
        # [extractor] …" string. This classifier is only invoked for
        # source-extraction failures, so an unrecognised extractor
        # error still maps to the generic extraction message instead
        # of leaking raw English (and repo URLs) to the user. Scoped
        # to yt-dlp's bracketed shape on purpose: a bare "ERROR: …"
        # string is not necessarily a source error and must stay
        # unclassified (hunt-6 / TikTok-block no-overreach pins).
        return (
            f"⚠️ ဒီ {video_ref} ကို ဆာဗာမှ ထုတ်ယူ၍ မရပါ။ လင့်ခ် မှန်မမှန် "
            "ပြန်စစ်ပြီး ထပ်စမ်းကြည့်ပါ။ " + upload_hint
        )
    return None


# ── R39: extraction-boost chain + guarded tikwm fallback ────────────
#
# Measured 2026-11-02 (extract-boost-2026-11-02/REPORT.md): from
# datacenter IPs, YouTube's default yt-dlp client chain is walled
# (sign-in challenge cookieless; player-less / format-stripped
# responses even with the prod cookie session), while
# player_client=web_embedded returns the full HD ladder and
# android/mweb answer at a 360p ceiling. Antigravity second-opinion:
# web_embedded goes FIRST (the default chain burns time and bot-score
# when it always fails first); the cookie'd default stays in the
# chain second for cookie-rescuable (age/region) videos; android/mweb
# are low-res last resorts and their results are flagged low_res.

YOUTUBE_ATTEMPT_SOCKET_TIMEOUT = 20   # bounded per-attempt socket timeout (media stage)
# R39.1 info-stage latency trim: R39 shipped retries=2 with a 20s
# socket cap at the info stage, and a fully-walled YouTube URL took
# ~68–78s to fail on prod (~17–19s per client). The per-client cost
# is multiplicative: the outer `retries` wraps the whole extract_info
# call while `extractor_retries` re-fetches inside the extractor, so
# 2/2 allowed up to (2+1)x(2+1)=9 fetch rounds per client, each
# round's sockets capped at 20s. The info stage now runs retries=1
# (≤2x2=4 rounds) under a 10s socket cap: the observed fast-fail mode
# ("Failed to extract any player response", ~2s/round) lands at
# ~7–9s per client, ≤ ~40s for the full 4-client chain. Media-byte
# budgets are untouched (retries:10, socket_timeout 20).
INFO_STAGE_RETRIES = 1                # info extraction fails fast; retries:10 stays for media bytes
INFO_STAGE_SOCKET_TIMEOUT = 10        # info-stage socket cap; media keeps YOUTUBE_ATTEMPT_SOCKET_TIMEOUT

# ── PO Token sidecar (bgutil-ytdlp-pot-provider) ────────────────────
#
# R39/R39.1 proved YouTube's datacenter bot wall is Render egress-IP
# reputation: every client fails "Failed to extract any player
# response" from prod while the identical code succeeds elsewhere.
# The owner-approved route (2026-11-02) is a proof-of-origin token
# provider: the bgutil server runs inside the same container on
# loopback (see Dockerfile + docker-entrypoint.sh) and the pip plugin
# bgutil-ytdlp-pot-provider feeds its tokens to yt-dlp's POT
# framework, making the web clients' requests look legitimate despite
# the IP reputation. Wiring lives ONLY in _attempt_plans below: every
# YouTube attempt carries the provider base_url; non-YouTube paths
# are untouched. Degradation is graceful by design — when the server
# is unreachable the plugin warns and extraction continues exactly as
# before (proven locally with the server stopped).
#
# FORCED-MINT EXPERIMENT — CONCLUDED 2026-11-02: the first sidecar
# prod proof failed on all 3 attempts with NO /get_pot activity
# visible, so PR #51 forced the 'youtube' extractor arg
# fetch_pot='always' for one prod round. On prod the fetches then
# fired and the mints completed (zero fetch errors in the proof
# window) — and YouTube still refused this egress IP at the
# player-response stage on every attempt. The PO-token route is
# exhausted; 'always' only added latency to walled failures, so the
# R40 release merge reverts the fetch policy to yt-dlp's default
# 'auto' (no fetch_pot arg is set anywhere). The provider base_url
# wiring below and _PotLogShim (log observability, see its
# docstring) are kept exactly as-is.
POT_SERVER_DEFAULT_URL = "http://127.0.0.1:4416"


def _pot_extractor_args() -> Dict[str, Any]:
    """Extractor args wiring yt-dlp's POT framework to the local
    bgutil provider server (env-overridable for local testing).

    ONLY the provider base_url is set. The POT fetch policy
    ('youtube' extractor arg 'fetch_pot', read by yt-dlp core's
    YoutubeIE._fetch_po_token) is deliberately left unset, i.e. at
    core's default 'auto': a token is fetched only when a request
    is pre-marked required. The one prod round that forced
    'always' (PR #51, 2026-11-02) proved tokens mint fine yet do
    not pass this egress IP's wall — see the block comment above.
    """
    base_url = os.getenv("POT_SERVER_URL") or POT_SERVER_DEFAULT_URL
    return {
        'youtubepot-bgutilhttp': {'base_url': [base_url]},
    }


class _PotLogShim:
    """Scoped yt-dlp logger for YouTube attempts (observability).

    yt-dlp routes ALL of its screen output through params['logger']
    when one is set (to_screen → logger.debug, report_warning →
    logger.warning, to_stderr → logger.error). Without a logger the
    app runs yt-dlp quiet and the PO Token provider's fetch line —
    '[pot:bgutil:http] Generating a <context> PO Token for <client>
    client via bgutil HTTP server', emitted at provider-info level —
    never reaches the app logs, which is why the first prod proof
    could not tell whether a token was ever requested. This shim is
    attached ONLY to YouTube attempts (in _with_pot): debug/info
    chatter is forwarded to the app logger only when it concerns the
    POT provider, warnings/errors are forwarded whole (the container
    captured them on stderr before), and everything else is dropped,
    preserving the quiet behaviour. Grep the app logs for
    'PO Token' / '[pot:' to see provider fetch activity.
    """

    _POT_MARKERS = ("[pot:", "PO Token", "get_pot", "bgutil")

    @classmethod
    def _is_pot(cls, message: Any) -> bool:
        text = str(message)
        return any(marker in text for marker in cls._POT_MARKERS)

    def debug(self, message: Any) -> None:
        if self._is_pot(message):
            logger.info("yt-dlp: %s", message)

    def info(self, message: Any) -> None:
        if self._is_pot(message):
            logger.info("yt-dlp: %s", message)

    def warning(self, message: Any) -> None:
        logger.warning("yt-dlp: %s", message)

    def error(self, message: Any) -> None:
        logger.error("yt-dlp: %s", message)


_POT_LOG_SHIM = _PotLogShim()

# ── WARP egress sidecar (Cloudflare WARP via usque) ────────────────
#
# The POT sidecar alone did NOT pass the wall on prod (forced-mint
# proof, 2026-11-02): tokens minted and attached, YouTube still
# refused the player response — the wall keys on the egress IP
# itself. The owner-approved free-egress route is Cloudflare WARP:
# feasibility-proven with this app's own extraction code (Shorts 153
# formats/3840p, Watch 38 formats/1080p, on two WARP egress IPs,
# POT minting at the same time — see warp-test/REPORT.md). The usque
# client runs inside the same container (Dockerfile +
# docker-entrypoint.sh) in MASQUE-over-HTTP/2 mode (TCP — Render
# restricts UDP egress) and exposes SOCKS5 on loopback; its anonymous
# device config is registered at first boot and kept on the
# persistent disk, never in the image (the repo is public).
#
# Wiring lives in the SAME place as POT (_with_pot below): every
# YouTube attempt additionally carries this proxy; non-YouTube plans
# never do (direct TikTok extraction is healthy, and TikTok's web
# front door dislikes the WARP egress). Graceful by design: if the
# tunnel is down the loopback connection is refused, the attempt
# fails fast, and the chain/app carry on exactly as a walled
# attempt does today — a dead WARP can never take the bot down.
WARP_PROXY_DEFAULT_URL = "socks5h://127.0.0.1:18080"


def _warp_proxy_url() -> Optional[str]:
    """Proxy URL routed to YouTube attempts (WARP sidecar egress).

    Env WARP_PROXY_URL overrides the loopback default; an explicitly
    EMPTY value disables the proxy entirely (attempts then carry no
    'proxy' key, byte-identical to the pre-WARP wiring). 'socks5h'
    (remote DNS) so name lookups also egress through the tunnel.
    """
    raw = os.getenv("WARP_PROXY_URL")
    if raw is None:
        return WARP_PROXY_DEFAULT_URL
    raw = raw.strip()
    return raw or None

# Hard ceiling for any single downloaded source file (audit fix
# 2026-11-03): yt-dlp receives it as max_filesize and the tikwm byte
# fetch enforces the same cap while streaming. Disk protection only —
# legitimate sources are far below 5 GB.
MAX_DOWNLOAD_BYTES = 5 * 1024 ** 3

# tikwm resolver guardrails (failure-triggered only): hard timeouts,
# response validation, and an in-memory circuit breaker so a sick or
# hostile resolver can never stall or poison the extraction path.
TIKWM_API_URL = "https://www.tikwm.com/api/"
TIKWM_TIMEOUT = 2.5                   # seconds — API call AND validation probe
TIKWM_MEDIA_READ_TIMEOUT = 30         # seconds — streaming the CDN bytes after validation
TIKWM_BREAKER_THRESHOLD = 3           # consecutive resolver failures that open the breaker
TIKWM_BREAKER_OPEN_SECONDS = 30 * 60  # breaker stays open 30 minutes
TIKWM_MIN_MEDIA_BYTES = 10_000        # a real TikTok mp4 is never this small
_TIKWM_CDN_HOST_RE = re.compile(r"(^|\.)(tiktokcdn(-[a-z0-9]+)?|tiktokv)\.com$", re.IGNORECASE)


def _is_youtube_url(url: str) -> bool:
    u = str(url or "").lower()
    return "youtube.com" in u or "youtu.be" in u


def _is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in str(url or "").lower()


# Definitive content truths: rotating to another player client can
# never resurrect a private/deleted/removed video or make an
# unsupported URL supported, so the chain stops at the first attempt.
_NON_ROTATABLE_NEEDLES = (
    "unsupported url",
    "private video",
    "video is private",
    "video unavailable",
    "no longer available",
    "has been removed",
)


def _is_rotation_worthy_error(err_msg: str) -> bool:
    """Only extraction-class failures (sign-in wall, player response,
    no formats, format unavailable, network) are worth another client."""
    low = str(err_msg or "").lower()
    return not any(n in low for n in _NON_ROTATABLE_NEEDLES)


def _build_video_format_spec(max_height: Optional[int]) -> str:
    """Build an orientation-aware yt-dlp format selector string.

    ``max_height`` caps the short ("p") side of the frame:
    - Portrait sources (``aspect_ratio < 1``): ``width <= max_height``.
    - Landscape / square sources: ``height <= max_height``.
    Preserves the H.264 MP4 + M4A preference and existing fallback ladder.
    """
    if not max_height or max_height <= 0:
        return (
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/"
            "best"
        )
    return (
        f"bestvideo[aspect_ratio<1][width<={max_height}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo[aspect_ratio<1][width<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[aspect_ratio<1][width<={max_height}]+bestaudio/"
        f"best[aspect_ratio<1][width<={max_height}]/"
        f"bestvideo[height<={max_height}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[width<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={max_height}]+bestaudio/"
        f"bestvideo[width<={max_height}]+bestaudio/"
        f"best[height<={max_height}]/"
        f"best[width<={max_height}]/"
        "bestvideo+bestaudio/"
        "best"
    )


def _local_video_scale_filter(
    width: Optional[int], height: Optional[int], max_height: Optional[int]
) -> Optional[str]:
    """Return an orientation-aware ffmpeg scale filter, or None if under cap.

    ``max_height`` caps the short ("p") side: width for portrait videos
    (``height > width > 0``) and height for landscape/square videos,
    never upscaling.
    """
    if not max_height or max_height <= 0:
        return None
    w = int(width or 0)
    h = int(height or 0)
    is_portrait = bool(w > 0 and h > w)
    limiting_dim = w if is_portrait else h
    if limiting_dim <= max_height:
        return None
    if is_portrait:
        return f"scale='min({max_height},iw)':-2"
    return f"scale=-2:'min({max_height},ih)'"


def _best_format_height(info: Optional[Dict[str, Any]]) -> Optional[int]:
    """Highest video format height in an info dict (None when unknown)."""
    if not isinstance(info, dict):
        return None
    heights = []
    top_h = info.get("height")
    if isinstance(top_h, (int, float)) and top_h:
        heights.append(int(top_h))
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("vcodec") in (None, "none"):
            continue
        fh = fmt.get("height")
        if isinstance(fh, (int, float)) and fh:
            heights.append(int(fh))
    return max(heights) if heights else None


def _flag_low_res(info: Dict[str, Any], url: str, client_label: str) -> bool:
    """Record low_res on an info dict when the best format is ≤360p
    (the android/mweb rescue ceiling) and log it loudly."""
    best_h = _best_format_height(info)
    if best_h is not None and best_h <= 360:
        info["low_res"] = True
        logger.warning(
            f"LOW_RES source via client '{client_label}': best format height "
            f"{best_h}p (<=360p) for {url}"
        )
        return True
    info.setdefault("low_res", False)
    return False


def _valid_tikwm_play_url(play_url: Any) -> bool:
    """A tikwm play URL is accepted only as an https URL on a TikTok
    CDN host (tiktokcdn*.com / tiktokv.com)."""
    if not isinstance(play_url, str) or not play_url.startswith("https://"):
        return False
    try:
        host = urlparse(play_url).hostname or ""
    except Exception:
        return False
    return bool(_TIKWM_CDN_HOST_RE.search(host))


class _TikwmCircuitBreaker:
    """In-memory breaker for the tikwm resolver: after
    TIKWM_BREAKER_THRESHOLD consecutive failures it opens for
    TIKWM_BREAKER_OPEN_SECONDS and the fallback is skipped entirely."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_until = 0.0

    def allow(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._opened_until

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= TIKWM_BREAKER_THRESHOLD:
                self._opened_until = time.monotonic() + TIKWM_BREAKER_OPEN_SECONDS

    def reset(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_until = 0.0


_TIKWM_BREAKER = _TikwmCircuitBreaker()


def format_duration(seconds: Optional[int]) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if not seconds:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def format_filesize(bytes_val: Optional[int]) -> str:
    """Format bytes into MB or KB string."""
    if not bytes_val:
        return "N/A"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.1f} MB"
    kb = bytes_val / 1024
    return f"{kb:.1f} KB"

def find_best_preview_timestamp(video_path: str, target_dir: Optional[str] = None) -> float:
    """
    Find the most optimal timestamp (in seconds) containing original subtitles/dialogue for preview frame.
    Checks:
    1. Existing .srt / .vtt / transcript.json in target_dir or video directory.
    2. Embedded subtitle tracks in the video file via ffprobe.
    3. Safe dialogue start heuristic based on video duration (avoiding opening logos/black frames).
    """
    video_path = str(video_path).strip('\'"')
    search_dirs = []
    if target_dir and os.path.isdir(target_dir):
        search_dirs.append(target_dir)
        ws = os.path.join(target_dir, "workspace")
        if os.path.isdir(ws):
            search_dirs.append(ws)
    if os.path.isfile(video_path):
        search_dirs.append(os.path.dirname(os.path.abspath(video_path)))

    # 1. Check existing subtitle/transcript files on disk
    for d in search_dirs:
        try:
            for fname in os.listdir(d):
                full_p = os.path.join(d, fname)
                if not os.path.isfile(full_p):
                    continue
                lower_name = fname.lower()
                if lower_name.endswith(('.srt', '.vtt')) and not lower_name.startswith('preview'):
                    try:
                        with open(full_p, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read(50000)
                        pattern = r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})'
                        matches = re.findall(pattern, content)
                        for m in matches:
                            sh, sm, ss, sms, eh, em, es, ems = map(int, m)
                            st = sh * 3600 + sm * 60 + ss + sms / 1000.0
                            et = eh * 3600 + em * 60 + es + ems / 1000.0
                            if 4.0 <= st <= 90.0:
                                return round((st + et) / 2.0, 2)
                        if matches:
                            sh, sm, ss, sms, eh, em, es, ems = map(int, matches[0])
                            st = sh * 3600 + sm * 60 + ss + sms / 1000.0
                            et = eh * 3600 + em * 60 + es + ems / 1000.0
                            if st > 0.5:
                                return round((st + et) / 2.0, 2)
                    except Exception as e:
                        logger.debug(f"Error reading sub file {full_p}: {e}")

                elif lower_name in ('whisper_transcript.json', 'atomic_units.json', 'plan.json', 'transcript.json'):
                    try:
                        with open(full_p, 'r', encoding='utf-8', errors='ignore') as f:
                            data = json.load(f)
                        items = []
                        if isinstance(data, list):
                            items = data
                        elif isinstance(data, dict):
                            items = data.get('segments') or data.get('items') or data.get('atomic_units') or []
                        for it in items:
                            if isinstance(it, dict) and 'start' in it and 'end' in it:
                                st = float(it['start'])
                                et = float(it['end'])
                                if 4.0 <= st <= 90.0:
                                    return round((st + et) / 2.0, 2)
                        if items and isinstance(items[0], dict) and 'start' in items[0]:
                            st = float(items[0]['start'])
                            et = float(items[0].get('end', st + 2.0))
                            if st > 0.5:
                                return round((st + et) / 2.0, 2)
                    except Exception as e:
                        logger.debug(f"Error reading json transcript {full_p}: {e}")
        except Exception:
            pass

    # 2. Check embedded subtitle stream in the video container via ffprobe
    if os.path.isfile(video_path):
        try:
            cmd = [
                "ffprobe", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-v", "error",
                "-select_streams", "s",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                video_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            if res.returncode == 0 and res.stdout.strip():
                cmd2 = [
                    "ffprobe", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-v", "error",
                    "-select_streams", "s:0",
                    "-show_entries", "packet=pts_time",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    "-read_intervals", "0%+120",
                    video_path
                ]
                res2 = subprocess.run(cmd2, capture_output=True, text=True, check=False, timeout=30)
                if res2.returncode == 0 and res2.stdout.strip():
                    for line in res2.stdout.strip().splitlines():
                        try:
                            t_val = float(line.strip())
                            if 3.0 <= t_val <= 120.0:
                                return round(t_val + 0.5, 2)
                        except ValueError:
                            continue
        except Exception as e:
            logger.debug(f"ffprobe subtitle check error: {e}")

    # 3. Intelligent heuristic based on duration (skips intro logos)
    dur = 0.0
    if os.path.isfile(video_path):
        try:
            dur = probe_local_media(video_path).get("duration", 0.0)
        except Exception:
            dur = 0.0

    if dur >= 35.0:
        return 14.5
    elif dur >= 15.0:
        return round(dur * 0.35, 2)
    elif dur > 4.0:
        return round(dur * 0.30, 2)
    return 3.5


class MediaDownloader:
    find_best_preview_timestamp = staticmethod(find_best_preview_timestamp)
    def __init__(self, download_dir: Optional[str] = None):
        self.download_dir = download_dir or _default_download_dir()
        os.makedirs(self.download_dir, exist_ok=True)

    def _get_base_opts(self, target_url: str = "") -> Dict[str, Any]:
        """Common yt-dlp options for reliable extraction, JS challenge solving, and 403 bypass."""
        opts = {
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            # TLS certificates are verified (audit fix 2026-11-03: the
            # old 'nocheckcertificate': True disabled verification for
            # every source host).
            'max_filesize': MAX_DOWNLOAD_BYTES,
            'retries': 10,
            'fragment_retries': 10,
            'concurrent_fragment_downloads': 4,
            'file_access_retries': 5,
            'remote_components': ['ejs:github'],
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
            },
        }

        target_lower = str(target_url or "").lower()
        if "douyin.com" in target_lower or "iesdouyin.com" in target_lower:
            opts['http_headers']['Referer'] = 'https://www.douyin.com/'

        # Enable Node.js for solving YouTube JS / n challenges if installed or available via package
        node_bin = shutil.which("node") or shutil.which("nodejs")
        if not node_bin:
            for sys_cand in ["/usr/bin/node", "/usr/bin/nodejs", "/usr/local/bin/node", "/opt/render/project/src/.venv/bin/node"]:
                if os.path.isfile(sys_cand) and os.access(sys_cand, os.X_OK):
                    node_bin = sys_cand
                    break
        if not node_bin:
            try:
                import nodejs_wheel.executable
                cand = os.path.join(nodejs_wheel.executable.ROOT_DIR, 'node.exe' if os.name == 'nt' else 'bin/node')
                if not os.path.exists(cand):
                    cand = os.path.join(nodejs_wheel.executable.ROOT_DIR, 'node')
                if os.path.exists(cand):
                    node_bin = cand
            except Exception:
                pass

        if node_bin:
            opts['js_runtimes'] = {'node': {'path': node_bin}}
            logger.info(f"Using Node.js runtime for YouTube JS challenges: {node_bin}")

        # Intelligent Multi-Platform Cookies Loading (YouTube & Douyin combined support)
        cookie_path = None
        yt_env = (os.getenv("YOUTUBE_COOKIES") or "").strip()
        dy_env = (os.getenv("DOUYIN_COOKIES") or "").strip()
        general_env = (os.getenv("COOKIES_FILE") or os.getenv("COOKIES_CONTENT") or "").strip()

        combined_cookies = []
        if dy_env and ("# Netscape" in dy_env or "douyin.com" in dy_env):
            combined_cookies.append(dy_env)
        if yt_env and ("# Netscape" in yt_env or "youtube.com" in yt_env or ".google.com" in yt_env):
            combined_cookies.append(yt_env)
        if general_env and ("# Netscape" in general_env):
            combined_cookies.append(general_env)
        elif general_env and os.path.isfile(general_env):
            try:
                with open(general_env, "r", encoding="utf-8", errors="replace") as gf:
                    g_content = gf.read().strip()
                    if g_content:
                        combined_cookies.append(g_content)
            except Exception:
                pass

        if combined_cookies:
            try:
                data_dir = os.path.join(os.getcwd(), "data")
                os.makedirs(data_dir, exist_ok=True)
                unified_cookie = os.path.join(data_dir, "auth_cookies.txt")
                with open(unified_cookie, "w", encoding="utf-8") as cf:
                    cf.write("# Netscape HTTP Cookie File\n\n" + "\n\n".join(combined_cookies) + "\n")
                cookie_path = unified_cookie
            except Exception as ce:
                try:
                    import tempfile
                    tmp_cookie = os.path.join(tempfile.gettempdir(), "auth_cookies.txt")
                    with open(tmp_cookie, "w", encoding="utf-8") as cf:
                        cf.write("# Netscape HTTP Cookie File\n\n" + "\n\n".join(combined_cookies) + "\n")
                    cookie_path = tmp_cookie
                except Exception as ce2:
                    logger.warning(f"Failed to write authentication cookies from env: {ce2}")

        if not cookie_path or not os.path.isfile(cookie_path):
            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cands = []
            if "douyin" in target_lower:
                cands += [
                    os.path.join(os.getcwd(), "douyin_cookies.txt"),
                    os.path.join(os.getcwd(), "data", "douyin_cookies.txt"),
                    os.path.join(repo_root, "douyin_cookies.txt"),
                ]
            elif "youtube" in target_lower or "youtu.be" in target_lower:
                cands += [
                    os.path.join(os.getcwd(), "youtube_cookies.txt"),
                    os.path.join(os.getcwd(), "data", "youtube_cookies.txt"),
                    os.path.join(repo_root, "youtube_cookies.txt"),
                ]
            cands += [
                os.path.join(os.getcwd(), "cookies.txt"),
                os.path.join(os.getcwd(), "data", "cookies.txt"),
                os.path.join(os.getcwd(), "douyin_cookies.txt"),
                os.path.join(os.getcwd(), "data", "douyin_cookies.txt"),
                os.path.join(repo_root, "cookies.txt"),
                os.path.join(repo_root, "data", "cookies.txt"),
            ]
            for cand in cands:
                if os.path.isfile(cand):
                    cookie_path = cand
                    break

        if cookie_path and os.path.isfile(cookie_path):
            opts['cookiefile'] = cookie_path
            logger.info(f"Loaded platform authentication cookies from: {cookie_path}")

        return opts

    def _attempt_plans(self, base_opts: Dict[str, Any], expanded_url: str, *, stage: str):
        """Ordered (label, opts) extraction attempts for a URL.

        YouTube gets the R39 client chain — web_embedded first (full
        HD ladder from datacenter IPs), then the cookie'd default
        chain (age/region rescues), then the 360p rescues android and
        mweb. Info-stage attempts fail fast (retries/fragment_retries
        1 under a 10s socket cap — the retries:10 policy and the 20s
        cap are for media bytes only); every attempt carries a
        bounded socket timeout. Every other host
        yields exactly one attempt: the base opts, unchanged.
        Every YouTube attempt additionally carries the bgutil PO
        Token provider args (see _with_pot / _pot_extractor_args):
        the provider base_url and the scoped POT log shim — plus the
        WARP egress proxy (see _warp_proxy_url) when not disabled.

        Batch AJ / M10a: media-stage attempts additionally share ONE
        total-deadline progress hook (armed here, when the plan list
        is first consumed) so client rotation cannot reset the clock
        and a slow-drip host cannot pin the download forever. The
        info stage carries no hook: it has its own trimmed budget.
        """
        deadline_hook = None
        if stage == "media":
            deadline_hook = _media_deadline_progress_hook(
                started=time.monotonic(),
                budget_seconds=MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS,
            )

        def _with_deadline(opts: Dict[str, Any]) -> Dict[str, Any]:
            if deadline_hook is None:
                return opts
            o = dict(opts)
            hooks = list(o.get('progress_hooks') or [])
            hooks.append(deadline_hook)
            o['progress_hooks'] = hooks
            return o

        if not _is_youtube_url(expanded_url):
            yield "default", _with_deadline(base_opts)
            return

        def _staged(opts: Dict[str, Any]) -> Dict[str, Any]:
            o = dict(opts)
            if stage == "info":
                o['retries'] = INFO_STAGE_RETRIES
                o['fragment_retries'] = INFO_STAGE_RETRIES
                o['extractor_retries'] = INFO_STAGE_RETRIES
                o['socket_timeout'] = INFO_STAGE_SOCKET_TIMEOUT
            else:
                o['socket_timeout'] = YOUTUBE_ATTEMPT_SOCKET_TIMEOUT
            return o

        def _with_client(opts: Dict[str, Any], client: str) -> Dict[str, Any]:
            o = dict(opts)
            ea = dict(o.get('extractor_args') or {})
            yt_args = dict(ea.get('youtube') or {})
            yt_args['player_client'] = [client]
            ea['youtube'] = yt_args
            o['extractor_args'] = ea
            return o

        def _with_pot(opts: Dict[str, Any]) -> Dict[str, Any]:
            # ONE wiring point for both YouTube sidecars: every
            # YouTube attempt (any client, any stage) points the
            # bgutil plugin at the in-container POT provider server,
            # attaches the scoped POT log shim, AND routes the
            # attempt through the WARP egress proxy (see
            # _warp_proxy_url — the wall keys on egress IP, so the
            # proxy is the load-bearing half; an empty WARP_PROXY_URL
            # disables it and the attempt carries no 'proxy' key).
            # No fetch policy is forced (the forced-mint experiment
            # concluded — see the block comment at
            # _pot_extractor_args). Dict-valued args merge per-key
            # alongside the 'youtube' extractor args (player_client)
            # instead of clobbering them.
            o = dict(opts)
            ea = dict(o.get('extractor_args') or {})
            for key, value in _pot_extractor_args().items():
                if isinstance(value, dict) and isinstance(ea.get(key), dict):
                    merged = dict(ea[key])
                    merged.update(value)
                    ea[key] = merged
                else:
                    ea[key] = value
            o['extractor_args'] = ea
            o.setdefault('logger', _POT_LOG_SHIM)
            proxy = _warp_proxy_url()
            if proxy:
                o['proxy'] = proxy
            return o

        yield "web_embedded", _with_deadline(_with_pot(_staged(_with_client(base_opts, "web_embedded"))))
        yield "default", _with_deadline(_with_pot(_staged(dict(base_opts))))
        yield "android", _with_deadline(_with_pot(_staged(_with_client(base_opts, "android"))))
        yield "mweb", _with_deadline(_with_pot(_staged(_with_client(base_opts, "mweb"))))

    def _tikwm_resolve(self, page_url: str) -> Optional[Dict[str, Any]]:
        """Guarded tikwm resolver lookup for a TikTok page URL.

        Returns {'id', 'title', 'author', 'duration', 'cover',
        'play_url', 'play_size'} on success, None on any failure.
        Guardrails: circuit breaker, a hard TIKWM_TIMEOUT on both the
        API call and the media validation probe, JSON shape checks,
        https TikTok-CDN host validation, and a content-type/size
        assertion on the play URL itself. Never raises.
        """
        if not _TIKWM_BREAKER.allow():
            logger.warning("tikwm fallback skipped: circuit breaker open")
            return None
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}
        try:
            resp = requests.get(
                TIKWM_API_URL,
                params={'url': page_url, 'hd': 1},
                timeout=TIKWM_TIMEOUT,
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            _TIKWM_BREAKER.record_failure()
            logger.warning(f"tikwm resolver call failed for {page_url}: {e}")
            return None

        data = payload.get('data') if isinstance(payload, dict) else None
        code = payload.get('code') if isinstance(payload, dict) else None
        if not isinstance(data, dict) or code not in (0, "0", None):
            _TIKWM_BREAKER.record_failure()
            logger.warning(f"tikwm resolver returned an unusable payload for {page_url}: code={code!r}")
            return None

        play_url = data.get('hdplay') or data.get('play') or data.get('wmplay')
        if not _valid_tikwm_play_url(play_url):
            _TIKWM_BREAKER.record_failure()
            logger.warning(f"tikwm play URL failed validation for {page_url}: {play_url!r}")
            return None

        # Batch AJ L19: the play URL is extractor-supplied, so it also
        # goes through the shared SSRF validator (scheme/credentials/
        # DNS answers must all be public) before any byte is fetched.
        # The host allowlist above already constrains it to TikTok CDN
        # names; this adds the DNS-answer check those names resolve
        # to. (The residual DNS-rebinding TOCTOU against yt-dlp's own
        # fetching is documented in url_safety.py — accepted, with the
        # recap path re-validating immediately before download.)
        try:
            from app.services.url_safety import validate_public_url

            validate_public_url(play_url)
        except Exception as e:
            _TIKWM_BREAKER.record_failure()
            logger.warning(f"tikwm play URL failed SSRF validation for {page_url}: {e}")
            return None

        # Assert the play URL really serves an mp4 of plausible size
        # before anyone trusts it (reject HTML error pages / stubs).
        play_size: Optional[int] = None
        try:
            with requests.get(
                play_url,
                headers={**headers, 'Range': 'bytes=0-0'},
                timeout=TIKWM_TIMEOUT,
                stream=True,
            ) as probe:
                ctype = (probe.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                if probe.status_code not in (200, 206) or ctype != 'video/mp4':
                    _TIKWM_BREAKER.record_failure()
                    logger.warning(
                        f"tikwm play URL probe failed for {page_url}: "
                        f"status={probe.status_code} content-type={ctype!r}"
                    )
                    return None
                crange = probe.headers.get('Content-Range') or ''
                if '/' in crange:
                    try:
                        play_size = int(crange.rsplit('/', 1)[1])
                    except (ValueError, IndexError):
                        play_size = None
                if play_size is None:
                    try:
                        play_size = int(probe.headers.get('Content-Length') or 0) or None
                    except (TypeError, ValueError):
                        play_size = None
                if play_size is not None and play_size < TIKWM_MIN_MEDIA_BYTES:
                    _TIKWM_BREAKER.record_failure()
                    logger.warning(f"tikwm play URL implausibly small ({play_size}B) for {page_url}")
                    return None
        except Exception as e:
            _TIKWM_BREAKER.record_failure()
            logger.warning(f"tikwm play URL probe errored for {page_url}: {e}")
            return None

        _TIKWM_BREAKER.record_success()
        author = data.get('author') or {}
        return {
            'id': str(data.get('id') or ''),
            'title': str(data.get('title') or 'tiktok_video'),
            'author': str(author.get('nickname') or author.get('unique_id') or 'TikTok') if isinstance(author, dict) else 'TikTok',
            'duration': data.get('duration'),
            'cover': data.get('cover') or data.get('origin_cover'),
            'play_url': play_url,
            'play_size': play_size,
        }

    def _tikwm_get_info(self, url: str, expanded_url: str) -> Optional[Dict[str, Any]]:
        """get_info()-shaped metadata from the tikwm fallback."""
        data = self._tikwm_resolve(expanded_url)
        if data is None:
            return None
        duration = data.get('duration')
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        filesize = data.get('play_size')
        logger.info(f"get_info: tikwm fallback resolved TikTok metadata for {expanded_url}")
        return {
            "id": data['id'] or None,
            "title": data['title'],
            "duration": duration,
            "duration_formatted": format_duration(duration),
            "uploader": data['author'],
            "thumbnail": data.get('cover'),
            "platform": "TikTok",
            "original_url": url,
            "expanded_url": expanded_url,
            "filesize": filesize,
            "filesize_formatted": format_filesize(filesize),
            "available_subs": [],
            "has_subtitles": False,
            "width": None,
            "height": None,
            "low_res": False,
            "tikwm_fallback": True,
            "raw_info": {"id": data['id'], "tikwm_fallback": True, "play_url": data['play_url']},
        }

    def _tikwm_download_video(self, url: str, expanded_url: str, dest_dir: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Download a TikTok video's bytes via the tikwm fallback.

        The play URL is re-validated by _tikwm_resolve (the probe
        asserts video/mp4 + size); the streaming fetch re-asserts the
        content type and the final file size before the file is
        trusted. Returns (file_path, info) or None. Never raises.
        """
        data = self._tikwm_resolve(expanded_url)
        if data is None:
            return None
        if data.get('play_size') is not None and data['play_size'] > MAX_DOWNLOAD_BYTES:
            logger.warning(
                f"tikwm download refused: play size {data['play_size']}B exceeds "
                f"the {MAX_DOWNLOAD_BYTES}B ceiling for {expanded_url}"
            )
            return None
        play_url = data['play_url']
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}
        safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', data['id'] or 'tiktok')[:40] or 'tiktok'
        file_path = os.path.join(dest_dir, f"{safe_id}_tikwm.mp4")
        try:
            with requests.get(
                play_url,
                headers=headers,
                timeout=(TIKWM_TIMEOUT, TIKWM_MEDIA_READ_TIMEOUT),
                stream=True,
            ) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                if ctype != 'video/mp4':
                    logger.warning(f"tikwm download refused: content-type {ctype!r} for {expanded_url}")
                    return None
                written = 0
                # Batch AJ / M10a: the same total wall-clock budget
                # as the yt-dlp media stage — the per-chunk read
                # timeout alone cannot stop a slow-drip host.
                _drip_started = time.monotonic()
                with open(file_path, 'wb') as fh:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if time.monotonic() - _drip_started > MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS:
                            raise RuntimeError(
                                f"tikwm download exceeded the "
                                f"{MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS}s total budget"
                            )
                        if chunk:
                            written += len(chunk)
                            if written > MAX_DOWNLOAD_BYTES:
                                # Over the disk-protection ceiling:
                                # abort; the except path below removes
                                # the partial file.
                                raise RuntimeError(
                                    f"tikwm download exceeded the {MAX_DOWNLOAD_BYTES}B ceiling"
                                )
                            fh.write(chunk)
        except Exception as e:
            logger.warning(f"tikwm download failed for {expanded_url}: {e}")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass
            return None
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        if size < TIKWM_MIN_MEDIA_BYTES:
            logger.warning(f"tikwm download implausibly small ({size}B) for {expanded_url}")
            try:
                os.remove(file_path)
            except Exception:
                pass
            return None
        logger.info(f"download_video: tikwm fallback fetched {size}B for {expanded_url}")
        duration = data.get('duration')
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        info = {
            "id": data['id'] or safe_id,
            "title": data['title'],
            "duration": duration,
            "uploader": data['author'],
            "thumbnail": data.get('cover'),
            "webpage_url": expanded_url,
            "tikwm_fallback": True,
            "low_res": False,
        }
        return file_path, info

    def get_local_info(self, file_path: str) -> Dict[str, Any]:
        """Inspect and return metadata dictionary for a local or uploaded video/audio file."""
        file_path = os.path.abspath(str(file_path).strip('\'"'))
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Local media file not found: {file_path}")

        meta = probe_local_media(file_path)
        base_name = os.path.basename(file_path)
        title, _ = os.path.splitext(base_name)
        file_id = f"local_{uuid.uuid4().hex[:8]}"

        # Generate thumbnail if it has a video stream
        thumbnail_path = None
        if meta.get("has_video"):
            thumb_dest = os.path.join(self.download_dir, "thumbnails", f"{file_id}.jpg")
            seek_time = min(2.0, meta.get("duration", 0.0) / 2) if meta.get("duration") else 1.0
            thumbnail_path = extract_thumbnail_from_media(file_path, thumb_dest, seek_time)

        is_audio_only = meta.get("has_audio") and not meta.get("has_video")
        platform = "Uploaded Audio" if is_audio_only else "Uploaded Video"

        duration = int(round(meta.get("duration", 0.0)))
        filesize = meta.get("size", os.path.getsize(file_path))

        return {
            "id": file_id,
            "title": title,
            "duration": duration,
            "duration_formatted": format_duration(duration),
            "uploader": "Uploaded Media",
            "thumbnail": thumbnail_path,
            "platform": platform,
            "original_url": file_path,
            "expanded_url": file_path,
            "filesize": filesize,
            "filesize_formatted": format_filesize(filesize),
            "available_subs": [],
            "has_subtitles": False,
            "is_local": True,
            "local_path": file_path,
            "width": meta.get("width"),
            "height": meta.get("height"),
            "vcodec": meta.get("vcodec"),
            "acodec": meta.get("acodec"),
            "has_video": meta.get("has_video"),
            "has_audio": meta.get("has_audio"),
            "raw_info": meta
        }

    def get_info(self, url: str) -> Dict[str, Any]:
        """
        Extract video/media metadata without downloading content.
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.get_local_info(url)
        expanded_url = unshorten_url(url)
        opts = self._get_base_opts(expanded_url)
        opts['extract_flat'] = False

        info = None
        last_exc: Optional[Exception] = None
        for client_label, attempt_opts in self._attempt_plans(opts, expanded_url, stage="info"):
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    info = ydl.extract_info(expanded_url, download=False)
                    if not info:
                        raise ValueError("Could not extract media info from the provided URL.")
                logger.info(f"get_info: extraction succeeded via client '{client_label}' for {expanded_url}")
                _flag_low_res(info, expanded_url, client_label)
                break
            except Exception as e:
                last_exc = e
                info = None
                if not _is_rotation_worthy_error(str(e)):
                    break
                logger.warning(f"get_info: client '{client_label}' failed for {expanded_url}: {e}")

        if info is None:
            e = last_exc if last_exc is not None else RuntimeError("Could not extract media info from the provided URL.")
            # TikTok: one guarded resolver try after ANY primary-chain
            # failure (audit fix 2026-11-03 — the gate used to require
            # a blocked-family error) before surfacing the failure.
            if _is_tiktok_url(expanded_url):
                tw_info = self._tikwm_get_info(url, expanded_url)
                if tw_info is not None:
                    return tw_info
            err_msg = str(e)
            if "Douyin" in err_msg or "douyin.com" in expanded_url.lower() or "iesdouyin.com" in expanded_url.lower():
                if "Fresh cookies" in err_msg or "403" in err_msg or "login" in err_msg.lower():
                    logger.warning(f"Douyin anti-bot cookie verification needed: {err_msg}")
                    raise RuntimeError(DOUYIN_BLOCKED_MESSAGE) from e

            if "Sign in to confirm you" in err_msg or "confirm you're not a bot" in err_msg or "Please sign in" in err_msg:
                logger.warning(f"YouTube anti-bot challenge encountered in get_info: {err_msg}")
                raise RuntimeError(classify_source_error(err_msg, expanded_url) or YOUTUBE_BOT_WALL_MESSAGE) from e
            classified = classify_source_error(err_msg, expanded_url)
            if classified:
                logger.warning(f"Classified source error in get_info for {expanded_url}: {err_msg}")
                raise RuntimeError(classified) from e
            raise e

        # Subtitles inspection
        subtitles = info.get('subtitles', {})
        auto_captions = info.get('automatic_captions', {})
        available_subs = list(set(list(subtitles.keys()) + list(auto_captions.keys())))

        duration = info.get('duration')
        title = info.get('title') or "media"
        uploader = info.get('uploader') or info.get('channel') or info.get('creator') or "Unknown"
        thumbnail = info.get('thumbnail')
        filesize = info.get('filesize') or info.get('filesize_approx')
        platform = detect_platform(expanded_url)

        # Source dimensions: most extractors fill the top-level pair, but
        # some expose dimensions only per-format. The Recap preview's
        # "Original" ratio depends on these — dropping them (as this
        # function used to) made every URL source report null dims, and
        # the client then framed vertical sources as 16:9.
        width = info.get('width')
        height = info.get('height')
        if not (width and height):
            best_w = best_h = None
            for fmt in info.get('formats') or []:
                fw, fh = fmt.get('width'), fmt.get('height')
                if fw and fh and fmt.get('vcodec') not in (None, 'none'):
                    if best_h is None or (fh, fw) > (best_h, best_w):
                        best_w, best_h = fw, fh
            if best_w and best_h:
                width, height = best_w, best_h

        return {
            "id": info.get("id"),
            "title": title,
            "duration": duration,
            "duration_formatted": format_duration(duration),
            "uploader": uploader,
            "thumbnail": thumbnail,
            "platform": platform,
            "original_url": url,
            "expanded_url": expanded_url,
            "filesize": filesize,
            "filesize_formatted": format_filesize(filesize),
            "available_subs": available_subs,
            "has_subtitles": len(available_subs) > 0,
            "width": width,
            "height": height,
            "low_res": bool(info.get("low_res")),
            "raw_info": info
        }

    def download_video(self, url: str, target_dir: Optional[str] = None, max_height: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
        """
        Download video in MP4 (H.264/AAC for best Telegram compatibility).
        Returns (file_path, info_dict).
        Supports both Online URLs and Local/Uploaded files.
        When ``max_height`` is set, it caps the short ("p") side
        orientation-aware (height for landscape/square, width for portrait).
        """
        if is_local_media(url):
            return self.process_local_video(url, target_dir=target_dir, max_height=max_height)

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        # Output template
        outtmpl = os.path.join(dest_dir, "%(id)s_%(title).50s.%(ext)s")

        # Select compatible format: MP4 with H.264 video and AAC audio if available,
        # otherwise gracefully accept ANY video+audio stream (including vertical Shorts, WebM, VP9, AV01)
        # and let FFmpeg convert it to pristine MP4.
        fmt = _build_video_format_spec(max_height)

        opts = self._get_base_opts(expanded_url)
        opts.update({
            'format': fmt,
            'outtmpl': outtmpl,
            'merge_output_format': 'mp4',
            'writethumbnail': True,
            'postprocessors': [
                {
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }
            ]
        })

        def _download_once(attempt_opts: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                info_dl = ydl.extract_info(expanded_url, download=True)
                # Find the downloaded mp4 file
                fp = ydl.prepare_filename(info_dl)
                if not fp.endswith('.mp4'):
                    base, _ = os.path.splitext(fp)
                    fp = f"{base}.mp4"

                if not os.path.exists(fp):
                    # Search directory for file with id
                    media_id = info_dl.get("id")
                    if media_id:
                        for fname in os.listdir(dest_dir):
                            if str(media_id) in fname and fname.endswith(('.mp4', '.mkv', '.webm')):
                                fp = os.path.join(dest_dir, fname)
                                break

                return fp, info_dl

        # R39: run the extraction chain (YouTube client rotation;
        # single attempt for other hosts). The last attempt's opts
        # seed the resilient format fallback below, exactly as the
        # single primary attempt's opts used to.
        last_opts = opts
        primary_exc: Optional[Exception] = None
        for client_label, attempt_opts in self._attempt_plans(opts, expanded_url, stage="media"):
            last_opts = attempt_opts
            try:
                file_path, info = _download_once(attempt_opts)
                _flag_low_res(info, expanded_url, client_label)
                return file_path, info
            except Exception as exc:
                primary_exc = exc
                if not _is_rotation_worthy_error(str(exc)):
                    break
                logger.warning(f"download_video: client '{client_label}' failed for {expanded_url} ({exc}). Trying next client...")

        e = primary_exc if primary_exc is not None else RuntimeError("Download failed")
        # TikTok: guarded resolver byte fetch after ANY primary-chain
        # failure (audit fix 2026-11-03), before the format fallback /
        # classified failure below.
        if _is_tiktok_url(expanded_url):
            tw_dl = self._tikwm_download_video(url, expanded_url, dest_dir)
            if tw_dl is not None:
                return tw_dl

        try:
            raise e
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"Primary download_video failed ({err_msg}). Attempting resilient format fallback...")
            fb_exc = None
            try:
                fallback_opts = dict(last_opts)
                fallback_opts['format'] = 'bestvideo+bestaudio/best'
                fallback_opts['ignoreerrors'] = False
                # If cookies caused invalid session / reload error, drop cookiefile for clean fallback
                if "page needs to be reloaded" in err_msg or "Requested format is not available" in err_msg:
                    fallback_opts.pop('cookiefile', None)
                file_path, info = _download_once(fallback_opts)
                _flag_low_res(info, expanded_url, "format-fallback")
                return file_path, info
            except Exception as fb_err:
                logger.warning(f"Fallback download also failed: {fb_err}")
                # Keep the fallback failure past the except block (the
                # `as` name itself is unbound when the block ends).
                fb_exc = fb_err

            if "Douyin" in err_msg or "douyin.com" in expanded_url.lower() or "iesdouyin.com" in expanded_url.lower():
                if "Fresh cookies" in err_msg or "403" in err_msg or "login" in err_msg.lower():
                    logger.warning(f"Douyin anti-bot cookie verification needed in download_video: {err_msg}")
                    raise RuntimeError(DOUYIN_BLOCKED_MESSAGE) from e

            if "Sign in to confirm you" in err_msg or "confirm you're not a bot" in err_msg or "Please sign in" in err_msg:
                logger.warning(f"YouTube anti-bot challenge encountered in download_video: {err_msg}")
                raise RuntimeError(classify_source_error(err_msg, expanded_url) or YOUTUBE_BOT_WALL_MESSAGE) from e
            # Classify the FINAL failure (the resilient fallback's error
            # is the one that actually decided the outcome).
            classified = None
            if fb_exc is not None:
                classified = classify_source_error(str(fb_exc), expanded_url)
            if not classified:
                classified = classify_source_error(err_msg, expanded_url)
            if classified:
                logger.warning(f"Classified source error in download_video for {expanded_url}: {fb_exc}")
                raise RuntimeError(classified) from (fb_exc or e)
            raise

    def download_audio(self, url: str, target_dir: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """
        Download audio and convert to MP3 (192kbps) with metadata & album art.
        Returns (file_path, info_dict).
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.extract_local_audio(url, target_dir=target_dir)

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        outtmpl = os.path.join(dest_dir, "%(id)s_%(title).50s.%(ext)s")

        opts = self._get_base_opts(expanded_url)
        opts.update({
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'writethumbnail': True,
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                },
                {
                    'key': 'FFmpegMetadata',
                    'add_metadata': True,
                }
            ]
        })

        def _download_audio_once(attempt_opts: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                info_dl = ydl.extract_info(expanded_url, download=True)
                # Filename after audio extraction is .mp3
                base_filename = ydl.prepare_filename(info_dl)
                base_name, _ = os.path.splitext(base_filename)
                mp3 = f"{base_name}.mp3"

                if not os.path.exists(mp3):
                    media_id = info_dl.get("id")
                    if media_id:
                        for fname in os.listdir(dest_dir):
                            if str(media_id) in fname and fname.endswith('.mp3'):
                                mp3 = os.path.join(dest_dir, fname)
                                break

                return mp3, info_dl

        primary_exc: Optional[Exception] = None
        for client_label, attempt_opts in self._attempt_plans(opts, expanded_url, stage="media"):
            try:
                mp3_path, info = _download_audio_once(attempt_opts)
                _flag_low_res(info, expanded_url, client_label)
                return mp3_path, info
            except Exception as exc:
                primary_exc = exc
                if not _is_rotation_worthy_error(str(exc)):
                    break
                logger.warning(f"download_audio: client '{client_label}' failed for {expanded_url} ({exc}). Trying next client...")

        e = primary_exc if primary_exc is not None else RuntimeError("Audio download failed")
        # TikTok: after ANY primary-chain failure (audit fix
        # 2026-11-03), fetch the video via the guarded resolver, then
        # extract its audio locally.
        if _is_tiktok_url(expanded_url):
            tw_dl = self._tikwm_download_video(url, expanded_url, dest_dir)
            if tw_dl is not None:
                tw_path, _tw_info = tw_dl
                mp3_path, local_info = self.extract_local_audio(tw_path, target_dir=dest_dir)
                local_info["tikwm_fallback"] = True
                return mp3_path, local_info

        try:
            raise e
        except Exception as e:
            err_msg = str(e)
            if "Douyin" in err_msg or "douyin.com" in expanded_url.lower() or "iesdouyin.com" in expanded_url.lower():
                if "Fresh cookies" in err_msg or "403" in err_msg or "login" in err_msg.lower():
                    logger.warning(f"Douyin anti-bot cookie verification needed in download_audio: {err_msg}")
                    raise RuntimeError(DOUYIN_BLOCKED_MESSAGE) from e

            if "Sign in to confirm you" in err_msg or "confirm you're not a bot" in err_msg or "Please sign in" in err_msg:
                logger.warning(f"YouTube anti-bot challenge encountered in download_audio: {err_msg}")
                raise RuntimeError(classify_source_error(err_msg, expanded_url) or YOUTUBE_BOT_WALL_MESSAGE) from e
            classified = classify_source_error(err_msg, expanded_url)
            if classified:
                logger.warning(f"Classified source error in download_audio for {expanded_url}: {err_msg}")
                raise RuntimeError(classified) from e
            raise

    def download_audio_for_transcription(self, url: str, target_dir: Optional[str] = None) -> str:
        """
        Fast audio download specifically for Whisper transcription.
        Downloads lightweight raw audio without thumbnail or MP3 conversion.
        """
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        outtmpl = os.path.join(dest_dir, "transcribe_%(id)s.%(ext)s")
        opts = self._get_base_opts(expanded_url)
        opts.update({
            'format': 'ba/b',
            'outtmpl': outtmpl,
            'writethumbnail': False,
        })

        info = None
        fname = None
        primary_exc: Optional[Exception] = None
        for client_label, attempt_opts in self._attempt_plans(opts, expanded_url, stage="media"):
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    info = ydl.extract_info(expanded_url, download=True)
                    fname = ydl.prepare_filename(info)
                _flag_low_res(info, expanded_url, client_label)
                break
            except Exception as exc:
                primary_exc = exc
                info = None
                fname = None
                if not _is_rotation_worthy_error(str(exc)):
                    break
                logger.warning(f"download_audio_for_transcription: client '{client_label}' failed for {expanded_url} ({exc}). Trying next client...")

        if info is None:
            e = primary_exc if primary_exc is not None else RuntimeError("Audio download failed")
            # TikTok: after ANY primary-chain failure (audit fix
            # 2026-11-03), guarded resolver fetch, then pull the audio
            # track out of the video locally.
            if _is_tiktok_url(expanded_url):
                tw_dl = self._tikwm_download_video(url, expanded_url, dest_dir)
                if tw_dl is not None:
                    tw_path, _tw_info = tw_dl
                    mp3_path, _local_info = self.extract_local_audio(tw_path, target_dir=dest_dir)
                    return mp3_path
            try:
                raise e
            except Exception as e:
                classified = classify_source_error(str(e), expanded_url)
                if classified:
                    logger.warning(f"Classified source error in download_audio_for_transcription for {expanded_url}: {e}")
                    raise RuntimeError(classified) from e
                raise
        if os.path.exists(fname):
            return fname

        media_id = str(info.get("id", ""))
        for f in os.listdir(dest_dir):
            if media_id in f and f.startswith("transcribe_"):
                return os.path.join(dest_dir, f)

        raise FileNotFoundError(f"Downloaded audio for transcription not found: {fname}")

    def download_subtitles(
        self,
        url: str,
        target_dir: Optional[str] = None,
        lang: str = 'en',
        allow_ai_fallback: bool = True
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Download subtitles and convert to .srt.
        If the media lacks subtitles, automatically falls back to AI Speech-to-Text (Whisper).
        Returns (list_of_srt_file_paths, info_dict).
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.transcribe_local_media(url, target_dir=target_dir)

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        # Check available languages first
        info = self.get_info(expanded_url)
        available_subs = info.get("available_subs", [])

        # Target language selection:
        # If requested lang in available_subs, use it. Otherwise use the first available.
        target_lang = lang
        if target_lang not in available_subs:
            # Check prefix match (e.g. 'en' matches 'en-US' or 'en-orig')
            prefix_matches = [s for s in available_subs if s.startswith(lang)]
            if prefix_matches:
                target_lang = prefix_matches[0]
            elif 'my' in available_subs: # Myanmar subtitle priority if available
                target_lang = 'my'
            elif available_subs:
                target_lang = available_subs[0]
            else:
                target_lang = 'en'

        outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")

        opts = self._get_base_opts(expanded_url)
        opts.update({
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [target_lang],
            'subtitlesformat': 'srt',
            'ignoreerrors': True,
            'outtmpl': outtmpl,
            'postprocessors': [{
                'key': 'FFmpegSubtitlesConvertor',
                'format': 'srt',
            }]
        })

        # R39: same client chain as the other extraction paths
        # (ignoreerrors means a walled client simply yields no files,
        # so rotate until subtitle files actually appear).
        media_id = str(info.get("id", ""))
        srt_files: List[str] = []
        for _client_label, attempt_opts in self._attempt_plans(opts, expanded_url, stage="media"):
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                ydl.download([expanded_url])
            # Collect generated .srt files in destination
            srt_files = []
            for fname in os.listdir(dest_dir):
                if fname.endswith('.srt'):
                    if not media_id or media_id in fname:
                        srt_files.append(os.path.join(dest_dir, fname))
            if srt_files:
                break

        if srt_files:
            info["is_ai_generated"] = False
            return srt_files, info

        # AI Fallback: When no native subtitles exist, transcribe audio via Whisper
        if allow_ai_fallback and transcribe_to_srt is not None:
            logger.info(f"No native subtitles found for {url}. Generating SRT with AI Whisper...")
            audio_path = None
            try:
                try:
                    audio_path = self.download_audio_for_transcription(expanded_url, target_dir=dest_dir)
                except Exception as dl_err:
                    logger.warning(f"Fast transcription download failed ({dl_err}), falling back to standard audio download...")
                    audio_path, _ = self.download_audio(expanded_url, target_dir=dest_dir)

                if audio_path and os.path.exists(audio_path):
                    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', media_id) or "media"
                    ai_srt_path = os.path.join(dest_dir, f"{safe_id}_ai.srt")
                    out_srt, detected_lang, prob = transcribe_to_srt(
                        audio_file_path=audio_path,
                        output_srt_path=ai_srt_path
                    )
                    if os.path.exists(out_srt):
                        srt_files.append(out_srt)
                        info["is_ai_generated"] = True
                        info["detected_lang"] = detected_lang
                        info["lang_probability"] = prob
            except Exception as e:
                logger.error(f"AI Subtitle transcription error: {e}")
            finally:
                if audio_path:
                    self.cleanup_files(audio_path)

        return srt_files, info

    def process_local_video(
        self,
        file_path: str,
        target_dir: Optional[str] = None,
        max_height: Optional[int] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Ensure local video is standard H.264/AAC MP4 compatible with Telegram and players.

        When ``max_height`` is set, it caps the short ("p") side
        orientation-aware (height for landscape/square, width for portrait).
        """
        file_path = os.path.abspath(str(file_path).strip('\'"'))
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        info = self.get_local_info(file_path)

        meta = probe_local_media(file_path)
        scale_filter = _local_video_scale_filter(meta.get("width"), meta.get("height"), max_height)
        needs_scale = scale_filter is not None
        is_mp4 = file_path.lower().endswith(".mp4") and meta.get("vcodec") in {"h264", "avc1"}

        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', info.get("title", "video"))[:50]
        out_mp4 = os.path.join(dest_dir, f"{info['id']}_{safe_title}.mp4")

        # If already mp4 h264 and no resize needed, simply copy or reuse
        if is_mp4 and not needs_scale:
            if os.path.abspath(file_path) != os.path.abspath(out_mp4):
                shutil.copy2(file_path, out_mp4)
            return out_mp4, info

        vf_args = ["-vf", scale_filter] if scale_filter else []

        cmd = [
            "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-y",
            "-i", file_path,
            *vf_args,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            out_mp4
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=_local_media_timeout(info.get("duration")))
            transcode_ok = res.returncode == 0
        except subprocess.TimeoutExpired:
            logger.warning(f"Local transcode timed out for {file_path}; falling back to copy")
            transcode_ok = False
        if not transcode_ok or not os.path.exists(out_mp4):
            shutil.copy2(file_path, out_mp4)

        return out_mp4, info

    def extract_local_audio(
        self,
        file_path: str,
        target_dir: Optional[str] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Extract high-quality 192kbps MP3 audio from a local video or audio file."""
        file_path = os.path.abspath(str(file_path).strip('\'"'))
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        info = self.get_local_info(file_path)

        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', info.get("title", "audio"))[:50]
        out_mp3 = os.path.join(dest_dir, f"{info['id']}_{safe_title}.mp3")

        cmd = [
            "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-y",
            "-i", file_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "192k",
            out_mp3
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=_local_media_timeout(info.get("duration")))
            extract_ok = res.returncode == 0
        except subprocess.TimeoutExpired:
            logger.warning(f"Local audio extraction timed out for {file_path}; using silent fallback")
            cmd_silent = [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", str(min(1800, max(10, int(info.get("duration") or 10)))),
                "-q:a", "9", "-acodec", "libmp3lame", out_mp3
            ]
            subprocess.run(cmd_silent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=_local_media_timeout(info.get("duration")))
            if os.path.exists(out_mp3):
                return out_mp3, info
            extract_ok = False
        if not extract_ok or not os.path.exists(out_mp3):
            raise RuntimeError(f"Audio extraction failed for {file_path} (no usable audio stream)")

        return out_mp3, info

    def transcribe_local_media(
        self,
        file_path: str,
        target_dir: Optional[str] = None
    ) -> Tuple[List[str], Dict[str, Any]]:
        """Transcribe local video or audio directly into .srt subtitle with Whisper."""
        if transcribe_to_srt is None:
            raise ImportError("transcriber module (faster-whisper) is not available.")

        file_path = os.path.abspath(str(file_path).strip('\'"'))
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        info = self.get_local_info(file_path)

        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', info.get("title", "media"))[:50]
        out_srt = os.path.join(dest_dir, f"{info['id']}_{safe_title}_ai.srt")

        srt_path, detected_lang, prob = transcribe_to_srt(
            audio_file_path=file_path,
            output_srt_path=out_srt
        )
        info["is_ai_generated"] = True
        info["detected_lang"] = detected_lang
        info["lang_probability"] = prob

        return [srt_path], info

    def generate_recap_video(
        self,
        url_or_path: str,
        target_dir: Optional[str] = None,
        voice: str = "my-MM-ThihaNeural",
        narrative_style: str = "narrator",
        voice_speed: str = "+12%",
        tts_engine: str = "edge_tts",
        source_language: str = "auto",
        render_quality: str = "standard",
        gemini_voice: Optional[str] = None,
        gemini_route=None,
        paid_tier: bool = False,
        voice_choice_mode: Optional[str] = None,
        voxcpm_control: Optional[str] = None,
        voxcpm_ref_audio: Optional[str] = None,
        is_female: bool = False,
        mirror_video: bool = True,
        zoom_factor: float = 1.0,
        blur_subtitles: bool = True,
        blur_y: float = 0.85,
        blur_h: float = 0.07,
        blur_x: Optional[float] = None,
        blur_w: Optional[float] = None,
        blur_box_explicit: bool = False,
        blur_strength: int = 18,
        blur_auto_detect: bool = True,
        blur_watermark: bool = False,
        burn_subtitles: bool = True,
        sub_y: Optional[float] = 0.85,
        sub_margin: Optional[float] = None,
        sub_x: Optional[float] = None,
        sub_w: Optional[float] = None,
        sub_lines: int = 1,
        sub_font_size: int = 16,
        sub_style: str = "yellow",
        sub_font: str = "padauk",
        blur_boxes: Optional[list] = None,
        sub_visual_overrides: Optional[dict] = None,
        sub_font_custom_path: Optional[str] = None,
        sub_bold: bool = True,
        sub_render_mode: str = "png",
        sub_effect: str = "none",
        embed_soft_subs: bool = True,
        protection_level: str = "standard",
        logo_path: Optional[str] = None,
        logo_position: Optional[list[float]] = None,
        logo_scale: float = 0.16,
        logo_opacity: float = 0.70,
        extra_text: Optional[str] = None,
        output_ratio: str = "Original",
        frame_fill: str = "blur",
        crop_top: float = 0.0,
        crop_bottom: float = 0.0,
        crop_left: float = 0.0,
        crop_right: float = 0.0,
        is_vip: bool = False,
        max_duration_seconds: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        part_index: int = 1,
        total_parts: int = 1,
        prev_part_summary: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Executes the full Recap English -> Myanmar workflow:
        If given a URL, downloads the source video first.
        Then runs Whisper -> Gemini Translation -> Edge TTS Myanmar -> Smart Sync -> Final Video.
        Applies video transformations: Flip/Mirror, Zoom, Blur Subtitles/Watermark, Burn Synced Myanmar Subs.
        """
        if run_recap_pipeline is None:
            raise ImportError("recap_engine is not available.")

        dest_dir = target_dir or os.path.join(self.download_dir, "recap_output")
        os.makedirs(dest_dir, exist_ok=True)

        # 1. Resolve source video file
        dl_title: Optional[str] = None
        if is_local_media(url_or_path):
            source_video_path = os.path.abspath(str(url_or_path).strip('\'"'))
            if not os.path.exists(source_video_path):
                raise FileNotFoundError(f"Source video not found: {source_video_path}")
        else:
            if log_callback:
                log_callback("📥 Downloading source video from URL for Recap processing...")
            if progress_callback:
                progress_callback(0, 4, "📥 Downloading source video from URL...")
            recap_max_height = 1080 if is_vip else 720
            _dl_t0 = time.monotonic()
            source_video_path, info = self.download_video(url_or_path, target_dir=dest_dir, max_height=recap_max_height)
            dl_title = str((info or {}).get("title") or "").strip() or None
            _dl_elapsed = round(max(0.0, time.monotonic() - _dl_t0), 3)
            _download_stage_sec = _dl_elapsed

        # Enforce maximum video duration limit: Free users = 2 mins (120s), VIP = 10 mins (600s)
        try:
            from app.config import settings
            if max_duration_seconds is not None:
                max_dur = max_duration_seconds
            elif is_vip:
                max_dur = getattr(settings, "recap_max_duration_seconds", 600)
            else:
                max_dur = getattr(settings, "recap_free_max_duration_seconds", 120)

            meta = probe_local_media(source_video_path)
            actual_dur = float(meta.get("duration", 0.0) or 0.0)
            if actual_dur > max_dur:
                raise ValueError(
                    recap_over_limit_message(actual_dur, max_dur, is_vip)
                    + (" ပိုရှည်သော ဗီဒီယိုများ (၁၀ မိနစ်အထိ) ပြုလုပ်လိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။" if not is_vip else "")
                )
        except ValueError:
            raise
        except Exception as probe_err:
            logger.debug(f"Duration limit check skipped on probe error: {probe_err}")

        # 2. Run the full recap engine pipeline with transformations and branding
        recap_res = run_recap_pipeline(
            source_video_path=source_video_path,
            job_dir=os.path.join(dest_dir, "workspace"),
            voice=voice,
            narrative_style=narrative_style,
            voice_speed=voice_speed,
            tts_engine=tts_engine,
            source_language=source_language,
            render_quality=render_quality,
            gemini_voice=gemini_voice,
            # Batch CG: BYOK route context (in-memory object, never
            # serialised by the pipeline).
            gemini_route=gemini_route,
            # Batch DP: park-and-ask plumbing (paid tier + choice).
            paid_tier=paid_tier,
            voice_choice_mode=voice_choice_mode,
            voxcpm_control=voxcpm_control,
            voxcpm_ref_audio=voxcpm_ref_audio,
            is_female=is_female,
            mirror_video=mirror_video,
            zoom_factor=zoom_factor,
            blur_subtitles=blur_subtitles,
            blur_y=blur_y,
            blur_h=blur_h,
            blur_x=blur_x,
            blur_w=blur_w,
            blur_box_explicit=blur_box_explicit,
            blur_strength=blur_strength,
            blur_auto_detect=blur_auto_detect,
            blur_watermark=blur_watermark,
            burn_subtitles=burn_subtitles,
            sub_y=sub_y,
            sub_margin=sub_margin,
            sub_x=sub_x,
            sub_w=sub_w,
            sub_lines=sub_lines,
            sub_font_size=sub_font_size,
            sub_style=sub_style,
            sub_font=sub_font,
            blur_boxes=blur_boxes,
            sub_visual_overrides=sub_visual_overrides,
            sub_font_custom_path=sub_font_custom_path,
            sub_bold=sub_bold,
            sub_render_mode=sub_render_mode,
            sub_effect=sub_effect,
            embed_soft_subs=embed_soft_subs,
            protection_level=protection_level,
            logo_path=logo_path,
            logo_position=logo_position,
            logo_scale=logo_scale,
            logo_opacity=logo_opacity,
            extra_text=extra_text,
            source_title=dl_title,
            output_ratio=output_ratio,
            frame_fill=frame_fill,
            crop_top=crop_top,
            crop_bottom=crop_bottom,
            crop_left=crop_left,
            crop_right=crop_right,
            log_fn=log_callback,
            progress_fn=progress_callback,
            should_stop=should_stop,
            part_index=part_index,
            total_parts=total_parts,
            prev_part_summary=prev_part_summary,
        )
        if isinstance(recap_res, dict) and "_download_stage_sec" in locals():
            _st = dict(recap_res.get("stage_timings") or {})
            _st.setdefault("download", _download_stage_sec)
            recap_res["stage_timings"] = _st

        return recap_res

    def generate_recap_preview(
        self,
        url_or_path: str,
        target_dir: Optional[str] = None,
        blur_subtitles: bool = True,
        blur_y: float = 0.85,
        blur_h: float = 0.07,
        blur_x: Optional[float] = None,
        blur_w: Optional[float] = None,
        blur_box_explicit: bool = False,
        blur_strength: int = 18,
        blur_auto_detect: bool = True,
        burn_subtitles: bool = True,
        sub_y: Optional[float] = 0.85,
        sub_margin: Optional[float] = None,
        sub_x: Optional[float] = None,
        sub_w: Optional[float] = None,
        sub_lines: int = 1,
        sub_font_size: int = 16,
        sub_style: str = "yellow",
        sub_font: str = "padauk",
        manual_blur_boxes: Optional[list] = None,
        sub_visual_overrides: Optional[dict] = None,
        sub_font_custom_path: Optional[str] = None,
        sub_bold: bool = True,
        sub_render_mode: str = "png",
        sub_effect: str = "none",
        embed_soft_subs: bool = True,
        protection_level: str = "standard",
        sample_text: str = "",
        output_ratio: str = "Original",
        frame_fill: str = "blur",
        crop_top: float = 0.0,
        crop_bottom: float = 0.0,
        crop_left: float = 0.0,
        crop_right: float = 0.0,
        mirror_video: bool = True,
        zoom_factor: float = 1.0,
        at_seconds: Optional[float] = None,
        logo_enabled: bool = False,
        logo_path: Optional[str] = None,
        logo_position: Optional[list[float]] = None,
        logo_scale: float = 0.16,
        logo_opacity: float = 0.70,
        is_vip: bool = False,
        watermark: Optional[bool] = None,
    ) -> str:
        """
        Extracts a single frame snapshot with blur box and sample subtitle for preview.
        """
        if generate_recap_preview is None:
            raise ImportError("recap_engine.recap_pipeline.generate_recap_preview is not available.")

        dest_dir = target_dir or os.path.join(self.download_dir, "recap_output")
        os.makedirs(dest_dir, exist_ok=True)

        if is_local_media(url_or_path):
            source_video_path = os.path.abspath(str(url_or_path).strip('\'"'))
            if not os.path.exists(source_video_path):
                raise FileNotFoundError(f"Source video not found: {source_video_path}")
        else:
            recap_max_height = 1080 if is_vip else 720
            # Blur safefix (2026-11-03): this branch used to capture
            # the download's title into a `title_text` variable that
            # does not exist in this method — a NameError on every
            # URL preview. The engine preview takes no title, so the
            # dead capture is gone; the info dict is not needed here.
            source_video_path, _info = self.download_video(url_or_path, target_dir=dest_dir, max_height=recap_max_height)

        if at_seconds is None or float(at_seconds) <= 0:
            at_seconds = find_best_preview_timestamp(source_video_path, target_dir=dest_dir)

        out_img = os.path.join(dest_dir, f"preview_{uuid.uuid4().hex[:8]}.jpg")
        return generate_recap_preview(
            source_video_path=source_video_path,
            output_image_path=out_img,
            blur_subtitles=blur_subtitles,
            blur_y=blur_y,
            blur_h=blur_h,
            blur_x=blur_x,
            blur_w=blur_w,
            blur_box_explicit=blur_box_explicit,
            blur_strength=blur_strength,
            blur_auto_detect=blur_auto_detect,
            burn_subtitles=burn_subtitles,
            sub_y=sub_y,
            sub_margin=sub_margin,
            sub_x=sub_x,
            sub_w=sub_w,
            sub_lines=sub_lines,
            sub_font_size=sub_font_size,
            sub_style=sub_style,
            sub_font=sub_font,
            sample_text=sample_text,
            manual_blur_boxes=manual_blur_boxes,
            sub_visual_overrides=sub_visual_overrides,
            sub_font_custom_path=sub_font_custom_path,
            sub_bold=sub_bold,
            sub_render_mode=sub_render_mode,
            sub_effect=sub_effect,
            embed_soft_subs=embed_soft_subs,
            protection_level=protection_level,
            output_ratio=output_ratio,
            frame_fill=frame_fill,
            crop_top=crop_top,
            crop_bottom=crop_bottom,
            crop_left=crop_left,
            crop_right=crop_right,
            mirror_video=mirror_video,
            zoom_factor=zoom_factor,
            at_seconds=at_seconds,
            logo_enabled=logo_enabled,
            logo_path=logo_path,
            logo_position=logo_position,
            logo_scale=logo_scale,
            logo_opacity=logo_opacity,
            is_vip=is_vip,
            watermark=watermark,
        )

    @staticmethod
    def cleanup_files(*file_paths: str):
        """Remove temporary files and directories safely and thoroughly."""
        import gc, stat
        gc.collect()
        for path in file_paths:
            if not path or not os.path.exists(path):
                continue
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    try:
                        os.chmod(path, stat.S_IWRITE)
                    except Exception:
                        pass
                    os.remove(path)
                elif os.path.isdir(path):
                    def _on_rm_error(func, p, exc_info):
                        try:
                            os.chmod(p, stat.S_IWRITE)
                            func(p)
                        except Exception:
                            pass
                    shutil.rmtree(path, onerror=_on_rm_error)
            except Exception as e:
                logger.debug(f"cleanup_files warning: {e}")
