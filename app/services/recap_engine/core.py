from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import re
import sys
import unicodedata
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


LogFn = Callable[[str], None]
def _noop(_: str) -> None:
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def resolve_binary(explicit: str | None, executable: str, common: Iterable[str] = ()) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(common)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    found = shutil.which(executable)
    if found:
        return found
    raise FileNotFoundError(f"{executable} မတွေ့ပါ။ config.json / PATH ကိုစစ်ပါ။")


# A user's PC got "WSServerHandshakeError: 403, message='Invalid response status'" on every try (2026-10-12):
# Microsoft's voice service refused the connection. It refuses many VPN / proxy servers: here the direct
# route worked while the same request through a v2rayN server got 403. edge-tts already corrects a wrong clock
# once. The "proxy" tries go through the PC's proxy (aiohttp does not read the Windows proxy by itself) for
# networks where only a proxy works, and the error says what to try.
EDGE_403_HELP = (
    "Microsoft's Edge voice service refused the connection (403). It refuses many VPN / proxy servers.\n"
    "1) If a VPN is on (v2rayN, Psiphon, 1.1.1.1 …), turn it off — or switch to another server — then press START / RESUME.\n"
    "2) Windows clock: Settings → Time & language → Set time automatically + the right time zone.\n"
    "3) Run INSTALL_REQUIREMENTS.bat again (it updates edge-tts).\n"
    "VPN ဖွင့်ထားရင် ပိတ်ပြီး (သို့) server ပြောင်းပြီး START / RESUME ပြန်နှိပ်ပါ။ ကွန်ပျူတာ နာရီ/Time zone မှန်မမှန်လည်း စစ်ပါ။"
)


def edge_proxy() -> str | None:
    """Proxy for the Edge TTS "proxy" tries: EDGE_TTS_PROXY / GEMINI_PROXY, else the Windows proxy, else a local
    v2rayN on 127.0.0.1:10808 when it is running, else none."""
    value = str(os.getenv("EDGE_TTS_PROXY") or os.getenv("GEMINI_PROXY") or "").strip()
    if value:
        return None if value.lower() in {"none", "direct", "off"} else value
    try:
        import urllib.request
        found = urllib.request.getproxies()
        value = str(found.get("https") or found.get("http") or "").strip()
        if value:
            return value if "://" in value else "http://" + value
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", 10808), timeout=0.2):
            return "http://127.0.0.1:10808"
    except OSError:
        return None


def is_edge_refused(error: BaseException | str) -> bool:
    text = str(error)
    return "403" in text or "WSServerHandshakeError" in text


class EdgeTTSEngine:
    """Local Edge-TTS adapter with resilient direct/system-route retries.

    Edge TTS uses a WebSocket service. ``NoAudioReceived`` is often a
    transport/route failure rather than a Smart Sync failure. Generate the
    MP3 in-process so the adapter can classify errors, back off, and try both
    a proxy-bypassed route and the inherited system/proxy route.
    """

    _PROXY_KEYS = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "NO_PROXY", "no_proxy",
    )
    _env_lock = threading.Lock()

    def __init__(self, *, ffmpeg: str, work_dir: Path, rate: str = "+0%", pitch: str = "+0Hz", log: LogFn = _noop) -> None:
        import importlib.util
        if importlib.util.find_spec("edge_tts") is None:
            raise RuntimeError("edge-tts မတွေ့ပါ။ pip install -U edge-tts လုပ်ပြီး ပြန်စမ်းပါ။")
        self.ffmpeg = ffmpeg
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.rate = str(rate or "+0%").strip()
        self.pitch = str(pitch or "+0Hz").strip()
        self.log = log
        self.VOICE_DATABASE: dict[str, dict[str, Any]] = {}
        self._counter = 0
        try:
            from importlib.metadata import version as _pkg_version
            edge_ver = _pkg_version("edge-tts")
        except Exception:
            edge_ver = "unknown"
        self.proxy = edge_proxy()
        self.log(f"✅ Edge TTS READY — version={edge_ver}, rate={self.rate}, pitch={self.pitch}"
                 + (f" | proxy tries via {self.proxy}" if self.proxy else ""))

    @staticmethod
    def _clean_text(text: str) -> str:
        value = unicodedata.normalize("NFC", str(text or ""))
        value = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in value)
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            raise RuntimeError("Edge TTS text is empty after cleanup.")
        if not any(ch.isalnum() for ch in value):
            raise RuntimeError(
                f"Edge TTS text contains no speakable letters/numbers: {value!r}. "
                "This is a translation placeholder, not a network failure."
            )
        return value

    async def _save_mp3_async(self, text: str, voice: str, mp3: Path, proxy: str | None = None) -> None:
        import edge_tts
        communicate = edge_tts.Communicate(
            text,
            voice=str(voice),
            rate=self.rate,
            pitch=self.pitch,
            proxy=proxy,
        )
        await communicate.save(str(mp3))

    def _synthesize_once(self, text: str, voice: str, mp3: Path, *, direct: bool) -> None:
        # edge_tts/aiohttp respects environment/system proxy settings. Some
        # setups need a proxy for Gemini while Edge TTS succeeds only direct.
        with self._env_lock:
            backup = {key: os.environ.get(key) for key in self._PROXY_KEYS}
            try:
                if direct:
                    for key in self._PROXY_KEYS:
                        os.environ.pop(key, None)
                    os.environ["NO_PROXY"] = "*"
                    os.environ["no_proxy"] = "*"
                asyncio.run(self._save_mp3_async(text, voice, mp3, None if direct else self.proxy))
            finally:
                for key, old_value in backup.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value

    def generate_channel99_voice(self, text: str, voice: str):
        clean_text = self._clean_text(text)
        self._counter += 1
        stem = f"edge_{self._counter:05d}_{hashlib.sha1(clean_text.encode('utf-8')).hexdigest()[:10]}"
        mp3 = self.work_dir / f"{stem}.mp3"
        wav = self.work_dir / f"{stem}.wav"
        mp3.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)

        # The previous 0.5-second retry was too short for transient WebSocket
        # failures. Try direct twice, then inherited system/proxy twice.
        routes = [
            (True, "direct", 0.0),
            (True, "direct", 1.5),
            (False, "system/proxy", 2.5),
            (False, "system/proxy", 5.0),
        ]
        errors: list[str] = []
        for attempt, (direct, route_name, delay) in enumerate(routes, start=1):
            if delay:
                time.sleep(delay)
            mp3.unlink(missing_ok=True)
            try:
                self.log(
                    f"🌐 Edge TTS request {attempt}/{len(routes)} — {route_name} route | "
                    f"voice={voice} | chars={len(clean_text)}"
                )
                self._synthesize_once(clean_text, voice, mp3, direct=direct)
                if not mp3.is_file() or mp3.stat().st_size < 256:
                    raise RuntimeError("Edge TTS returned no usable MP3 data.")
                if attempt > 1:
                    self.log(f"✅ Edge TTS recovered on {route_name} route (attempt {attempt}/{len(routes)})")
                break
            except Exception as exc:
                name = type(exc).__name__
                msg = str(exc).strip()
                errors.append(f"{route_name}#{attempt}: {name}: {msg}")
                self.log(f"⚠️ Edge TTS {route_name} attempt {attempt}/{len(routes)} failed: {name}: {msg}")
        else:
            preview = clean_text[:160].replace("\n", " ")
            if all(is_edge_refused(e) for e in errors):
                raise RuntimeError(EDGE_403_HELP + "\n\nLast error: " + errors[-1][:300])
            raise RuntimeError(
                "Edge TTS service returned no audio after direct + system/proxy retries. "
                f"voice={voice}, rate={self.rate}, pitch={self.pitch}, chars={len(clean_text)}, "
                f"text_preview={preview!r}. Last errors: " + " | ".join(errors[-4:])
            )

        conv = subprocess.run(
            [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3),
             "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        try:
            mp3.unlink(missing_ok=True)
        except Exception:
            pass
        if conv.returncode != 0 or not wav.is_file():
            raise RuntimeError("Edge TTS WAV conversion failed: " + (conv.stderr or "")[-2000:])
        return str(wav)


def _seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    for part in text.split(":"):
        total = total * 60.0 + float(part)
    return total


def normalize_segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw = plan.get("segments")
    if not isinstance(raw, list) or not raw:
        raise ValueError("smart_sync_plan.json ထဲမှာ timestamp-resolved 'segments' မရှိပါ။")
    out: list[dict[str, Any]] = []
    for source_index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text = str(item.get("narration", item.get("text", ""))).strip()
        start = _seconds(item.get("video_start", item.get("start", 0.0)))
        end = _seconds(item.get("video_end", item.get("end", 0.0)))
        if not text or end <= start + 0.01:
            continue
        out.append({
            "segment_id": len(out) + 1,
            "source_index": source_index,
            "video_start": round(max(0.0, start), 3),
            "video_end": round(max(0.0, end), 3),
            "narration": text,
        })
    if not out:
        raise ValueError("အသုံးပြုလို့ရတဲ့ narration + timestamp segments မရှိပါ။")
    out.sort(key=lambda x: (x["video_start"], x["video_end"]))
    for i, seg in enumerate(out, 1):
        seg["segment_id"] = i
    return out


def _stamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000.0)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}h{m:02d}m{s:02d}s{ms:03d}"


def segment_basename(seg: dict[str, Any]) -> str:
    return f"S{int(seg['segment_id']):04d}_{_stamp(float(seg['video_start']))}__{_stamp(float(seg['video_end']))}"


# English glosses in brackets ("ဂူ (Gu)") are not read by the Burmese voice (2026-10-12).
_GLOSS_RE = re.compile(r"\s*\((?=[^)]*[A-Za-z])[^)]*\)")
_LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*")


def spoken_text(text: str) -> str:
    """The narration as the voice should say it: Latin-letter glosses in ( ) dropped."""
    value = str(text or "")
    bare = _GLOSS_RE.sub("", value)
    if bare != value and any(ch.isalpha() for ch in bare):
        return re.sub(r"\s+", " ", bare).strip()
    return value


def without_english(text: str) -> str:
    """Last try for a line Edge TTS gave no audio for: the English words left out."""
    return re.sub(r"\s+", " ", _LATIN_RUN_RE.sub(" ", str(text or ""))).strip()


def segment_fingerprint(seg: dict[str, Any], voice: str) -> str:
    payload = {
        "voice": voice,
        "video_start": seg["video_start"],
        "video_end": seg["video_end"],
        "narration": seg["narration"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def probe_duration(ffprobe: str, path: Path) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-2000:]}")
    return float(result.stdout.strip())


def _concat_list_line(path: Path) -> str:
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace("'", "'\\''")
    return f"file '{p}'"


def trim_edge_silence(
    source: Path,
    target: Path,
    *,
    keep_lead: float = 0.08,
    keep_tail: float = 0.22,
    min_duration: float = 0.0,
    threshold_db: float = -50.0,
) -> tuple[Path, float]:
    """Short pauses: cut excessive Edge TTS silence while keeping a natural breath pause (~0.5s) between sentences.

    Joined clips pause keep_lead + keep_tail (~0.50 s, like a natural human breath/sentence break)
    instead of 0.25s frantic pacing or ~1.2s dead air.
    A clip is never made shorter than min_duration: below source window / max_video_speed Smart
    Sync would have to cut the picture, so there a longer pause is kept. Pauses inside the clip
    are untouched and the source WAV (the TTS cache) is never changed.
    Returns (file to use, its duration).
    """
    import wave

    import numpy as np

    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        raw = reader.readframes(params.nframes)
    rate = int(params.framerate or 1)
    if params.sampwidth != 2 or not params.nframes:
        return source, params.nframes / float(rate)
    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, params.nchannels)
    duration = len(samples) / rate
    window = max(1, rate // 100)  # 10 ms
    count = len(samples) // window
    if not count:
        return source, duration
    blocks = samples[: count * window].astype(np.float32).reshape(count, window, params.nchannels)
    level = np.sqrt(np.mean(blocks ** 2, axis=(1, 2))) / 32768.0
    loud = np.nonzero(level > 10 ** (threshold_db / 20.0))[0]
    if not loud.size:
        return source, duration
    start = max(0.0, loud[0] * window / rate - keep_lead)
    end = min(duration, (loud[-1] + 1) * window / rate + keep_tail)
    if end - start < min_duration:
        end = min(duration, start + min_duration)
        start = max(0.0, end - min_duration)
    first, last = int(round(start * rate)), int(round(end * rate))
    if first <= 0 and last >= len(samples):
        return source, duration
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        try:
            with wave.open(str(target), "rb") as existing:
                if existing.getnframes() == last - first:
                    return target, (last - first) / rate
        except (wave.Error, EOFError):
            pass
    partial = target.with_name(target.stem + ".partial.wav")
    with wave.open(str(partial), "wb") as writer:
        writer.setnchannels(params.nchannels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples[first:last].tobytes())
    partial.replace(target)
    return target, (last - first) / rate


def concat_audio(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path) -> None:
    if not parts:
        raise ValueError("Audio parts မရှိပါ။")
    list_file = work_dir / "audio_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Audio concat failed:\n{result.stderr[-4000:]}")


def _sync_filter(
    source_duration: float,
    target_duration: float,
    *,
    max_video_speed: float = 1.25,
    max_video_slow: float = 1.35,
    smooth_freeze_fallback: bool = False,
    max_freeze_hold: float = 0.75,
) -> tuple[str, dict[str, float]]:
    """Recap-style hybrid timing plan for one timestamp segment.

    - Normal case: exact setpts to the actual TTS duration.
    - Audio much shorter: cap speed-up at max_video_speed, then trim.
    - Audio much longer: cap slow-down at max_video_slow.  When the requested
      stretch exceeds that visual limit, the caller may distribute short
      freeze holds across the segment instead of making motion unnaturally slow.
    """
    if source_duration <= 0.01 or target_duration <= 0.01:
        raise ValueError("Smart Sync duration invalid")
    max_speed = max(1.0, float(max_video_speed))
    max_slow = max(1.0, float(max_video_slow))
    desired = target_duration / source_duration
    min_factor = 1.0 / max_speed

    if desired < min_factor:
        applied = min_factor
        adjusted = source_duration * applied
        return (
            f"trim=duration={source_duration:.6f},setpts={applied:.9f}*(PTS-STARTPTS),trim=duration={target_duration:.6f},setpts=PTS-STARTPTS,fps=30",
            {
                "desired_factor": desired,
                "applied_factor": applied,
                "source_duration": source_duration,
                "target_duration": target_duration,
                "freeze_duration": 0.0,
                "freeze_count": 0.0,
                "hybrid_used": 0.0,
            },
        )

    if desired <= max_slow or not smooth_freeze_fallback:
        # With fallback disabled this deliberately behaves like V0.2.4 exact sync.
        applied = desired
        return (
            f"trim=duration={source_duration:.6f},setpts={applied:.9f}*(PTS-STARTPTS),fps=30",
            {
                "desired_factor": desired,
                "applied_factor": applied,
                "source_duration": source_duration,
                "target_duration": target_duration,
                "freeze_duration": 0.0,
                "freeze_count": 0.0,
                "hybrid_used": 0.0,
            },
        )

    applied = max_slow
    adjusted = source_duration * applied
    freeze_duration = max(0.0, target_duration - adjusted)
    # The filter string is not used for the hybrid case; render_video_part builds
    # a split/concat graph so the hold time is distributed across the segment.
    return (
        "",
        {
            "desired_factor": desired,
            "applied_factor": applied,
            "source_duration": source_duration,
            "target_duration": target_duration,
            "freeze_duration": freeze_duration,
            "freeze_count": 0.0,
            "hybrid_used": 1.0,
            "max_freeze_hold": max(0.15, float(max_freeze_hold)),
        },
    )


OUTPUT_RATIOS = ("16:9", "9:16", "1:1", "Original")
FRAME_FILLS = ("blur", "black", "crop")  # fit on a blurred copy / fit with black bars / crop to fill
OUTPUT_LONG_SIDE = 1920


def _even(value: float) -> int:
    return max(2, int(round(float(value) / 2.0)) * 2)


def probe_video_size(ffprobe: str, path: Path | str) -> tuple[int, int]:
    """Width/height of the first video stream as ffmpeg decodes it (phone rotation applied)."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-1200:]}")
    streams = (json.loads(result.stdout or "{}").get("streams") or [{}])
    video = streams[0] if streams else {}
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int(round(float((video.get("tags") or {}).get("rotate", 0) or 0)))
        except (TypeError, ValueError):
            rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def output_canvas(ratio: str, source_w: int = 0, source_h: int = 0) -> tuple[int, int]:
    """Final frame size: 16:9 = 1920x1080, 9:16 = 1080x1920, 1:1 = 1080x1080, Original = the
    source shape (long side at most 1920)."""
    if ratio == "9:16":
        return 1080, 1920
    if ratio == "1:1":
        return 1080, 1080
    if ratio == "Original" and source_w > 0 and source_h > 0:
        scale = min(1.0, OUTPUT_LONG_SIDE / max(source_w, source_h))
        return _even(source_w * scale), _even(source_h * scale)
    return 1920, 1080


def frame_layout(ratio: str, fill: str, source_w: int, source_h: int,
                 canvas: tuple[int, int] | None = None,
                 crop_top: float = 0.0,
                 crop_bottom: float = 0.0,
                 crop_left: float = 0.0,
                 crop_right: float = 0.0) -> dict[str, Any]:
    """Everything _visual_filters needs; `canvas` overrides the size (the preview draws smaller)."""
    ratio = ratio if ratio in OUTPUT_RATIOS else "16:9"
    cw, ch = canvas or output_canvas(ratio, source_w, source_h)
    cw, ch = _even(cw), _even(ch)
    fill = fill if fill in FRAME_FILLS else "blur"
    sw, sh = int(source_w or 0) or cw, int(source_h or 0) or ch

    # Clamp custom crop margins (0.0 to 0.35 max crop per side)
    ct = max(0.0, min(0.35, float(crop_top or 0.0)))
    cb = max(0.0, min(0.35, float(crop_bottom or 0.0)))
    cl = max(0.0, min(0.35, float(crop_left or 0.0)))
    cr = max(0.0, min(0.35, float(crop_right or 0.0)))

    has_crop = (ct > 0.001 or cb > 0.001 or cl > 0.001 or cr > 0.001)
    if has_crop and (cl + cr < 0.90) and (ct + cb < 0.90):
        c_sw = _even(sw * (1.0 - cl - cr))
        c_sh = _even(sh * (1.0 - ct - cb))
        c_x = int(round(float(sw * cl) / 2.0)) * 2 if cl > 0.0001 else 0
        c_y = int(round(float(sh * ct) / 2.0)) * 2 if ct > 0.0001 else 0
        crop_filter = f"crop={c_sw}:{c_sh}:{c_x}:{c_y}"
    else:
        c_sw, c_sh = sw, sh
        c_x, c_y = 0, 0
        crop_filter = ""

    if fill == "crop" or (not has_crop and abs(sw / sh - cw / ch) < 0.01):
        pw, ph = cw, ch
        ox, oy = 0, 0
    else:
        scale = min(cw / c_sw, ch / c_sh) if c_sw and c_sh else 1.0
        pw, ph = min(cw, _even(c_sw * scale)), min(ch, _even(c_sh * scale))
        ox, oy = _even((cw - pw) / 2.0) if cw > pw else 0, _even((ch - ph) / 2.0) if ch > ph else 0

    return {
        "ratio": ratio,
        "fill": fill,
        "cw": cw,
        "ch": ch,
        "sw": int(source_w or 0),
        "sh": int(source_h or 0),
        "c_sw": c_sw,
        "c_sh": c_sh,
        "crop_filter": crop_filter,
        "crop_top": ct,
        "crop_bottom": cb,
        "crop_left": cl,
        "crop_right": cr,
        "pw": pw,
        "ph": ph,
        "ox": ox,
        "oy": oy,
    }


def source_band_to_canvas_box(band: dict[str, Any], layout: dict[str, Any]) -> dict[str, float]:
    """Map a blur box from SOURCE-frame fractions to output-CANVAS fractions.

    detect_source_subtitle_bands reports boxes in the source picture's own
    fraction space ({x: left, y: centre, w, h} of the source frame), while
    every overlays["blur_boxes"] consumer (apply_production_overlays, the
    preview-frame renderer) reads boxes as fractions of the full output
    canvas. Feeding source fractions straight through anchors the box at
    the canvas top-left and scales it by the canvas size: on the default
    9:16 blur-fill layout (a 1080x810 picture centred in a 1080x1920
    canvas) a genuine subtitle band at source y~0.70 landed at canvas
    y=1344 — the picture's bottom edge, mostly over the blurred
    background, missing the subtitles it exists to cover — and the
    height was inflated x2.37. Worse, a mid-frame false positive at
    source y~0.48 blurred a visible strip across the middle of the
    finished movie (the "tear" on job rc_1793503698_9e6603, round 4).
    Convert through the layout's picture rect so the box covers the
    same picture area it described in the source.
    """
    cw, ch = int(layout["cw"]), int(layout["ch"])
    pw, ph = int(layout.get("pw", cw)), int(layout.get("ph", ch))
    ox, oy = int(layout.get("ox", 0)), int(layout.get("oy", 0))
    bx = _clamp01(band.get("x"), 0.0)
    by = _clamp01(band.get("y"), 0.5)
    bw = _clamp01(band.get("w"), 1.0)
    bh = _clamp01(band.get("h"), 0.07)
    return {
        "x": round((ox + bx * pw) / cw, 4),
        "y": round((oy + by * ph) / ch, 4),
        "w": round(bw * pw / cw, 4),
        "h": round(bh * ph / ch, 4),
    }


def _visual_filters(mirror_video: bool, zoom_factor: float,
                    layout: dict[str, Any] | None = None) -> tuple[str, float]:
    """Flip + zoom + placement on the output frame, as one filtergraph string.

    Crop (and any source already of the frame's shape) fills the frame and cuts the overflow —
    exactly the old 16:9 behaviour. Blur / black keep the whole picture visible, centred, with a
    blurred copy of it or black bars in the free room. Zoom always zooms the picture itself.
    """
    zoom = max(1.0, min(float(zoom_factor), 1.25))
    lay = layout or {"fill": "crop", "cw": 1920, "ch": 1080, "sw": 0, "sh": 0}
    cw, ch = int(lay["cw"]), int(lay["ch"])
    sw, sh = int(lay.get("sw") or 0) or cw, int(lay.get("sh") or 0) or ch
    c_sw = int(lay.get("c_sw") or sw)
    c_sh = int(lay.get("c_sh") or sh)

    crop_filt = str(lay.get("crop_filter") or "").strip()
    prefix = f"{crop_filt}," if crop_filt else ""
    head = f"{prefix}hflip," if mirror_video else prefix
    color_grade = ",eq=contrast=1.03:brightness=-0.01:saturation=1.05" if mirror_video else ""

    if lay.get("fill") == "crop" or (not crop_filt and abs(sw / sh - cw / ch) < 0.01):
        fill_w, fill_h = max(cw, _even(cw * zoom)), max(ch, _even(ch * zoom))
        return (f"{head}scale={fill_w}:{fill_h}:force_original_aspect_ratio=increase,"
                f"crop={cw}:{ch},setsar=1{color_grade},format=yuv420p"), zoom

    if "pw" in lay and "ph" in lay and "ox" in lay and "oy" in lay:
        pw, ph = int(lay["pw"]), int(lay["ph"])
        ox, oy = int(lay["ox"]), int(lay["oy"])
    else:
        scale = min(cw / c_sw, ch / c_sh)
        pw, ph = min(cw, _even(c_sw * scale)), min(ch, _even(c_sh * scale))
        ox, oy = _even((cw - pw) / 2.0) if cw > pw else 0, _even((ch - ph) / 2.0) if ch > ph else 0

    zw, zh = max(pw, _even(pw * zoom)), max(ph, _even(ph * zoom))
    picture = f"scale={zw}:{zh},crop={pw}:{ph},setsar=1{color_grade}"
    if lay.get("fill") == "black":
        return f"{head}{picture},pad={cw}:{ch}:{ox}:{oy}:color=black,setsar=1,format=yuv420p", zoom
    bw, bh = _even(cw / 4.0), _even(ch / 4.0)  # the blurred copy is made small (fast), then scaled up
    luma = max(1, min(14, min(bw, bh) // 2 - 1))
    chroma = max(1, min(7, min(bw, bh) // 4 - 1))
    return (f"{head}split=2[vffg0][vfbg0];"
            f"[vfbg0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=luma_radius={luma}:luma_power=2:chroma_radius={chroma}:chroma_power=1,"
            f"scale={cw}:{ch},eq=brightness=-0.06:saturation=0.85,setsar=1[vfbg];"
            f"[vffg0]{picture}[vffg];"
            f"[vfbg][vffg]overlay={ox}:{oy},format=yuv420p"), zoom


def _distributed_freeze_graph(
    source_duration: float,
    target_duration: float,
    slow_factor: float,
    freeze_duration: float,
    mirror_video: bool,
    zoom_factor: float,
    max_freeze_hold: float,
    layout: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, float]]:
    """Build a smooth-ish hybrid graph: moderate slow motion + distributed holds.

    Rather than one long frozen tail, the remaining hold time is spread over
    several positions inside the source segment.  Each hold clones the nearby
    frame for a short period, then motion continues.
    """
    import math

    max_hold = max(0.15, min(float(max_freeze_hold), 1.50))
    # Aim for <= max_hold per pause, but avoid pathological graph sizes.
    hold_count = max(1, int(math.ceil(freeze_duration / max_hold)))
    hold_count = min(24, hold_count)
    hold_each = freeze_duration / hold_count if hold_count else 0.0

    # Split source into equal pieces; hold after each piece (including the last).
    # This makes long extra duration less noticeable than one multi-second tail freeze.
    chunk_count = hold_count
    chunk_source = source_duration / chunk_count
    filters: list[str] = []
    split_labels = "".join(f"[src{i}]" for i in range(chunk_count))
    filters.append(f"[0:v]split={chunk_count}{split_labels}")
    out_labels: list[str] = []
    for i in range(chunk_count):
        a = i * chunk_source
        b = source_duration if i == chunk_count - 1 else (i + 1) * chunk_source
        src_len = b - a
        slowed_len = src_len * slow_factor
        target_len = slowed_len + hold_each
        label = f"[h{i}]"
        filters.append(
            f"[src{i}]trim=start={a:.6f}:end={b:.6f},"
            f"setpts={slow_factor:.9f}*(PTS-STARTPTS),fps=30,"
            f"tpad=stop_mode=clone:stop_duration={hold_each:.6f},"
            f"trim=duration={target_len:.6f},setpts=PTS-STARTPTS{label}"
        )
        out_labels.append(label)
    filters.append(f"{''.join(out_labels)}concat=n={chunk_count}:v=1:a=0[vhyb]")
    visual, zoom = _visual_filters(mirror_video, zoom_factor, layout)
    filters.append(f"[vhyb]{visual},tpad=stop_mode=clone:stop_duration=2.0[vout]")
    metrics = {
        "desired_factor": target_duration / source_duration,
        "applied_factor": slow_factor,
        "source_duration": source_duration,
        "target_duration": target_duration,
        "freeze_duration": freeze_duration,
        "freeze_count": float(hold_count),
        "freeze_each": hold_each,
        "hybrid_used": 1.0,
        "zoom_factor": zoom,
        "mirror_video": 1.0 if mirror_video else 0.0,
    }
    return ";".join(filters), "[vout]", metrics

def ffmpeg_encoders(ffmpeg: str) -> set[str]:
    result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    encoders: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("V"):
            encoders.add(fields[1])
    return encoders


def render_video_part(
    ffmpeg: str,
    source: Path,
    output: Path,
    seg: dict[str, Any],
    target_duration: float,
    encoder: str,
    mirror_video: bool = True,
    zoom_factor: float = 1.05,
    max_video_speed: float = 1.25,
    max_video_slow: float = 1.35,
    smooth_freeze_fallback: bool = False,
    max_freeze_hold: float = 0.75,
    frame_count: int | None = None,
    layout: dict[str, Any] | None = None,
    render_quality: str = "standard",
) -> dict[str, float]:
    """frame_count (when given) locks the part to exactly that many 30 fps frames.
    layout (frame_layout) is the output ratio / fill; None = the original 16:9 fill.

    Without it each part rounds up to the next whole frame; over ~270 parts that
    added ~6 s, so the picture drifted behind the narration and -shortest cut the end.
    """
    source_duration = float(seg["video_end"]) - float(seg["video_start"])
    frame_lock = ["-frames:v", str(int(frame_count))] if frame_count and frame_count > 0 else []
    sync_vf, metrics = _sync_filter(
        source_duration,
        target_duration,
        max_video_speed=max_video_speed,
        max_video_slow=max_video_slow,
        smooth_freeze_fallback=smooth_freeze_fallback,
        max_freeze_hold=max_freeze_hold,
    )

    # CRITICAL: -t MUST come BEFORE -i so FFmpeg limits the INPUT read duration to
    # source_duration instead of truncating the stretched OUTPUT duration at source_duration!
    common_input = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{float(seg['video_start']):.6f}",
        "-t", f"{source_duration:.6f}",
        "-i", str(source),
        "-an",
    ]

    if metrics.get("hybrid_used", 0.0) >= 0.5:
        graph, out_label, metrics = _distributed_freeze_graph(
            source_duration=source_duration,
            target_duration=target_duration,
            slow_factor=float(metrics["applied_factor"]),
            freeze_duration=float(metrics["freeze_duration"]),
            mirror_video=mirror_video,
            zoom_factor=zoom_factor,
            max_freeze_hold=max_freeze_hold,
            layout=layout,
        )
        cmd = [
            *common_input,
            "-filter_complex", graph,
            "-map", out_label,
            *frame_lock,
            *encoder_args_for_quality(encoder, render_quality),
            "-movflags", "+faststart", str(output),
        ]
    else:
        visual, zoom = _visual_filters(mirror_video, zoom_factor, layout)
        vf = sync_vf + "," + visual
        if frame_lock:
            # Clone the last frame so -frames:v can always be satisfied even if input ends a few frames early.
            vf += ",tpad=stop_mode=clone:stop_duration=2.0"
            metrics["frame_count"] = float(frame_count)
        metrics["zoom_factor"] = zoom
        metrics["mirror_video"] = 1.0 if mirror_video else 0.0
        cmd = [
            *common_input,
            "-vf", vf,
            *frame_lock,
            *encoder_args_for_quality(encoder, render_quality),
            "-movflags", "+faststart", str(output),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Video segment render failed ({encoder}):\n{result.stderr[-4000:]}")
    return metrics

def concat_video_parts(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path) -> None:
    list_file = work_dir / "video_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Video concat failed:\n{result.stderr[-4000:]}")


def mux_audio(
    ffmpeg: str,
    video: Path,
    audio: Path,
    output: Path,
    volume_percent: int = 100,
    bgm_path: Optional[Path] = None,
    bgm_volume: float = 0.18,
) -> None:
    # Warm broadcast mastering chain:
    # 1. highpass=60Hz: cuts low-frequency hum/rumble
    # 2. lowpass=12kHz: softens harsh digital sibilance
    # 3. volume scaling: user configured volume
    # 4. alimiter: soft-knee limiter to prevent clipping and ensure comfortable listening
    vol_scale = float(volume_percent or 100) / 100.0

    if bgm_path and Path(bgm_path).is_file():
        # Studio ducking: Voice triggers sidechain compressor on BGM
        # When voice is active, BGM ducks smoothly; when voice pauses, BGM rises gently.
        filter_complex = (
            f"[1:a]highpass=f=75,deesser=i=0.45:m=0.5:f=0.5:s=o,equalizer=f=7200:width_type=q:w=2:g=-3.0,lowpass=f=9500,acompressor=threshold=-18dB:ratio=2.5:attack=15:release=120:makeup=1.3,volume={vol_scale:.3f}[v_master];"
            f"[v_master]asplit=2[v_out][v_sc];"
            f"[2:a]volume={bgm_volume:.3f},aloop=loop=-1:size=2e+09[bgm_loop];"
            f"[bgm_loop][v_sc]sidechaincompress=threshold=0.08:ratio=4:attack=50:release=350[bgm_ducked];"
            f"[v_out][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2,alimiter=limit=0.95:level=disabled[a_final]"
        )
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio), "-i", str(bgm_path),
            "-map", "0:v:0", "-map", "[a_final]", "-map_metadata", "-1", "-c:v", "copy",
            "-filter_complex", filter_complex, "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(output),
        ]
    else:
        af_filters = [
            "highpass=f=75",
            "deesser=i=0.45:m=0.5:f=0.5:s=o",
            "equalizer=f=7200:width_type=q:w=2:g=-3.0",
            "lowpass=f=9500",
            "acompressor=threshold=-18dB:ratio=2.5:attack=15:release=120:makeup=1.3",
            f"volume={vol_scale:.3f}",
            "alimiter=limit=0.95:level=disabled",
        ]
        af_str = ",".join(af_filters)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1", "-c:v", "copy",
            "-af", af_str, "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(output),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Final mux failed:\n{result.stderr[-4000:]}")



def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _render_text_with_pillow(
    text: str,
    output: Path,
    font_size: int = 46,
    max_width: int = 1500,
    bg_alpha: float = 0.75,
    border_color: tuple = (255, 215, 0, 180),
    font_path: str = ""
) -> Path:
    """Render Myanmar text onto a transparent PNG badge with dark pill and gold/white styling."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("Pillow is required for rendering title text. Install with: pip install pillow")

    fonts_dir = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
    cand_fonts = [
        str(font_path or "").strip(),
        str(fonts_dir / "Pyidaungsu.ttf"),
        str(fonts_dir / "NotoSansMyanmar.ttf"),
        os.getenv("RECAP_MM_FONT", ""),
        "/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf",
        r"C:\Windows\Fonts\mmrtext.ttf",
        r"C:\Windows\Fonts\MyanmarText.ttf",
        r"C:\Windows\Fonts\Pyidaungsu.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    font = None
    for cf in cand_fonts:
        if cf and os.path.exists(cf):
            try:
                font = ImageFont.truetype(cf, font_size)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        lines = [text.strip()]

    dummy = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(dummy)

    # Wrap long lines
    wrapped_lines = []
    for line in lines:
        words = line.split(" ")
        curr = ""
        for w in words:
            test = (curr + " " + w).strip()
            bbox = d.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_width and curr:
                wrapped_lines.append(curr)
                curr = w
            else:
                curr = test
        if curr:
            wrapped_lines.append(curr)

    if not wrapped_lines:
        wrapped_lines = [text]

    line_bboxes = [d.textbbox((0, 0), l, font=font) for l in wrapped_lines]
    line_widths = [max(1, b[2] - b[0]) for b in line_bboxes]
    line_heights = [max(font_size, b[3] - b[1]) for b in line_bboxes]
    max_tw = max(line_widths) if line_widths else 200
    line_h = max(line_heights) if line_heights else font_size
    spacing = int(font_size * 0.25)
    total_th = len(wrapped_lines) * line_h + (len(wrapped_lines) - 1) * spacing

    pad_x = int(font_size * 0.8)
    pad_y = int(font_size * 0.35)
    w = max_tw + pad_x * 2
    h = total_th + pad_y * 2

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    alpha_int = int(round(bg_alpha * 255))
    if alpha_int > 0:
        draw.rounded_rectangle([0, 0, w, h], radius=16, fill=(15, 15, 20, alpha_int), outline=border_color, width=2)

    curr_y = pad_y
    for i, l in enumerate(wrapped_lines):
        lw = line_widths[i]
        curr_x = (w - lw) // 2
        # Drop shadow
        draw.text((curr_x + 2, curr_y + 2), l, font=font, fill=(0, 0, 0, 220))
        # Main text in rich luxury gold
        draw.text((curr_x, curr_y), l, font=font, fill=(255, 225, 120, 255))
        curr_y += line_h + spacing

    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output), "PNG")
    return output


def _render_title_png(text: str, output: Path, font_path: str, font_size: int, max_width: int = 1500) -> Path:
    """Render Myanmar title through Chromium renderer with Pillow fallback."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Empty title")

    import sys
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    try:
        from services.text_overlay_service import make_video_title_png
        cfg = {
            "font_path": str(font_path or ""),
            "font_size": int(font_size),
            "max_width": int(max_width),
        }
        return make_video_title_png(text, output, cfg)
    except Exception:
        # Seamless Pillow fallback
        return _render_text_with_pillow(
            text=text,
            output=output,
            font_size=max(24, int(font_size)),
            max_width=int(max_width),
            bg_alpha=0.82,
            border_color=(255, 215, 0, 200),
            font_path=font_path
        )


def _render_extra_text_png(
    text: str,
    output: Path,
    font_size: int,
    background_alpha: float = 0.40,
    max_width: int = 1500,
):
    """Render extra-text overlay with Chromium renderer and Pillow fallback."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Empty extra text")

    import sys
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    try:
        from services.text_overlay_service import make_video_title_png
        size = max(18, min(int(font_size), 84))
        cfg = {
            "font_size": size,
            "min_title_font_size": max(16, min(size, int(round(size * 0.62)))),
            "panel_width": max(320, min(int(max_width), 1800)),
            "video_title_panel_height": max(110, min(360, int(round(size * 4.2)))),
            "max_title_lines": 3,
            "panel_bg_alpha": max(0.0, min(1.0, float(background_alpha))),
            "panel_radius": 22,
            "title_horizontal_padding": 34,
            "title_vertical_padding": 14,
            "stroke_px": 1.4,
            "font_weight": 700,
        }
        return make_video_title_png(text, output, cfg)
    except Exception:
        # Seamless Pillow fallback
        return _render_text_with_pillow(
            text=text,
            output=output,
            font_size=max(18, int(font_size)),
            max_width=int(max_width),
            bg_alpha=max(0.1, float(background_alpha)),
            border_color=(255, 215, 0, 200)
        )

# Free-tier watermark v3 (2026-10-30): v1 was a corner-only lockup —
# rejected (cheap-looking, croppable). v2 added a full-frame DIAGONAL
# TILED pattern of the brand name PLUS that corner lockup — the owner
# still rejected the lockup: it read as a cheap sticker and collided with
# a pattern tile in the top-right corner. v3 therefore carries the
# identity in the pattern ALONE, and the pattern text is the bot's
# username "@voiceclonepro_bot": the text repeats across the whole
# picture (centre and edges alike), so no crop can remove it, while the
# low opacity keeps the video watchable and the subtitles perfectly
# readable. Text is drawn with the bundled Noto Sans Myanmar file (Latin
# glyphs) — never an emoji or a system font, so the ✨-tofu failure of
# the old panel can never recur.
BRAND_PATTERN_TEXT = "@voiceclonepro_bot"
BRAND_PATTERN_ANGLE = 24.0  # PIL rotate(): CCW — text lines rise to the right
BRAND_PATTERN_OPACITY = 0.19  # tuned on frames; watchable but unmistakable


def _brand_text_sprite(font_file: Path, font_size: int, alpha: int) -> Any:
    """One '@voiceclonepro_bot' wordmark as a transparent sprite.

    Generous letter tracking (drawn glyph-by-glyph), rendered at 3x and
    downscaled for smooth edges. A soft dark twin sits under the white
    text so the mark stays visible on bright scenes too.
    """
    from PIL import Image, ImageDraw, ImageFont

    ss = 3
    font = ImageFont.truetype(str(font_file), max(12, font_size) * ss)
    tracking = int(round(font_size * 0.30)) * ss
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    advances = [probe.textlength(ch, font=font) for ch in BRAND_PATTERN_TEXT]
    asc, desc = font.getmetrics()
    top = probe.textbbox((0, 0), BRAND_PATTERN_TEXT, font=font)[1]
    width = int(sum(advances) + tracking * (len(BRAND_PATTERN_TEXT) - 1)) + 2 * ss
    height = asc + desc + 2 * ss
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    shadow_alpha = max(0, min(255, int(alpha * 0.55)))
    for dx, dy, fill in ((ss, ss, (0, 0, 0, shadow_alpha)), (0, 0, (255, 255, 255, alpha))):
        x = float(ss + dx)
        for ch, adv in zip(BRAND_PATTERN_TEXT, advances):
            d.text((x, ss + dy - top), ch, font=font, fill=fill)
            x += adv + tracking
    return img.resize((max(1, width // ss), max(1, height // ss)), Image.LANCZOS)


def render_brand_pattern_png(
    output: Path,
    *,
    frame_width: int,
    frame_height: int,
    opacity: float = BRAND_PATTERN_OPACITY,
) -> Path:
    """Render the full-frame diagonal tiled brand pattern as a PNG.

    The returned PNG is exactly frame_width x frame_height, transparent
    except for the repeated wordmark. The grid is drawn on an oversized
    layer, rotated, then centre-cropped, so tiles cover the corners and
    edges as densely as the centre — cropping any part of the frame still
    leaves the mark in shot.
    """
    import math

    from PIL import Image

    fw = max(64, int(frame_width))
    fh = max(64, int(frame_height))
    opacity = max(0.08, min(0.35, float(opacity)))
    alpha = int(round(255 * opacity))
    font_file = bundled_fonts_dir() / "NotoSansMyanmar.ttf"
    font_size = max(15, int(round(min(fw, fh) * 0.046)))
    sprite = _brand_text_sprite(font_file, font_size, alpha)

    step_x = int(sprite.width * 1.15)
    step_y = int(sprite.height * 2.2)
    diag = int(math.ceil(math.hypot(fw, fh))) + step_x
    layer = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
    row = 0
    y = -sprite.height
    while y < diag + sprite.height:
        xoff = (step_x // 2) if row % 2 else 0
        x = -sprite.width + xoff
        while x < diag + sprite.width:
            layer.alpha_composite(sprite, (x, y))
            x += step_x
        y += step_y
        row += 1
    layer = layer.rotate(BRAND_PATTERN_ANGLE, resample=Image.BICUBIC)
    left = (diag - fw) // 2
    top = (diag - fh) // 2
    img = layer.crop((left, top, left + fw, top + fh))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output)
    return output


# Render quality presets (production-grade programme, 2026-10-29).
# "standard" is EXACTLY the historical encode (libx264 veryfast CRF 22 /
# NVENC p4 CQ 20) so the default behaviour is unchanged; "high" and
# "max" trade render time for lower CRF/CQ and slower presets. Quality
# changes ENCODER SETTINGS ONLY — output resolution is governed by the
# ratio canvas and the tier source cap (720p free / 1080p VIP download).
RENDER_QUALITIES: dict[str, dict[str, list[str]]] = {
    "standard": {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0"],
        "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "0"],
    },
    "high": {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "16", "-b:v", "0"],
        "libx264": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-threads", "0"],
    },
    "max": {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr", "-cq", "12", "-b:v", "0", "-tune", "hq"],
        "libx264": ["-c:v", "libx264", "-preset", "slow", "-crf", "15", "-threads", "0"],
    },
}


def normalize_render_quality(value: Any) -> str:
    """Unknown/empty quality values fall back to "standard" (the
    historical default) rather than failing a render."""
    q = str(value or "standard").strip().lower()
    return q if q in RENDER_QUALITIES else "standard"


def encoder_args_for_quality(encoder: str, quality: Any = "standard") -> list[str]:
    table = RENDER_QUALITIES[normalize_render_quality(quality)]
    return list(table.get(encoder) or table["libx264"])


def _encoder_args(encoder: str) -> list[str]:
    # Historical default encode == the "standard" quality preset.
    return encoder_args_for_quality(encoder, "standard")


BURMESE_SYLLABLE_BREAK = re.compile(
    r'(?<![္])(?=[က-အ\u1023-\u102A\u104E](?![\u1037\u1038]?[်္]))'
)

PARTICLE_SYLS = {
    'ပြီးတော့', 'ပြီးနောက်', 'ပြီးရင်', 'ပြီး', 'တဲ့အခါ', 'အခါ', 'သော်လည်း',
    'ဒါပေမဲ့', 'ဒါပေမယ့်', 'ကြောင့်', 'အတွက်', 'ဖြစ်ပြီး', 'လျက်',
    'ပါတယ်', 'ခဲ့တယ်', 'တယ်', 'နေတယ်', 'နေတာ', 'ပါပဲ', 'ဖြစ်တယ်', 'ရှိတယ်', 'သွားတယ်',
    'လိုက်တယ်', 'လိုက်တာ', 'ကို', 'မှာ', 'က', 'နဲ့', 'နှင့်', 'သို့', 'မှ', 'လည်း', 'တော့',
    'ကနေ', 'နေ', 'ထဲ', 'တုန်း'
}


BURMESE_COMPOUNDS = [
    "ကျွန်တော်", "ကျွန်မ", "သူမ", "သူတို့", "သူက",
    "ကောင်မလေး", "ကောင်လေး", "လူငယ်လေး", "အမျိုးသမီး", "အမျိုးသား",
    "ကမန်းကတန်း", "အလောတကြီး", "တခြား", "စိတ်ပျက်လက်ပျက်", "စိတ်ပျက်", "မထင်မှတ်ဘဲ", "မထင်မှတ်ထား",
    "တစ်ယောက်ဆီကနေ", "တစ်ယောက်ဆီက", "တစ်ယောက်တည်း", "တစ်ယောက်", "တစ်ပွဲ", "တစ်ခု", "ခေါက်ဆွဲတစ်ပွဲ", "ခေါက်ဆွဲ",
    "ဆိုင်ကယ်ပေါ်", "ဆိုင်ကယ်", "ပစ္စည်းတွေကို", "ပစ္စည်းတွေ", "ပစ္စည်း",
    "လမ်းဘေးဈေးသည်တွေနဲ့", "လမ်းဘေးဈေးသည်", "ဈေးသည်",
    "ဆွဲလုယူပြီး", "ဆွဲလုယူ", "ဆွဲယူသွား", "ဆွဲယူ", "မောင်းပြေးသွား", "မောင်းပြေး", "ထွက်ပြေးသွား", "ထွက်ပြေး",
    "ထိတ်လန့်တကြား", "ကြောင်အမ်းအမ်း", "အဖက်မလုပ်ဘဲ", "လက်ထဲက", "အဲ့ဒီတော့မှပဲ", "အဲ့ဒီတော့မှ", "အဲ့ဒီနောက်",
    "အဲဒီတော့မှပဲ", "အဲဒီတော့မှ", "အဲဒီနောက်",
    "ဘာဖြစ်သွားမှန်းမသိ", "ဘာဖြစ်", "ဘာကြောင့်လဲ", "လတ်လတ်ဆတ်ဆတ်", "ထပ်ရောက်လာ", "ထပ်ရောက်", "ပြန်ရောက်လာ", "ပြန်ရောက်",
    "ဖိတ်စင်ကုန်", "ဖိတ်စင်", "ဝင်ထိုင်လိုက်တာနဲ့", "ထိုင်လိုက်တာနဲ့", "ထိုင်လိုက်ရုံပဲ", "ထိုင်လိုက်", "လိုက်ပါသေးတယ်", "သွားပါတော့တယ်",
    "လာစစ်နေတာမှန်း", "လာစစ်နေတာ", "ရိပ်မိလိုက်ရတာပါ", "ရိပ်မိလိုက်တာ", "စိတ်တိုသွား",
    "စည်ပင်တွေ", "စည်ပင်", "တာနဲ့", "တာမို့", "တာကြောင့်", "တဲ့အတွက်", "ပြီးတော့", "ပြီးနောက်", "ပြီးရင်", "တဲ့အခါ", "ဒါပေမဲ့",
    "ဒါပေမယ့်", "ကြောင့်", "အတွက်", "ဖြစ်ပြီး"
]

PARTICLE_SYLS = {
    'ပြီးတော့', 'ပြီးနောက်', 'ပြီးရင်', 'ပြီး', 'တဲ့အခါ', 'အခါ', 'သော်လည်း',
    'ဒါပေမဲ့', 'ဒါပေမယ့်', 'ကြောင့်', 'အတွက်', 'ဖြစ်ပြီး', 'လျက်',
    'ပါတယ်', 'ခဲ့တယ်', 'တယ်', 'နေတယ်', 'နေတာ', 'ပါပဲ', 'ဖြစ်တယ်', 'ရှိတယ်', 'သွားတယ်',
    'လိုက်တယ်', 'လိုက်တာ', 'တာနဲ့', 'တာမို့', 'ကို', 'မှာ', 'က', 'နဲ့', 'နှင့်', 'သို့', 'မှ', 'လည်း', 'တော့',
    'ကနေ', 'နေ', 'ထဲ', 'တုန်း'
}

SUBTITLE_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "yellow": {
        "name": "Classic Gold",
        "label": "အဝါရောင် (Classic Gold)",
        "short": "အဝါရောင်",
        "primary": "&H0000FFFF",  # BGR: Yellow (0x00, 0xFF, 0xFF)
        "outline_col": "&H00000000",
        "back_col": "&H80000000",
        "border_style": 1,
        "outline": 2.4,
        "shadow": 1.4,
    },
    "white": {
        "name": "Clean White",
        "label": "အဖြူရောင် (Clean White)",
        "short": "အဖြူရောင်",
        "primary": "&H00FFFFFF",  # BGR: White
        "outline_col": "&H00000000",
        "back_col": "&H80000000",
        "border_style": 1,
        "outline": 2.2,
        "shadow": 1.2,
    },
    "boxed": {
        "name": "Dark Box",
        "label": "နောက်ခံဘောက် (Dark Box)",
        "short": "နောက်ခံဘောက်",
        "primary": "&H00FFFFFF",
        "outline_col": "&H00000000",
        "back_col": "&H70000000",  # Semi-transparent black box
        "border_style": 3,
        "outline": 1.0,
        "shadow": 0.0,
    },
    "neon": {
        "name": "Neon Cyan",
        "label": "နီယွန်စိမ်းပြာ (Neon Cyan)",
        "short": "နီယွန်",
        "primary": "&H00FFFF00",  # BGR: Cyan (0x00, 0xFF, 0xFF)
        "outline_col": "&H00151515",
        "back_col": "&H80000000",
        "border_style": 1,
        "outline": 2.2,
        "shadow": 1.2,
    },
    # Added by the production-grade programme (2026-10-29). Both burn-in
    # paths (ASS writer + SRT force_style) read this dict, so new presets
    # apply everywhere subtitles are burned; size/position keep flowing
    # through the existing sub_font_size / sub_y controls.
    "band": {
        "name": "Band",
        "label": "နောက်ခံ Band (Band)",
        "short": "Band",
        "primary": "&H00FFFFFF",
        "outline_col": "&H00000000",
        "back_col": "&H99000000",  # Stronger translucent black band than "boxed"
        "border_style": 3,
        "outline": 1.0,
        "shadow": 0.0,
    },
    "heavy": {
        "name": "Heavy Outline",
        "label": "အနားထူ (Heavy Outline)",
        "short": "အနားထူ",
        "primary": "&H00FFFFFF",
        "outline_col": "&H00000000",
        "back_col": "&H80000000",
        "border_style": 1,
        "outline": 4.2,
        "shadow": 2.0,
    },
    "soft": {
        "name": "Soft",
        "label": "နူးညံ့ (Soft)",
        "short": "နူးညံ့",
        "primary": "&H00FFFFFF",
        "outline_col": "&H00222222",
        "back_col": "&HA0000000",
        "border_style": 1,
        "outline": 1.2,
        "shadow": 0.8,
    },
}


# Subtitle font options (recap-quality pass, 2026-10-30). Every file is
# bundled in app/assets/fonts/ under the SIL Open Font License 1.1 (see the
# LICENSE-*.txt files beside them), so subtitle burn-in resolves fonts from
# the repo on any host (incl. Render) — never from a system-font assumption.
# "default" is a SELECTOR, not a family: it means "no explicit choice" and
# resolves to DEFAULT_SUBTITLE_FONT below. The "default" registry entry is
# kept for back-compat — the Noto Sans Myanmar family stays reachable by
# passing its family name explicitly — but it is no longer what an
# un-chosen job burns (owner decision 2026-10-30: Padauk is the default;
# at production size its stacks stay separate and read clearly, where the
# Noto Sans variable font's stacks merged under the heavy outline).
SUBTITLE_FONT_OPTIONS: dict[str, dict[str, Any]] = {
    "default": {
        "name": "Noto Sans",
        "label": "Noto Sans",
        "short": "Noto Sans",
        "family": "Noto Sans Myanmar",
        "files": ["NotoSansMyanmar.ttf"],
        "license": "OFL-1.1 (LICENSE-NotoSansMyanmar-OFL.txt)",
    },
    "serif": {
        "name": "Noto Serif Myanmar",
        "label": "စာလုံးလှ Serif (Noto Serif)",
        "short": "Serif",
        "family": "Noto Serif Myanmar",
        "files": ["NotoSerifMyanmar-Regular.ttf", "NotoSerifMyanmar-Bold.ttf"],
        "license": "OFL-1.1 (LICENSE-NotoSerifMyanmar-OFL.txt)",
    },
    "padauk": {
        "name": "Padauk",
        "label": "ပတေက် (Padauk)",
        "short": "Padauk",
        "family": "Padauk",
        "files": ["Padauk-Regular.ttf", "Padauk-Bold.ttf"],
        "license": "OFL-1.1 (LICENSE-Padauk-OFL.txt)",
    },
}

# The production default: what burns when the user has NOT explicitly
# chosen a font (empty value, the "default" selector, or anything unknown).
# Padauk since 2026-10-30 (owner decision) — clearest Myanmar stacks at
# production subtitle size in the forensic font comparison.
DEFAULT_SUBTITLE_FONT = "padauk"


def bundled_fonts_dir() -> Path:
    """Directory holding the repo-bundled subtitle fonts."""
    return Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"


def bundled_font_files() -> list[str]:
    """Every bundled subtitle font file name (all SUBTITLE_FONT_OPTIONS)."""
    names: list[str] = []
    for opt in SUBTITLE_FONT_OPTIONS.values():
        for fn in opt["files"]:
            if fn not in names:
                names.append(fn)
    return names


def resolve_subtitle_font(value: str | None) -> tuple[str, str]:
    """Resolve a subtitle-font selector to (option_key, family_name).

    Accepts an option key ("serif"), a family name ("Noto Serif Myanmar"),
    or a legacy value. The "default" selector means "no explicit choice"
    and resolves to DEFAULT_SUBTITLE_FONT (Padauk) — same for empty and
    unknown values. Legacy "Pyidaungsu" maps to the default too:
    Pyidaungsu's GSUB shaping is incomplete for libass (see
    ensure_system_myanmar_fonts) and it is not bundled. A family name
    passed explicitly ("Noto Sans Myanmar") still round-trips to its own
    family — only the un-chosen path changed.
    """
    v = str(value or "").strip()
    if not v:
        return DEFAULT_SUBTITLE_FONT, SUBTITLE_FONT_OPTIONS[DEFAULT_SUBTITLE_FONT]["family"]
    low = v.lower()
    if low == "default":
        return DEFAULT_SUBTITLE_FONT, SUBTITLE_FONT_OPTIONS[DEFAULT_SUBTITLE_FONT]["family"]
    if low in SUBTITLE_FONT_OPTIONS:
        return low, SUBTITLE_FONT_OPTIONS[low]["family"]
    for key, opt in SUBTITLE_FONT_OPTIONS.items():
        if low == str(opt["family"]).lower():
            return key, opt["family"]
    return DEFAULT_SUBTITLE_FONT, SUBTITLE_FONT_OPTIONS[DEFAULT_SUBTITLE_FONT]["family"]


def stage_bundled_fonts(dest_dir: Path) -> Path | None:
    """Copy every bundled subtitle font into dest_dir for libass fontsdir.

    Returns dest_dir when at least one font was staged, else None.
    """
    src_dir = bundled_fonts_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        staged = False
        for fn in bundled_font_files():
            src_f = src_dir / fn
            if src_f.is_file():
                shutil.copy2(src_f, dest_dir / fn)
                staged = True
        return dest_dir if staged else None
    except Exception:
        return None


def split_into_burmese_syllables(text: str) -> list[str]:
    """Splits Burmese text into indivisible atomic syllables, merging compound connectives so they stay together."""
    if not text:
        return []
    raw_syls = [s for s in BURMESE_SYLLABLE_BREAK.split(text) if s]
    merged = []
    i = 0
    while i < len(raw_syls):
        matched = False
        for comp in BURMESE_COMPOUNDS:
            joined = ""
            j = i
            while j < len(raw_syls) and len(joined.rstrip()) < len(comp):
                joined += raw_syls[j]
                j += 1
            if joined.rstrip() == comp:
                merged.append(joined)
                i = j
                matched = True
                break
        if not matched:
            merged.append(raw_syls[i])
            i += 1
    return merged


MYANMAR_HOMOGLYPHS: dict[str, str] = {
    "\u1293": "\u1014\u102c",  # Ethiopic NAA -> Burmese 'နာ'
    "\u1290": "\u1014",        # Ethiopic NA -> Burmese 'န'
    "\u1260": "\u1018",        # Ethiopic BA -> Burmese 'ဗ'
    "\u1261": "\u1018\u102f",   # Ethiopic BU -> Burmese 'ဗု'
    "\u1220": "\u101e",        # Ethiopic SA -> Burmese 'သ'
    "\u1223": "\u101e\u102c",  # Ethiopic SAA -> Burmese 'သာ'
    "\u12d0": "\u1025",        # Ethiopic A -> Burmese 'ဥ'
    "\u10dd": "\u101d",        # Georgian ON -> Burmese 'ဝ'
    "\u10d8": "\u102d",        # Georgian INI -> Burmese 'ိ'
    "\u10d0": "\u1001",        # Georgian ANI -> Burmese 'ခ'
    "\u10d2": "\u101e",        # Georgian GANI -> Burmese 'သ'
    "\u10e7": "\u101c",        # Georgian QARI -> Burmese 'လ'
    "\u10e3": "\u1015",        # Georgian UNI -> Burmese 'ပ'
    "\u10e9": "\u101b",        # Georgian CHINI -> Burmese 'ရ'
}


def normalize_burmese_homoglyphs(text: Any) -> str:
    """Normalize foreign homoglyphs (Ethiopic/Georgian look-alikes) to authentic Myanmar Unicode."""
    s = str(text or "")
    if not s:
        return ""
    for k, v in MYANMAR_HOMOGLYPHS.items():
        if k in s:
            s = s.replace(k, v)
    return s


MYANMAR_SPELLING_CORRECTIONS: list[tuple[re.Pattern, str]] = [
    # Unicode codepoints & stacked/composed letter fixes
    (re.compile(r"[\u1025\u1008]\u103A"), "\u1009\u103A"),  # ဥ/ဉ + ် -> ဉ်
    (re.compile(r"\u1009\u103A\u103A+"), "\u1009\u103A"),
    # Zero vs Wa confusion within Myanmar words:
    (re.compile(r"(?<=[\u1000-\u1021\u102B-\u103E])\u1040(?=[\u1000-\u1021\u102B-\u103E])"), "\u101D"),
    # Common Movie Recap spelling fixes
    (re.compile(r"အဲ့ဒီ"), "အဲဒီ"),  # standard written form အဲဒီ, never colloquial အဲ့ဒီ (owner rule)
    (re.compile(r"သေခြာ"), "သေချာ"),
    (re.compile(r"ဘာဖစ်ဖစ်"), "ဘာဖြစ်ဖြစ်"),
    (re.compile(r"အဖစ်(?=[^\w\s]|\s|$|မှန်|အပျက်)"), "အဖြစ်"),
    (re.compile(r"မဖစ်"), "မဖြစ်"),
    (re.compile(r"ဖစ်(?=[တယ်ပြီးလာသွားနေခဲ့တာစေသလိုလို့ရင်ပေါ်ပျက်ရပ်နိုင်ချင်ရကောင်းစရာပဲပါကြတော့])"), "ဖြစ်"),
    (re.compile(r"(^|\s)ဖစ်(\s|$)"), r"\1ဖြစ်\2"),
    (re.compile(r"ပြသာနာ|ပြသနာ"), "ပြဿနာ"),
    (re.compile(r"ရုပ်တရက်"), "ရုတ်တရက်"),
    (re.compile(r"ဂရုဆိုက်"), "ဂရုစိုက်"),
    (re.compile(r"အံသြ|အံ့သြ"), "အံ့ဩ"),
    # NOTE: continuations must be explicit words — a character class built from the
    # letters of those words also matches 'း'/'ု', which corrupted the classifier
    # noun 'ခန်း' (room) into 'ခဏ်း' and 'အခန်း' into 'အခဏ်း'.
    (re.compile(r"ခန(?=စောင့်|လေး|ခန|အတွင်း|အကြာ|နေတာ|တဖြုတ်|\s|$)"), "ခဏ"),
    (re.compile(r"မတက်နိုင်"), "မတတ်နိုင်"),
    (re.compile(r"တက်နိုင်သမျှ"), "တတ်နိုင်သမျှ"),
    (re.compile(r"တက်နိုင်သလောက်"), "တတ်နိုင်သလောက်"),
    (re.compile(r"ကြေးဇူး"), "ကျေးဇူး"),
    (re.compile(r"ကျိုးစား(?=[ပြီးနေခဲ့ပါကြမလားမယ်ချင်])"), "ကြိုးစား"),
    (re.compile(r"မကြာမှီ"), "မကြာမီ"),
    (re.compile(r"လွှတ်မြောက်"), "လွတ်မြောက်"),
    (re.compile(r"တကယ့်တော့"), "တကယ်တော့"),
    (re.compile(r"အခြေနေ(?=[^\w\s]|\s|$|မှာ|ကို|က|ကြောင့်)"), "အခြေအနေ"),
    (re.compile(r"အမှန်တစ်ကယ်"), "အမှန်တကယ်"),
    (re.compile(r"တိတ်တဆိပ်"), "တိတ်တဆိတ်"),
    (re.compile(r"ကြေနပ်(?=[တယ်ပြီးလာသွားနေခဲ့တာပဲပါစရာ])"), "ကျေနပ်"),
]


# Curated word-level spelling fixes for generated recap narration (data-driven so
# the table can grow). These are model-output errors the regex table above does not
# cover: loanword transliterations and common misspellings found in live recap
# scripts (owner report 2026-10-29, job rc_1793279760). Plain substring
# replacements — every key must be unambiguous (never a substring of a correct
# word) so correct text passes through unchanged.
MYANMAR_SPELLING_FIXES: dict[str, str] = {
    "ဒီအန်အေ": "ဒီအဲန်အေ",  # DNA — the letter N is 'အဲန်', not 'အန်'
    "သံသဝင်": "သံသယဝင်",  # သံသယ (doubt) keeps its ယ
    "လူပုံအလယ်": "လူပုံလယ်",  # the idiom is လူပုံလယ်, not အလယ်
    "အကြပ်အတည်း": "အကျပ်အတည်း",  # ကျပ်, not ကြပ်
    "အအပြဲ": "အပြင်းအထန်",  # garbled doubled-အ token in model output
    "ဘွားအေ": "အဘွား",  # grandmother — standard written form
    "လက်ဝတ်ရစား": "လက်ဝတ်ရတနာ",  # jewelry — modern standard term
}


def normalize_burmese_orthography(text: Any) -> str:
    """Normalize Myanmar Unicode spelling according to standard Myanmar Language Commission orthography."""
    s = str(text or "")
    if not s:
        return ""
    s = normalize_burmese_homoglyphs(s)
    for pat, rep in MYANMAR_SPELLING_CORRECTIONS:
        s = pat.sub(rep, s)
    for wrong, right in MYANMAR_SPELLING_FIXES.items():
        if wrong in s:
            s = s.replace(wrong, right)
    return s


def ensure_system_myanmar_fonts() -> str:
    """Ensure authentic Myanmar Unicode fonts (Noto Sans Myanmar) are registered
    with Fontconfig on Linux, and return the best available font family name.

    NOTE: Noto Sans Myanmar has complete HarfBuzz mym2 GSUB pre-base reordering tables
    required by libass so pre-base vowels ('ေ') and medials ('ြ') render on the left of
    consonants instead of backwards.
    """
    fonts_dir = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
    if sys.platform.startswith("linux"):
        try:
            sys_font_dir = Path("/usr/share/fonts/truetype/myanmar")
            user_font_dir = Path.home() / ".local" / "share" / "fonts" / "myanmar"
            target_dir = sys_font_dir if sys_font_dir.exists() and os.access(str(sys_font_dir), os.W_OK) else user_font_dir
            if not target_dir.is_dir():
                target_dir.mkdir(parents=True, exist_ok=True)

            needs_cache = False
            # Register every bundled subtitle font (all SUBTITLE_FONT_OPTIONS)
            # so SRT force_style burns can also resolve the chosen family.
            for font_fn in bundled_font_files():
                font_src = fonts_dir / font_fn
                if not font_src.is_file():
                    continue
                dest = target_dir / font_src.name
                if not dest.is_file() or dest.stat().st_size != font_src.stat().st_size:
                    try:
                        shutil.copy2(font_src, dest)
                        needs_cache = True
                    except Exception:
                        pass
            # Remove legacy Pyidaungsu.ttf from system/user font dir so libass never picks its incomplete GSUB table
            bad_pyi = target_dir / "Pyidaungsu.ttf"
            if bad_pyi.is_file():
                try:
                    bad_pyi.unlink()
                    needs_cache = True
                except Exception:
                    pass

            conf_src = fonts_dir / "99-myanmar.conf"
            if conf_src.is_file():
                for conf_dir in (Path("/etc/fonts/conf.d"), Path.home() / ".config" / "fontconfig" / "conf.d"):
                    try:
                        if conf_dir == Path("/etc/fonts/conf.d") and not (conf_dir.exists() and os.access(str(conf_dir), os.W_OK)):
                            continue
                        conf_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(conf_src, conf_dir / "99-myanmar.conf")
                        needs_cache = True
                    except Exception:
                        pass

            if needs_cache and shutil.which("fc-cache"):
                subprocess.run(["fc-cache", "-f", str(target_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            pass

    # Resolve active font family via fc-match if available (prefer Noto Sans Myanmar first!)
    if shutil.which("fc-match"):
        try:
            res2 = subprocess.run(["fc-match", "-f", "%{family}\n", "Noto Sans Myanmar"], capture_output=True, text=True, timeout=5)
            matched2 = (res2.stdout or "").strip().lower()
            if "noto" in matched2 or "myanmar" in matched2:
                return "Noto Sans Myanmar"
            res3 = subprocess.run(["fc-match", "-f", "%{family}\n", "Padauk"], capture_output=True, text=True, timeout=5)
            if "padauk" in (res3.stdout or "").lower():
                return "Padauk"
        except Exception:
            pass
    return "Noto Sans Myanmar"


def _fmt_ass_time(sec: float) -> str:
    """Format seconds into ASS timestamp H:MM:SS.cs."""
    cs = max(0, int(round(sec * 100)))
    h = cs // 360000
    m = (cs % 360000) // 6000
    s = (cs % 6000) // 100
    c = cs % 100
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def write_synced_ass_file(
    cues: list[tuple[float, float, str]],
    ass_path: Path,
    cw: int,
    ch: int,
    font_name: str = "Noto Sans Myanmar",
    raw_font_size: int = 16,
    sub_style: str = "yellow",
    sub_lines: int = 1,
    sub_y: float | None = None,
    subtitles_margin_v: float | None = None,
    boxes: list[dict[str, Any]] | None = None,
) -> Path:
    """Generate a pixel-accurate ASS subtitle file with complex Myanmar script text shaping."""
    cw = max(320, int(cw))
    ch = max(240, int(ch))
    is_vert = ch > cw
    # Resolve option keys ("serif") and legacy values ("Pyidaungsu") to a
    # bundled family name for libass HarfBuzz mym2 shaping.
    _, font_name = resolve_subtitle_font(font_name)

    # Canvas-scaled font size based on height
    scale_factor = (raw_font_size / 288.0)
    if is_vert:
        target_size = int(round(ch * scale_factor * 0.42)) if sub_lines == 1 else int(round(ch * scale_factor * 0.62))
        font_size = max(22, min(44 if sub_lines == 1 else 56, target_size))
    else:
        target_size = int(round(ch * scale_factor * 0.60)) if sub_lines == 1 else int(round(ch * scale_factor * 0.80))
        font_size = max(26, min(48 if sub_lines == 1 else 62, target_size))

    # Canvas-normalized subtitle positioning
    # sy is the center of the subtitle line as a fraction from top (0.0 to 1.0).
    # ASS Alignment 2 (bottom-center) positions text baseline at margin_v.
    # To place the subtitle center exactly at sy*ch, subtract half the rendered text height:
    text_half_height = (font_size * (1.15 if sub_lines == 1 else 2.3)) / 2.0
    if sub_y is not None:
        sy = _clamp01(float(sub_y), 0.85)
        pixels_from_bottom = (ch * (1.0 - sy)) - text_half_height
    elif subtitles_margin_v is not None:
        mv_val = float(subtitles_margin_v)
        pixels_from_bottom = mv_val if mv_val > 100 else (mv_val / 288.0) * ch
    elif boxes:
        b0 = boxes[0]
        by = _clamp01(b0.get("y", 0.85))
        pixels_from_bottom = (ch * (1.0 - by)) - text_half_height
    else:
        pixels_from_bottom = max(20, int(round(ch * 0.12)))

    margin_v = max(10, min(ch - 80, int(round(pixels_from_bottom))))

    st_key = str(sub_style or "yellow").lower()
    st = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
    p_col = st["primary"]
    o_col = st["outline_col"]
    b_col = st["back_col"]
    b_style = st["border_style"]
    outl = max(1.5, float(st["outline"]) * (ch / 720.0))
    shad = max(1.0, float(st["shadow"]) * (ch / 720.0))
    wrap_style = 2 if sub_lines == 1 else 0
    margin_h = max(20, int(round(cw * 0.04)))

    header = f"""[Script Info]
Title: Synced Myanmar Subtitles
ScriptType: v4.00+
WrapStyle: {wrap_style}
ScaledBorderAndShadow: yes
YCbCr Matrix: None
PlayResX: {cw}
PlayResY: {ch}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{p_col},&H000000FF,{o_col},{b_col},-1,0,0,0,100,100,0,0,{b_style},{outl:.2f},{shad:.2f},2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = []
    for st_sec, en_sec, cue_text in cues:
        c_text = canonicalize_burmese_text(cue_text)
        if not c_text:
            continue
        if sub_lines == 1:
            ass_line_text = re.sub(r'\s+', ' ', c_text.replace("\r\n", " ").replace("\n", " ")).strip()
        else:
            ass_line_text = c_text.replace("\r\n", r"\N").replace("\n", r"\N").strip()
        st_str = _fmt_ass_time(st_sec)
        en_str = _fmt_ass_time(en_sec)
        dialogues.append(f"Dialogue: 0,{st_str},{en_str},Default,,0,0,0,,{ass_line_text}")

    ass_path.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")
    return ass_path


def detect_source_subtitle_bands(
    ffmpeg: str,
    ffprobe: str,
    source: Path,
    work_dir: Path,
    *,
    samples: int = 12,
    log: LogFn = _noop,
) -> list[dict[str, float]]:
    """Detect the vertical band(s) where the SOURCE video's own burned-in subtitles sit.

    Recap sources (short dramas) almost always carry hard-coded dialogue subtitles at a
    fixed height. The preset blur box only covers the Myanmar subtitle position (y≈0.85),
    so when the source's own band sits higher (commonly y≈0.70) those subtitles survive
    into the output — and because the picture is mirrored by default they come out as
    unreadable mirror-writing right above the Myanmar line.

    Method: sample frames across the video, score every image row by "bright glyph pixels
    with a dark outline nearby" (the classic hard-sub look), aggregate across frames.
    A genuine subtitle band is text-dense in most frames; scene brightness is not.
    Returns blur-box dicts in SOURCE-frame fractions ({x, y=center, w, h} of the
    source picture) — NOT canvas fractions. Callers must map each box through
    source_band_to_canvas_box() with the render layout before adding it to
    overlays["blur_boxes"], whose consumers use output-canvas fractions.
    Any failure returns [] — detection must never block a render.
    """
    try:
        import numpy as np
        from PIL import Image

        duration = probe_duration(ffprobe, source)
        if duration <= 1.0:
            return []
        frame_dir = work_dir / "subband_frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        agg = None
        pers = None
        used = 0
        for i in range(max(4, samples)):
            t = duration * (i + 0.5) / max(4, samples)
            fp = frame_dir / f"band_{i:02d}.png"
            p = subprocess.run(
                [ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(source),
                 "-frames:v", "1", "-vf", "scale=270:-2", str(fp), "-y"],
                capture_output=True, text=True, timeout=60,
            )
            if p.returncode != 0 or not fp.is_file():
                continue
            g = np.asarray(Image.open(fp).convert("L")).astype(int)
            h_px, w_px = g.shape
            bright = g > 175
            dark = g < 110
            near_dark = np.zeros_like(bright)
            for dx in (2, 3, 4):
                near_dark[:, dx:] |= dark[:, :-dx]
                near_dark[:, :-dx] |= dark[:, dx:]
            row_score = (bright & near_dark).sum(axis=1) / float(w_px)
            agg = row_score if agg is None else agg + row_score
            hit = (row_score > 0.02).astype(float)
            pers = hit if pers is None else pers + hit
            used += 1
        if not used or agg is None or pers is None:
            return []
        agg /= used
        pers /= used
        h_px = agg.shape[0]
        y_lo = int(0.40 * h_px)
        cand = [y for y in range(y_lo, h_px) if pers[y] >= 0.35 and agg[y] >= 0.012]
        if not cand:
            return []
        groups: list[list[int]] = []
        for y in cand:
            if groups and y - groups[-1][-1] <= max(3, int(0.013 * h_px)):
                groups[-1].append(y)
            else:
                groups.append([y])
        boxes: list[dict[str, float]] = []
        for grp in groups:
            if max(float(agg[y]) for y in grp) < 0.03:
                continue  # weak smear, not a subtitle band
            top = grp[0] / h_px
            bot = (grp[-1] + 1) / h_px
            core_h = bot - top
            if core_h < 0.015:
                # Hairline row noise (1-3 rows at sample scale), never a
                # text line: a real subtitle glyph band is several times
                # taller. Round-4 evidence: on a subtitle-free cartoon
                # the bright exhibit rows at y~0.48 formed a 0.5%-tall
                # group that passed every other threshold and blurred a
                # strip across the middle of the finished movie.
                continue
            # Pad: dialogue subs often have a second line ABOVE the persistent one,
            # and the renderer's zoom shifts the band slightly — cover both.
            top = max(0.0, top - 1.4 * core_h)
            bot = min(1.0, bot + 0.6 * core_h)
            if bot - top > 0.16:  # safety cap: never blur a huge slab on a guess
                mid = (top + bot) / 2.0
                top, bot = mid - 0.08, mid + 0.08
            boxes.append({"x": 0.0, "y": round((top + bot) / 2.0, 4), "w": 1.0, "h": round(bot - top, 4)})
        if boxes:
            log("🔍 Source subtitle band(s) detected: " + ", ".join(f"y={b['y']:.2f} h={b['h']:.2f}" for b in boxes))
        return boxes
    except Exception as exc:
        log(f"⚠️ Source subtitle band detection skipped: {exc}")
        return []


def canonicalize_burmese_text(text: str) -> str:
    """Fix common typing inversions, redundant spaces, and normalize Myanmar Unicode."""
    if not text:
        return ""
    # 0. Normalize foreign homoglyphs and standard Myanmar orthography / spelling
    t = normalize_burmese_orthography(text)
    # 1. Visual-order 'ေ' typed BEFORE its consonant (e.g. "ေမ" at a word start -> "မေ").
    #    ONLY when the ေ is not attached to a preceding Myanmar letter/mark — in correct Unicode
    #    "ေ" always follows its consonant, and the next syllable's consonant must never be swapped
    #    (the old unconditional swap turned "မေမေ"->"မမေေ", "တွေကို"->"တွကေို", "နေတယ်"->"နတေယ်").
    t = re.sub(r'(?<![\u1000-\u103F\u1050-\u109F])\u1031([\u1000-\u1021][\u103B-\u103E]*)', '\\1\u1031', t)
    # 2. Fix medial order if reversed (e.g. ွ before ျ/ြ)
    t = re.sub(r'(\u103D)(\u103B|\u103C)', r'\2\1', t)
    # 3. Fix ေ before medials (e.g. က + ေ + ျ -> က + ျ + ေ)
    t = re.sub(r'([\u1000-\u1021])(\u1031)([\u103B-\u103E]+)', r'\1\3\2', t)
    # 4. Remove unwanted spaces before punctuation or dependent vowels
    t = re.sub(r'\s+([\u102B-\u103E\u104A\u104B])', r'\1', t)
    # 5. Clean up multiple spaces
    t = re.sub(r'[ \t]+', ' ', t).strip()
    # 6. Fix common Myanmar spelling/Unicode stacking errors
    # ြ (U+103C) must come before ျ (U+103B) if both present after same base
    t = re.sub(r'(\u103B)(\u103C)', r'\2\1', t)
    # Remove stray zero-width joiners/non-joiners that garble rendering
    t = t.replace('\u200B', '').replace('\u200C', '').replace('\u200D', '')
    # Normalize double-Myanmar period (။။) → single (။)
    t = re.sub(r'(။)+', '။', t)
    # Normalize multiple Myanmar comma (၊၊) → single (၊)
    t = re.sub(r'(၊)+', '၊', t)
    # Remove trailing Myanmar period at very end if followed by nothing (already ended)
    t = t.rstrip()
    return t


BURMESE_PROTECTED_WORDS = sorted([
    "သူငယ်ချင်း", "အရင်းနှီးဆုံး", "အရင်းနှီး", "ကျွန်တော့်ရဲ့", "ကျွန်တော့်ကို", "ကျွန်တော်",
    "ကျွန်မရဲ့", "ကျွန်မကို", "ကျွန်မ", "သူမ", "သူတို့", "ကောင်မလေး", "ကောင်လေး", "လူငယ်လေး",
    "အမျိုးသမီး", "အမျိုးသား", "မအိပ်ရသေးဘူး", "မထင်မှတ်ဘဲ", "မထင်မှတ်ထား", "တစ်ယောက်တည်း",
    "တစ်ယောက်", "အခုချက်ချင်း", "ကျေးဇူးတင်ပါတယ်", "ကျေးဇူးတင်", "အဆင်ပြေရဲ့လား", "အဆင်ပြေ",
    "ပင်ပန်း", "ဒေါသထွက်", "သတိထား", "ပြန်ရောက်", "ဖြစ်သွားတာလဲ", "အခြေအနေ", "ဘာတွေဖြစ်",
    "ဘာမှမလိုတော့ဘူး", "ငြိမ်ငြိမ်လေး", "သလောက်", "ဘယ်လောက်", "ဒီလောက်", "အဲ့လောက်", "အဲ့ဒီလောက်", "အဲဒီလောက်",
    "ကိန်းဂဏန်း", "အံ့အားသင့်", "မန်နေဂျာ", "အလုပ်သမား", "သေမတတ်", "ရေခဲတိုက်", "စည်းကမ်းချက်",
    "လတ်လတ်ဆတ်ဆတ်", "ပတ်ဝန်းကျင်", "အခွင့်အရေး", "တစ်ခါတစ်ရံ", "ချက်ချင်းပဲ", "သက်သေပြ",
], key=len, reverse=True)


def _is_valid_burmese_token_boundary(text: str, idx: int) -> bool:
    """Return True only if idx is the end of text, non-Myanmar char/punctuation, or a true syllable break."""
    if idx >= len(text):
        return True
    ch = text[idx]
    if not ('\u1000' <= ch <= '\u109F'):
        return True
    if '\u1040' <= ch <= '\u104B':
        return True
    return BURMESE_SYLLABLE_BREAK.match(text, idx) is not None


def tokenize_burmese_words(text: str) -> list[str]:
    """Tokenize Burmese text into natural words/particles without breaking syllables or vowel clusters."""
    tokens = []
    i = 0
    particles = [
        'ပြီးတော့', 'ပြီးနောက်', 'ပြီးရင်', 'တဲ့အခါ', 'ဒါပေမဲ့', 'ဒါပေမယ့်',
        'ကြောင့်', 'အတွက်', 'ပါတော့တယ်', 'ပါတယ်', 'ခဲ့တယ်', 'တယ်', 'နေတာ',
        'ပါပဲ', 'လိုက်တယ်', 'က', 'နဲ့', 'မှာ', 'ကို', 'တော့', 'ရဲ့', 'လေ', 'နော်', '၊', '။', ' ',
    ]
    while i < len(text):
        matched = False
        for pw in BURMESE_PROTECTED_WORDS:
            if text[i:].startswith(pw) and _is_valid_burmese_token_boundary(text, i + len(pw)):
                tokens.append(pw)
                i += len(pw)
                matched = True
                break
        if not matched:
            for p in particles:
                if text[i:].startswith(p) and _is_valid_burmese_token_boundary(text, i + len(p)):
                    tokens.append(p)
                    i += len(p)
                    matched = True
                    break
        if not matched:
            m = BURMESE_SYLLABLE_BREAK.search(text, i + 1)
            next_idx = m.start() if m else len(text)
            tokens.append(text[i:next_idx])
            i = next_idx
    return tokens


def _split_burmese_into_single_line_chunks(text: str, max_chars: int = 38) -> list[str]:
    """Split text into 1-line chunks without cutting words or syllables in half."""
    text = re.sub(r'\s+', ' ', text.replace("\r\n", " ").replace("\n", " ")).strip()
    if len(text) <= max_chars:
        return [text]

    # If full sentences exist, split first at ။
    if "။" in text:
        parts = [p.strip() for p in re.split(r'(?<=[။])\s*', text) if p.strip()]
        if len(parts) > 1:
            res = []
            for p in parts:
                res.extend(_split_burmese_into_single_line_chunks(p, max_chars=max_chars))
            return res

    # If clauses exist, split at ၊
    if "၊" in text:
        parts = [p.strip() for p in re.split(r'(?<=[၊])\s*', text) if p.strip()]
        if len(parts) > 1:
            res = []
            for p in parts:
                res.extend(_split_burmese_into_single_line_chunks(p, max_chars=max_chars))
            return res

    # Otherwise split into words/tokens. Prefer the natural phrase spaces Gemini writes
    # (never break inside a space-delimited phrase); only an over-long phrase falls back to
    # syllable-safe particle tokenization.
    tokens: list[str] = []
    for word in text.split(" "):
        if not word:
            continue
        if len(word) > max_chars:
            tokens.extend(t for t in tokenize_burmese_words(word) if t)
        else:
            tokens.append(word)
        tokens.append(" ")
    if tokens and tokens[-1] == " ":
        tokens.pop()
    if not tokens:
        return [text]

    chunks = []
    curr = ""
    for tok in tokens:
        if curr and (len(curr) + len(tok) > max_chars):
            chunks.append(curr.strip())
            curr = tok
        else:
            curr += tok
    if curr.strip():
        chunks.append(curr.strip())

    # Merge very short tail chunk (< 12 chars) with preceding chunk so it doesn't flash awkwardly
    if len(chunks) > 1 and len(chunks[-1]) < 12:
        sep = " " if (chunks[-2] + " " + chunks[-1]) in text else ""
        merged_tail = (chunks[-2] + sep + chunks[-1]).strip()
        chunks[-2] = merged_tail
        chunks.pop()

    return chunks if chunks else [text]


def _format_cue_lines(text: str, lines_cnt: int) -> str:
    """Format subtitle text into 1 or 2 lines without cutting words in half."""
    text = canonicalize_burmese_text(text)
    if lines_cnt == 1:
        # Strictly return a single line with no embedded newlines!
        return re.sub(r'\s+', ' ', text.replace("\r\n", " ").replace("\n", " ")).strip()

    if "\n" in text:
        return text

    # If spaces exist, try breaking at the most balanced space
    words = text.split(" ")
    if len(words) > 1:
        half = len(text) // 2
        best_idx = 1
        best_diff = 9999
        for i in range(1, len(words)):
            l1 = " ".join(words[:i])
            diff = abs(len(l1) - half)
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        l1 = " ".join(words[:best_idx]).strip()
        l2 = " ".join(words[best_idx:]).strip()
        if l1 and l2:
            return f"{l1}\n{l2}"

    # If no spaces, tokenize preserving protected words and particle boundaries
    tokens = tokenize_burmese_words(text)

    if len(tokens) > 1:
        half_len = len(text) // 2
        best_idx = 1
        best_diff = 9999
        for k in range(1, len(tokens)):
            l1 = "".join(tokens[:k])
            diff = abs(len(l1) - half_len)
            prev_tok = tokens[k - 1].strip()
            if prev_tok in ('။', '၊', 'ပြီးတော့', 'ပြီးနောက်', 'ပြီးရင်', 'တဲ့အခါ', 'ဒါပေမဲ့', 'ဒါပေမယ့်', 'ကြောင့်', 'အတွက်'):
                diff -= 15
            elif prev_tok in ('ပါတယ်', 'ခဲ့တယ်', 'တယ်', 'နေတာ', 'ပါပဲ', 'လိုက်တယ်', 'က', 'နဲ့', 'မှာ', 'ကို', 'တော့', 'ရဲ့', 'လေ', 'နော်'):
                diff -= 8
            if diff < best_diff:
                best_diff = diff
                best_idx = k
        p1 = "".join(tokens[:best_idx]).strip()
        p2 = "".join(tokens[best_idx:]).strip()
        if p1 and p2:
            return f"{p1}\n{p2}"

    return text


def split_burmese_subtitle_cues(
    start_sec: float,
    end_sec: float,
    text: str,
    max_cue_chars: int = 40,
    lines_per_cue: int = 1,
) -> list[tuple[float, float, str]]:
    """
    Formats spoken segment narration into synchronized, comfortable, and readable subtitle cues.
    - If lines_per_cue == 1: strictly splits long sentences across time into sequential 1-line cues.
      NEVER forces 2 lines onto screen when 1 line is selected!
    - If lines_per_cue == 2: wraps into 2 balanced lines within the same cue.
    - NEVER cuts words in half (preserves compounds and syllables).
    """
    text = canonicalize_burmese_text(text)
    if not text:
        return []

    lines_cnt = 1 if int(lines_per_cue) == 1 else 2
    total_dur = max(0.6, float(end_sec) - float(start_sec))

    if lines_cnt == 1:
        chunks = _split_burmese_into_single_line_chunks(text, max_chars=max_cue_chars)
        if len(chunks) <= 1:
            return [(round(start_sec, 3), round(end_sec, 3), _format_cue_lines(text, 1))]

        tot_chars = sum(len(c) for c in chunks) or 1
        cur_t = start_sec
        results = []
        for i, chunk in enumerate(chunks):
            c_dur = max(1.1, (len(chunk) / tot_chars) * total_dur)
            c_end = cur_t + c_dur if i < len(chunks) - 1 else end_sec
            if c_end > end_sec:
                c_end = end_sec
            results.append((round(cur_t, 3), round(c_end, 3), _format_cue_lines(chunk, 1)))
            cur_t = c_end
        return results

    # If lines_per_cue == 2:
    if total_dur >= 4.0 and "။" in text:
        sentences = [s.strip() for s in re.split(r'(?<=[။])\s*', text) if s.strip()]
        if len(sentences) >= 2 and (total_dur / len(sentences)) >= 1.8:
            tot_chars = sum(len(s) for s in sentences) or 1
            cur_t = start_sec
            results = []
            for i, sent in enumerate(sentences):
                s_dur = max(1.8, (len(sent) / tot_chars) * total_dur)
                s_end = cur_t + s_dur if i < len(sentences) - 1 else end_sec
                formatted = _format_cue_lines(sent, 2)
                results.append((round(cur_t, 3), round(s_end, 3), formatted))
                cur_t = s_end
            return results

    # Otherwise, keep as ONE cue spanning the full spoken segment duration!
    formatted_text = _format_cue_lines(text, 2)
    return [(round(start_sec, 3), round(end_sec, 3), formatted_text)]


def apply_production_overlays(
    ffmpeg: str,
    input_video: Path,
    output_video: Path,
    overlays: dict[str, Any] | None,
    encoder: str,
    work_dir: Path,
    log: LogFn = _noop,
    frame_size: tuple[int, int] = (1920, 1080),
    layout: dict[str, Any] | None = None,
    render_quality: str = "standard",
) -> Path:
    overlays = dict(overlays or {})
    cw, ch = int(frame_size[0]), int(frame_size[1])
    lay = layout or {}
    if "pw" in lay and "ph" in lay and "ox" in lay and "oy" in lay:
        pw, ph = int(lay["pw"]), int(lay["ph"])
        ox, oy = int(lay["ox"]), int(lay["oy"])
    else:
        sw, sh = int(lay.get("sw") or 0) or cw, int(lay.get("sh") or 0) or ch
        if lay.get("fill") == "crop" or abs(sw / sh - cw / ch) < 0.01:
            pw, ph = cw, ch
            ox, oy = 0, 0
        else:
            scale = min(cw / sw, ch / sh) if sw and sh else 1.0
            pw, ph = min(cw, _even(sw * scale)), min(ch, _even(sh * scale))
            ox, oy = _even((cw - pw) / 2.0) if cw > pw else 0, _even((ch - ph) / 2.0) if ch > ph else 0

    text_width = int(pw * 0.78)  # Scaled to actual picture width
    boxes = [x for x in (overlays.get("blur_boxes") or []) if isinstance(x, dict)]
    title_text = str(overlays.get("title_text") or "").strip()
    logo_enabled = bool(overlays.get("logo_enabled", True))
    logo_path = str(overlays.get("logo_path") or "").strip() if logo_enabled else ""
    logo_file = Path(logo_path) if logo_path else None
    extra_text = str(overlays.get("extra_text") or "").strip()
    subtitles_file = str(overlays.get("subtitles_file") or "").strip()
    subtitles_path = Path(subtitles_file) if subtitles_file else None
    watermark_mode = bool(overlays.get("watermark"))
    if not boxes and not title_text and not extra_text and not watermark_mode and not (logo_file and logo_file.is_file()) and not (subtitles_path and subtitles_path.is_file()):
        return input_video

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_video)]
    input_index = 1
    title_file: Path | None = None
    title_idx: int | None = None
    logo_idx: int | None = None
    extra_file: Path | None = None
    extra_idx: int | None = None
    if title_text:
        title_file = _render_title_png(
            title_text, work_dir / "title_overlay.png",
            str(overlays.get("title_font_path") or ""), int(overlays.get("title_font_size", 54)),
            max_width=text_width,
        )
        title_idx = input_index
        cmd += ["-i", str(title_file)]
        input_index += 1
    if logo_file and logo_file.is_file():
        logo_idx = input_index
        cmd += ["-i", str(logo_file)]
        input_index += 1
    if extra_text:
        extra_file = _render_extra_text_png(
            extra_text,
            work_dir / "extra_text_overlay.png",
            int(overlays.get("extra_text_font_size", 42)),
            0.18,
            max_width=text_width,
        )
        extra_idx = input_index
        cmd += ["-i", str(extra_file)]
        input_index += 1
    # Free-tier brand watermark v3 (2026-10-30): when the pipeline flags
    # overlays["watermark"], the extra-text panel below is REPLACED by a
    # single mark — the full-frame diagonal tiled pattern of the bot
    # username (crop-resistant; see render_brand_pattern_png). The v2
    # corner lockup is gone entirely (owner: it looked cheap and collided
    # with a pattern tile). The pattern is registered as an input here but
    # composited LAST in the graph (after subtitles), and this whole pass
    # runs after the mirror/zoom/ratio part renders, so the mark is never
    # mirrored.
    pattern_idx: int | None = None
    if watermark_mode:
        pattern_opacity = float(overlays.get("watermark_pattern_opacity", BRAND_PATTERN_OPACITY))
        pattern_file = render_brand_pattern_png(
            work_dir / "brand_pattern.png",
            frame_width=pw,
            frame_height=ph,
            opacity=pattern_opacity,
        )
        pattern_idx = input_index
        cmd += ["-i", str(pattern_file)]
        input_index += 1

    graph: list[str] = []
    current = "[0:v]"
    blur_strength = max(4, min(40, int(overlays.get("blur_strength", 18))))
    for i, b in enumerate(boxes):
        # Coordinates are fractions of the FULL output canvas (cw×ch) — the UI blur box is
        # absolutely positioned against the whole Live Canvas frame, not the inner picture.
        w = int(round(_clamp01(b.get("w"), 0.90) * cw))
        h = int(round(_clamp01(b.get("h"), 0.07) * ch))
        w = max(8, min(w, cw - 8))
        h = max(8, min(h, ch - 8))
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        # b.get("x") is left fraction (0.05 default); b.get("y") is center Y fraction (0.85 default)
        x_val = _clamp01(b.get("x"), 0.05) * cw
        y_center = _clamp01(b.get("y"), 0.85) * ch
        y_top = y_center - (h / 2.0)

        x = int(round(max(0, min(cw - w, x_val))))
        y = int(round(max(0, min(ch - h, y_top))))
        x = x if x % 2 == 0 else x - 1
        y = y if y % 2 == 0 else y - 1

        # Prevent boxblur crashes by strictly clamping luma radius < min(w, h) / 2
        safe_luma = max(2, min(blur_strength, (min(w, h) // 2) - 1))
        safe_chroma = max(1, min(safe_luma // 2, 9))
        base = f"bbase{i}"; crop = f"bcrop{i}"; blur = f"bblur{i}"; out = f"bv{i}"
        graph.append(f"{current}split=2[{base}][{crop}]")
        graph.append(
            f"[{crop}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={safe_luma}:luma_power=2:"
            f"chroma_radius={safe_chroma}:chroma_power=1[{blur}]"
        )
        graph.append(f"[{base}][{blur}]overlay={x}:{y}[{out}]")
        current = f"[{out}]"

    if title_idx is not None and title_file is not None:
        pos = overlays.get("title_position") or [0.5, 0.12]
        tx = _clamp01(pos[0] if len(pos) > 0 else 0.5, 0.5)
        ty = _clamp01(pos[1] if len(pos) > 1 else 0.12, 0.12)
        out = "vtitle"
        graph.append(f"{current}[{title_idx}:v]overlay=x='{ox}+({pw}*{tx:.6f}-w/2)':y='{oy}+({ph}*{ty:.6f}-h/2)':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if logo_idx is not None:
        pos = overlays.get("logo_position") or [0.80, 0.06]
        lx = _clamp01(pos[0] if len(pos) > 0 else 0.80, 0.80)
        ly = _clamp01(pos[1] if len(pos) > 1 else 0.06, 0.06)
        lw = max(0.04, min(0.40, float(overlays.get("logo_width_fraction", 0.16))))
        lo = max(0.10, min(1.00, float(overlays.get("logo_opacity", 0.65))))
        graph.append(f"[{logo_idx}:v]scale={max(24, int(round(pw * lw)))}:-1[lg0]")
        if lo < 0.999:
            graph.append(f"[lg0]format=rgba,colorchannelmixer=aa={lo:.3f}[lg]")
        else:
            graph.append(f"[lg0]copy[lg]")
        out = "vlogo"
        graph.append(f"{current}[lg]overlay=x='{ox}+({pw}*{lx:.6f})':y='{oy}+({ph}*{ly:.6f})':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if extra_idx is not None and extra_file is not None and not watermark_mode:
        pos = overlays.get("extra_text_position") or [0.50, 0.68]
        ex = _clamp01(pos[0] if len(pos) > 0 else 0.50, 0.50)
        ey = _clamp01(pos[1] if len(pos) > 1 else 0.68, 0.68)
        eo = max(0.10, min(1.00, float(overlays.get("extra_text_opacity", 0.65))))
        if eo < 0.999:
            graph.append(f"[{extra_idx}:v]format=rgba,colorchannelmixer=aa={eo:.3f}[etxt]")
        else:
            graph.append(f"[{extra_idx}:v]copy[etxt]")
        out = "vextra"
        graph.append(f"{current}[etxt]overlay=x='{ox}+({pw}*{ex:.6f}-w/2)':y='{oy}+({ph}*{ey:.6f}-h/2)':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if subtitles_path and subtitles_path.is_file():
        sub_esc = subtitles_path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
        fonts_dir = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
        fonts_arg = ""
        ass_fonts_dir = work_dir / "ass_fonts"
        try:
            staged_dir = stage_bundled_fonts(ass_fonts_dir)
            if staged_dir is None:
                raise RuntimeError("no bundled fonts staged")
            fonts_dir_esc = staged_dir.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
            fonts_arg = f":fontsdir='{fonts_dir_esc}'"
        except Exception:
            if fonts_dir.is_dir():
                fonts_dir_esc = fonts_dir.as_posix().replace(":", r"\:").replace("'", r"\'")
                fonts_arg = f":fontsdir='{fonts_dir_esc}'"

        out = "vsub"
        if subtitles_path.suffix.lower() == ".ass":
            # For ASS subtitles, libass strictly follows the styles, resolution (PlayResX/Y),
            # margins, and fonts embedded inside the ASS header.
            graph.append(f"{current}subtitles='{sub_esc}'{fonts_arg}[{out}]")
        else:
            # Bind the chosen bundled Myanmar font family for accurate complex script text shaping
            _, sub_font = resolve_subtitle_font(
                str(overlays.get("subtitles_font") or ensure_system_myanmar_fonts())
            )
            sub_lines = int(overlays.get("sub_lines", 1))
            raw_size = int(overlays.get("subtitles_font_size", 16))
            # WrapStyle=2 is NO WRAPPING (strictly keeps 1 line). WrapStyle=0 is smart multi-line wrapping.
            wrap_style = 2 if sub_lines == 1 else 0
            if ch > cw:  # 9:16 Vertical
                sub_size = max(9, min(22, int(round(raw_size * 0.72)))) if sub_lines == 1 else max(10, min(24, raw_size))
            else:
                sub_size = max(11, min(26, raw_size))
            # Canvas-normalized subtitle positioning matching Live Canvas Preview (sy=0.85 means 15% from bottom)
            text_half_h = (sub_size * (1.15 if sub_lines == 1 else 2.3)) / 2.0
            if "sub_y" in overlays and overlays["sub_y"] is not None:
                sy = _clamp01(float(overlays["sub_y"]), 0.85)
                pixels_from_bottom = max(10.0, (ch * (1.0 - sy)) - text_half_h)
            elif "subtitles_margin_v" in overlays:
                mv_val = float(overlays["subtitles_margin_v"])
                pixels_from_bottom = mv_val if mv_val > 100 else (mv_val / 288.0) * ch
            elif boxes:
                b0 = boxes[0]
                by = _clamp01(b0.get("y", 0.85))
                pixels_from_bottom = max(10.0, (ch * (1.0 - by)) - text_half_h)
            else:
                pixels_from_bottom = max(14, int(round(ch * 0.12)))

            # FFmpeg's subtitles filter (libass on SRT) uses a 288 reference scale for MarginV
            sub_margin = max(4, min(280, int(round((pixels_from_bottom / max(1, ch)) * 288))))
            st_key = str(overlays.get("sub_style") or overlays.get("subtitles_style") or "yellow").lower()
            st = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
            p_col = st["primary"]
            o_col = st["outline_col"]
            b_col = st["back_col"]
            b_style = st["border_style"]
            outl = st["outline"]
            shad = st["shadow"]
            graph.append(
                f"{current}subtitles='{sub_esc}'{fonts_arg}:force_style='Fontname={sub_font},FontSize={sub_size},"
                f"PrimaryColour={p_col},OutlineColour={o_col},BackColour={b_col},BorderStyle={b_style},Outline={outl},Shadow={shad},MarginV={sub_margin},MarginL=20,MarginR=20,Alignment=2,WrapStyle={wrap_style}'[{out}]"
            )
        current = f"[{out}]"

    if pattern_idx is not None:
        # Diagonal tiled pattern over the WHOLE picture area — centre and
        # edges alike, so no crop can remove it. It is composited over the
        # burned subtitles at ~0.19 opacity: visible as a brand texture,
        # never enough to hurt subtitle readability.
        graph.append(f"{current}[{pattern_idx}:v]overlay=x='{ox}':y='{oy}':eof_action=repeat[vpat]")
        current = "[vpat]"

    graph.append(f"{current}format=yuv420p[vout]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map_metadata", "-1", "-an"] + encoder_args_for_quality(encoder, render_quality) + ["-movflags", "+faststart", str(output_video)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 and encoder == "h264_nvenc":
        log("⚠️ Overlay NVENC failed — libx264 fallback")
        cmd2 = cmd[:]
        # Rebuild tail safely rather than trying to mutate codec options in-place.
        map_pos = cmd2.index("-map")
        prefix = cmd2[:map_pos] + ["-map", "[vout]", "-map_metadata", "-1", "-an"]
        cmd2 = prefix + encoder_args_for_quality("libx264", render_quality) + ["-movflags", "+faststart", str(output_video)]
        p = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not output_video.is_file() or output_video.stat().st_size < 5000:
        raise RuntimeError("Production overlay render failed: " + p.stderr[-3000:])
    return output_video


def extract_preview_with_overlays(
    ffmpeg: str,
    source_video: Path | str,
    output_image: Path | str,
    timestamp: float = 3.0,
    overlays: dict[str, Any] | None = None,
    output_ratio: str = "Original",
    frame_fill: str = "blur",
    mirror_video: bool = True,
    zoom_factor: float = 1.06,
    sample_text: str = "ဒီဇာတ်လမ်းမှာတော့ အဓိကဇာတ်ကောင်ဟာ မမျှော်လင့်ဘဲ...",
    work_dir: Path | str | None = None,
    ffprobe: str | None = None,
    crop_top: float = 0.0,
    crop_bottom: float = 0.0,
    crop_left: float = 0.0,
    crop_right: float = 0.0,
) -> Path:
    """Extracts a single representative frame with layout, blur box, and sample 1-line subtitle applied."""
    source = Path(source_video).resolve()
    out_img = Path(output_image).resolve()
    out_img.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(work_dir).resolve() if work_dir else out_img.parent
    temp_dir.mkdir(parents=True, exist_ok=True)

    ffprobe_bin = resolve_binary(ffprobe, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"])
    try:
        sw, sh = probe_video_size(ffprobe_bin, source)
    except Exception:
        sw, sh = 1920, 1080

    try:
        vid_dur = probe_duration(ffprobe_bin, source)
    except Exception:
        vid_dur = 10.0

    if vid_dur > 1.2:
        seek_sec = min(max(0.3, float(timestamp)), max(0.3, vid_dur - 0.4))
    else:
        seek_sec = max(0.05, vid_dur * 0.3)

    layout = frame_layout(
        output_ratio,
        frame_fill,
        sw,
        sh,
        crop_top=crop_top,
        crop_bottom=crop_bottom,
        crop_left=crop_left,
        crop_right=crop_right,
    )
    cw, ch = int(layout["cw"]), int(layout["ch"])
    pw, ph = int(layout.get("pw", cw)), int(layout.get("ph", ch))
    ox, oy = int(layout.get("ox", 0)), int(layout.get("oy", 0))

    vf, _ = _visual_filters(mirror_video, zoom_factor, layout)
    overlays = dict(overlays or {})
    boxes = [x for x in (overlays.get("blur_boxes") or []) if isinstance(x, dict)]

    graph: list[str] = [f"[0:v]{vf}[base]"]
    current = "[base]"
    blur_strength = max(4, min(40, int(overlays.get("blur_strength", 18))))

    for i, b in enumerate(boxes):
        # Precise coordinates mapped directly onto the output canvas frame (WYSIWYG with Live Canvas Preview)
        w = int(round(_clamp01(b.get("w"), 0.90) * cw))
        h = int(round(_clamp01(b.get("h"), 0.07) * ch))
        w = max(8, min(w, cw - 8))
        h = max(8, min(h, ch - 8))
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        # b.get("x") is left fraction (0.05 default); b.get("y") is center Y fraction (0.85 default)
        x_val = _clamp01(b.get("x"), 0.05) * cw
        y_center = _clamp01(b.get("y"), 0.85) * ch
        y_top = y_center - (h / 2.0)

        x = int(round(max(0, min(cw - w, x_val))))
        y = int(round(max(0, min(ch - h, y_top))))
        x = x if x % 2 == 0 else x - 1
        y = y if y % 2 == 0 else y - 1

        safe_luma = max(2, min(blur_strength, (min(w, h) // 2) - 1))
        safe_chroma = max(1, min(safe_luma // 2, 9))

        base = f"bbase{i}"; crop = f"bcrop{i}"; blur = f"bblur{i}"; out = f"bv{i}"
        graph.append(f"{current}split=2[{base}][{crop}]")
        graph.append(
            f"[{crop}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={safe_luma}:luma_power=2:"
            f"chroma_radius={safe_chroma}:chroma_power=1[{blur}]"
        )
        graph.append(f"[{base}][{blur}]overlay={x}:{y}[{out}]")
        current = f"[{out}]"

    # Add Logo overlay if enabled
    logo_file = None
    if overlays.get("logo_enabled", False):
        lp = str(overlays.get("logo_path") or "").strip()
        if lp and Path(lp).is_file():
            logo_file = Path(lp)

    logo_idx: int | None = None
    cmd_inputs: list[str] = ["-ss", f"{seek_sec:.2f}", "-i", str(source)]
    if logo_file:
        logo_idx = 1
        cmd_inputs += ["-i", str(logo_file)]

    if logo_idx is not None:
        pos = overlays.get("logo_position") or [0.80, 0.05]
        px = _clamp01(pos[0]) * pw
        py = _clamp01(pos[1]) * ph
        lw = max(0.04, min(0.40, float(overlays.get("logo_width_fraction", 0.16))))
        lo = max(0.10, min(1.00, float(overlays.get("logo_opacity", 0.70))))
        graph.append(f"[{logo_idx}:v]scale={max(24, int(round(pw * lw)))}:-1[lg0]")
        if lo < 0.98:
            graph.append(f"[lg0]format=rgba,colorchannelmixer=aa={lo:.2f}[lg]")
            lg_in = "[lg]"
        else:
            lg_in = "[lg0]"
        out = "vlogo"
        graph.append(f"{current}{lg_in}overlay={int(round(ox + px))}:{int(round(oy + py))}[{out}]")
        current = f"[{out}]"

    # Add sample subtitle if burn_subtitles is enabled
    srt_temp = None
    if overlays.get("burn_subtitles", True):
        sub_lines = int(overlays.get("sub_lines", 1))
        sample = str(sample_text or "").strip()
        if not sample:
            if sub_lines == 2:
                sample = "မူရင်းဇာတ်လမ်းကို မြန်မာဘာသာဖြင့်\nအချိန်ကိုက် ပြန်လည်တင်ဆက်ထားပါသည်"
            else:
                sample = "မြန်မာဘာသာဖြင့် ပြန်လည်တင်ဆက်ထားပါသည်"
        elif sub_lines == 2 and "\n" not in sample and len(sample) > 20:
            syls = split_into_burmese_syllables(sample)
            if len(syls) > 1:
                half = len(sample) // 2
                best_idx = 1
                best_diff = 9999
                cand = []
                for i in range(1, len(syls)):
                    left_len = sum(len(s) for s in syls[:i])
                    diff = abs(left_len - half)
                    curr = syls[i - 1].strip()
                    if curr in PARTICLE_SYLS or any(curr.endswith(p) for p in ['ပြီး', 'တော့', 'က', 'နဲ့', 'မှာ', 'ကို', 'တယ်', 'တာ', '၊', ' ']):
                        cand.append((diff, i))
                if cand:
                    cand.sort(key=lambda x: x[0])
                    best_idx = cand[0][1]
                else:
                    for i in range(1, len(syls)):
                        left_len = sum(len(s) for s in syls[:i])
                        diff = abs(left_len - half)
                        if diff < best_diff:
                            best_diff = diff
                            best_idx = i
                l1 = "".join(syls[:best_idx]).strip()
                l2 = "".join(syls[best_idx:]).strip()
                if l1 and l2:
                    sample = f"{l1}\n{l2}"
        elif sub_lines == 1:
            sample = sample.replace("\n", " ").strip()

        ass_temp = temp_dir / "preview_sample.ass"
        _, sub_font = resolve_subtitle_font(
            str(overlays.get("subtitles_font") or ensure_system_myanmar_fonts())
        )
        write_synced_ass_file(
            cues=[(0.0, 10.0, sample)],
            ass_path=ass_temp,
            cw=cw,
            ch=ch,
            font_name=sub_font,
            raw_font_size=int(overlays.get("subtitles_font_size", 16)),
            sub_style=str(overlays.get("sub_style") or overlays.get("subtitles_style") or "yellow"),
            sub_lines=sub_lines,
            sub_y=overlays.get("sub_y"),
            subtitles_margin_v=overlays.get("subtitles_margin_v"),
            boxes=boxes,
        )
        sub_esc = ass_temp.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
        fonts_dir = bundled_fonts_dir()
        safe_fonts_dir = temp_dir / "fonts"
        staged = stage_bundled_fonts(safe_fonts_dir)
        active_fonts_dir = staged if staged is not None else fonts_dir
        fonts_arg = ""
        if active_fonts_dir.is_dir():
            fonts_dir_esc = active_fonts_dir.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
            fonts_arg = f":fontsdir='{fonts_dir_esc}'"

        out = "vsub"
        graph.append(f"{current}subtitles='{sub_esc}'{fonts_arg}[{out}]")
        current = f"[{out}]"

    graph.append(f"{current}format=yuv420p[vout]")

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
    ] + cmd_inputs + [
        "-filter_complex", ";".join(graph),
        "-map", "[vout]",
        "-frames:v", "1",
        "-q:v", "2",
        "-strict", "-2",
        "-update", "1",
        str(out_img),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if ass_temp and ass_temp.is_file():
        ass_temp.unlink(missing_ok=True)
    if p.returncode != 0 or not out_img.is_file():
        raise RuntimeError("Preview extraction failed: " + p.stderr[-1500:])
    return out_img


@dataclass
class PipelinePaths:
    root: Path
    tts_parts: Path
    video_parts: Path
    work: Path
    checkpoint: Path
    voice: Path
    synced_video_noaudio: Path
    decorated_video_noaudio: Path
    plan: Path
    final: Path


def make_paths(job_dir: Path) -> PipelinePaths:
    root = job_dir / "edge_tts_smart_sync"
    tts = root / "tts_parts"
    video = root / "video_parts"
    work = root / "work"
    for p in (root, tts, video, work):
        p.mkdir(parents=True, exist_ok=True)
    return PipelinePaths(
        root=root,
        tts_parts=tts,
        video_parts=video,
        work=work,
        checkpoint=root / "checkpoint.json",
        voice=root / "voice_edge_tts.wav",
        synced_video_noaudio=work / "video_synced_noaudio.mp4",
        decorated_video_noaudio=work / "video_synced_decorated_noaudio.mp4",
        plan=root / "smart_sync_plan_edge_tts.json",
        final=root / "final_edge_tts_smart_sync.mp4",
    )


class EdgeSmartSync:
    def __init__(
        self,
        *,
        job_dir: str = "",
        source_path: str | None = None,
        plan_path: str | None = None,
        voice: str,
        edge_tts_rate: str = "+0%",
        edge_tts_pitch: str = "+0Hz",
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        max_video_speed: float = 1.25,
        max_video_slow: float = 1.35,
        mirror_video: bool = True,
        zoom_factor: float = 1.05,
        smooth_freeze_fallback: bool = False,
        max_freeze_hold: float = 0.75,
        render_final_video: bool = True,
        output_ratio: str = "16:9",
        frame_fill: str = "blur",
        crop_top: float = 0.0,
        crop_bottom: float = 0.0,
        crop_left: float = 0.0,
        crop_right: float = 0.0,
        overlays: dict[str, Any] | None = None,
        log: LogFn = _noop,
        progress: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.source_path = Path(source_path).resolve() if source_path else (self.job_dir / "source.mp4")
        self.plan_path = Path(plan_path).resolve() if plan_path else (self.job_dir / "smart_sync_plan.json")
        self.voice = voice
        self.edge_tts_rate = str(edge_tts_rate or "+0%")
        self.edge_tts_pitch = str(edge_tts_pitch or "+0Hz")
        self.ffmpeg = resolve_binary(ffmpeg_path, "ffmpeg", [r"C:\ffmpeg\bin\ffmpeg.exe"])
        self.ffprobe = resolve_binary(ffprobe_path, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"])
        # Kept for backward config compatibility; exact Recap-style sync does
        # not clamp segment speed/slow when Auto Freeze is OFF.
        self.max_video_speed = max(1.0, float(max_video_speed))
        self.max_video_slow = max(1.0, float(max_video_slow))
        self.mirror_video = bool(mirror_video)
        self.zoom_factor = max(1.0, min(float(zoom_factor), 1.25))
        self.output_ratio = output_ratio if output_ratio in OUTPUT_RATIOS else "16:9"
        self.frame_fill = frame_fill if frame_fill in FRAME_FILLS else "blur"
        self.crop_top = float(crop_top or 0.0)
        self.crop_bottom = float(crop_bottom or 0.0)
        self.crop_left = float(crop_left or 0.0)
        self.crop_right = float(crop_right or 0.0)
        self.smooth_freeze_fallback = bool(smooth_freeze_fallback)
        self.max_freeze_hold = max(0.15, min(float(max_freeze_hold), 1.50))
        self.render_final_video = bool(render_final_video)
        # Short pauses: see trim_edge_silence. Trims excessive TTS dead air while keeping natural breath.
        self.short_pauses = True
        self.overlays = dict(overlays or {})
        # Render quality preset (overlays key set by recap_pipeline);
        # "standard" == the historical encode, so old callers are
        # unaffected. Included in the video-part checkpoint fingerprint
        # below so changing quality re-renders instead of reusing parts
        # encoded at another quality.
        self.render_quality = normalize_render_quality(self.overlays.get("render_quality"))
        self.log = log
        self.progress = progress or (lambda a, b, c: None)
        self.should_stop = should_stop or (lambda: False)

    def _checkpoint_load(self, paths: PipelinePaths) -> dict[str, Any]:
        if paths.checkpoint.is_file():
            try:
                data = load_json(paths.checkpoint)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"version": 1, "audio": {}, "video": {}}

    def _log_sync_check(self, paths: "PipelinePaths", segments: list[dict[str, Any]], checkpoint: dict[str, Any]) -> None:
        """Automatic, language-independent sync report written to the run log.

        Narration is the clock; every video part is frame-locked to it. So if the
        concatenated picture and the narration track have the same length, every
        segment starts on its own narration line. Speed-capped/trimmed segments are
        listed only for information: trimming shortens footage, not the timeline.
        """
        try:
            video_len = probe_duration(self.ffprobe, paths.synced_video_noaudio)
            audio_len = probe_duration(self.ffprobe, paths.voice)
            diff = video_len - audio_len
            trimmed = 0
            for seg in segments:
                sync = ((checkpoint.get("video") or {}).get(segment_basename(seg)) or {}).get("sync") or {}
                if float(sync.get("applied_factor", 0.0) or 0.0) > float(sync.get("desired_factor", 0.0) or 0.0) + 1e-6:
                    trimmed += 1
            status = "✅ SYNC OK" if abs(diff) <= 0.1 else "⚠️ SYNC DRIFT"
            self.log(
                f"{status}: picture={video_len:.2f}s · narration={audio_len:.2f}s · diff={diff:+.3f}s "
                f"| {len(segments)} segments, {trimmed} speed-capped ({self.max_video_speed:g}x) + footage trimmed"
            )
        except Exception as exc:
            self.log(f"⚠️ Sync check skipped: {exc}")

    def _vox_synthesize_with_retry(
        self,
        vox_engine: Any,
        *,
        speak: str,
        dst: Path,
        ctrl_prompt: str,
        persona_ref_wav: Any,
        persona_ref_text: str,
        preset_v: Any,
        speed_float: float,
        attempts: int = 2,
    ) -> tuple[Any, Exception | None]:
        """Per-segment VoxCPM attempt with retries (fix 2026-10-29).

        The engine itself fails over across spaces/tokens; this adds one
        segment-level retry on top. Returns (produced_path, None) on
        success or (None, last_error) when the segment — and ONLY this
        segment — should fall back to the Studio Persona engine.
        """
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                produced = vox_engine.synthesize(
                    text=speak,
                    output_wav=dst,
                    control_instruction=ctrl_prompt,
                    reference_wav=persona_ref_wav,
                    prompt_text=persona_ref_text,
                    preset_voice=preset_v,
                    speed=speed_float,
                    timeout=75,
                    retries=2,
                    allow_fallback=False,
                )
                return produced, None
            except Exception as exc:
                last_err = exc
                self.log(f"⚠️ VoxCPM 2 segment attempt {attempt + 1}/{attempts} failed ({exc})")
        return None, last_err

    def run(self) -> dict[str, Any]:
        source = self.source_path
        plan_file = self.plan_path
        if not source.is_file():
            raise FileNotFoundError(f"Source video မတွေ့ပါ: {source}")
        if not plan_file.is_file():
            raise FileNotFoundError(f"smart_sync_plan.json မတွေ့ပါ: {plan_file}")

        plan = load_json(plan_file)
        segments = normalize_segments(plan)
        paths = make_paths(self.job_dir)
        checkpoint = self._checkpoint_load(paths)
        checkpoint.setdefault("audio", {})
        checkpoint.setdefault("video", {})
        checkpoint["engine"] = "edge_tts"
        checkpoint["voice"] = self.voice

        self.log(f"📁 Job: {self.job_dir}")
        self.log(f"🧩 Timestamp segments: {len(segments)}")
        self.log(f"🎙️ TTS: Edge TTS | Voice: {self.voice}")
        self.log(f"🎞️ Smart Sync: Exact Recap timing — distributed freeze fallback={'ON' if self.smooth_freeze_fallback else 'OFF'}")
        try:
            source_w, source_h = probe_video_size(self.ffprobe, source)
        except Exception as exc:  # only the frame shape depends on it: Original falls back to 16:9
            source_w, source_h = 0, 0
            self.log(f"⚠️ Video size not readable ({exc}); Original ratio falls back to 16:9")
        layout = frame_layout(
            self.output_ratio,
            self.frame_fill,
            source_w,
            source_h,
            crop_top=self.crop_top,
            crop_bottom=self.crop_bottom,
            crop_left=self.crop_left,
            crop_right=self.crop_right,
        )
        crop_msg = (
            f" | Crop={self.crop_top*100:.0f}%T/{self.crop_bottom*100:.0f}%B/{self.crop_left*100:.0f}%L/{self.crop_right*100:.0f}%R"
            if (self.crop_top > 0 or self.crop_bottom > 0 or self.crop_left > 0 or self.crop_right > 0)
            else ""
        )
        self.log(f"🪞 Mirror={'ON' if self.mirror_video else 'OFF'} | Ratio={layout['ratio']} "
                 f"{layout['cw']}x{layout['ch']} ({layout['fill']}){crop_msg} from {source_w}x{source_h} | Zoom={self.zoom_factor:.3f}x")

        # Method 2: Optimize default Edge TTS speed (+12% to eliminate dragging robotic tone)
        effective_rate = self.edge_tts_rate
        if not effective_rate or effective_rate in ("+0%", "0%", "+0Hz"):
            effective_rate = "+12%"
        effective_pitch = self.edge_tts_pitch or "+0Hz"

        engine = EdgeTTSEngine(
            ffmpeg=self.ffmpeg,
            work_dir=paths.work / "edge_tts_temp",
            rate=effective_rate,
            pitch=effective_pitch,
            log=self.log,
        )

        # Method 1: Check if VoxCPM Cloud Engine is requested
        use_voxcpm = (
            str(self.voice).lower().startswith(("voxcpm", "vox_"))
            or str(self.overlays.get("tts_engine") or "").lower() == "voxcpm"
        )
        vox_engine = None
        if use_voxcpm:
            try:
                try:
                    from app.services.recap_engine.voxcpm_engine import VoxCPMEngine
                except ImportError:
                    try:
                        from recap_engine.voxcpm_engine import VoxCPMEngine
                    except ImportError:
                        from voxcpm_engine import VoxCPMEngine
                vox_engine = VoxCPMEngine(work_dir=paths.work / "voxcpm_temp", log_fn=self.log)
                self.log(f"🎙️ Initialized VoxCPM 2 Cloud Neural Voice Engine ({self.voice})")
            except Exception as vox_init_err:
                self.log(f"⚠️ VoxCPM 2 init error ({vox_init_err}) — will use Edge TTS")
                use_voxcpm = False

        # Gemini TTS option (production-grade programme): an additional
        # engine, NOT a replacement. The persona path below stays as the
        # per-segment fallback, reported via the same fallback_used /
        # fallback_segments transparency fields as VoxCPM fallbacks.
        use_gemini = str(self.overlays.get("tts_engine") or "").lower() == "gemini"
        gem_engine = None
        gemini_voice_name = ""
        if use_gemini:
            try:
                try:
                    from app.services.recap_engine.gemini_tts_engine import (
                        GeminiTTSEngine,
                        collect_gemini_keys,
                        default_voice_for_gender,
                    )
                except ImportError:
                    try:
                        from recap_engine.gemini_tts_engine import (  # type: ignore
                            GeminiTTSEngine,
                            collect_gemini_keys,
                            default_voice_for_gender,
                        )
                    except ImportError:
                        from gemini_tts_engine import (  # type: ignore
                            GeminiTTSEngine,
                            collect_gemini_keys,
                            default_voice_for_gender,
                        )
                gemini_voice_name = str(self.overlays.get("gemini_voice") or "").strip() or default_voice_for_gender(
                    bool(self.overlays.get("is_female"))
                )
                _gem_keys = collect_gemini_keys()
                if _gem_keys:
                    gem_engine = GeminiTTSEngine(
                        ffmpeg=self.ffmpeg,
                        work_dir=paths.work / "gemini_tts",
                        rate=effective_rate,
                        keys=_gem_keys,
                        log=self.log,
                    )
                    self.log(f"✨ Gemini TTS engine ready (voice={gemini_voice_name}); persona engine stays as per-segment fallback")
                else:
                    self.log("⚠️ Gemini TTS selected but no Gemini keys are configured — every line will use the persona engine (reported as fallback)")
            except Exception as gem_init_err:
                self.log(f"⚠️ Gemini TTS init error ({gem_init_err}) — every line will use the persona engine (reported as fallback)")
                gem_engine = None

        base_edge_voice = self.voice
        if str(self.voice).startswith(("clone_", "clone:")):
            try:
                try:
                    from app.services.recap_engine.voice_clone_bridge import resolve_clone_model
                except ImportError:
                    try:
                        from recap_engine.voice_clone_bridge import resolve_clone_model
                    except ImportError:
                        from voice_clone_bridge import resolve_clone_model
                minfo = resolve_clone_model(self.voice)
                base_edge_voice = minfo.get("edge_base", "my-MM-ThihaNeural") if minfo else "my-MM-ThihaNeural"
            except Exception:
                base_edge_voice = "my-MM-ThihaNeural"
        elif str(self.voice).startswith("voxcpm"):
            base_edge_voice = "my-MM-NilarNeural" if (self.overlays.get("is_female") or "nilar" in str(self.voice).lower()) else "my-MM-ThihaNeural"

        short_pauses = bool(self.short_pauses)

        # AUDIO: timestamp segment = master save unit. The user's engine may split
        # sentences internally, but only one deterministic WAV is kept per timestamp segment.
        # Resolve preset persona ONCE before the segment loop (used by both VoxCPM and fallback)
        _voice_str = str(self.voice)
        if _voice_str.startswith("voxcpm_"):
            _global_preset_v: str | None = _voice_str.replace("voxcpm_", "")
        elif _voice_str in (
            "komin", "kozayar", "kothura", "koaung", "mahnin",
            "manadi", "mamay", "komyat", "kophone", "mayoon",
            "machaw", "mathiri"
        ):
            _global_preset_v = _voice_str
        else:
            _global_preset_v = None

        # Convert Edge-TTS rate string (e.g. "+12%") → VoxCPM float speed (e.g. 1.12)
        def _rate_to_float(rate_str: str) -> float:
            import re as _re
            m = _re.search(r"([+-]?\d+)", str(rate_str or ""))
            if m:
                return round(1.0 + int(m.group(1)) / 100.0, 3)
            return 1.10

        voxcpm_speed_float = _rate_to_float(effective_rate)

        # Persona reference clip (the exact preview sample) — VoxCPM clones THIS voice for every
        # segment, so the recap sounds like the preview and never drifts between lines.
        persona_ref_wav: str | None = self.overlays.get("voxcpm_ref_audio") or None
        persona_ref_text: str = str(self.overlays.get("voxcpm_ref_text") or "")
        if not persona_ref_wav and _global_preset_v:
            try:
                from app.services.curated_voices import get_persona_reference
                persona_ref_wav, persona_ref_text = get_persona_reference(_global_preset_v)
            except Exception as ref_err:
                self.log(f"⚠️ Persona reference lookup failed ({ref_err}) — VoxCPM voice design only")
        if use_voxcpm:
            if persona_ref_wav and persona_ref_text:
                self.log(f"🎯 VoxCPM 2 locked-voice cloning from persona sample: {Path(persona_ref_wav).name}")
            else:
                self.log("⚠️ No persona sample transcript — VoxCPM voice design (voice may vary slightly)")

        timing: list[dict[str, Any]] = []
        audio_parts: list[Path] = []
        cursor = 0.0
        tts_total = 0.0
        for _tts_pass in range(2):
            engine_provider = "gemini" if use_gemini else ("voxcpm" if use_voxcpm else "edge_tts")
            if use_gemini:
                # Gemini lines must never reuse persona/VoxCPM cache
                # entries (or vice versa): the fingerprint names the
                # engine AND the Gemini voice.
                fingerprint_voice = (
                    f"gemini|{gemini_voice_name}|persona={_global_preset_v}|"
                    f"rate={effective_rate}"
                )
            else:
                fingerprint_voice = (
                    f"{engine_provider}|{base_edge_voice}|persona={_global_preset_v}|"
                    f"ref={Path(persona_ref_wav).name if persona_ref_wav else '-'}|"
                    f"rate={effective_rate}|pitch={effective_pitch}"
                )
            checkpoint["tts_provider"] = engine_provider
            timing = []
            audio_parts = []
            cursor = 0.0
            tts_total = 0.0
            restart_uniform = False
            fallback_segments: list[int] = []

            for idx, seg in enumerate(segments, 1):
                if self.should_stop():
                    raise InterruptedError("STOP requested — လက်ရှိပြီးထားတာတွေ checkpoint ထဲသိမ်းထားပါတယ်။")
                base = segment_basename(seg)
                dst = paths.tts_parts / f"{base}.wav"
                fp = segment_fingerprint(seg, fingerprint_voice)
                rec = checkpoint["audio"].get(base, {})
                reusable = rec.get("fingerprint") == fp and dst.is_file() and dst.stat().st_size > 1000
                if reusable:
                    duration = probe_duration(self.ffprobe, dst)
                    self.log(f"♻️ [{idx}/{len(segments)}] {base}.wav reuse ({duration:.2f}s)")
                else:
                    speak = spoken_text(seg["narration"])
                    produced = None
                    preset_v = _global_preset_v  # always defined for both VoxCPM and fallback paths

                    # 0. Gemini TTS if selected (production-grade option).
                    # Success: the WAV is copied onto dst here so the
                    # common tail below treats it like any other produced
                    # file (and never deletes the engine's own cache).
                    # Failure: ONLY this segment falls back to the persona
                    # path below and is recorded in fallback_segments —
                    # the same transparency rule as VoxCPM fallbacks.
                    seg_provider = "edge_tts"
                    if use_gemini:
                        if gem_engine is not None:
                            self.progress(idx - 1, len(segments), f"Gemini TTS {idx}/{len(segments)}")
                            self.log(f"✨ [{idx}/{len(segments)}] {base} generate via Gemini TTS ({gemini_voice_name})")
                            try:
                                _gem_wav = gem_engine.generate_channel99_voice(speak, gemini_voice_name)
                                shutil.copy2(_gem_wav, dst)
                                produced = dst
                                seg_provider = "gemini"
                            except Exception as gem_err:
                                self.log(
                                    f"⚠️ GEMINI_FALLBACK_SEGMENT: line {idx} fell back to Studio Persona "
                                    f"({gem_err}); other lines keep Gemini TTS where possible"
                                )
                                fallback_segments.append(idx)
                                seg_provider = "edge_fallback"
                        else:
                            fallback_segments.append(idx)
                            seg_provider = "edge_fallback"

                    # 1. Try VoxCPM 2 if selected — per-segment retry with
                    # space failover inside the engine (fix 2026-10-29).
                    # A single failing line must NOT switch the whole recap
                    # to Edge TTS: only THIS segment falls back, and only
                    # after its retries are exhausted. Other segments keep
                    # using VoxCPM.
                    # Skipped entirely when Gemini TTS is the selected
                    # engine: use_voxcpm is also true for persona voices
                    # (voice starts with "voxcpm"), and without this gate
                    # VoxCPM would overwrite the Gemini segment produced
                    # above and misreport the provider.
                    if use_voxcpm and vox_engine and not use_gemini:
                        self.progress(idx - 1, len(segments), f"VoxCPM 2 {idx}/{len(segments)}")
                        self.log(f"🎙️ [{idx}/{len(segments)}] {base} generate via VoxCPM 2 Cloud AI")
                        ctrl_prompt = (
                            self.overlays.get("voxcpm_control")
                            or "A clear, natural, emotionally expressive Burmese movie recap narrator with warm tone and fluent pronunciation"
                        )
                        produced, vox_err = self._vox_synthesize_with_retry(
                            vox_engine,
                            speak=speak,
                            dst=dst,
                            ctrl_prompt=ctrl_prompt,
                            persona_ref_wav=persona_ref_wav,
                            persona_ref_text=persona_ref_text,
                            preset_v=preset_v,
                            speed_float=voxcpm_speed_float,
                        )
                        if vox_err is not None:
                            self.log(
                                f"⚠️ EDGE_FALLBACK_SEGMENT: line {idx} fell back to Studio Persona (Edge TTS) "
                                f"after retries ({vox_err}); other lines keep VoxCPM 2"
                            )
                            fallback_segments.append(idx)
                            seg_provider = "edge_fallback"
                        else:
                            seg_provider = "voxcpm"

                    # 2. Studio Persona TTS (consistent fallback preserving persona timbre & pitch)
                    if not produced or not Path(produced).is_file():
                        curr_persona = preset_v or (str(self.voice).replace("voxcpm_", "") if str(self.voice).startswith("voxcpm_") else str(self.voice))
                        self.progress(idx - 1, len(segments), f"Studio TTS {idx}/{len(segments)}")
                        self.log(f"🗣️ [{idx}/{len(segments)}] {base} generate via Studio Persona ({curr_persona})")
                        try:
                            from app.services.curated_voices import synthesize_curated_persona, get_curated_voice
                            if get_curated_voice(curr_persona):
                                synthesize_curated_persona(
                                    text=speak,
                                    persona_id=curr_persona,
                                    output_wav=dst,
                                    speed=effective_rate or 1.10,
                                    log_fn=self.log,
                                )
                                produced = dst
                            else:
                                produced = engine.generate_channel99_voice(speak, base_edge_voice)
                        except Exception as persona_err:
                            self.log(f"⚠️ Persona TTS notice: {persona_err} — using standard engine")
                            try:
                                produced = engine.generate_channel99_voice(speak, base_edge_voice)
                            except Exception as first:
                                plain = without_english(speak)
                                if is_edge_refused(first) or plain == speak or not any(ch.isalpha() for ch in plain):
                                    raise
                                # A line mixing English and Burmese sometimes gets no audio at all
                                self.log(f"⚠️ [{idx}/{len(segments)}] no audio — trying again without the English words "
                                         "(Edit Text can rewrite the line)")
                                produced = engine.generate_channel99_voice(plain, base_edge_voice)
                            if not produced or not Path(produced).is_file():
                                raise RuntimeError("TTS output path မရပါ။")
                        except Exception as exc:
                            if is_edge_refused(exc):
                                raise RuntimeError(f"TTS stopped at line {idx}/{len(segments)}.\n\n{exc}") from exc
                            raise RuntimeError(f"TTS FAILED at segment {idx} ({base}): {exc}\n\n"
                                               f"Text: {seg['narration'][:200]}") from exc
                    produced_path = Path(produced).resolve()
                    if produced_path != dst.resolve():
                        shutil.copy2(produced_path, dst)
                        produced_path.unlink(missing_ok=True)
                    duration = probe_duration(self.ffprobe, dst)
                    if duration <= 0.05:
                        raise RuntimeError(f"Generated audio duration invalid: {dst}")
                    checkpoint["audio"][base] = {
                        "fingerprint": fp,
                        "provider": seg_provider if (use_voxcpm or use_gemini) else "edge_tts",
                        "fallback": bool((use_voxcpm or use_gemini) and seg_provider == "edge_fallback"),
                        "file": str(dst),
                        "duration": round(duration, 3),
                        "video_start": seg["video_start"],
                        "video_end": seg["video_end"],
                        "text": seg["narration"],
                    }
                    atomic_write_json(paths.checkpoint, checkpoint)
                    self.log(f"✅ {base}.wav = {duration:.2f}s")

                part = dst
                tts_total += duration
                if short_pauses:
                    window = float(seg["video_end"]) - float(seg["video_start"])
                    part, duration = trim_edge_silence(dst, paths.tts_parts.parent / "tts_parts_short" / dst.name,
                                                       min_duration=window / self.max_video_speed)
                start = cursor
                end = cursor + duration
                timing.append({
                    "segment_id": seg["segment_id"],
                    "source_index": seg["source_index"],
                    "video_start": seg["video_start"],
                    "video_end": seg["video_end"],
                    "audio_start": round(start, 3),
                    "audio_end": round(end, 3),
                    "audio_duration": round(duration, 3),
                    "text": seg["narration"],
                    "audio_file": str(part),
                })
                cursor = end
                audio_parts.append(part)
                self.progress(idx, len(segments), f"Audio {idx}/{len(segments)}")


            if not restart_uniform:
                break
        checkpoint["fallback_used"] = bool(fallback_segments)
        checkpoint["fallback_segments"] = list(fallback_segments)
        checkpoint["tts_provider"] = (
            "gemini" if use_gemini and not fallback_segments
            else "gemini+persona_fallback" if use_gemini
            else "voxcpm" if use_voxcpm and not fallback_segments
            else "voxcpm+edge_fallback" if use_voxcpm
            else "edge_tts"
        )
        atomic_write_json(paths.checkpoint, checkpoint)

        if short_pauses:
            self.log(f"✂️ Short pauses: Edge TTS silence trimmed · voice {tts_total / 60:.1f} → {cursor / 60:.1f} min "
                     f"(~0.25 s between lines; picture never cut for it)")
        self.log("🔗 Timestamp audio parts concat...")
        concat_audio(self.ffmpeg, audio_parts, paths.voice, paths.work)
        final_audio_duration = probe_duration(self.ffprobe, paths.voice)
        self.log(f"✅ {paths.voice.name} = {final_audio_duration:.2f}s")

        # Voice v2: legacy trained-model conversion removed; base audio is kept.
        if str(self.voice).startswith(("clone_", "clone:")) or self.overlays.get("clone_model"):
            clone_tag = str(self.overlays.get("clone_model") or self.voice)
            cloned_audio_path = paths.root / "recap_voice_cloned.wav"
            try:
                try:
                    from recap_engine.voice_clone_bridge import convert_voice_rvc
                except ImportError:
                    from voice_clone_bridge import convert_voice_rvc
                self.log(f"Legacy trained voice '{clone_tag}' is no longer supported; keeping base TTS audio.")
                cloned_res = convert_voice_rvc(
                    input_wav=paths.voice,
                    output_wav=cloned_audio_path,
                    model_tag=clone_tag,
                    pitch=int(self.overlays.get("clone_pitch", 0)),
                    f0_method=str(self.overlays.get("clone_f0_method", "rmvpe")),
                    log_fn=self.log,
                )
                if cloned_res.is_file() and cloned_res.stat().st_size > 1000:
                    paths.voice = cloned_res
                    final_audio_duration = probe_duration(self.ffprobe, paths.voice)
                    self.log(f"✅ Voice Clone Pro RVC success! Using cloned audio: {paths.voice.name}")
            except Exception as vc_err:
                self.log(f"⚠️ Voice Clone Pro conversion error (fallback to Edge TTS): {vc_err}")

        sync_segments = [
            {
                "video_start": item["video_start"],
                "video_end": item["video_end"],
                "audio_start": item["audio_start"],
                "audio_end": item["audio_end"],
            }
            for item in timing
        ]
        out_plan = dict(plan)
        out_plan["segments"] = [dict(seg) for seg in plan.get("segments", [])]
        out_plan["tts_provider"] = checkpoint.get("tts_provider", "edge_tts")
        out_plan["fallback_used"] = bool(fallback_segments)
        out_plan["fallback_segments"] = list(fallback_segments)
        out_plan["tts_voice"] = self.voice
        out_plan["actual_audio_duration"] = round(final_audio_duration, 3)
        out_plan["tts_timing_segments"] = timing
        out_plan["sync_segments"] = sync_segments
        out_plan["subtitle_segments"] = [
            {"start": item["audio_start"], "end": item["audio_end"], "text": item["text"]}
            for item in timing
        ]
        out_plan["production_overlays"] = self.overlays
        atomic_write_json(paths.plan, out_plan)

        # Generate synchronized Myanmar SRT & ASS subtitle files
        srt_file = paths.root / "recap_myanmar_synced.srt"
        ass_file = paths.root / "recap_myanmar_synced.ass"
        all_sub_cues: list[tuple[float, float, str]] = []
        try:
            srt_lines = []
            cue_counter = 1
            cw_val = int(layout.get("cw", 1920))
            ch_val = int(layout.get("ch", 1080))
            is_vert = (ch_val > cw_val) or (self.output_ratio == "9:16")
            lines_per_cue = int(self.overlays.get("sub_lines", 1))
            max_chars = (45 if is_vert else 52) if lines_per_cue == 1 else (80 if is_vert else 96)

            for s_item in timing:
                raw_st = float(s_item.get("audio_start", 0.0))
                raw_en = float(s_item.get("audio_end", 0.0))
                txt = str(s_item.get("text", "")).strip()
                if not txt:
                    continue
                sub_cues = split_burmese_subtitle_cues(
                    raw_st, raw_en, txt,
                    max_cue_chars=max_chars,
                    lines_per_cue=lines_per_cue
                )
                if not sub_cues:
                    sub_cues = [(raw_st, raw_en, txt)]
                for c_st, c_en, c_txt in sub_cues:
                    all_sub_cues.append((c_st, c_en, c_txt))
                    st_ms = max(0, int(round(c_st * 1000)))
                    en_ms = max(0, int(round(c_en * 1000)))
                    st_str = f"{st_ms // 3600000:02d}:{(st_ms % 3600000) // 60000:02d}:{(st_ms % 60000) // 1000:02d},{st_ms % 1000:03d}"
                    en_str = f"{en_ms // 3600000:02d}:{(en_ms % 3600000) // 60000:02d}:{(en_ms % 60000) // 1000:02d},{en_ms % 1000:03d}"
                    srt_lines.append(f"{cue_counter}\n{st_str} --> {en_str}\n{c_txt}\n")
                    cue_counter += 1
            srt_file.write_text("\n".join(srt_lines) + "\n", encoding="utf-8")
            self.log(f"📝 Synced Myanmar SRT saved: {srt_file.name} ({cue_counter - 1} cues)")

            # Generate pixel-perfect ASS subtitle with HarfBuzz complex script shaping
            active_font = str(self.overlays.get("subtitles_font") or ensure_system_myanmar_fonts())
            write_synced_ass_file(
                cues=all_sub_cues,
                ass_path=ass_file,
                cw=cw_val,
                ch=ch_val,
                font_name=active_font,
                raw_font_size=int(self.overlays.get("subtitles_font_size", 16)),
                sub_style=str(self.overlays.get("sub_style") or self.overlays.get("subtitles_style") or "yellow"),
                sub_lines=lines_per_cue,
                sub_y=self.overlays.get("sub_y"),
                subtitles_margin_v=self.overlays.get("subtitles_margin_v"),
                boxes=self.overlays.get("boxes"),
            )
            self.log(f"📝 Synced Myanmar ASS saved: {ass_file.name} (font={active_font})")
        except Exception as srt_err:
            self.log(f"⚠️ Failed to write synced subtitles: {srt_err}")

        if self.overlays.get("burn_subtitles"):
            self.overlays["subtitles_file"] = str(ass_file) if ass_file.is_file() else (str(srt_file) if srt_file.is_file() else None)

        if not self.render_final_video:
            self.log("✅ Audio + Smart Sync plan READY (final video render skipped).")
            return {
                "audio": str(paths.voice),
                "plan": str(paths.plan),
                "final": None,
                "segments": len(timing),
            }

        self.log("🎬 Smart Sync video parts render စမယ်...")
        encoders = ffmpeg_encoders(self.ffmpeg)
        preferred = "h264_nvenc" if "h264_nvenc" in encoders else "libx264"
        remembered = str(checkpoint.get("selected_encoder") or "")
        encoder = remembered if remembered in {"h264_nvenc", "libx264"} else preferred
        video_parts: list[Path] = []
        # Cumulative frame lock: each part gets round(audio_end*30) - frames_so_far
        # frames, so video never drifts more than half a frame from the narration.
        audio_cursor = 0.0
        frames_done = 0

        for idx, (seg, item) in enumerate(zip(segments, timing), 1):
            if self.should_stop():
                raise InterruptedError("STOP requested — video parts checkpoint ထဲကနေ Resume လုပ်နိုင်ပါတယ်။")
            base = segment_basename(seg)
            dst = paths.video_parts / f"{base}.mp4"
            target = float(item["audio_duration"])
            audio_cursor += target
            frame_count = max(1, int(round(audio_cursor * 30)) - frames_done)
            frames_done += frame_count
            vfp_raw = json.dumps({
                "frame_count": frame_count,
                "source": str(source.resolve()),
                "source_mtime": source.stat().st_mtime_ns,
                "start": seg["video_start"], "end": seg["video_end"],
                "target": target, "sync_mode": "hybrid_recap_distributed_freeze",
                "max_speed": self.max_video_speed, "max_slow": self.max_video_slow,
                "smooth_freeze": self.smooth_freeze_fallback, "max_freeze_hold": self.max_freeze_hold,
                "mirror": self.mirror_video, "zoom": self.zoom_factor, "encoder": encoder,
                "quality": self.render_quality,
                "frame": [layout["ratio"], layout["fill"], layout["cw"], layout["ch"]],
            }, sort_keys=True).encode()
            vfp = hashlib.sha256(vfp_raw).hexdigest()
            rec = checkpoint["video"].get(base, {})
            reusable = rec.get("fingerprint") == vfp and dst.is_file() and dst.stat().st_size > 5000
            if reusable:
                self.log(f"♻️ VIDEO [{idx}/{len(segments)}] {base}.mp4 reuse")
            else:
                self.progress(idx - 1, len(segments), f"Smart Sync video {idx}/{len(segments)}")
                self.log(f"🎞️ VIDEO [{idx}/{len(segments)}] {base} → {target:.2f}s")
                try:
                    metrics = render_video_part(
                        self.ffmpeg, source, dst, seg, target, encoder,
                        self.mirror_video, self.zoom_factor,
                        self.max_video_speed, self.max_video_slow,
                        self.smooth_freeze_fallback, self.max_freeze_hold,
                        frame_count=frame_count, layout=layout,
                        render_quality=self.render_quality,
                    )
                except Exception:
                    # Only on the first non-reused part, allow global NVENC -> CPU fallback.
                    if encoder == "h264_nvenc" and not video_parts:
                        self.log("⚠️ NVENC မရပါ — libx264 CPU encoder ကို fallback လုပ်မယ်။")
                        encoder = "libx264"
                        # Encoder changed, invalidate any video checkpoint rows to avoid mixed codecs.
                        checkpoint["video"] = {}
                        for old in paths.video_parts.glob("*.mp4"):
                            old.unlink(missing_ok=True)
                        metrics = render_video_part(
                            self.ffmpeg, source, dst, seg, target, encoder,
                            self.mirror_video, self.zoom_factor,
                            self.max_video_speed, self.max_video_slow,
                            self.smooth_freeze_fallback, self.max_freeze_hold,
                            frame_count=frame_count, layout=layout,
                            render_quality=self.render_quality,
                        )
                        # Recompute fingerprint with CPU encoder.
                        vfp_raw = json.dumps({
                            "frame_count": frame_count,
                            "source": str(source.resolve()),
                            "source_mtime": source.stat().st_mtime_ns,
                            "start": seg["video_start"], "end": seg["video_end"],
                            "target": target, "sync_mode": "hybrid_recap_distributed_freeze",
                            "max_speed": self.max_video_speed, "max_slow": self.max_video_slow,
                            "smooth_freeze": self.smooth_freeze_fallback, "max_freeze_hold": self.max_freeze_hold,
                            "mirror": self.mirror_video, "zoom": self.zoom_factor, "encoder": encoder,
                            "quality": self.render_quality,
                            "frame": [layout["ratio"], layout["fill"], layout["cw"], layout["ch"]],
                        }, sort_keys=True).encode()
                        vfp = hashlib.sha256(vfp_raw).hexdigest()
                    else:
                        raise
                if metrics.get("hybrid_used", 0.0) >= 0.5:
                    self.log(
                        f"   ↳ Hybrid: slow {metrics['applied_factor']:.3f}x + "
                        f"{int(metrics.get('freeze_count', 0))} short holds "
                        f"({metrics.get('freeze_duration', 0.0):.2f}s total)"
                    )
                checkpoint["video"][base] = {
                    "fingerprint": vfp,
                    "file": str(dst),
                    "target_duration": round(target, 3),
                    "encoder": encoder,
                    "sync": {k: round(v, 6) for k, v in metrics.items()},
                }
                checkpoint["selected_encoder"] = encoder
                atomic_write_json(paths.checkpoint, checkpoint)
            video_parts.append(dst)
            self.progress(idx, len(segments), f"Video {idx}/{len(segments)}")

        self.log("🔗 Smart Sync video parts concat...")
        concat_video_parts(self.ffmpeg, video_parts, paths.synced_video_noaudio, paths.work)
        self._log_sync_check(paths, segments, checkpoint)
        video_for_mux = paths.synced_video_noaudio
        if (
            self.overlays.get("blur_boxes")
            or str(self.overlays.get("title_text") or "").strip()
            or str(self.overlays.get("logo_path") or "").strip()
            or str(self.overlays.get("extra_text") or "").strip()
            or str(self.overlays.get("subtitles_file") or "").strip()
        ):
            self.log("🎨 Production overlay pass — Blur / Title PNG / Logo / Subtitles...")
            video_for_mux = apply_production_overlays(
                self.ffmpeg, paths.synced_video_noaudio, paths.decorated_video_noaudio,
                self.overlays, encoder, paths.work, self.log, frame_size=(layout["cw"], layout["ch"]),
                layout=layout, render_quality=self.render_quality,
            )
        self.log("🎧 Final audio mux (with limiter & mastering chain)...")
        volume_percent = int(getattr(self, "voice_volume_percent", 100) or 100)
        if volume_percent != 100:
            self.log(f"🔊 Voice volume {volume_percent}% applied at final mux (limiter on)")
        bgm_file = self.overlays.get("bgm_path")
        bgm_vol = float(self.overlays.get("bgm_volume", 0.18))
        if bgm_file and Path(bgm_file).is_file():
            self.log(f"🎵 Studio BGM sidechain ducking applied ({Path(bgm_file).name}, vol={bgm_vol:.2f})")
        mux_audio(
            self.ffmpeg,
            video_for_mux,
            paths.voice,
            paths.final,
            volume_percent,
            bgm_path=Path(bgm_file) if bgm_file else None,
            bgm_volume=bgm_vol,
        )
        final_duration = probe_duration(self.ffprobe, paths.final)

        # Copy synced SRT and ASS next to final video
        final_srt = paths.final.with_suffix(".srt")
        if srt_file.is_file() and srt_file.resolve() != final_srt.resolve():
            try:
                shutil.copy2(srt_file, final_srt)
            except Exception:
                pass

        final_ass = paths.final.with_suffix(".ass")
        if ass_file.is_file() and ass_file.resolve() != final_ass.resolve():
            try:
                shutil.copy2(ass_file, final_ass)
            except Exception:
                pass

        # Export standalone MP3 of the voiceover audio for separate delivery
        final_mp3 = paths.final.with_suffix(".mp3")
        try:
            cmd_mp3 = [
                self.ffmpeg, "-y", "-i", str(paths.voice),
                "-vn", "-af", "highpass=f=60,lowpass=f=12000,alimiter=limit=0.95:level=disabled",
                "-c:a", "libmp3lame", "-b:a", "192k",
                "-map_metadata", "-1",
                str(final_mp3)
            ]
            subprocess.run(cmd_mp3, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e_mp3:
            self.log(f"⚠️ Standalone MP3 export warning: {e_mp3}")
            final_mp3 = paths.voice

        self.log(f"✅ FINAL READY: {paths.final}")
        self.log(f"⏱️ Final duration: {final_duration:.2f}s")
        return {
            "audio": str(final_mp3 if (isinstance(final_mp3, Path) and final_mp3.is_file()) else paths.voice),
            "voice_wav": str(paths.voice),
            "plan": str(paths.plan),
            "final": str(paths.final),
            "subtitles_srt": str(final_srt) if final_srt.is_file() else (str(srt_file) if srt_file.is_file() else None),
            "subtitles_ass": str(final_ass) if final_ass.is_file() else (str(ass_file) if ass_file.is_file() else None),
            "segments": len(timing),
            "duration": final_duration,
        }
