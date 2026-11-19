"""Video Splitter — smart-cut part splitting for finished recap videos.

Ported from the desktop Recapter tool's ``splitter.py`` (2026-10-29,
production-grade programme) and adapted to this codebase:

* Recapter splits a *source* video with a ratio layout; here the input is
  the FINISHED recap render, whose frame is already final, so parts keep
  the output's own canvas — no ratio conversion, no re-framing.
* The smart-cut logic is ported verbatim in behaviour: when a part's time
  is up mid-speech, the cut waits for the next pause (up to ``max_extra``
  seconds) so no narration sentence is cut in two. The recap's audio track
  IS the narration, so pause detection works exactly as in the desktop
  tool.
* Every part is re-encoded (cuts land on the planned second, not on key
  frames) and gets a burned-in part label — ``အပိုင်း {mm}/{total}`` with
  Myanmar digits — at the top of the frame, so serial posting to
  TikTok/Facebook keeps episode order obvious.
* Parts already rendered with identical settings are kept (fingerprint
  state file), so a stopped run resumes where it stopped.

Splitting is a pure post-process of one finished render: it never
re-runs transcription/translation/TTS and is included in the single
recap credit (see api.py — no extra charge, by design).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

try:  # package import (app runtime)
    from app.services.recap_engine.core import (
        PROBE_TIMEOUT_SECONDS,
        _render_extra_text_png,
        encoder_args_for_quality,
        ffmpeg_encoders,
        ffmpeg_timeout,
        nvenc_usable,
        probe_duration,
        probe_video_size,
    )
except ImportError:  # flat import (engine runtime, sys.path includes this dir)
    from core import (  # type: ignore
        PROBE_TIMEOUT_SECONDS,
        _render_extra_text_png,
        encoder_args_for_quality,
        ffmpeg_encoders,
        ffmpeg_timeout,
        nvenc_usable,
        probe_duration,
        probe_video_size,
    )

LogFn = Callable[[str], None]

LEVEL_RATE = 8000             # audio is read at 8 kHz mono: enough to hear where speech stops
LEVEL_FRAME = 0.05            # one loudness value per 50 ms
PAUSE_BELOW_SPEECH_DB = 18.0  # silence: this much quieter than the video's speech level …
PAUSE_ABOVE_FLOOR_DB = 6.0    # … or, under background music, close to the quietest sound around the cut
GOOD_PAUSE_SEC = 0.3          # a pause this long is taken as the end of a sentence; shorter = a breath
MIN_TAIL_SEC = 20.0           # a last piece shorter than this (and than a quarter part) joins the part before it
SPLIT_STATE = ".split_state.json"
DEFAULT_LABEL_TEMPLATE = "အပိုင်း {mm}/{total}"

MM_DIGITS = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")


def _noop(_: str) -> None:
    pass


def part_label(template: str, n: int, total: int) -> str:
    """"Part {n}" -> "Part 2"; {mm} = the number in Myanmar digits
    (အပိုင်း {mm} -> အပိုင်း ၂); {total} = how many parts, in Myanmar
    digits too when the label uses {mm} ("အပိုင်း {mm}/{total}" ->
    "အပိုင်း ၂/၅", not "၂/5")."""
    text = str(template or "")
    total_text = str(total).translate(MM_DIGITS) if "{mm}" in text else str(total)
    return text.replace("{n}", str(n)).replace("{mm}", str(n).translate(MM_DIGITS)) \
        .replace("{total}", total_text).strip()


AUDIO_LEVELS_TIMEOUT_SECONDS = 1800  # hang backstop; the 8 kHz decode runs far faster than realtime

# Batch AJ / M10b: hard cap for the audio_levels watchdog. Callers
# used to pass timeout=ffmpeg_timeout(duration) — 18,000 s (5 h) for
# a 600 s source — pinning a recap slot during source splitting,
# outside the job deadline. The decode runs far faster than
# realtime, so 1800 s is a generous ceiling, and audio_levels
# clamps any larger caller value to it.
AUDIO_LEVELS_MAX_TIMEOUT_SECONDS = 1800

# Batch AJ / L21: ffmpeg/ffprobe may only open local files and
# pipes — never network protocols (see core.MEDIA_PROTOCOL_WHITELIST).
MEDIA_PROTOCOL_WHITELIST = "file,pipe"


def split_audio_levels_timeout(duration: float | None) -> float:
    """Watchdog for an audio_levels pass over this much media:
    ffmpeg_timeout(duration) capped at AUDIO_LEVELS_MAX_TIMEOUT_SECONDS."""
    return min(float(ffmpeg_timeout(duration)), float(AUDIO_LEVELS_MAX_TIMEOUT_SECONDS))


def audio_levels(ffmpeg: str, source: str | Path, *, timeout: float | None = None) -> Optional[np.ndarray]:
    """Loudness (dBFS) of every 50 ms of the video's sound; None when it has no sound.

    The decode is bounded (audit fix 2026-11-03): a watchdog kills
    ffmpeg after ``timeout`` seconds and the call raises RuntimeError
    instead of blocking the job forever on a wedged decoder."""
    # Batch AJ / M10b: clamp — no caller may re-arm the 5 h watchdog.
    effective_timeout = (
        AUDIO_LEVELS_TIMEOUT_SECONDS if timeout is None
        else min(float(timeout), float(AUDIO_LEVELS_MAX_TIMEOUT_SECONDS))
    )
    cmd = [ffmpeg, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
           "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", str(LEVEL_RATE),
           "-f", "s16le", "pipe:1"]
    hop = int(LEVEL_RATE * LEVEL_FRAME)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timed_out = threading.Event()
    watchdog = threading.Timer(
        effective_timeout,
        lambda: (timed_out.set(), proc.kill()),
    )
    watchdog.daemon = True
    watchdog.start()
    levels: list[np.ndarray] = []
    rest = np.zeros(0, dtype=np.float32)
    try:
        while True:
            chunk = proc.stdout.read(hop * 2 * 4000)      # ~200 s of sound at a time
            if not chunk:
                break
            data = np.concatenate([rest, np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0])
            usable = len(data) // hop * hop
            frames = data[:usable].reshape(-1, hop)
            rest = data[usable:]
            levels.append(10.0 * np.log10(np.mean(frames * frames, axis=1) + 1e-10))
    finally:
        watchdog.cancel()
        if proc.stdout:
            proc.stdout.close()
        proc.wait()
    if timed_out.is_set():
        raise RuntimeError(
            f"audio_levels timed out after "
            f"{effective_timeout:.0f}s "
            f"decoding {source} — failing instead of hanging."
        )
    if not levels:
        return None
    return np.concatenate(levels)


def speech_level(levels: np.ndarray) -> float:
    """How loud the speech of this video is (90th percentile of its sounding 50 ms frames)."""
    sounding = levels[levels > -70.0]
    return float(np.percentile(sounding, 90)) if len(sounding) else -20.0


def quiet_point(levels: np.ndarray, start: float, end: float, speech_db: float) -> tuple[float, str]:
    """Where to cut between start (the part's time is up) and end (start + the extra seconds allowed).

    Speech already over at `start` (a pause of 0.3 s+ running through it) -> cut in that pause. Otherwise the
    longest pause before `end` - the likeliest end of a sentence - taking the earliest when several are nearly as
    long; no pause at all (music, shouting) -> the quietest moment. Returns (seconds, "pause" | "breath" |
    "quietest")."""
    i0 = max(0, int(round(start / LEVEL_FRAME)))
    i1 = min(len(levels) - 1, int(round(end / LEVEL_FRAME)))
    if i1 <= i0:
        return start, "exact"
    floor = float(np.percentile(levels[i0:i1 + 1], 10))
    quiet = levels <= max(speech_db - PAUSE_BELOW_SPEECH_DB, floor + PAUSE_ABOVE_FLOOR_DB)
    lo, hi = max(0, i0 - int(3.0 / LEVEL_FRAME)), min(len(levels) - 1, i1 + int(3.0 / LEVEL_FRAME))
    runs: list[tuple[int, int]] = []
    i = lo
    while i <= hi:
        if not quiet[i]:
            i += 1
            continue
        a = b = i
        while b + 1 <= hi and (quiet[b + 1] or (b + 2 <= hi and quiet[b + 2])):   # one loud 50 ms blip is no end
            b += 1
        runs.append((a, b))
        i = b + 1
    good = round(GOOD_PAUSE_SEC / LEVEL_FRAME)

    def middle(a: int, b: int) -> float:
        return round(((max(a, i0) + b) / 2.0 + 0.5) * LEVEL_FRAME, 3)     # never before the part's time is up

    for a, b in runs:
        if a <= i0 <= b and b - a + 1 >= good and b - i0 >= 3:      # and still 0.2 s of it left to cut in
            return middle(a, b), "pause"
    later = [(a, b) for a, b in runs if i0 <= a <= i1]
    if later:
        longest = max(b - a + 1 for a, b in later)
        if longest >= 2:
            a, b = next(r for r in later if r[1] - r[0] + 1 >= 0.8 * longest)
            return middle(a, b), "pause" if b - a + 1 >= good else "breath"
    power = np.convolve(10.0 ** (levels[i0:i1 + 1] / 10.0), np.ones(4) / 4, mode="same")
    j = i0 + int(np.argmin(power))
    return round((j + 0.5) * LEVEL_FRAME, 3), "quietest"


def plan_parts(duration: float, part_seconds: float, *, smart: bool = True, max_extra: float = 20.0,
               levels: Optional[np.ndarray] = None, min_tail: float = MIN_TAIL_SEC,
               max_part_seconds: Optional[float] = None) -> list[dict[str, Any]]:
    """[{n, start, end, cut}] covering the whole video; cut = how the part ends ("exact", "pause", "breath",
    "quietest", "end"). Pure function — unit-tested without ffmpeg."""
    duration, part_seconds = float(duration), max(5.0, float(part_seconds))
    speech_db = speech_level(levels) if levels is not None else 0.0
    parts: list[dict[str, Any]] = []
    start = 0.0
    while start + part_seconds < duration - 0.05:
        target = start + part_seconds
        upper = min(target + max(0.0, float(max_extra)), duration)
        if max_part_seconds is not None:
            upper = min(upper, start + float(max_part_seconds))
        if smart and levels is not None:
            cut, how = quiet_point(levels, min(target, upper), upper, speech_db)
        else:
            cut, how = min(target, upper), "exact"
        if duration - cut < min(min_tail, part_seconds / 4.0):   # a few seconds left: they join this part
            if max_part_seconds is None or (duration - start) <= float(max_part_seconds):
                break
        parts.append({"n": len(parts) + 1, "start": round(start, 3), "end": round(cut, 3), "cut": how})
        start = cut
    while max_part_seconds is not None and (duration - start) > float(max_part_seconds) + 0.05:
        cut = start + float(max_part_seconds)
        parts.append({"n": len(parts) + 1, "start": round(start, 3), "end": round(cut, 3), "cut": "exact"})
        start = cut
    parts.append({"n": len(parts) + 1, "start": round(start, 3), "end": round(duration, 3), "cut": "end"})
    return parts


# Telegram's official Bot API refuses bot-sent files over 50 MB; when Local Bot
# API is active, the cap lifts to 2,000 MB (1,980 MB part budget). Keep
# MAX_PART_BYTES as the 48 MB default so callers/tests overriding it still win.
_DEFAULT_MAX_PART_BYTES = 48 * 1024 * 1024
MAX_PART_BYTES = _DEFAULT_MAX_PART_BYTES


def _effective_max_part_bytes() -> int:
    if MAX_PART_BYTES != _DEFAULT_MAX_PART_BYTES:
        return MAX_PART_BYTES
    try:
        from app.services.telegram_bot_factory import get_splitter_max_part_bytes

        return get_splitter_max_part_bytes()
    except Exception:
        return MAX_PART_BYTES


def size_capped_part_seconds(duration: float, total_bytes: int, part_seconds: float,
                             cap_bytes: int = MAX_PART_BYTES) -> float:
    """Cap a part length so a part at the render's average byte rate
    stays under ``cap_bytes``.

    Pure helper (unit-tested without ffmpeg). Returns ``part_seconds``
    unchanged when the byte rate is unknown or the cap does not bind;
    the result never goes below plan_parts' own 5 s floor. The cap is
    an average-rate estimate — split_rendered_video re-splits any part
    that still lands over the cap after rendering.
    """
    duration = float(duration or 0.0)
    if duration <= 0 or not total_bytes or float(total_bytes) <= 0:
        return float(part_seconds)
    bytes_per_second = float(total_bytes) / duration
    if bytes_per_second <= 0:
        return float(part_seconds)
    return max(5.0, min(float(part_seconds), float(cap_bytes) / bytes_per_second))


def _has_audio(ffprobe: str, source: str | Path) -> bool:
    p = subprocess.run([ffprobe, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
                        "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of",
                        "csv=p=0", str(source)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=PROBE_TIMEOUT_SECONDS)
    return bool(p.stdout.strip())


def render_label_png(text: str, output: Path, font_size: int, max_width: int, box: bool = True) -> Path:
    """Myanmar-safe part label (core text renderer), cropped to the text
    itself, on a dark rounded box when `box` is on. Port of Recapter's
    render_text_png."""
    from PIL import Image, ImageDraw

    raw = output.with_name(output.stem + ".raw.png")
    _render_extra_text_png(text, raw, font_size, 0.0, max_width=max_width)
    with Image.open(raw) as img:
        img = img.convert("RGBA")
        bbox = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
        if bbox:
            img = img.crop(bbox)
        pad_x, pad_y = max(8, int(font_size * 0.45)), max(5, int(font_size * 0.22))
        panel = Image.new("RGBA", (img.width + 2 * pad_x, img.height + 2 * pad_y), (0, 0, 0, 0))
        if box:
            ImageDraw.Draw(panel).rounded_rectangle((0, 0, panel.width - 1, panel.height - 1),
                                                    radius=max(6, int(font_size * 0.35)), fill=(0, 0, 0, 120))
        panel.alpha_composite(img, (pad_x, pad_y))
        panel.save(output)
    raw.unlink(missing_ok=True)
    return output


def render_part(ffmpeg: str, source: str | Path, start: float, end: float, output: Path, *,
                encoder: str, codec_args: Optional[list[str]] = None, has_audio: bool = True,
                render_quality: str = "standard",
                label_png: Optional[Path] = None, label_pos: tuple[float, float] = (0.5, 0.08)) -> None:
    """One part: exact start/end re-encode of the finished video, with the
    part label PNG overlaid at its 0..1 centre position. NVENC failure
    falls back to libx264 (same pattern as the overlay renderer)."""
    cmd = [ffmpeg, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
           "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{float(start):.3f}", "-t", f"{max(0.05, float(end) - float(start)):.3f}",
           "-i", str(source)]
    if label_png is not None and Path(label_png).is_file():
        cmd += ["-i", str(label_png)]
        lx, ly = float(label_pos[0]), float(label_pos[1])
        graph = (f"[0:v][1:v]overlay=x='W*{lx:.4f}-w/2':y='H*{ly:.4f}-h/2':eof_action=repeat,"
                 f"format=yuv420p[vout]")
        cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "0:a:0?"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-map_metadata", "-1"]
    sound = ["-c:a", "aac", "-b:a", "192k"] if has_audio else ["-an"]
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.stem + ".partial" + output.suffix)

    def run(args: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd + sound + args + ["-movflags", "+faststart", str(temp)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=ffmpeg_timeout(float(end) - float(start)))
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Part render timed out after {ffmpeg_timeout(float(end) - float(start))}s "
                f"for {output.name} — failing instead of hanging."
            ) from None
        return p.returncode, p.stderr or ""

    args = list(codec_args) if codec_args else encoder_args_for_quality(encoder, render_quality)
    code, err = run(args)
    if code != 0 and encoder != "libx264":
        code, err = run(encoder_args_for_quality("libx264", render_quality))
    if code != 0 or not temp.is_file() or temp.stat().st_size < 2000:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Part render failed for {output.name}: {err[-1500:]}")
    temp.replace(output)


def _fingerprint(source: Path, part: dict[str, Any], settings: dict[str, Any], label: str) -> str:
    stat = source.stat()
    data = {"src": str(source.resolve()), "size": stat.st_size, "mtime": int(stat.st_mtime),
            "start": part["start"], "end": part["end"], "label": label,
            **{k: settings.get(k) for k in ("part_seconds", "smart_cut", "max_extra", "codec", "quality")}}
    return hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    frac = int(round((seconds - int(seconds)) * 10)) % 10
    return f"{h}:{m:02d}:{s:02d}.{frac}" if h else f"{m:02d}:{s:02d}.{frac}"


def split_rendered_video(
    source: str | Path,
    out_dir: str | Path,
    *,
    part_minutes: float = 3.0,
    smart_cut: bool = True,
    max_extra: float = 20.0,
    label_template: str = DEFAULT_LABEL_TEMPLATE,
    label_on: bool = True,
    render_quality: str = "standard",
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    codec_args: Optional[list[str]] = None,
    log: LogFn = _noop,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list[dict[str, Any]]:
    """Cut a finished video into labelled parts inside *out_dir*.

    Returns [{n, path, start, end, cut, label}]. A single-element list
    means the video was too short to split (the "part" is the whole
    video, NOT rendered again — ``path`` is None in that case so callers
    can skip part deliverables). Raises on render failure; parts already
    finished stay on disk and are reused on a later call (fingerprint
    state), so a retry never redoes completed parts.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Split source not found: {source}")
    out_dir = Path(out_dir)
    duration = probe_duration(ffprobe, source)
    part_seconds = max(30.0, float(part_minutes) * 60.0)
    # Size-aware cap (batch N): cap the part length by the finished
    # render's own average byte rate so parts stay deliverable
    # (≤ MAX_PART_BYTES); the re-split pass after rendering handles
    # sections whose bitrate runs hotter than the average.
    _uncapped_part_seconds = part_seconds
    _cap_bytes = _effective_max_part_bytes()
    part_seconds = size_capped_part_seconds(
        duration, source.stat().st_size, part_seconds, _cap_bytes
    )
    if part_seconds < _uncapped_part_seconds:
        log(f"📦 Splitter: size cap — part length limited to ~{part_seconds:.0f}s "
            f"so each part stays under {_cap_bytes // (1024 * 1024)} MB")
    has_audio = _has_audio(ffprobe, source)

    levels = None
    if smart_cut and has_audio:
        log("🔊 Splitter: reading the sound to find the pauses…")
        levels = audio_levels(ffmpeg, source, timeout=split_audio_levels_timeout(duration))
    elif smart_cut:
        log("ℹ️ Splitter: no sound in this video — exact cuts")

    parts = plan_parts(duration, part_seconds, smart=smart_cut,
                       max_extra=float(max_extra), levels=levels)
    total = len(parts)
    if total < 2:
        log(f"ℹ️ Splitter: {duration:.0f}s fits in one part — nothing to split")
        return [{"n": 1, "path": None, "start": 0.0, "end": round(duration, 3),
                 "cut": "end", "label": part_label(label_template, 1, 1) if label_on else ""}]

    out_dir.mkdir(parents=True, exist_ok=True)
    cw, _ch = probe_video_size(ffprobe, source)
    font_size = max(20, min(72, int(round(48 * (cw or 1080) / 1080.0))))
    encoders = ffmpeg_encoders(ffmpeg)
    encoder = "h264_nvenc" if ("h264_nvenc" in encoders and nvenc_usable(ffmpeg)) else "libx264"
    settings = {"part_seconds": part_seconds, "smart_cut": bool(smart_cut),
                "max_extra": float(max_extra), "quality": str(render_quality or "standard"),
                "codec": " ".join(codec_args) if codec_args else encoder}
    log(f"✂️ Splitter: {source.name} → {total} part(s) of ~{part_minutes:g} min (smart={smart_cut}, {encoder})")
    for p in parts:
        log(f"   Part {p['n']}: {_clock(p['start'])} → {_clock(p['end'])} ({p['end'] - p['start']:.1f} s, {p['cut']})")

    state_file = out_dir / SPLIT_STATE
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    work = out_dir / ".labels"

    def _render_plan(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        plan_total = len(plan)
        rendered: list[dict[str, Any]] = []
        for p in plan:
            if should_stop and should_stop():
                raise InterruptedError("Split stopped — finished parts are kept for resume")
            out = out_dir / f"recap_part_{p['n']:02d}.mp4"
            label = part_label(label_template, p["n"], plan_total) if label_on else ""
            key = _fingerprint(source, p, settings, label)
            if out.is_file() and state.get(out.name) == key:
                log(f"♻️ Part {p['n']}/{plan_total} already made — kept")
            else:
                label_png = None
                if label:
                    work.mkdir(exist_ok=True)
                    label_png = render_label_png(label, work / f"label_{p['n']:02d}.png",
                                                 font_size, int((cw or 1080) * 0.6), box=True)
                render_part(ffmpeg, source, p["start"], p["end"], out,
                            encoder=encoder, codec_args=codec_args, has_audio=has_audio,
                            render_quality=render_quality,
                            label_png=label_png)
                state[out.name] = key
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"✅ {out.name} ({label})" if label else f"✅ {out.name}")
            rendered.append({"n": p["n"], "path": str(out), "start": p["start"], "end": p["end"],
                             "cut": p["cut"], "label": label})
        return rendered

    done = _render_plan(parts)

    # Re-split pass (batch N): a part whose ACTUAL bytes still exceed
    # the cap (its section ran hotter than the render's average rate)
    # is divided ONCE, proportionally to its own measured byte rate,
    # and the refined plan is rendered again — the fingerprint check
    # keeps every part whose range and label survived, so only the
    # divided range (and any part whose label total changed) actually
    # re-renders. A sub-part that is STILL over the cap is left as-is:
    # one re-split is the designed bound.
    oversized = {
        (d["start"], d["end"])
        for d in done
        if d.get("path") and Path(d["path"]).stat().st_size > _cap_bytes
    }
    if oversized:
        refined: list[dict[str, Any]] = []
        for p in parts:
            if (p["start"], p["end"]) in oversized:
                span = float(p["end"]) - float(p["start"])
                actual = Path(out_dir / f"recap_part_{p['n']:02d}.mp4").stat().st_size
                sub_seconds = max(5.0, span * (_cap_bytes / float(actual)))
                sub_plan = plan_parts(span, sub_seconds, smart=False, levels=None)
                for sp in sub_plan:
                    refined.append({
                        "start": round(float(p["start"]) + float(sp["start"]), 3),
                        "end": round(float(p["start"]) + float(sp["end"]), 3),
                        "cut": sp["cut"],
                    })
                log(f"📦 Splitter: part {p['n']} rendered at "
                    f"{actual / (1024 * 1024):.1f} MB — re-splitting into "
                    f"{len(sub_plan)} smaller part(s)")
            else:
                refined.append({"start": p["start"], "end": p["end"], "cut": p["cut"]})
        if len(refined) > len(parts):
            for idx, rp in enumerate(refined, 1):
                rp["n"] = idx
            parts = refined
            total = len(parts)
            done = _render_plan(parts)

    # Parts made earlier for a different plan (e.g. 3-min before 5-min) are removed.
    keep = {Path(d["path"]).name for d in done}
    for name in [n for n in state if n not in keep]:
        (out_dir / name).unlink(missing_ok=True)
        state.pop(name, None)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return done


def split_source_into_segments(
    source: str | Path,
    out_dir: str | Path,
    *,
    part_seconds: float,
    max_part_seconds: Optional[float] = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    log: LogFn = _noop,
) -> list[dict[str, Any]]:
    """Cut a SOURCE video into clean processing segments (no labels).

    Used by the >10-minute split path: the per-job duration cap stays in
    force, so an over-cap source is cut at narration pauses into
    segments of at most ``part_seconds`` (the caller passes a target
    below the cap, leaving room for the smart-cut slack) and each
    segment is then run through the recap pipeline on its own. The
    outputs are intermediates — the pipeline's per-segment renders
    become the job's deliverable parts.

    Returns [{n, path, start, end, cut}] with real paths (unlike
    split_rendered_video there is no single-part None case: a source
    that fits in one segment yields the source itself as segment 1).
    """
    from app.services.recap_engine.core import probe_duration

    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Segment source not found: {source}")
    out_dir = Path(out_dir)
    duration = probe_duration(ffprobe, source)
    part_seconds = max(30.0, float(part_seconds))
    eff_cap = float(max_part_seconds) if max_part_seconds is not None else (part_seconds + 25.0)
    if duration <= part_seconds:
        return [{"n": 1, "path": str(source), "start": 0.0,
                 "end": round(duration, 3), "cut": "end"}]

    has_audio = _has_audio(ffprobe, source)
    levels = audio_levels(ffmpeg, source, timeout=split_audio_levels_timeout(duration)) if has_audio else None
    plan = plan_parts(duration, part_seconds, smart=True, levels=levels, max_part_seconds=eff_cap)
    encoders = ffmpeg_encoders(ffmpeg)
    encoder = "h264_nvenc" if ("h264_nvenc" in encoders and nvenc_usable(ffmpeg)) else "libx264"
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"✂️ Source split: {source.name} → {len(plan)} segment(s) of "
        f"≤{part_seconds:.0f}s (smart cuts at narration pauses)")
    done: list[dict[str, Any]] = []
    for p in plan:
        out = out_dir / f"source_seg_{p['n']:02d}.mp4"
        render_part(ffmpeg, source, p["start"], p["end"], out,
                    encoder=encoder, has_audio=has_audio,
                    render_quality="standard", label_png=None)
        done.append({"n": p["n"], "path": str(out), "start": p["start"],
                     "end": p["end"], "cut": p["cut"]})
        log(f"   Segment {p['n']}/{len(plan)}: {_clock(p['start'])} → "
            f"{_clock(p['end'])} ({p['end'] - p['start']:.1f} s, {p['cut']})")
    return done
