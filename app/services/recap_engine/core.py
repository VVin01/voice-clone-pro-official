from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import queue
import shutil
import socket
import subprocess
import re
import sys
import threading
import unicodedata
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from app.services.speed import DEFAULT_SPEED, resolve_speed_rate, resolve_speed_tempo

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from app.services.myanmar_text import repair_burmese_mark_order
except ImportError:  # core.py loaded standalone (recap_engine dir on sys.path)
    _ROOT = _HERE.parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from app.services.myanmar_text import repair_burmese_mark_order

try:
    from app.services.number_words import normalize_numbers_for_speech
except ImportError:  # core.py loaded standalone (recap_engine dir on sys.path)
    _ROOT = _HERE.parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from app.services.number_words import normalize_numbers_for_speech

try:
    from app.services.recap_engine import png_subtitles as _png_subs
except ImportError:  # core.py loaded standalone (recap_engine dir on sys.path)
    import png_subtitles as _png_subs  # type: ignore[no-redef]

try:
    from app.services.voxcpm_engine import (
        JobQuotaMemory as _JobQuotaMemory,
        VoxCPMQuotaExhaustedError as _VoxCPMQuotaExhaustedError,
    )
except Exception:  # pragma: no cover - standalone load without the app package
    _JobQuotaMemory = None  # type: ignore[assignment]
    _VoxCPMQuotaExhaustedError = None  # type: ignore[assignment]


LogFn = Callable[[str], None]
def _noop(_: str) -> None:
    pass


def _emit_progress_stage(progress_fn: Any, label: str) -> None:
    """Invoke a stage progress callback supporting both 1-arg and 3-arg signatures."""
    if not progress_fn:
        return
    try:
        params = list(inspect.signature(progress_fn).parameters.values())
    except (TypeError, ValueError):
        params = []
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    required = [p for p in positional if p.default is inspect.Parameter.empty]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    try:
        if len(required) >= 3 or (not has_varargs and len(positional) >= 3 and len(required) == 0):
            progress_fn(1, 1, label)
        else:
            progress_fn(label)
    except Exception:
        pass


class RecapParkRequested(Exception):
    """Batch DP (Item 3): a PAID recap job hit VoxCPM quota exhaustion
    with no owner choice in force. Raised out of the TTS stage instead
    of silently degrading the segment to the persona/Edge engine; the
    webapp driver catches it and parks the job (WAITING_CHOICE) with
    its charge held — never a failure, never a refund trigger."""


SYNC_DRIFT_TOLERANCE_SECONDS: float = 0.30


class SyncDriftError(RuntimeError):
    """Raised when concatenated or final muxed video drifts from narration
    beyond SYNC_DRIFT_TOLERANCE_SECONDS, or video part assembly violates
    the 1-to-1 segment invariant. Fails loudly so the job refunds instead
    of shipping mismatched scenes or silently truncating footage."""


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


def find_ffprobe() -> str:
    """Locate the ffprobe executable via resolve_binary."""
    return resolve_binary(None, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"])


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


# ---------------------------------------------------------------------------
# Media subprocess timeouts + job time budget (audit fix 2026-11-03).
#
# Every ffmpeg/ffprobe call in the recap engine is bounded: a render
# that wedges on a corrupt input used to hang the whole job forever —
# no failure, no refund, one execution slot burned. Render timeouts
# scale with the media duration (RENDER_TIMEOUT_PER_SECOND seconds of
# wall clock per second of media, never less than the floor); probes
# and other short operations get a fixed cap. A blown timeout raises
# RuntimeError so the job fails through the normal path. On top of the
# per-call bounds, EdgeSmartSync enforces a whole-job budget checked at
# segment boundaries (see recap_job_timeout).
PROBE_TIMEOUT_SECONDS = 120
# Batch AJ / M11: bound a single Edge TTS save. A stalled
# third-party socket used to pin the recap slot for the whole
# segment-retry loop; 120 s sits in the same spirit as the VoxCPM
# total budget (240 s) for one narration segment.
EDGE_TTS_TIMEOUT_SECONDS = 120
# Batch AJ / L21: ffmpeg/ffprobe may only open local files and
# pipes — never network protocols. The hunt confirmed playlist /
# concat-style inputs can make the demuxer follow embedded
# references (a crafted ffconcat file with a video extension was
# probed through the tree's own probe path and returned the
# referenced file's streams), and probes accepted http:// inputs.
# `file` covers every legitimate local input (including the
# engine's OWN concat list files), `pipe` the pipe workflows, and
# lavfi sources are unaffected (verified against the real binary).
MEDIA_PROTOCOL_WHITELIST = "file,pipe"
RENDER_TIMEOUT_FLOOR_SECONDS = 600
RENDER_TIMEOUT_PER_SECOND = 30.0
JOB_TIMEOUT_FLOOR_SECONDS = 3600
JOB_TIMEOUT_PER_SECOND = 15.0
JOB_TIMEOUT_CAP_SECONDS = 4 * 3600


def ffmpeg_timeout(duration_seconds: float | None) -> int:
    """Wall-clock timeout (seconds) for a render over this much media."""
    try:
        duration = float(duration_seconds or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return int(max(RENDER_TIMEOUT_FLOOR_SECONDS, RENDER_TIMEOUT_PER_SECOND * max(0.0, duration)))


def recap_job_timeout(source_duration: float | None) -> int:
    """Whole-job time budget (seconds) for one pipeline run."""
    try:
        duration = float(source_duration or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    scaled = JOB_TIMEOUT_PER_SECOND * max(0.0, duration)
    return int(min(JOB_TIMEOUT_CAP_SECONDS, max(JOB_TIMEOUT_FLOOR_SECONDS, scaled)))


# Batch AL: default width of the recap TTS worker pool. Chosen by the
# local 4/6/8 benchmark recorded in the Batch AL PR (pool overhead is
# negligible; the pick is bounded by the 15-account HF pool and the
# 4 Space queues, not by this box's CPU — synthesis is network-bound).
# RECAP_TTS_WORKERS=1 restores the exact pre-AL serial stage.
DEFAULT_TTS_WORKERS = 6


# Batch CY: per-segment VoxCPM failover budget constants. The Batch
# AL slope (20 s of budget per second of window) saturated at a
# 10.5 s window, so every normal narration line still received the
# old flat 240 s and one pathological segment could burn ~8 min
# (2 wrapper attempts x 240 s) before falling back. CY derives the
# budget from the segment's expected synthesis load instead: the
# spoken length is estimated from the window and from the text
# (~21 chars/s of Burmese narration, the rate in the Batch AL
# fixtures), and the budget is 25 s of walk patience plus 2.5 s per
# expected audio second — a normal 15-25 s line lands at 62-88 s,
# floored at 45 s (the AL floor: ~2 healthy attempts at the CB
# fresh-quota pace of 15-21 s/segment) and capped at 150 s, below
# the historical 240 s. With the wrapper's 2 attempts a normal
# line's worst case is <= ~3 min; even the longest segment's
# absolute ceiling is 2 x 150 s, well under the old ~8 min.
VOX_FAILOVER_BUDGET_FLOOR_SECONDS = 45.0
VOX_FAILOVER_BUDGET_CAP_SECONDS = 150.0
# Batch DR: the paid tier gets a longer clock — same base and slope,
# only the floor and cap move. The 2026-11-11 evening evidence
# showed paid segments dying on the 45 s floor while the walk was
# still working through slow attempts and fragmented shared
# balances, before it ever reached an admittable pair (the DP
# reserve included). Free values are unchanged.
VOX_FAILOVER_BUDGET_PAID_FLOOR_SECONDS = 120.0
VOX_FAILOVER_BUDGET_PAID_CAP_SECONDS = 300.0
VOX_FAILOVER_BUDGET_BASE_SECONDS = 25.0
VOX_FAILOVER_BUDGET_PER_AUDIO_SECOND = 2.5
VOX_NARRATION_CHARS_PER_SECOND = 21.0


def vox_failover_budget(window_seconds: float, text_chars: int, *, paid: bool = False) -> float:
    """Batch AL + CY: per-segment VoxCPM failover budget, scaled to
    the segment instead of the old flat 240 s for every line.

    Batch AL introduced the scaling (30 s + 20 s per window second,
    cap 240 s); Batch CY re-slopes it around the segment's expected
    synthesis load — see the constants above. The expected spoken
    length is the larger of the video window and the text-implied
    length, so a dense long line in a short window still earns a
    workable budget.

    Batch DR: ``paid=True`` swaps in the paid floor/cap (120 s /
    300 s) — the shared base and slope are identical, and the free
    result is byte-identical to before."""
    try:
        window = float(window_seconds or 0.0)
    except (TypeError, ValueError):
        window = 0.0
    try:
        chars = float(text_chars or 0)
    except (TypeError, ValueError):
        chars = 0.0
    expected_audio = max(
        max(0.0, window),
        max(0.0, chars) / VOX_NARRATION_CHARS_PER_SECOND,
    )
    if paid:
        floor_seconds = VOX_FAILOVER_BUDGET_PAID_FLOOR_SECONDS
        cap_seconds = VOX_FAILOVER_BUDGET_PAID_CAP_SECONDS
    else:
        floor_seconds = VOX_FAILOVER_BUDGET_FLOOR_SECONDS
        cap_seconds = VOX_FAILOVER_BUDGET_CAP_SECONDS
    return float(min(
        cap_seconds,
        max(
            floor_seconds,
            VOX_FAILOVER_BUDGET_BASE_SECONDS
            + VOX_FAILOVER_BUDGET_PER_AUDIO_SECOND * expected_audio,
        ),
    ))


def _with_protocol_whitelist(cmd: list[str]) -> list[str]:
    """Batch AJ / L21: insert ``-protocol_whitelist file,pipe``
    directly after the binary for media commands that read an input
    (an ``-i`` argument, or any ffprobe probe, whose input is
    positional). Pure listing commands (``ffmpeg -encoders``) take
    no input and are left byte-identical. The option sits before the
    first ``-i``, so ffmpeg applies it to that input; every engine
    input after the first is an internally generated file (PNG
    overlays, WAV parts) on the same `file` protocol anyway."""
    if not cmd:
        return cmd
    binary = Path(str(cmd[0])).name.lower()
    if "ffprobe" in binary or "-i" in cmd:
        return [cmd[0], "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, *cmd[1:]]
    return cmd


def _run_media(cmd: list[str], *, timeout: float, what: str) -> subprocess.CompletedProcess:
    """subprocess.run for media tools, with a bounded wait: a timeout
    becomes a clear RuntimeError instead of an unbounded hang."""
    cmd = _with_protocol_whitelist(cmd)
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{what} timed out after {int(timeout)}s — failing the job instead of hanging."
        ) from None


def _file_identity(path: Any, *, content_hash_limit: int = 8 * 1024 * 1024) -> dict[str, Any] | None:
    """Stable identity of a file input for checkpoint fingerprints:
    a content hash for small files, size+mtime for large ones, None
    when the file is missing or unreadable."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        st = p.stat()
        if st.st_size <= content_hash_limit:
            return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        return {"path": str(p), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except Exception:
        return None


def _stage_fingerprint(stage: str, components: Any) -> str:
    """Fingerprint of one late pipeline stage's inputs (concat /
    overlay / mux), so a resumed run can skip a stage whose inputs
    are provably unchanged and whose output is still on disk."""
    raw = json.dumps(
        {"stage": stage, "components": components},
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class EdgeTTSEngine:
    """Local Edge-TTS adapter with resilient direct/system-route retries.

    Edge TTS uses a WebSocket service. ``NoAudioReceived`` is often a
    transport/route failure rather than a Smart Sync failure. Generate the
    MP3 in-process so the adapter can classify errors, back off, and try both
    a proxy-bypassed route and the inherited system/proxy route.
    """

    def __init__(self, *, ffmpeg: str, work_dir: Path, rate: str = "+0%", pitch: str = "+0Hz", log: LogFn = _noop) -> None:
        import importlib.util
        if importlib.util.find_spec("edge_tts") is None:
            raise RuntimeError("edge-tts မတွေ့ပါ။ pip install -U edge-tts လုပ်ပြီး ပြန်စမ်းပါ။")
        self.ffmpeg = ffmpeg
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.rate = resolve_speed_rate(rate if str(rate or "").strip() else "+0%")
        self.pitch = str(pitch or "+0Hz").strip()
        self.log = log
        self.VOICE_DATABASE: dict[str, dict[str, Any]] = {}
        self._counter = 0
        # Batch AL: the TTS worker pool shares this engine; the stem
        # counter must advance atomically or two concurrent calls with
        # the same text could mint the same temp file stem.
        self._counter_lock = threading.Lock()
        self._tls = threading.local()
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
        try:
            from app.services.myanmar_text import repair_burmese_mark_order

            value = repair_burmese_mark_order(value)
        except Exception:
            pass
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
        try:
            communicate = edge_tts.Communicate(
                text,
                voice=str(voice),
                rate=self.rate,
                pitch=self.pitch,
                proxy=proxy,
                boundary="WordBoundary",
            )
        except TypeError:
            communicate = edge_tts.Communicate(
                text,
                voice=str(voice),
                rate=self.rate,
                pitch=self.pitch,
                proxy=proxy,
            )
        tls = getattr(self, "_tls", None)
        if tls is not None:
            tls.last_word_timings = []
        # Quality Pass §2G: stream MP3 + WordBoundary events when Edge TTS
        # supports stream(), falling back to save() for mocks or older wrappers.
        if callable(getattr(communicate, "stream", None)):
            async def _stream_to_file() -> None:
                audio_buf = bytearray()
                boundaries: list[dict[str, Any]] = []
                async for chunk in communicate.stream():
                    ctype = chunk.get("type") if isinstance(chunk, dict) else None
                    if ctype == "audio":
                        data = chunk.get("data") or b""
                        if data:
                            audio_buf.extend(data)
                    elif ctype in ("WordBoundary", "SentenceBoundary"):
                        off = float(chunk.get("offset") or 0.0) / 10_000_000.0
                        dur = max(0.0, float(chunk.get("duration") or 0.0)) / 10_000_000.0
                        tok = str(chunk.get("text") or "").strip()
                        if tok:
                            boundaries.append({
                                "type": str(ctype),
                                "start": round(off, 4),
                                "end": round(off + dur, 4),
                                "text": tok,
                            })
                if not audio_buf:
                    raise RuntimeError("Edge TTS stream produced no audio bytes.")
                mp3.write_bytes(bytes(audio_buf))
                if tls is not None:
                    tls.last_word_timings = boundaries

            await asyncio.wait_for(_stream_to_file(), timeout=EDGE_TTS_TIMEOUT_SECONDS)
            return

        # Batch AJ / M11: per-call timeout — the retry loop in
        # generate_channel99_voice catches the TimeoutError like any
        # other route failure, so a stalled socket fails over (and
        # ultimately fails the segment) instead of hanging forever.
        await asyncio.wait_for(
            communicate.save(str(mp3)), timeout=EDGE_TTS_TIMEOUT_SECONDS
        )

    def _synthesize_once(self, text: str, voice: str, mp3: Path, *, direct: bool) -> None:
        # Route selection is per-call (audit fix 2026-11-03). The old
        # code stripped/restored the process-global proxy env vars
        # under a lock for the whole synthesis; with two recap jobs
        # running concurrently — and the planner/downloader reading
        # proxy settings from the same environment — that raced every
        # other thread in the process. edge_tts.Communicate takes an
        # explicit proxy argument instead:
        #   * proxy route: the configured proxy URL, passed as-is;
        #   * direct route: "" — a non-None proxy makes aiohttp skip
        #     its environment lookup entirely, and the empty URL is
        #     falsy in the connector, so the connection goes direct;
        #   * system route with no proxy configured: None, which keeps
        #     the historical "inherit the system/env proxy" meaning.
        proxy = "" if direct else self.proxy
        asyncio.run(self._save_mp3_async(text, voice, mp3, proxy))

    def generate_channel99_voice(self, text: str, voice: str):
        clean_text = self._clean_text(text)
        with getattr(self, "_counter_lock", threading.Lock()):
            self._counter += 1
            counter_value = self._counter
        stem = f"edge_{counter_value:05d}_{hashlib.sha1(clean_text.encode('utf-8')).hexdigest()[:10]}"
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

        conv = _run_media(
            [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3),
             "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav)],
            timeout=PROBE_TIMEOUT_SECONDS, what="Edge TTS WAV conversion",
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
    # Wire ending-credit audit repair before synthesis: drop credit ending only
    try:
        from app.services.recap_engine.recap_planner import audit_script_ending_credit
        out = audit_script_ending_credit(out)
    except Exception:
        pass
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
    """The narration as the voice should say it: Latin-letter glosses in ( ) dropped.

    The line is canonicalized first (mark order, orthography) so every
    TTS engine -- Edge, Gemini, VoxCPM -- speaks the corrected text,
    not the raw planner output."""
    value = canonicalize_burmese_text(str(text or ""))
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
    # Fast-path stdlib WAV header read (~0.05 ms vs ~80 ms ffprobe spawn × 50–150 segments).
    try:
        p = Path(path)
        if p.suffix.lower() == ".wav" and p.is_file():
            import wave as _wave

            with _wave.open(str(p), "rb") as _wf:
                _rate = _wf.getframerate()
                _frames = _wf.getnframes()
                if _rate > 0 and _frames > 0:
                    return float(_frames) / float(_rate)
    except Exception:
        pass
    result = _run_media(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        timeout=PROBE_TIMEOUT_SECONDS, what="ffprobe duration probe",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-2000:]}")
    return float(result.stdout.strip())


def _concat_list_line(path: Path, *, posix: bool = False) -> str:
    resolved = path.resolve()
    raw = resolved.as_posix() if posix else str(resolved)
    p = raw.replace("'", "'\\''")
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


def concat_audio(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path, *, total_duration: float | None = None) -> None:
    if not parts:
        raise ValueError("Audio parts မရှိပါ။")
    list_file = work_dir / "audio_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output),
    ]
    result = _run_media(cmd, timeout=ffmpeg_timeout(total_duration), what="Audio concat")
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
    result = _run_media(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)],
        timeout=PROBE_TIMEOUT_SECONDS, what="ffprobe video size probe",
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


def output_canvas(
    ratio: str,
    source_w: int = 0,
    source_h: int = 0,
    *,
    max_short_side: int | None = None,
    fill: str = "blur",
    crop_top: float = 0.0,
    crop_bottom: float = 0.0,
    crop_left: float = 0.0,
    crop_right: float = 0.0,
) -> tuple[int, int]:
    """Final frame size capped by BOTH the tier cap AND 1.0x native source scale.

    Round 15 Part I (never upscale):
      * Tier cap: VIP/default = 1080 short side (1920x1080 / 1080x1920 / 1080x1080),
        Free = 720 short side (1280x720 / 720x1280 / 720x720).
      * When source dimensions are known (source_w > 0 and source_h > 0), output
        dimensions = min(tier cap, the dimensions produced at 1.0x source scale)
        under the active ratio/crop/fill framing, so the main foreground layer is
        never scaled above 100% of its native pixels.
      * When source meets or exceeds the tier cap, output equals the tier cap
        (byte-for-byte identical to the pre-Round-15 geometry).
    """
    ratio = ratio if ratio in OUTPUT_RATIOS else "16:9"
    fill = fill if fill in FRAME_FILLS else "blur"
    cap_short = (
        1080
        if (max_short_side is None or int(max_short_side) <= 0)
        else _even(int(max_short_side))
    )
    cap_long = _even(cap_short * 16.0 / 9.0)
    sw, sh = int(source_w or 0), int(source_h or 0)

    if ratio == "Original":
        if sw > 0 and sh > 0:
            scale = min(1.0, cap_long / max(sw, sh), cap_short / min(sw, sh))
            return _even(sw * scale), _even(sh * scale)
        return cap_long, cap_short

    if ratio == "9:16":
        cap_w, cap_h = cap_short, cap_long
    elif ratio == "1:1":
        cap_w, cap_h = cap_short, cap_short
    else:
        cap_w, cap_h = cap_long, cap_short

    if sw <= 0 or sh <= 0:
        return cap_w, cap_h

    ct = max(0.0, min(0.35, float(crop_top or 0.0)))
    cb = max(0.0, min(0.35, float(crop_bottom or 0.0)))
    cl = max(0.0, min(0.35, float(crop_left or 0.0)))
    cr = max(0.0, min(0.35, float(crop_right or 0.0)))
    has_crop = (ct > 0.001 or cb > 0.001 or cl > 0.001 or cr > 0.001)
    if has_crop and (cl + cr < 0.90) and (ct + cb < 0.90):
        c_sw = _even(sw * (1.0 - cl - cr))
        c_sh = _even(sh * (1.0 - ct - cb))
    else:
        c_sw, c_sh = sw, sh

    if fill == "crop":
        # Crop-to-fill scales by max(cw/c_sw, ch/c_sh); keeping <= 1.0x requires
        # both canvas dimensions <= cropped source dimensions.
        k = min(1.0, c_sw / float(cap_w), c_sh / float(cap_h))
    else:
        # Fit framing (blur/black) scales by min(cw/c_sw, ch/c_sh); at 1.0x
        # source scale the smallest matching-aspect canvas enclosing (c_sw, c_sh)
        # has factor max(c_sw/cap_w, c_sh/cap_h).
        k = min(1.0, max(c_sw / float(cap_w), c_sh / float(cap_h)))

    if k >= 1.0:
        return cap_w, cap_h
    return min(cap_w, _even(cap_w * k)), min(cap_h, _even(cap_h * k))


def frame_layout(ratio: str, fill: str, source_w: int, source_h: int,
                 canvas: tuple[int, int] | None = None,
                 crop_top: float = 0.0,
                 crop_bottom: float = 0.0,
                 crop_left: float = 0.0,
                 crop_right: float = 0.0,
                 *,
                 max_short_side: int | None = None) -> dict[str, Any]:
    """Everything _visual_filters needs; `canvas` overrides the size (the preview draws smaller)."""
    ratio = ratio if ratio in OUTPUT_RATIOS else "16:9"
    fill = fill if fill in FRAME_FILLS else "blur"
    cw, ch = canvas or output_canvas(
        ratio,
        source_w,
        source_h,
        max_short_side=max_short_side,
        fill=fill,
        crop_top=crop_top,
        crop_bottom=crop_bottom,
        crop_left=crop_left,
        crop_right=crop_right,
    )
    cw, ch = _even(cw), _even(ch)
    sw, sh = int(source_w or 0) or cw, int(source_h or 0) or ch

    if ratio == "Original":
        ct = cb = cl = cr = 0.0
    else:
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

    # Batch AK (premium audit P7): the near-aspect shortcut tests
    # the CROPPED aspect (c_sw/c_sh — what the fit scale below
    # actually scales), not the uncropped source aspect; with crop
    # margins set the two could disagree about the same frame.
    if fill == "crop" or (not has_crop and abs(c_sw / c_sh - cw / ch) < 0.01):
        pw, ph = cw, ch
        ox, oy = 0, 0
    else:
        scale = min(cw / c_sw, ch / c_sh) if c_sw and c_sh else 1.0
        if canvas is None and source_w and source_h:
            scale = min(1.0, scale)
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


USER_BLUR_BOX_WIDTH_FRACTION = 0.92


def user_blur_box_to_canvas_box(
    blur_y: float, blur_h: float, layout: dict[str, Any] | None = None,
    blur_x: float | None = None, blur_w: float | None = None,
) -> dict[str, float]:
    """Map the user's blur box from PICTURE fractions to a canvas box.

    Batch AA contract: ``blur_y`` / ``blur_h`` are fractions of the
    PICTURE height end-to-end (Mini App sliders → submit payload →
    persisted recap_config → engine), exactly like Batch P's sub_y.
    The preview draws the box on the picture — centre at
    oy + blur_y × ph, height blur_h × ph, width fixed at 92% of the
    canvas and centred (the .draggable-box CSS: left 4% / right 4%) —
    so the render converts with the same layout math and the box the
    user drew is the box that ships: centre (oy + blur_y × ph) / ch,
    height blur_h × ph / ch, x = 0.04, w = 0.92. On full-frame
    layouts (oy = 0, ph = ch) this is the identity mapping. With no
    layout (the size probe failed) the picture is assumed to fill
    the canvas, matching the plate's no-layout fallback. The box is
    returned verbatim — consumers clamp the drawn rectangle into
    the canvas exactly as they do for every other box.

    Batch AG: ``blur_x`` / ``blur_w`` add the horizontal axis in the
    same picture basis (fractions of the PICTURE width, converted
    exactly like source_band_to_canvas_box: x = (ox + bx × pw) / cw,
    w = bw × pw / cw). Both None — every pre-AG caller and stored
    config — keeps the legacy centred 92%-of-canvas width
    byte-identical; a lone blur_w stays centred at that width.
    """
    by = _clamp01(blur_y, 0.85)
    bh = _clamp01(blur_h, 0.07)
    if blur_x is None and blur_w is None:
        w_frac = USER_BLUR_BOX_WIDTH_FRACTION
        x_frac = (1.0 - w_frac) / 2.0
        if layout is not None:
            cw = int(layout.get("cw") or 0)
            ch = int(layout.get("ch") or 0)
            if cw > 0 and ch > 0:
                _pw, ph, _ox, oy = _layout_content_box(layout, cw, ch)
                return {
                    "x": round(x_frac, 4),
                    "y": round((oy + by * ph) / ch, 4),
                    "w": round(w_frac, 4),
                    "h": round(bh * ph / ch, 4),
                }
        return {
            "x": round(x_frac, 4),
            "y": round(by, 4),
            "w": round(w_frac, 4),
            "h": round(bh, 4),
        }
    bw_frac = (
        _clamp01(blur_w, USER_BLUR_BOX_WIDTH_FRACTION)
        if blur_w is not None else USER_BLUR_BOX_WIDTH_FRACTION
    )
    bx_frac = (
        _clamp01(blur_x, (1.0 - bw_frac) / 2.0)
        if blur_x is not None else (1.0 - bw_frac) / 2.0
    )
    if layout is not None:
        cw = int(layout.get("cw") or 0)
        ch = int(layout.get("ch") or 0)
        if cw > 0 and ch > 0:
            pw, ph, ox, oy = _layout_content_box(layout, cw, ch)
            return {
                "x": round((ox + bx_frac * pw) / cw, 4),
                "y": round((oy + by * ph) / ch, 4),
                "w": round(bw_frac * pw / cw, 4),
                "h": round(bh * ph / ch, 4),
            }
    return {
        "x": round(bx_frac, 4),
        "y": round(by, 4),
        "w": round(bw_frac, 4),
        "h": round(bh, 4),
    }


# ── R39: subtitle plate + letterbox geometry ────────────────────────
#
# Two geometry defects from the 2026-11-02 blur/subtitle inspection
# of real production renders:
#  1. The blur plate behind the burned subtitles was a 90%-wide box
#     whose hard vertical edges sat inside the frame; a wider original
#     caption could leak past it. The plate is now a fixed 92%-wide
#     centred band whose height fits the wrapped subtitle lines.
#  2. On letterboxed layouts (blur/black fill — e.g. a 16:9 picture
#     centred in a 9:16 canvas) the subtitle was sized from the
#     CANVAS and anchored at 85% of the canvas height, so it floated
#     small and disconnected in the bottom padding.
#
# ── Batch P (2026-11-04): recapter-standard geometry ────────────────
#
# The R39 letterbox rule anchored the subtitle at the content box's
# lower rim and IGNORED the user's sub_y there, while the Mini App
# preview honoured sub_y literally as a percent of the canvas — the
# live parity test (sub_y=55%, 22pt) rendered at ~65% of the canvas
# and visibly smaller than previewed. The geometry now follows the
# recapter standard (VVin01/recapter): coordinates are relative to
# the PLACED PICTURE, and the font size is ONE height-relative
# formula shared by preview and render.
#  * On letterboxed layouts sub_y is a percent of the CONTENT
#    (picture) height: the subtitle centre sits at oy + sub_y × ph.
#    Full-frame layouts keep the historical meaning (sub_y of the
#    canvas height).
#  * subtitle_font_px maps the picker size to a fixed fraction of
#    the CANVAS height (13→2.6%, 16→3.2%, 19→4.0%, 22→5.0%) with NO
#    content-box scaling, so the four picker sizes always render at
#    four distinct sizes and the preview can show the same number.

SUBTITLE_PLATE_WIDTH_FRACTION = 0.92
SUBTITLE_PLATE_SAFE_BOTTOM_FRACTION = 0.92  # full-frame plate bottom keeps an ~8% canvas margin


def _layout_content_box(layout: dict[str, Any] | None, cw: int, ch: int) -> tuple[int, int, int, int]:
    """(pw, ph, ox, oy) of the picture inside the canvas."""
    lay = layout or {}
    pw = int(lay.get("pw") or cw)
    ph = int(lay.get("ph") or ch)
    ox = int(lay.get("ox") or 0)
    oy = int(lay.get("oy") or 0)
    return pw, ph, ox, oy


def content_rim_y(layout: dict[str, Any] | None, cw: int, ch: int) -> int | None:
    """Y (px, from canvas top) of the content box's lower rim when the
    layout leaves padding BELOW the picture (letterbox); else None."""
    pw, ph, ox, oy = _layout_content_box(layout, cw, ch)
    rim = oy + ph
    return rim if rim < ch - 4 else None


# Picker size (pt) → fraction of the PICTURE height (Batch AK;
# Batch P/Q measured it against the canvas — see subtitle_font_px).
# One shared table: subtitle_font_px below and the Mini App preview
# (subtitleFontFraction in app.js) both read it, so the size the
# user sees is the size the render burns. Batch Q raised every
# anchor one notch (16pt default 3.2% → 4.0%) after the shaping
# S3: recalibrated pt→fraction mapping anchored on the picture's
# SHORTER side min(picture_width, picture_height) so relative glyph
# size stays uniform across 9:16 full-frame, letterbox, and 16:9
# landscape. Default 16pt → 0.050 × 1080 = 54 px (52–60 px sweet spot).
SUBTITLE_FONT_HEIGHT_FRACTIONS = (
    (13, 0.040),
    (16, 0.050),
    (19, 0.060),
    (22, 0.070),
)
SUBTITLE_TWO_LINE_FONT_FACTOR = 1.25
SUBTITLE_FONT_MIN_PX = 18
# S1: rendered-width ceiling ≈ 820–860 px at 1080 width, scaled by picture width.
SUBTITLE_MAX_LINE_WIDTH_AT_1080 = 840


def _subtitle_max_line_width_px(picture_w: int) -> int:
    """S1 visual line-width cap scaled proportionally by render width."""
    return max(120, int(round(max(240, int(picture_w)) * (SUBTITLE_MAX_LINE_WIDTH_AT_1080 / 1080.0))))


def _subtitle_font_fraction(raw_font_size: int) -> float:
    """Height fraction for a picker size, linearly interpolated
    between the table points (extended proportionally outside)."""
    pts = SUBTITLE_FONT_HEIGHT_FRACTIONS
    v = float(raw_font_size or 16)
    if v <= pts[0][0]:
        return pts[0][1] * v / pts[0][0]
    for (p0, f0), (p1, f1) in zip(pts, pts[1:]):
        if v <= p1:
            return f0 + (f1 - f0) * (v - p0) / (p1 - p0)
    (p0, f0), (p1, f1) = pts[-2], pts[-1]
    return f1 + (f1 - f0) * (v - p1) / (p1 - p0)


def subtitle_font_px(cw: int, ch: int, raw_font_size: int = 16, sub_lines: int = 1,
                     layout: dict[str, Any] | None = None) -> int:
    """Subtitle font size in px (S3: anchored on min(picture_width, picture_height)).

    rendered px = min(pw, ph) × the picker's height fraction
    (×1.25 in two-line mode), minimum 18 px. Anchoring on the shorter
    side of the content picture keeps 9:16 full-frame (1080×1920 -> 1080)
    and 16:9 landscape (1920×1080 -> 1080) at matching relative glyph scale,
    while letterboxed pictures (1080×608 -> 608) stay proportional to the
    visible frame instead of ballooning.
    """
    cw = max(240, int(cw))
    ch = max(240, int(ch))
    if layout is not None:
        pw, ph, _ox, _oy = _layout_content_box(layout, cw, ch)
        basis = max(120, min(int(pw), int(ph)))
    else:
        basis = min(cw, ch)
    frac = _subtitle_font_fraction(raw_font_size)
    if int(sub_lines or 1) != 1:
        frac *= SUBTITLE_TWO_LINE_FONT_FACTOR
    return max(SUBTITLE_FONT_MIN_PX, int(round(basis * frac)))


def subtitle_centre_px(cw: int, ch: int, layout: dict[str, Any] | None,
                       sub_y: float | None) -> float | None:
    """Subtitle centre Y in canvas px (from the top) for a sub_y
    fraction, or None when sub_y is None (caller falls back to its
    legacy margin/box/default rules).

    Batch P picture-relative rule: on letterboxed layouts (padding
    below the picture) sub_y is a percent of the CONTENT height —
    centre = oy + sub_y × ph. On full-frame layouts sub_y keeps its
    historical meaning — a percent of the canvas height. The ASS
    writer, the SRT fallback, subtitle_anchor_y and the subtitle
    plate all resolve through this one function so they can never
    drift apart again.
    """
    if sub_y is None:
        return None
    sy = _clamp01(float(sub_y), 0.85)
    if content_rim_y(layout, cw, ch) is not None:
        _pw, ph, _ox, oy = _layout_content_box(layout, cw, ch)
        return oy + sy * ph
    return sy * ch


# ── Batch AK: bottom-edge subtitle anchor (Recapter standard) ─────
#
# The centre-% anchor (subtitle_centre_px above) made the SAME
# settings land at a different effective bottom margin on every
# layout (≈11% of frame height full-frame vs ≈2.5% of picture
# height on the flagship letterbox flow — premium audit S3). The
# Recapter standard anchors the subtitle PLATE to the picture's
# BOTTOM edge: sub_margin is the gap between the plate's bottom
# edge and the picture's bottom edge, as a fraction of the picture
# height (5% default). New clients send sub_margin; every resolver
# below accepts BOTH semantics so pre-AK jobs, stored configs and
# the bot flow keep their historical centre placement untouched.

def _subtitle_line_height_multiplier(sub_lines: int) -> float:
    """Visual text-block height multiplier for 1, 2, or 3 subtitle lines."""
    n = min(3, max(1, int(sub_lines or 1)))
    if n == 1:
        return 1.15
    if n == 2:
        return 2.30
    return 3.65


def _plate_height_px(font_px: int, sub_lines: int, ch: int) -> float:
    """The subtitle plate's fitted height in px for a resolved font
    size — the exact formula subtitle_plate_box has always used
    (wrapped text height + padding, clamped to [1%, 50%] of the
    canvas height). One implementation so the plate box, the PNG
    block top and the anchor resolver can never disagree."""
    text_h = float(font_px) * _subtitle_line_height_multiplier(sub_lines)
    pad = max(10.0, float(font_px) * 0.40)
    return max(0.01 * ch, min(0.50 * ch, text_h + 2.0 * pad))


def subtitle_anchor_centre_px(cw: int, ch: int, layout: dict[str, Any] | None,
                              *, sub_y: float | None = None,
                              sub_margin: float | None = None,
                              raw_font_size: int = 16, sub_lines: int = 1,
                              font_px: int | None = None) -> float | None:
    """The subtitle plate's centre Y in canvas px under EITHER
    anchor semantics. With ``sub_margin`` set (Batch AK clients)
    the plate's bottom edge sits ``sub_margin`` of the picture
    height above the picture's bottom edge, so the margin is the
    same on every layout by construction; the centre is derived
    from the plate's fitted height. Without it, this is exactly
    subtitle_centre_px (legacy centre-% semantics, unchanged).
    Returns None only when both are None."""
    if sub_margin is not None:
        _pw, ph, _ox, oy = _layout_content_box(layout, cw, ch)
        if font_px is None:
            font_px = subtitle_font_px(cw, ch, raw_font_size, sub_lines, layout)
        plate_h = _plate_height_px(int(font_px), sub_lines, ch)
        m = _clamp01(float(sub_margin), 0.05)
        centre = (oy + ph) - m * ph - plate_h / 2.0
        # The plate never leaves the canvas (only binds when the
        # plate is taller than the picture itself).
        centre = max(plate_h / 2.0, min(ch - plate_h / 2.0, centre))
        return centre
    return subtitle_centre_px(cw, ch, layout, sub_y)


# Reference picture height for the saved-settings conversion: the
# flagship 16:9→9:16 layout's picture (1080×608). The plate-height
# fraction is nearly layout-independent (the px floors in the
# formula move it by <0.2pt between 608 and 1920), so one constant
# keeps converted positions within a fraction of a percent of the
# original on any layout the user opens next.
SUB_MARGIN_CONVERT_REF_PICTURE_H = 608


def sub_centre_to_bottom_margin(centre: float, raw_font_size: int = 16,
                                sub_lines: int = 1,
                                picture_h: int = SUB_MARGIN_CONVERT_REF_PICTURE_H) -> float:
    """Convert a legacy centre-% sub_y (fraction of picture height,
    from the top) to the Batch AK bottom-margin fraction that puts
    the plate at the SAME visual position: margin = 1 − centre −
    plate_height/2, with the plate height computed from the user's
    own font size and line count (plate-height-aware). Clamped to
    [0, 0.95]. Used by the one-time settings migration (server
    prefs in api.py; mirrored in app.js for the local snapshot and
    History prefill)."""
    ph = max(120, int(picture_h))
    font_px = subtitle_font_px(ph, ph, raw_font_size, sub_lines, None)
    plate_h = _plate_height_px(font_px, sub_lines, ph)
    margin = 1.0 - float(centre) - (plate_h / ph) / 2.0
    return max(0.0, min(0.95, margin))


def subtitle_centre_x_px(cw: int, ch: int, layout: dict[str, Any] | None,
                         sub_x: float | None) -> float | None:
    """Subtitle centre X in canvas px (from the left) for a sub_x
    fraction, or None when sub_x is None (caller keeps the centred
    placement).

    Batch AG mirror of subtitle_centre_px on the horizontal axis:
    on pillarboxed layouts (padding at the sides, pw < cw) sub_x is
    a percent of the CONTENT width — centre = ox + sub_x × pw. On
    full-width layouts sub_x is a percent of the canvas width. The
    PNG burn, the ASS writer, subtitle_anchor_x and the subtitle
    plate all resolve through this one function so they can never
    drift apart (the Batch P invariant, extended sideways).
    """
    if sub_x is None:
        return None
    sx = _clamp01(float(sub_x), 0.5)
    pw, _ph, ox, _oy = _layout_content_box(layout, cw, ch)
    if pw < cw:
        return ox + sx * pw
    return sx * cw


def _subtitle_overlay_x_expr(cw: int, ch: int, layout: dict[str, Any] | None,
                             sub_x: float | None) -> str:
    """ffmpeg overlay ``x`` expression for a subtitle PNG (Batch AG).

    The default (sub_x None or exactly centred) keeps the historical
    centred expression verbatim — default renders stay byte-identical.
    An explicit sub_x resolves through subtitle_centre_x_px (the one
    shared resolver) and clamps the PNG inside the frame edges.
    """
    if sub_x is None:
        return "(W-w)/2"
    try:
        sx = float(sub_x)
    except (TypeError, ValueError):
        return "(W-w)/2"
    if abs(sx - 0.5) < 1e-9:
        return "(W-w)/2"
    centre_px = subtitle_centre_x_px(cw, ch, layout, sx)
    if centre_px is None:
        return "(W-w)/2"
    return f"max(0,min(W-w,{int(round(centre_px))}-w/2))"


def _sub_w_fraction(overlays: dict[str, Any] | None) -> float:
    """The effective wrap/fit width as a fraction of the picture
    width: overlays["sub_w"] when the user set one, else the
    historical 0.92 the plate and cue budgets were tuned for."""
    try:
        w = float((overlays or {}).get("sub_w") or 0.92)
    except (TypeError, ValueError):
        w = 0.92
    return max(0.10, min(1.0, w))


def _scaled_png_visuals(visuals: dict[str, Any], from_px: int, to_px: int) -> dict[str, Any]:
    """Visuals for a shrunk cue: the stroke scales with the font so
    a fitted cue keeps the preset's perceived outline weight. Same
    size in → the SAME dict out (fitting cues render byte-identical)."""
    if int(to_px) == int(from_px):
        return visuals
    out = dict(visuals)
    out["stroke_px"] = max(
        1, int(round(int(visuals.get("stroke_px", 1)) * int(to_px) / max(1, int(from_px))))
    )
    return out


# ── Batch CX: fixed-size subtitles with meaningful cue splitting ──
#
# Ulfa (2026-11-09): short cues rendered LARGE and long cues SMALL
# because the Batch AG fit shrank the font per cue. The font is now
# LOCKED at the job's chosen size for every cue, subtitles are
# ALWAYS ONE LINE on the PNG burn path, and a cue that overflows at
# the locked size is SPLIT into sequential cues — its time window
# split proportionally to each piece's text share — instead of being
# wrapped or shrunk. Split points are meaningful phrase/clause
# boundaries (punctuation ၊/။, conjunctions and particles); a split
# never lands inside a shaped syllable cluster. The ONLY shrink left
# is a single unbreakable token wider than the canvas at the locked
# size, which keeps the old floor logic for that one cue.

# Extra break markers beyond PARTICLE_SYLS (the vocabulary the cue
# formatter already uses): sentence/clause punctuation and the
# relativizer/quotative particles တာ / တဲ့. Built lazily —
# PARTICLE_SYLS is defined further down this module, after this block.
_CX_BREAK_SUFFIXES: tuple[str, ...] | None = None


def _cx_break_suffixes() -> tuple[str, ...]:
    """The meaningful-break vocabulary, resolved on first use."""
    global _CX_BREAK_SUFFIXES
    if _CX_BREAK_SUFFIXES is None:
        _CX_BREAK_SUFFIXES = ("။", "၊", "တာ", "တဲ့") + tuple(sorted(PARTICLE_SYLS))
    return _CX_BREAK_SUFFIXES


def _cx_is_stranded_particle(tok: str) -> bool:
    """True if ``tok`` is a particle/postposition/punctuation that must not start a line."""
    t = unicodedata.normalize("NFC", (tok or "").strip())
    if not t:
        return False
    return t in STRANDED_LINE_START_PARTICLES or t in _cx_break_suffixes()
# Minimum readable on-screen time (s) for one split piece, enforced
# only when the cue window is long enough to allow it.
_CX_MIN_PIECE_SECONDS = 0.4


def _png_cue_width(renderer, line, font_px, font_path, visuals) -> int:
    """Total rendered PNG width (px) of one subtitle line: ink width
    plus the padding render_lines adds on both sides — the same
    numbers the renderer will use, so a width predicted here is the
    width the burn produces."""
    st = int(visuals.get("stroke_px", 1))
    pad = _png_subs.png_pad_x(visuals, int(font_px), st)
    return renderer.text_width(line, int(font_px), font_path, st) + 2 * pad


def _png_cue_fits(renderer, line, font_px, font_path, visuals, max_width_px) -> bool:
    """True when one line renders within the width budget at the
    given size."""
    return _png_cue_width(renderer, line, font_px, font_path, visuals) <= int(max_width_px)


def _cx_is_break_token(tok: str) -> bool:
    """True when one token IS a break marker: a conjunction/
    particle from the break vocabulary, or ending with sentence/
    clause punctuation.

    Whole-token match only, deliberately: tokenize_burmese_words
    already emits particles as their own tokens, so an endswith
    test would also fire inside a written word (e.g. the verb
    လွှမ်းမိုးနေ ends with နေ) and split mid-word.
    """
    t = (tok or "").strip()
    if not t:
        return False
    if t.endswith("။") or t.endswith("၊"):
        return True
    return t in _cx_break_suffixes()


def _cx_is_meaningful_break(tokens: list[str], idx: int) -> bool:
    """True when a split AFTER tokens[idx] lands on a phrase/clause
    boundary."""
    return _cx_is_break_token(tokens[idx])


def _cx_next_starts_content(tokens: list[str], idx: int) -> bool:
    """True when the piece starting at tokens[idx] begins with a
    content token: skip whitespace, then the next token must not
    itself be a break marker or stranded particle."""
    j = idx
    while j < len(tokens) and not tokens[j].strip():
        j += 1
    if j >= len(tokens):
        return False
    return not _cx_is_break_token(tokens[j]) and not _cx_is_stranded_particle(tokens[j])


def _cx_cut_at_space(tokens: list[str], cut: int) -> bool:
    """True when the boundary BEFORE tokens[cut] sits at a space
    in the source text. tokenize_burmese_words glues a space to
    the END of the preceding token ('တု '), and a standalone
    whitespace token is possible too — accept either shape, on
    either side of the boundary."""
    if cut <= 0 or cut >= len(tokens):
        return True
    prev_tok, next_tok = tokens[cut - 1], tokens[cut]
    if not prev_tok.strip() or not next_tok.strip():
        return True
    return prev_tok != prev_tok.rstrip() or next_tok != next_tok.lstrip()


def _cx_cut_is_word_boundary(tokens: list[str], cut: int) -> bool:
    """True when a cut BEFORE tokens[cut] does not slice a
    space-delimited word in two: the boundary sits at a space, or
    the piece ends with clause punctuation. (Meaningful particle
    boundaries are the caller's preference loop, which runs
    before this is consulted.)"""
    if cut <= 0 or cut >= len(tokens):
        return True
    if _cx_cut_at_space(tokens, cut):
        return True
    return tokens[cut - 1].rstrip().endswith(("။", "၊"))


def _cx_last_space_boundary(tokens: list[str], start: int, end: int) -> int | None:
    """The most recent cut index in (start, end) that sits at a
    space — the last in-span point where a piece can end without
    breaking a word. None when the fitting span holds no space
    boundary at all."""
    for c in range(end - 1, start, -1):
        if _cx_cut_at_space(tokens, c):
            return c
    return None


def _cx_split_line(renderer, line, font_px, font_path, visuals, max_width_px) -> list[str]:
    """Split one over-wide line into pieces that each fit the bound
    at the LOCKED size.

    Greedy, measured with the real font: accumulate word tokens
    (tokenize_burmese_words — a split never lands inside a shaped
    cluster) until the next token would overflow, then cut at the
    last meaningful boundary inside the piece when that still
    leaves a substantial piece (≥40% of the fitting width),
    otherwise at the overflow point. The loop keeps cutting until
    every piece fits. A single token wider than the bound is
    returned whole (the caller's single-token shrink handles it).

    Batch DA (Ulfa, 2026-11-09 — "စာလုံးမကွဲအောင် ပြင်ပါ"): the
    overflow cut is a LAST resort. When it would land inside a
    space-delimited word (the live CX render split ပြုလုပ် and
    တိကျ that way), the cut BACKTRACKS to the most recent space
    boundary inside the fitting span, so the whole word moves to
    the next piece — unless the backtracked piece would fall
    below the same ≥40% substantial floor, because a stub piece
    flashes worse than a broken word; then the overflow cut
    stands. The rule applies to every piece (the loop re-enters
    for the remainder). No dictionary segmenter: "word boundary"
    here means a space, or the existing meaningful boundaries.
    """
    tokens = [t for t in tokenize_burmese_words(line) if t != ""]
    if not tokens:
        return []
    pieces: list[str] = []
    start = 0
    while start < len(tokens):
        # Extend the piece as far as it fits, tracking the last
        # meaningful boundary inside it — and the last CLEAN one
        # (whose next piece starts with a content word, not a
        # stranded particle).
        end = start
        cur = ""
        last_punct: int | None = None
        last_clean_punct: int | None = None
        last_good: int | None = None
        last_clean: int | None = None
        while end < len(tokens):
            cand = cur + tokens[end]
            if cur and not _png_cue_fits(renderer, cand, font_px, font_path, visuals, max_width_px):
                break
            cur = cand
            end += 1
            if end < len(tokens) and _cx_is_meaningful_break(tokens, end - 1):
                is_punct = tokens[end - 1].rstrip().endswith(("။", "၊"))
                last_good = end
                if is_punct:
                    last_punct = end
                if _cx_next_starts_content(tokens, end):
                    last_clean = end
                    if is_punct:
                        last_clean_punct = end
        if end == len(tokens):
            if cur.strip():
                pieces.append(cur.strip())
            break
        if end == start:
            # One token alone overflows the bound: unbreakable.
            pieces.append(tokens[start])
            start += 1
            continue
        # Prefer sentence/clause punctuation (။/၊), then clean meaningful cut,
        # then any meaningful cut, when the piece it leaves is still
        # substantial (≥40% of the fitting width); a sliver piece flashes awkwardly.
        cut = end
        full_w = _png_cue_width(renderer, cur.strip(), font_px, font_path, visuals)
        for cand_cut in (last_clean_punct, last_clean, last_punct, last_good):
            if cand_cut is not None and cand_cut > start:
                cand_w = _png_cue_width(
                    renderer, "".join(tokens[start:cand_cut]).strip(),
                    font_px, font_path, visuals,
                )
                if full_w > 0 and cand_w >= 0.4 * full_w:
                    cut = cand_cut
                    break
        if cut == end and not _cx_cut_is_word_boundary(tokens, end):
            # Batch DA: the overflow cut would slice a word in
            # two. Backtrack to the most recent space boundary in
            # the span so the whole word moves to the next piece —
            # unless that piece would be a stub (<40% of the
            # fitting width), in which case the overflow cut
            # stands as the last resort.
            back = _cx_last_space_boundary(tokens, start, end)
            if back is not None:
                back_w = _png_cue_width(
                    renderer, "".join(tokens[start:back]).strip(),
                    font_px, font_path, visuals,
                )
                if full_w > 0 and back_w >= 0.4 * full_w:
                    cut = back
        # S1: Avoid starting the next piece with a standalone spaced stranded particle
        # (e.g. "... 2026 | မှာ"), backtracking only across a clean space boundary so an
        # unspaced Burmese word/stub keeps its Batch CX/DA boundary behavior.
        if start < cut < len(tokens) and _cx_cut_at_space(tokens, cut):
            nxt_idx = cut
            while nxt_idx < len(tokens) and not tokens[nxt_idx].strip():
                nxt_idx += 1
            if nxt_idx < len(tokens) and _cx_is_stranded_particle(tokens[nxt_idx]):
                head = cut - 1
                while head > start and not tokens[head].strip():
                    head -= 1
                while head > start and not _cx_cut_at_space(tokens, head):
                    head -= 1
                if head > start and _cx_cut_at_space(tokens, head):
                    cand_w = _png_cue_width(
                        renderer, "".join(tokens[start:head]).strip(),
                        font_px, font_path, visuals,
                    )
                    if full_w > 0 and cand_w >= 0.4 * full_w:
                        cut = head
        piece_str = "".join(tokens[start:cut]).strip()
        pieces.append(piece_str)
        start = cut
    return [p for p in pieces if p]


def _cx_shrink_single_token(renderer, line, font_px, font_path, visuals, max_width_px):
    """Shrink ONE unbreakable token stepwise to the floor — the old
    Batch AG loop, single line only. Returns (lines, px, visuals).
    Only called when a single token is wider than the canvas at the
    locked size; at the floor the token ships as-is (it cannot be
    split without breaking a shaped cluster)."""
    floor = SUBTITLE_FONT_MIN_PX
    px = int(font_px)
    while px > floor:
        vis = _scaled_png_visuals(visuals, int(font_px), px)
        if _png_cue_fits(renderer, line, px, font_path, vis, max_width_px):
            return [line], px, vis
        need = _png_cue_width(renderer, line, px, font_path, vis)
        ratio = (int(max_width_px) / need) if need > 0 else 0.9
        factor = max(0.85, min(0.95, ratio * 0.98))
        px = max(floor, min(px - 1, int(px * factor)))
    vis = _scaled_png_visuals(visuals, int(font_px), floor)
    return [line], floor, vis


def fit_png_cue_pieces(renderer, text, font_px, font_path, visuals, max_width_px,
                       cue_start, cue_end, max_lines: int = 1):
    """Batch CX: fit ONE cue for the PNG burn at the LOCKED size.

    Returns a list of (lines, font_px, visuals, start, end) — one
    entry per piece, in order. A cue that already fits is one piece,
    unchanged (same lines, size and visuals as before, so its render
    is byte-identical). An overflowing cue is split at meaningful
    phrase/clause boundaries into one-line pieces, each at the
    locked size; the cue's time window is split proportionally to
    each piece's text share (with a 0.4 s readability floor when the
    window is long enough to allow it). A single unbreakable token
    wider than the canvas keeps the old floor shrink for that one
    piece only.

    Batch DD: with ``max_lines`` > 1 (the user's Lines 1/2/3
    choice), the split lines are wrapped into pieces of up to
    ``max_lines`` lines each instead of always shipping sequential
    one-line pieces — a cue that fits within the chosen line count
    stays ONE piece over its full window. max_lines=1 reproduces
    the CX/DA behaviour byte-identically (the default).
    """
    max_lines = min(3, max(1, int(max_lines or 1)))
    base_lines = _png_subs.normalize_cue_lines(text, max_lines)
    if not base_lines:
        return []
    start_px = int(font_px)
    start = float(cue_start)
    end = float(cue_end)
    if len(base_lines) <= max_lines and all(
        _png_cue_fits(renderer, ln, start_px, font_path, visuals, max_width_px)
        for ln in base_lines
    ):
        return [(list(base_lines), start_px, visuals, start, end)]
    line = " ".join(base_lines)
    raw_pieces = _cx_split_line(renderer, line, start_px, font_path, visuals, max_width_px)
    if not raw_pieces:
        return []
    # Batch DD: group the split lines into ≤max_lines-line pieces.
    groups: list[list[str]] = [
        raw_pieces[i:i + max_lines] for i in range(0, len(raw_pieces), max_lines)
    ]
    total_chars = sum(len(p) for p in raw_pieces) or 1
    total_dur = max(0.0, end - start)
    durs = [
        (sum(len(p) for p in g) / total_chars) * total_dur for g in groups
    ]
    # Readability floor: when the window is long enough, no piece
    # flashes shorter than _CX_MIN_PIECE_SECONDS; the extra time
    # comes out of the longest pieces, total window unchanged.
    if len(durs) > 1 and total_dur >= _CX_MIN_PIECE_SECONDS * len(durs):
        need = sum(max(0.0, _CX_MIN_PIECE_SECONDS - d) for d in durs)
        if need > 0:
            surplus = [max(0.0, d - _CX_MIN_PIECE_SECONDS) for d in durs]
            tot_surplus = sum(surplus)
            if tot_surplus >= need:
                spare = total_dur - _CX_MIN_PIECE_SECONDS * len(durs)
                durs = [
                    _CX_MIN_PIECE_SECONDS + (s / tot_surplus) * spare
                    for s in surplus
                ]
    out: list[tuple[list[str], int, dict[str, Any], float, float]] = []
    cur = start
    for i, group in enumerate(groups):
        piece_end = end if i == len(groups) - 1 else cur + durs[i]
        piece_px, piece_vis = start_px, visuals
        lines_out: list[str] = []
        for ln in group:
            if _png_cue_fits(renderer, ln, start_px, font_path, visuals, max_width_px):
                lines_out.append(ln)
            else:
                # A single unbreakable token wider than the bound:
                # the old floor shrink, for that line only; the
                # whole piece renders at the smallest shrunk size.
                shrunk_lines, shrunk_px, shrunk_vis = _cx_shrink_single_token(
                    renderer, ln, start_px, font_path, visuals, max_width_px
                )
                lines_out.extend(shrunk_lines)
                if shrunk_px < piece_px:
                    piece_px, piece_vis = shrunk_px, shrunk_vis
        out.append((lines_out, piece_px, piece_vis, round(cur, 3), round(piece_end, 3)))
        cur = piece_end
    return out


def fit_png_cue_lines(renderer, text, sub_lines, font_px, font_path,
                      visuals, max_width_px):
    """Batch CX: fit one cue for the PNG burn at the LOCKED size.

    The font never shrinks for a multi-token cue: a cue that
    overflows is split by fit_png_cue_pieces (the production burn
    shows every piece in turn); this single-cue entry point — the
    preview frame, which has no time window to split across —
    returns the FIRST fitting piece at the locked size. A cue that
    already fits renders EXACTLY as before — same lines, same size,
    same visuals (the fast path).

    Batch DD: the piece wraps into up to the chosen line count
    (Lines 1/2/3) at the locked size — sub_lines is honoured again
    (the CX one-line-only rule is superseded by Ulfa's 2026-11-09
    DD order), so the preview frame shows the same wrapped piece
    the production burn will show first.

    Returns (lines, effective_font_px, effective_visuals); lines is
    [] for a cue with no visible text. The only shrink left is a
    single unbreakable token wider than the canvas, which keeps the
    old floor logic for that one cue.
    """
    max_lines = min(3, max(1, int(sub_lines or 1)))
    base_lines = _png_subs.normalize_cue_lines(text, max_lines)
    if not base_lines:
        return [], int(font_px), visuals
    start_px = int(font_px)
    if len(base_lines) <= max_lines and all(
        _png_cue_fits(renderer, ln, start_px, font_path, visuals, max_width_px)
        for ln in base_lines
    ):
        return list(base_lines), start_px, visuals
    pieces = fit_png_cue_pieces(
        renderer, text, font_px, font_path, visuals, max_width_px, 0.0, 1.0,
        max_lines=max_lines,
    )
    if not pieces:
        return [], start_px, visuals
    first_lines, first_px, first_vis, _s, _e = pieces[0]
    return first_lines, first_px, first_vis


def _subtitle_block_top_px(
    cw: int, ch: int, layout: dict[str, Any] | None, *,
    sub_y: float | None, subtitles_margin_v: float | None,
    boxes: list[dict[str, Any]] | None, font_px: int, sub_lines: int,
    block_h: int,
    sub_margin: float | None = None,
) -> int:
    """Top Y (canvas px) for a subtitle block of height ``block_h``
    (Batch Q PNG placement).

    Shares the pre-Q anchor rules so the PNG lands where libass put
    the text: subtitle_centre_px when sub_y is set (Batch P
    picture-relative rule; the block is centred on it); otherwise
    the legacy fallbacks from write_synced_ass_file — explicit
    margin, first blur box, or the 12%-of-height default — with the
    block's bottom edge where those rules placed the text block's
    bottom (they were computed against the estimated text height,
    font_px × 1.15 per line).

    Batch AK: with sub_margin set, the block centres on the
    bottom-edge anchor instead (subtitle_anchor_centre_px) — the
    plate and the text block share the one anchor, as before.
    """
    if sub_margin is not None:
        centre_px = subtitle_anchor_centre_px(
            cw, ch, layout, sub_margin=sub_margin,
            font_px=font_px, sub_lines=sub_lines,
        )
        return int(round(centre_px - block_h / 2.0))
    centre_px = subtitle_centre_px(cw, ch, layout, sub_y)
    if centre_px is not None:
        return int(round(centre_px - block_h / 2.0))
    text_half_height = (font_px * _subtitle_line_height_multiplier(sub_lines)) / 2.0
    if subtitles_margin_v is not None:
        mv_val = float(subtitles_margin_v)
        pixels_from_bottom = mv_val if mv_val > 100 else (mv_val / 288.0) * ch
    elif boxes:
        by = _clamp01(boxes[0].get("y", 0.85))
        pixels_from_bottom = (ch * (1.0 - by)) - text_half_height
    else:
        pixels_from_bottom = max(20, int(round(ch * 0.12)))
    return int(round(ch - pixels_from_bottom - block_h))


def subtitle_plate_box(cw: int, ch: int, layout: dict[str, Any] | None = None, *,
                       raw_font_size: int = 16, sub_lines: int = 1,
                       sub_y: float = 0.85, min_h: float | None = None,
                       sub_x: float | None = None,
                       sub_w: float | None = None,
                       sub_margin: float | None = None) -> dict[str, float]:
    """Canvas-fraction blur plate behind the burned subtitles.

    Fixed ~92%-of-frame-width band, horizontally centred; height fits
    the wrapped subtitle lines (text height + padding) and never
    shrinks below the caller's blur_h (the R37 settle-fit contract —
    the plate grows when the lines need more room). The plate centre
    IS the subtitle centre (subtitle_centre_px): picture-relative
    on letterboxed layouts, the caller's sub_y on full-frame ones —
    where it is additionally clamped so the plate bottom respects
    an ~8% canvas-height safe margin.

    Batch AG: with sub_x / sub_w set, the plate FOLLOWS the text —
    its width derives from sub_w (percent of the PICTURE width, in
    canvas fractions) and its centre X is the subtitle's centre X
    (subtitle_centre_x_px), shifted only as far as needed to stay
    inside the frame. Both None — every pre-AG caller — reproduces
    the historical centred 0.92 band exactly; an explicit sub_w of
    0.92 on a full-width picture reproduces it too. The height is
    unchanged (text-fit; no user height control).

    Batch AK: with sub_margin set, the plate anchors to the
    picture's BOTTOM edge instead (subtitle_anchor_centre_px): its
    bottom edge sits sub_margin of the picture height above the
    picture's bottom rim on every layout, and the legacy safe-bottom
    clamp does not apply — the margin is explicit. Height fitting
    (including the min_h settle contract, growing UPWARD from the
    anchored bottom) and the horizontal rules are unchanged.
    """
    cw = max(320, int(cw))
    ch = max(240, int(ch))
    font_px = subtitle_font_px(cw, ch, raw_font_size, sub_lines, layout)
    text_h_px = font_px * _subtitle_line_height_multiplier(sub_lines)
    pad_px = max(10.0, font_px * 0.40)
    h_frac = (text_h_px + 2.0 * pad_px) / ch
    if min_h:
        h_frac = max(h_frac, float(min_h))
    h_frac = max(0.01, min(0.50, h_frac))
    if sub_w is None:
        w_frac = SUBTITLE_PLATE_WIDTH_FRACTION
    else:
        _pw0, _ph0, _ox0, _oy0 = _layout_content_box(layout, cw, ch)
        w_frac = max(0.05, min(1.0, _clamp01(sub_w, SUBTITLE_PLATE_WIDTH_FRACTION) * _pw0 / cw))
    centre_x_px = subtitle_centre_x_px(cw, ch, layout, sub_x)
    if centre_x_px is None:
        x_frac = (1.0 - w_frac) / 2.0
    else:
        x_frac = max(0.0, min(1.0 - w_frac, centre_x_px / cw - w_frac / 2.0))
    if sub_margin is not None:
        centre_m = subtitle_anchor_centre_px(
            cw, ch, layout, sub_margin=sub_margin,
            raw_font_size=raw_font_size, sub_lines=sub_lines, font_px=font_px,
        )
        y_frac = centre_m / ch
    else:
        centre_px = subtitle_centre_px(cw, ch, layout, sub_y)
        y_frac = (centre_px if centre_px is not None else 0.85 * ch) / ch
        if content_rim_y(layout, cw, ch) is None:
            if y_frac + h_frac / 2.0 > SUBTITLE_PLATE_SAFE_BOTTOM_FRACTION:
                y_frac = SUBTITLE_PLATE_SAFE_BOTTOM_FRACTION - h_frac / 2.0
    half = h_frac / 2.0
    y_frac = max(half, min(1.0 - half, y_frac))
    return {
        "x": round(x_frac, 4),
        "y": round(y_frac, 4),
        "w": round(w_frac, 4),
        "h": round(h_frac, 4),
    }


# ── R40 item 3: one blur band, anchored to the subtitle ─────────────
#
# The complaint job (rc_1793619536_beee63) shipped with sub_y=67 and
# blur_y=85: the subtitle floated at 67% on sharp picture while its
# plate blurred an empty strip at 85%, and the auto-detected source
# band drew a SECOND frosted rectangle. The plate now anchors to the
# subtitle's sub_y (callers pass it into subtitle_plate_box), detected
# bands are reconciled against the plate here, and the pipeline
# asserts the plate contains the subtitle anchor before rendering.


def subtitle_anchor_y(cw: int, ch: int, layout: dict[str, Any] | None = None, *,
                      sub_y: float | None = 0.85, raw_font_size: int = 16,
                      sub_lines: int = 1,
                      sub_margin: float | None = None) -> float:
    """Canvas-fraction centre Y where the burned subtitle will sit.

    Mirrors write_synced_ass_file through subtitle_centre_px: on
    letterboxed layouts sub_y is picture-relative (centre inside
    the content box at oy + sub_y × ph); otherwise it sits at the
    caller's sub_y of the canvas height (85% default). The
    raw_font_size/sub_lines parameters are kept for call-site
    compatibility; the Batch P anchor no longer depends on them.

    Batch AK: with sub_margin set, the centre comes from the
    bottom-edge anchor (subtitle_anchor_centre_px) — here the font
    size and line count DO matter, because the centre is derived
    from the plate's fitted height above the anchored bottom edge.
    """
    cw = max(320, int(cw))
    ch = max(240, int(ch))
    if sub_margin is not None:
        centre_px = subtitle_anchor_centre_px(
            cw, ch, layout, sub_margin=sub_margin,
            raw_font_size=raw_font_size, sub_lines=sub_lines,
        )
        return centre_px / ch
    centre_px = subtitle_centre_px(cw, ch, layout, sub_y)
    if centre_px is None:
        centre_px = 0.85 * ch
    return centre_px / ch


def subtitle_anchor_x(cw: int, ch: int, layout: dict[str, Any] | None = None, *,
                      sub_x: float | None = 0.5) -> float:
    """Canvas-fraction centre X where the burned subtitle will sit.

    Batch AG mirror of subtitle_anchor_y through
    subtitle_centre_x_px: picture-relative on pillarboxed layouts
    (centre inside the content box at ox + sub_x × pw); otherwise
    the caller's sub_x of the canvas width (50% centred default).
    """
    cw = max(320, int(cw))
    ch = max(240, int(ch))
    centre_px = subtitle_centre_x_px(cw, ch, layout, sub_x)
    if centre_px is None:
        centre_px = 0.5 * cw
    return centre_px / cw


def reconcile_source_bands(plate: dict[str, float] | None,
                           bands: list[dict[str, Any]],
                           layout: dict[str, Any] | None,
                           cw: int, ch: int) -> tuple[dict[str, float] | None, list[dict[str, float]]]:
    """Fold auto-detected source-caption bands into the subtitle plate.

    Boxes are canvas fractions with centre-based ``y`` (the blur-box
    convention used by every consumer). Returns ``(plate, extras)``:

    * band centre within (plate_h/2 + band_h/2 + 0.02) of the plate
      centre -> merge: the plate grows to the union rectangle and the
      band is dropped (one frosted band, never two stacked ones);
    * band lying fully inside the letterbox padding (above the
      content box or below its lower rim) -> dropped: that padding is
      already blurred fill, so only the band's hard edge would show;
    * anything else -> kept as the sole extra box.
    """
    extras: list[dict[str, float]] = []
    if plate is None:
        return None, [dict(b) for b in bands]
    plate = dict(plate)
    _pw, _ph, _ox, oy = _layout_content_box(layout, cw, ch)
    content_top = oy / float(ch)
    content_bottom = (oy + _ph) / float(ch)
    for band in bands:
        b = {k: float(band.get(k, 0.0)) for k in ("x", "y", "w", "h")}
        b_top = b["y"] - b["h"] / 2.0
        b_bot = b["y"] + b["h"] / 2.0
        if b_bot <= content_top + 0.002 or b_top >= content_bottom - 0.002:
            continue  # inside the letterbox padding — invisible anyway
        p_top = plate["y"] - plate["h"] / 2.0
        p_bot = plate["y"] + plate["h"] / 2.0
        centre_gap = abs(b["y"] - plate["y"])
        merge_reach = plate["h"] / 2.0 + b["h"] / 2.0 + 0.02
        if centre_gap <= merge_reach:
            new_top = min(p_top, b_top)
            new_bot = max(p_bot, b_bot)
            new_left = min(plate["x"], b["x"])
            new_right = max(plate["x"] + plate["w"], b["x"] + b["w"])
            plate = {
                "x": round(new_left, 4),
                "y": round((new_top + new_bot) / 2.0, 4),
                "w": round(new_right - new_left, 4),
                "h": round(new_bot - new_top, 4),
            }
        else:
            extras.append({k: round(v, 4) for k, v in b.items()})
    return plate, extras


def ensure_plate_contains_anchor(plate: dict[str, float], anchor_y: float,
                                 anchor_x: float | None = None) -> dict[str, float]:
    """Shift the plate (size unchanged) until it contains the
    subtitle anchor centre. Belt-and-braces for item 3: even if a
    future client sends divergent geometry again, the subtitle can
    never ship floating off its plate. Batch AG: the horizontal
    twin — with anchor_x given, the plate also shifts sideways
    (width unchanged) until it contains the anchor's centre X;
    anchor_x=None keeps the pre-AG behaviour exactly."""
    plate = dict(plate)
    top = plate["y"] - plate["h"] / 2.0
    bot = plate["y"] + plate["h"] / 2.0
    if anchor_y < top:
        plate["y"] = anchor_y + plate["h"] / 2.0
    elif anchor_y > bot:
        plate["y"] = anchor_y - plate["h"] / 2.0
    half = plate["h"] / 2.0
    plate["y"] = round(max(half, min(1.0 - half, plate["y"])), 4)
    if anchor_x is not None:
        left = plate["x"]
        right = plate["x"] + plate["w"]
        if anchor_x < left:
            plate["x"] = anchor_x
        elif anchor_x > right:
            plate["x"] = anchor_x - plate["w"]
        plate["x"] = round(max(0.0, min(1.0 - plate["w"], plate["x"])), 4)
    return plate


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
    # Batch AK (premium audit P5): the colour grade is NOT part of
    # the mirror — it used to ride the mirror flag, so turning
    # Mirror off silently un-graded the picture too. The grade is
    # the product's standard look and now applies unconditionally;
    # Mirror is purely the left/right flip its label claims.
    # Mirror-ON renders (the shipped default) are byte-identical.
    color_grade = ",eq=contrast=1.03:brightness=-0.01:saturation=1.05"

    # Batch AK (premium audit P7): the full-frame test uses the
    # CROPPED dims (c_sw/c_sh) — the same dims the fit scale uses —
    # not the uncropped source aspect frame_layout used to test, so
    # the branch decision and the scale can never disagree about
    # the same frame when crop margins are set.
    if lay.get("fill") == "crop" or (not crop_filt and abs(c_sw / c_sh - cw / ch) < 0.01):
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
    result = _run_media([ffmpeg, "-hide_banner", "-encoders"], timeout=PROBE_TIMEOUT_SECONDS, what="ffmpeg encoder probe")
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    encoders: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("V"):
            encoders.add(fields[1])
    return encoders


# Batch AB: the `-encoders` LISTING only proves the binary was built
# with NVENC support — on a GPU-less host (production Render has no
# GPU) every h264_nvenc encode still fails at runtime, and each render
# stage used to pay a doomed encode attempt before falling back.
# nvenc_usable() runs ONE real 1-frame test encode per process per
# ffmpeg binary and caches the verdict, so selection can trust it.
_NVENC_PROBE_CACHE: dict[str, bool] = {}
NVENC_PROBE_TIMEOUT_SECONDS = 30


def nvenc_usable(ffmpeg: str) -> bool:
    """True only when a real 1-frame h264_nvenc encode succeeds."""
    if ffmpeg in _NVENC_PROBE_CACHE:
        return _NVENC_PROBE_CACHE[ffmpeg]
    try:
        result = _run_media(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:r=1:d=0.1",
                "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            timeout=NVENC_PROBE_TIMEOUT_SECONDS,
            what="NVENC capability probe",
        )
        usable = result.returncode == 0
    except Exception:
        usable = False
    _NVENC_PROBE_CACHE[ffmpeg] = usable
    return usable


def _select_render_encoder(ffmpeg: str, checkpoint: dict[str, Any] | None) -> str:
    """Pick the render encoder for a run (Batch AB).

    NVENC counts only when it is both listed AND probe-usable. When
    the probe fails the answer is libx264 UNCONDITIONALLY — a
    checkpoint remembered on a GPU host must not resurrect the doomed
    encoder on a GPU-less one. When the probe passes, the historical
    remembered/preferred logic applies unchanged.
    """
    encoders = ffmpeg_encoders(ffmpeg)
    if "h264_nvenc" not in encoders or not nvenc_usable(ffmpeg):
        return "libx264"
    preferred = "h264_nvenc"
    remembered = str((checkpoint or {}).get("selected_encoder") or "")
    return remembered if remembered in {"h264_nvenc", "libx264"} else preferred


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
    intermediate: bool = False,
) -> dict[str, float]:
    """frame_count (when given) locks the part to exactly that many 30 fps frames.
    layout (frame_layout) is the output ratio / fill; None = the original 16:9 fill.
    When ``intermediate=True`` (Pass 2 overlays will immediately re-encode), uses
    fast high-fidelity mezzanine settings (ultrafast CRF 16 / NVENC p1 CQ 16) and
    skips redundant ``-movflags +faststart`` file rewrites on scratch segments.
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

    if intermediate:
        if encoder == "h264_nvenc":
            enc_args = ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", "16", "-pix_fmt", "yuv420p"]
        else:
            enc_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p"]
        tail_flags: list[str] = [str(output)]
    else:
        enc_args = encoder_args_for_quality(encoder, render_quality)
        tail_flags = ["-movflags", "+faststart", str(output)]

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
            *enc_args,
            *tail_flags,
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
            *enc_args,
            *tail_flags,
        ]

    result = _run_media(
        cmd,
        timeout=ffmpeg_timeout(max(source_duration, target_duration)),
        what="Video segment render",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Video segment render failed ({encoder}):\n{result.stderr[-4000:]}")
    return metrics

def concat_video_parts(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path, *, total_duration: float | None = None, faststart: bool = True) -> None:
    list_file = work_dir / "video_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy"]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(output))
    result = _run_media(cmd, timeout=ffmpeg_timeout(total_duration), what="Video concat")
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
    duration: float | None = None,
    soft_srt_path: Optional[Path] = None,
) -> bool:
    """Master audio + copy video (and optionally embed UTF-8 mov_text soft CC
    track in ONE FFmpeg pass to avoid a second full MP4 +faststart rewrite).
    Returns True if soft subtitles were embedded in this pass, False otherwise.
    """
    vol_scale = float(volume_percent or 100) / 100.0
    _has_soft = bool(soft_srt_path and Path(soft_srt_path).is_file())

    def _build_cmd(with_soft: bool) -> list[str]:
        if bgm_path and Path(bgm_path).is_file():
            filter_complex = (
                f"[1:a]highpass=f=75,deesser=i=0.45:m=0.5:f=0.5:s=o,equalizer=f=7200:width_type=q:w=2:g=-3.0,lowpass=f=9500,acompressor=threshold=-18dB:ratio=2.5:attack=15:release=120:makeup=1.3,volume={vol_scale:.3f}[v_master];"
                f"[v_master]asplit=2[v_out][v_sc];"
                f"[2:a]volume={bgm_volume:.3f},aloop=loop=-1:size=2e+09[bgm_loop];"
                f"[bgm_loop][v_sc]sidechaincompress=threshold=0.08:ratio=4:attack=50:release=350[bgm_ducked];"
                f"[v_out][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2,alimiter=limit=0.95:level=disabled[a_final]"
            )
            c = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video), "-i", str(audio), "-i", str(bgm_path),
            ]
            if with_soft:
                c += ["-sub_charenc", "UTF-8", "-i", str(soft_srt_path)]
            c += ["-map", "0:v:0", "-map", "[a_final]"]
            if with_soft:
                c += ["-map", "3:0"]
            c += ["-map_metadata", "-1", "-c:v", "copy", "-filter_complex", filter_complex, "-c:a", "aac", "-b:a", "192k"]
            if with_soft:
                c += ["-c:s", "mov_text", "-metadata:s:s:0", "language=mya", "-metadata:s:s:0", "title=Myanmar"]
            c += ["-shortest", "-movflags", "+faststart", str(output)]
            return c
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
        c = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
        ]
        if with_soft:
            c += ["-sub_charenc", "UTF-8", "-i", str(soft_srt_path)]
        c += ["-map", "0:v:0", "-map", "1:a:0"]
        if with_soft:
            c += ["-map", "2:0"]
        c += ["-map_metadata", "-1", "-c:v", "copy", "-af", af_str, "-c:a", "aac", "-b:a", "192k"]
        if with_soft:
            c += ["-c:s", "mov_text", "-metadata:s:s:0", "language=mya", "-metadata:s:s:0", "title=Myanmar"]
        c += ["-shortest", "-movflags", "+faststart", str(output)]
        return c

    result = _run_media(_build_cmd(_has_soft), timeout=ffmpeg_timeout(duration), what="Final mux")
    if result.returncode != 0 and _has_soft:
        # Fail-open retry without soft subtitle track so a malformed SRT never kills the render.
        result = _run_media(_build_cmd(False), timeout=ffmpeg_timeout(duration), what="Final mux")
        if result.returncode != 0:
            raise RuntimeError(f"Final mux failed:\n{result.stderr[-4000:]}")
        return False
    if result.returncode != 0:
        raise RuntimeError(f"Final mux failed:\n{result.stderr[-4000:]}")
    return _has_soft



def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _load_title_font(font_size: int, font_path: str = ""):
    """The title renderer's font resolution (bundled Myanmar fonts
    first) — shared by the Pillow renderer and the panel metrics so
    both measure with the SAME font the pixels are drawn with."""
    from PIL import ImageFont

    fonts_dir = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
    cand_fonts = [
        str(font_path or "").strip(),
        str(fonts_dir / "Padauk-Bold.ttf"),
        str(fonts_dir / "Pyidaungsu-Bold.ttf"),
        str(fonts_dir / "NotoSansMyanmar-Bold.ttf"),
        os.getenv("RECAP_MM_FONT", ""),
        "/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf",
        r"C:\Windows\Fonts\mmrtext.ttf",
        r"C:\Windows\Fonts\MyanmarText.ttf",
        r"C:\Windows\Fonts\Pyidaungsu.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for cf in cand_fonts:
        if cf and os.path.exists(cf):
            try:
                return ImageFont.truetype(cf, font_size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap_title_lines(text: str, font_size: int, max_width: int, font_path: str = "") -> list[str]:
    """Wrap a title into the lines the Pillow renderer will draw:
    word wrap at spaces, then a syllable-atom rewrap for any line
    still wider than max_width (Myanmar titles often have no
    spaces, so a single 'word' can span the whole string — it used
    to stay whole and overflow the panel). Breaks only at syllable
    boundaries, never inside a shaped cluster."""
    from PIL import Image, ImageDraw

    font = _load_title_font(font_size, font_path)
    dummy = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(dummy)

    def _w(s: str) -> int:
        b = d.textbbox((0, 0), s, font=font)
        return b[2] - b[0]

    lines = [l.strip() for l in str(text or "").split("\n") if l.strip()]
    if not lines:
        lines = [str(text or "").strip()]

    wrapped_lines: list[str] = []
    for line in lines:
        words = line.split(" ")
        curr = ""
        for w in words:
            test = (curr + " " + w).strip()
            if curr and _w(test) > max_width:
                wrapped_lines.append(curr)
                curr = w
            else:
                curr = test
        if curr:
            wrapped_lines.append(curr)

    fitted: list[str] = []
    for l in wrapped_lines:
        if _w(l) <= max_width or len(l) <= 1:
            fitted.append(l)
            continue
        atoms = split_into_burmese_syllables(l) or list(l)
        cur = ""
        for a in atoms:
            cand = cur + a
            if cur and _w(cand) > max_width:
                fitted.append(cur)
                cur = a
            else:
                cur = cand
        if cur:
            fitted.append(cur)
    return [l for l in fitted if l] or [str(text or "").strip()]


def _render_text_with_pillow(
    text: str,
    output: Path,
    font_size: int = 46,
    max_width: int = 1500,
    bg_alpha: float = 0.75,
    border_color: tuple = (255, 215, 0, 180),
    font_path: str = ""
) -> Path:
    """Render Myanmar text onto a transparent PNG badge with dark pill and gold/white styling.

    Batch AK (Addition A): the panel height is computed from the
    REAL per-line ink boxes and each line is drawn at its measured
    bbox offset, so the box always fits the wrapped text — the old
    version allocated max(font_size, ink height) per line but drew
    at the raw anchor, so tall Myanmar stacks on later lines ran
    past the panel's bottom edge and the last line was clipped
    (confirmed on the 2026-11-05 AL verification job's 4-line
    title)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise RuntimeError("Pillow is required for rendering title text. Install with: pip install pillow")

    font = _load_title_font(font_size, font_path)
    wrapped_lines = _wrap_title_lines(text, font_size, max_width, font_path)

    dummy = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(dummy)

    line_bboxes = [d.textbbox((0, 0), l, font=font) for l in wrapped_lines]
    line_widths = [max(1, b[2] - b[0]) for b in line_bboxes]
    line_heights = [max(1, b[3] - b[1]) for b in line_bboxes]
    max_tw = max(line_widths) if line_widths else 200
    spacing = int(font_size * 0.25)
    total_th = sum(line_heights) + (len(wrapped_lines) - 1) * spacing

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
        b = line_bboxes[i]
        lw = line_widths[i]
        # Draw so the line's INK lands in its allocated slot: PIL's
        # anchor sits at the ascender, and the bbox offset (b[0],
        # b[1]) is where the ink actually starts relative to it.
        curr_x = (w - lw) // 2 - b[0]
        draw_y = curr_y - b[1]
        # Drop shadow
        draw.text((curr_x + 2, draw_y + 2), l, font=font, fill=(0, 0, 0, 220))
        # Main text in rich luxury gold
        draw.text((curr_x, draw_y), l, font=font, fill=(255, 225, 120, 255))
        curr_y += line_heights[i] + spacing

    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output), "PNG")
    return output


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
BRAND_WATERMARK_TEXT = "VoiceClonePro"
WATERMARK_STYLES = ("tiled", "logo", "text")
WATERMARK_POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left", "centre")
_BRAND_LOGO_CACHE: Any = None


def _brand_text_sprite(font_file: Path, font_size: int, alpha: int, text: str = BRAND_PATTERN_TEXT) -> Any:
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
    advances = [probe.textlength(ch, font=font) for ch in text]
    asc, desc = font.getmetrics()
    top = probe.textbbox((0, 0), text, font=font)[1]
    width = int(sum(advances) + tracking * (len(text) - 1)) + 2 * ss
    height = asc + desc + 2 * ss
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    shadow_alpha = max(0, min(255, int(alpha * 0.55)))
    for dx, dy, fill in ((ss, ss, (0, 0, 0, shadow_alpha)), (0, 0, (255, 255, 255, alpha))):
        x = float(ss + dx)
        for ch, adv in zip(text, advances):
            d.text((x, ss + dy - top), ch, font=font, fill=fill)
            x += adv + tracking
    return img.resize((max(1, width // ss), max(1, height // ss)), Image.LANCZOS)


def _load_brand_logo_rgba() -> Any:
    """Decode the embedded VoiceClonePro brand logo from app/webapp/static/img/brand-logo.svg."""
    global _BRAND_LOGO_CACHE
    from PIL import Image, ImageDraw

    if _BRAND_LOGO_CACHE is not None:
        return _BRAND_LOGO_CACHE.copy()

    svg_path = Path(__file__).resolve().parent.parent.parent / "webapp" / "static" / "img" / "brand-logo.svg"
    if svg_path.is_file():
        try:
            import base64
            import io

            raw_svg = svg_path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"data:image/[a-zA-Z0-9+.-]+;base64,([A-Za-z0-9+/=\s]+)", raw_svg)
            if m:
                b64_bytes = base64.b64decode(re.sub(r"\s+", "", m.group(1)))
                with Image.open(io.BytesIO(b64_bytes)) as im:
                    _BRAND_LOGO_CACHE = im.convert("RGBA")
                    return _BRAND_LOGO_CACHE.copy()
        except Exception:
            pass

    fallback = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fallback)
    draw.rounded_rectangle([8, 8, 184, 184], radius=40, fill=(24, 24, 38, 240), outline=(255, 215, 0, 255), width=6)
    _BRAND_LOGO_CACHE = fallback
    return fallback.copy()


def _watermark_placement_coords(
    fw: int,
    fh: int,
    sw: int,
    sh: int,
    position: str,
) -> tuple[int, int]:
    """Compute top-left (x, y) inside (fw, fh) for a sprite of size (sw, sh)."""
    pos = str(position or "bottom-right").strip().lower()
    if pos == "center":
        pos = "centre"
    margin_x = max(12, int(round(fw * 0.035)))
    margin_y = max(12, int(round(fh * 0.035)))
    if pos == "top-left":
        x, y = margin_x, margin_y
    elif pos == "top-right":
        x, y = fw - margin_x - sw, margin_y
    elif pos == "bottom-left":
        x, y = margin_x, fh - margin_y - sh
    elif pos == "centre":
        x, y = (fw - sw) // 2, (fh - sh) // 2
    else:  # bottom-right (default)
        x, y = fw - margin_x - sw, fh - margin_y - sh
    return max(0, min(max(0, fw - sw), x)), max(0, min(max(0, fh - sh), y))


def _render_logo_watermark_png(
    output: Path,
    *,
    fw: int,
    fh: int,
    opacity: float,
    position: str,
    size_pct: int,
) -> Path:
    from PIL import Image, ImageDraw

    logo = _load_brand_logo_rgba()
    pct = max(5, min(40, int(size_pct or 16)))
    logo_w = max(24, int(round(fw * (pct / 100.0))))
    resized = logo.resize((logo_w, logo_w), Image.LANCZOS)

    radius = max(4, int(round(logo_w * 0.22)))
    mask = Image.new("L", (logo_w, logo_w), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, logo_w - 1, logo_w - 1], radius=radius, fill=255)

    badge = Image.new("RGBA", (logo_w, logo_w), (0, 0, 0, 0))
    badge.paste(resized, (0, 0), mask)
    ImageDraw.Draw(badge).rounded_rectangle(
        [0, 0, logo_w - 1, logo_w - 1],
        radius=radius,
        outline=(255, 215, 0, 215),
        width=max(2, int(round(logo_w * 0.025))),
    )

    op = max(0.08, min(1.0, float(opacity)))
    if op < 0.999:
        r, g, b, a = badge.split()
        a = a.point(lambda p: int(round(p * op)))
        badge = Image.merge("RGBA", (r, g, b, a))

    canvas = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    dest_x, dest_y = _watermark_placement_coords(fw, fh, logo_w, logo_w, position)
    canvas.alpha_composite(badge, (dest_x, dest_y))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    return output


def _render_text_watermark_png(
    output: Path,
    *,
    fw: int,
    fh: int,
    opacity: float,
    position: str,
    size_pct: int,
) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    pct = max(5, min(40, int(size_pct or 16)))
    target_w = max(48, int(round(fw * (pct / 100.0))))
    font_file = bundled_fonts_dir() / "Padauk-Bold.ttf"
    if not font_file.is_file():
        font_file = bundled_fonts_dir() / "Padauk-Regular.ttf"

    # Estimate font_px to hit target_w for BRAND_WATERMARK_TEXT ("VoiceClonePro")
    probe_draw = ImageDraw.Draw(Image.new("RGBA", (16, 16)))
    test_font = ImageFont.truetype(str(font_file), 64)
    test_w = max(1.0, float(probe_draw.textlength(BRAND_WATERMARK_TEXT, font=test_font)))
    font_px = max(14, min(180, int(round(64.0 * (target_w / test_w)))))

    font = ImageFont.truetype(str(font_file), font_px)
    stroke_w = max(2, int(round(font_px * 0.08)))
    bbox = probe_draw.textbbox((0, 0), BRAND_WATERMARK_TEXT, font=font, stroke_width=stroke_w)
    pad = max(6, stroke_w * 2)
    sw = max(1, (bbox[2] - bbox[0]) + pad * 2)
    sh = max(1, (bbox[3] - bbox[1]) + pad * 2)

    sprite = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(sprite)
    tx = pad - bbox[0]
    ty = pad - bbox[1]
    # Soft drop shadow + crisp white glyphs with dark outline
    d.text(
        (tx + max(1, stroke_w // 2), ty + max(1, stroke_w // 2)),
        BRAND_WATERMARK_TEXT,
        font=font,
        fill=(0, 0, 0, 160),
    )
    d.text(
        (tx, ty),
        BRAND_WATERMARK_TEXT,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke_w,
        stroke_fill=(0, 0, 0, 210),
    )

    op = max(0.08, min(1.0, float(opacity)))
    if op < 0.999:
        r, g, b, a = sprite.split()
        a = a.point(lambda p: int(round(p * op)))
        sprite = Image.merge("RGBA", (r, g, b, a))

    canvas = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    dest_x, dest_y = _watermark_placement_coords(fw, fh, sw, sh, position)
    canvas.alpha_composite(sprite, (dest_x, dest_y))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    return output


def resolve_watermark_overlay_options(overlays: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve effective watermark style, position, opacity, and size from overlays + admin settings."""
    from app.config.settings import get_watermark_config

    ov = overlays or {}
    cfg = get_watermark_config(bool(ov.get("is_vip", False)))

    raw_style = str(ov.get("watermark_style") or cfg["watermark_style"] or "tiled").strip().lower()
    style = raw_style if raw_style in WATERMARK_STYLES else "tiled"

    raw_pos = str(ov.get("watermark_position") or cfg["watermark_position"] or "bottom-right").strip().lower()
    if raw_pos == "center":
        raw_pos = "centre"
    position = raw_pos if raw_pos in WATERMARK_POSITIONS else "bottom-right"

    try:
        size_pct = max(5, min(40, int(ov.get("watermark_size") if ov.get("watermark_size") is not None else cfg["watermark_size"])))
    except (TypeError, ValueError):
        size_pct = 16

    if ov.get("watermark_opacity") is not None:
        try:
            op_pct = max(20, min(100, int(ov["watermark_opacity"])))
        except (TypeError, ValueError):
            op_pct = int(cfg["watermark_opacity"])
        opacity = BRAND_PATTERN_OPACITY if (style == "tiled" and op_pct == 20 and ov.get("watermark_pattern_opacity") in (None, BRAND_PATTERN_OPACITY)) else (op_pct / 100.0)
    elif int(cfg["watermark_opacity"]) != 20:
        opacity = max(20, min(100, int(cfg["watermark_opacity"]))) / 100.0
    elif ov.get("watermark_pattern_opacity") is not None:
        opacity = float(ov["watermark_pattern_opacity"])
    else:
        opacity = BRAND_PATTERN_OPACITY if style == "tiled" else (int(cfg["watermark_opacity"]) / 100.0)

    return {
        "style": style,
        "position": position,
        "opacity": float(opacity),
        "size_pct": int(size_pct),
    }


def render_brand_pattern_png(
    output: Path,
    *,
    frame_width: int,
    frame_height: int,
    opacity: float = BRAND_PATTERN_OPACITY,
    style: str = "tiled",
    position: str = "bottom-right",
    size_pct: int = 16,
) -> Path:
    """Render the recap brand watermark overlay as a frame_width x frame_height RGBA PNG.

    Supports three admin-selectable styles:
    - 'tiled' (default): full-frame diagonal tiled '@voiceclonepro_bot' pattern.
    - 'logo': VoiceClonePro brand logo placed at `position`.
    - 'text': fixed 'VoiceClonePro' wordmark placed at `position`.
    """
    import math

    from PIL import Image

    fw = max(64, int(frame_width))
    fh = max(64, int(frame_height))
    norm_style = str(style or "tiled").strip().lower()
    if norm_style == "logo":
        return _render_logo_watermark_png(
            output,
            fw=fw,
            fh=fh,
            opacity=opacity,
            position=position,
            size_pct=size_pct,
        )
    if norm_style == "text":
        return _render_text_watermark_png(
            output,
            fw=fw,
            fh=fh,
            opacity=opacity,
            position=position,
            size_pct=size_pct,
        )

    opacity = max(0.08, min(1.0, float(opacity)))
    alpha = int(round(255 * opacity))
    font_file = bundled_fonts_dir() / "Padauk-Regular.ttf"
    pct = max(5, min(40, int(size_pct or 16)))
    size_scale = (pct / 16.0) if pct != 16 else 1.0
    font_size = max(12 if pct != 16 else 15, int(round(min(fw, fh) * 0.046 * size_scale)))
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
# quality = encoder settings ONLY (standard ~6–8 Mbps, high ~10–12 Mbps,
# max stays premium — VBV maxrate/bufsize bounds grain spikes).
RENDER_QUALITIES: dict[str, dict[str, list[str]]] = {
    "standard": {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0", "-maxrate", "7200k", "-bufsize", "14400k"],
        "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "7200k", "-bufsize", "14400k", "-threads", "0"],
    },
    "high": {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-maxrate", "11000k", "-bufsize", "22000k"],
        "libx264": ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-maxrate", "11000k", "-bufsize", "22000k", "-threads", "0"],
    },
    "max": {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr", "-cq", "14", "-b:v", "0", "-maxrate", "18000k", "-bufsize", "36000k", "-tune", "hq"],
        "libx264": ["-c:v", "libx264", "-preset", "slow", "-crf", "15", "-maxrate", "18000k", "-bufsize", "36000k", "-threads", "0"],
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


BURMESE_SYLLABLE_BREAK = re.compile(
    r'(?<![္])(?=[က-အ\u1023-\u102A\u104E](?![\u1037\u1038]?[်္]))'
)


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
    'လိုက်တယ်', 'လိုက်တာ', 'တာနဲ့', 'တာမို့', 'တာကြောင့်', 'တဲ့အတွက်',
    'ကို', 'မှာ', 'က', 'နဲ့', 'နှင့်', 'ရဲ့', '၏', 'လို့', 'သည်', '၌', 'တွင်',
    'သို့', 'မှ', 'ဖြင့်', 'လည်း', 'တော့', 'ကနေ', 'နေ', 'ထဲ', 'တုန်း',
}

# S1: particles/postpositions/punctuation that belong to the preceding noun or
# verb and must NEVER appear stranded at the beginning of a wrapped line/cue.
STRANDED_LINE_START_PARTICLES: frozenset[str] = frozenset(
    PARTICLE_SYLS
    | {
        "တာ", "တဲ့", "ပါ", "ပဲ", "တွေ", "များ", "တို့",
        "နော်", "လေ", "လား", "မလား", "ပေါ့", "ဆို", "ဟု", "၍",
        "ကတော့", "မှာတော့", "ကိုတော့", "ဆိုတာ", "ဟာ", "အား",
        "။", "၊", ",", ".", "!", "?", ":", ";", ")", "]", "”", "’",
    }
)


def _init_subtitle_style_presets() -> dict[str, dict[str, Any]]:
    """Build ONE declarative config table for all subtitle presets (S4).

    Consumed uniformly by the PNG renderer (``preset_visuals``), ASS side-file
    writer (``write_synced_ass_file``), API validation, and Mini App preview.
    """
    specs: dict[str, dict[str, Any]] = {
        "yellow": {
            "name": "Classic Gold",
            "label": "အဝါရောင် (Classic Gold)",
            "short": "အဝါရောင်",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFF00",
            "outline_color": "#000000",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 1.3,
            "shadow": 0.8,
        },
        "white": {
            "name": "Clean White",
            "label": "အဖြူရောင် (Clean White)",
            "short": "အဖြူရောင်",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFFFF",
            "outline_color": "#000000",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 2.2,
            "shadow": 1.2,
        },
        "boxed": {
            "name": "Dark Box",
            "label": "နောက်ခံဘောက် (Dark Box)",
            "short": "နောက်ခံဘောက်",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFFFF",
            "outline_color": "#000000",
            "back_col": "&H70000000",  # Semi-transparent black box
            "bg_mode": "box",
            "bg_color": "#000000",
            "bg_opacity": round((255 - 0x70) / 255.0, 4),
            "border_style": 3,
            "outline": 1.0,
            "shadow": 0.0,
        },
        "neon": {
            "name": "Neon Cyan",
            "label": "နီယွန်စိမ်းပြာ (Neon Cyan)",
            "short": "နီယွန်",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#00FFFF",
            "outline_color": "#151515",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 2.2,
            "shadow": 1.2,
        },
        "band": {
            "name": "Band",
            "label": "နောက်ခံ Band (Band)",
            "short": "Band",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFFFF",
            "outline_color": "#000000",
            "back_col": "&H99000000",  # Stronger translucent black band than "boxed"
            "bg_mode": "box",
            "bg_color": "#000000",
            "bg_opacity": round((255 - 0x99) / 255.0, 4),
            "border_style": 3,
            "outline": 1.0,
            "shadow": 0.0,
        },
        "heavy": {
            "name": "Heavy Outline",
            "label": "အနားထူ (Heavy Outline)",
            "short": "အနားထူ",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFFFF",
            "outline_color": "#000000",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 4.2,
            "shadow": 2.0,
        },
        "soft": {
            "name": "Soft",
            "label": "နူးညံ့ (Soft)",
            "short": "နူးညံ့",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFFFF",
            "outline_color": "#222222",
            "back_col": "&HA0000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 1.2,
            "shadow": 0.8,
        },
        "rose": {
            "name": "Rose Pink",
            "label": "ပန်းရောင် (Rose Pink)",
            "short": "ပန်းရောင်",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FF69B4",
            "outline_color": "#000000",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 1.6,
            "shadow": 1.0,
        },
        "orange": {
            "name": "Sunset Orange",
            "label": "လိမ္မော်ရောင် (Sunset Orange)",
            "short": "လိမ္မော်ရောင်",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FF8C00",
            "outline_color": "#000000",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 1.6,
            "shadow": 1.0,
        },
        "mint": {
            "name": "Mint Green",
            "label": "ပူစီနံစိမ်း (Mint Green)",
            "short": "ပူစီနံစိမ်း",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#00FF9D",
            "outline_color": "#151515",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 1.8,
            "shadow": 1.0,
        },
        # S4 additions: Cinematic White (default ONLY for brand-new accounts per D2)
        # and Modern Clean (Glass Pill, 58% translucent rounded background).
        "cinematic": {
            "name": "Cinematic White",
            "label": "ရုပ်ရှင်အဖြူ (Cinematic White)",
            "short": "ရုပ်ရှင်အဖြူ",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFFFF",
            "outline_color": "#000000",
            "back_col": "&H80000000",
            "bg_mode": "none",
            "bg_color": "#000000",
            "bg_opacity": 0.0,
            "border_style": 1,
            "outline": 2.5,  # ≈3 px stroke @ 1080p/9:16 16pt (outside glyph fill)
            "shadow": 1.6,
            "shadow_soft": {"alpha": 128, "blur_px": 4, "offset_x": 2, "offset_y": 3},
        },
        "glass": {
            "name": "Modern Clean",
            "label": "မှန်ကြည် (Modern Clean)",
            "short": "မှန်ကြည်",
            "font": "sans",
            "font_weight": "bold",
            "text_color": "#FFFFFF",
            "outline_color": "#000000",
            "back_col": "&H6B000000",  # 58% opacity black pill (255 - 0x6B = 148/255)
            "bg_mode": "pill",
            "bg_color": "#000000",
            "bg_opacity": round((255 - 0x6B) / 255.0, 4),
            "bg_radius_frac": 0.42,
            "bg_pad_x_frac": 0.45,
            "bg_pad_y_frac": 0.24,
            "border_style": 3,
            "outline": 1.0,
            "shadow": 0.0,
        },
    }
    for entry in specs.values():
        t_rgba = _png_subs.hex_to_rgba(entry["text_color"]) or (255, 255, 255, 255)
        o_rgba = _png_subs.hex_to_rgba(entry["outline_color"]) or (0, 0, 0, 255)
        entry["primary"] = _png_subs.rgba_to_ass_colour(t_rgba)
        entry["outline_col"] = _png_subs.rgba_to_ass_colour(o_rgba)
    return specs


SUBTITLE_STYLE_PRESETS: dict[str, dict[str, Any]] = _init_subtitle_style_presets()


def subtitle_style_config(style_key: str | None) -> dict[str, Any]:
    """Return a copy of the canonical style configuration for ``style_key``."""
    key = str(style_key or "yellow").strip().lower()
    cfg = SUBTITLE_STYLE_PRESETS.get(key, SUBTITLE_STYLE_PRESETS["yellow"])
    return dict(cfg)


# Subtitle font options (recap-quality pass, 2026-10-30). Every file is
# bundled in app/assets/fonts/ under the SIL Open Font License 1.1 (see the
# LICENSE-*.txt files beside them), so subtitle burn-in resolves fonts from
# the repo on any host (incl. Render) — never from a system-font assumption.
# "default" is a SELECTOR, not a family: it means "no explicit choice" and
# resolves to DEFAULT_SUBTITLE_FONT below.
#
# 2026-11-01 shaping fix (branch fix/recap-subtitle-shaping): the 2026-10-30
# pass made Padauk the default because the bundled Noto Sans VARIABLE font
# (NotoSansMyanmar.ttf, an fvar build) merged its stacks under this style's
# heavy outline. But Padauk's own cluster rendering — medial-ra as a tall
# detached left stroke, subscript vowels hanging below the baseline — reads
# as detached/mis-shaped glyphs at production caption size (owner verdict
# 2026-11-01 on jobs rc_1793525723_d81691 / rc_1793526555_596395: the SRT
# text is codepoint-perfect, the burned Padauk glyphs look unshaped). The
# verified-good 2026-10-29 render (rc_1793279760_c4bede) burned the STATIC
# Noto Sans Myanmar from the system fonts-noto package. So the "default"
# entry now bundles the STATIC Noto Sans Myanmar Regular+Bold builds (the
# same Debian fonts-noto files the Docker image installs) and is the
# production default again. The variable NotoSansMyanmar.ttf must NEVER be
# staged for libass (merged stacks) — it stays on disk only for the PIL
# title/brand paths that reference it by filename. Padauk and Serif remain
# selectable options.
# Batch Q font choice (Ulfa, 2026-11-04): the registry now carries
# exactly FOUR user-selectable options, every one shaping-verified
# through the Pillow+raqm PNG renderer (the libass-era verdicts in
# the note above no longer bind — raqm shapes Padauk and Pyidaungsu
# clusters correctly at caption size, verified glyph-by-glyph in
# ~/workspace/font-compare). Padauk is the DEFAULT again, this time
# on the strength of the raqm verification. Keys were renamed to
# stable family keys (noto_sans / noto_serif / pyidaungsu); the old
# "default"/"serif" selectors survive as resolve-time aliases so
# stored configs keep working. The variable NotoSansMyanmar.ttf
# stays in NO option's file list (banned by construction).
SUBTITLE_FONT_OPTIONS: dict[str, dict[str, Any]] = {
    "padauk": {
        "name": "Padauk",
        "label": "ပတေက် (Padauk)",
        "short": "Padauk",
        "family": "Padauk",
        "files": ["Padauk-Regular.ttf", "Padauk-Bold.ttf"],
        "license": "OFL-1.1 (LICENSE-Padauk-OFL.txt)",
    },
    "noto_sans": {
        "name": "Noto Sans",
        "label": "Noto Sans",
        "short": "Noto Sans",
        "family": "Noto Sans Myanmar",
        "files": ["NotoSansMyanmar-Regular.ttf", "NotoSansMyanmar-Bold.ttf"],
        "license": "OFL-1.1 (LICENSE-NotoSansMyanmar-OFL.txt)",
    },
    "pyidaungsu": {
        "name": "Pyidaungsu",
        "label": "ပြည်ထောင်စု (Pyidaungsu)",
        "short": "Pyidaungsu",
        "family": "Pyidaungsu",
        "files": ["Pyidaungsu-Bold.ttf"],
        "license": "OFL-1.1 (LICENSE-Pyidaungsu-OFL.txt)",
    },
    "noto_serif": {
        "name": "Noto Serif Myanmar",
        "label": "စာလုံးလှ Serif (Noto Serif)",
        "short": "Serif",
        "family": "Noto Serif Myanmar",
        "files": ["NotoSerifMyanmar-Regular.ttf", "NotoSerifMyanmar-Bold.ttf"],
        "license": "OFL-1.1 (LICENSE-NotoSerifMyanmar-OFL.txt)",
    },
}

# The production default: what burns when the user has NOT explicitly
# chosen a font (empty value or anything unknown). Padauk since the
# Batch Q font choice (2026-11-04); before that Noto Sans Myanmar
# (2026-11-01 shaping fix) — see the registry notes above.
DEFAULT_SUBTITLE_FONT = "padauk"

# Legacy selector aliases → current keys (stored configs, history
# rows and old Mini App snapshots may still carry these values).
# "serif" was an explicit picker choice of Noto Serif Myanmar and
# keeps that meaning. "default" was the OLD picker's label for Noto
# Sans Myanmar and Batch Q aliased it to noto_sans; Batch T (Ulfa,
# 2026-11-04) migrates it to Padauk instead — "Default" was the
# factory setting most savers never actively chose, so they get the
# new premium default. Explicit current keys are unaffected (a
# stored "noto_sans" still means Noto Sans Myanmar).
SUBTITLE_FONT_ALIASES = {
    "default": "padauk",
    "serif": "noto_serif",
}


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

    Accepts a current option key ("noto_serif"), a family name
    ("Noto Serif Myanmar"), or a legacy selector (SUBTITLE_FONT_
    ALIASES: "serif" → noto_serif, "default" → padauk since
    Batch T migrated the old "Default" savers to the new default).
    Empty and unknown values are the un-chosen path and resolve to
    DEFAULT_SUBTITLE_FONT (Padauk since 2026-11-04). A family name
    passed explicitly still round-trips to its own family.
    """
    v = str(value or "").strip()
    if not v:
        return DEFAULT_SUBTITLE_FONT, SUBTITLE_FONT_OPTIONS[DEFAULT_SUBTITLE_FONT]["family"]
    low = v.lower()
    if low in SUBTITLE_FONT_ALIASES:
        key = SUBTITLE_FONT_ALIASES[low]
        return key, SUBTITLE_FONT_OPTIONS[key]["family"]
    if low in SUBTITLE_FONT_OPTIONS:
        return low, SUBTITLE_FONT_OPTIONS[low]["family"]
    for key, opt in SUBTITLE_FONT_OPTIONS.items():
        if low == str(opt["family"]).lower():
            return key, opt["family"]
    return DEFAULT_SUBTITLE_FONT, SUBTITLE_FONT_OPTIONS[DEFAULT_SUBTITLE_FONT]["family"]


def subtitle_font_file(value: str | None, bold: bool = True) -> Path:
    """Bundled static font FILE for a subtitle-font selector (Batch Q).

    The PNG renderer loads fonts by path, so the family-resolution
    lottery disappears entirely. ASS burned with Bold=-1, so the
    Bold build is preferred. Every candidate comes from
    SUBTITLE_FONT_OPTIONS' file lists — all static builds; the
    variable NotoSansMyanmar.ttf is in NO option's list and can
    therefore never be picked here (it stays banned by construction,
    not just by staging discipline).

    Batch DD: ``bold=False`` (the user's bold toggle off) prefers
    the Regular build instead; families with a single build
    (Pyidaungsu Bold) are unaffected either way.
    """
    key, _family = resolve_subtitle_font(value)
    src_dir = bundled_fonts_dir()
    files = list(SUBTITLE_FONT_OPTIONS[key]["files"])
    ordered = sorted(files, key=lambda fn: 0 if ("bold" in fn.lower()) == bool(bold) else 1)
    for fn in ordered:
        p = src_dir / fn
        if p.is_file():
            return p
    # The chosen family's files are missing on disk (should not
    # happen — the registry test pins their presence): fall back to
    # noto_sans and LOG it. A job must never fail over a font
    # (Batch Q font choice, 2026-11-04).
    logging.getLogger(__name__).warning(
        "subtitle font %r files missing in %s — falling back to noto_sans",
        key, src_dir,
    )
    for fn in SUBTITLE_FONT_OPTIONS["noto_sans"]["files"]:
        p = src_dir / fn
        if p.is_file():
            return p
    for fn in SUBTITLE_FONT_OPTIONS[DEFAULT_SUBTITLE_FONT]["files"]:
        p = src_dir / fn
        if p.is_file():
            return p
    raise RuntimeError("no bundled subtitle font file found")


# ── Batch DD: shared subtitle-look resolution ─────────────────────
# The preview composite and the production burn MUST consume the
# same font file and the same visuals for the same overlays dict —
# these helpers are the single resolution point for both.

def overlays_font_path(overlays: dict[str, Any] | None) -> Path:
    """The font FILE a job's subtitles render with.

    A user-uploaded custom font (``sub_font_custom_path``, resolved
    server-side at submit/preview time and existence-checked here)
    wins; on ANY problem it falls back to the chosen built-in —
    a job must never fail over a font. Otherwise the built-in file
    honours the bold toggle (``sub_bold``, default on — the
    historical always-Bold build).
    """
    ov = overlays or {}
    custom = str(ov.get("sub_font_custom_path") or "").strip()
    if custom:
        p = Path(custom)
        if p.is_file():
            return p
        logging.getLogger(__name__).warning(
            "custom subtitle font missing on disk (%s); falling back to built-in", custom
        )
    sel = ov.get("subtitles_font") or ov.get("sub_font")
    return subtitle_font_file(str(sel) if sel else None, bold=bool(ov.get("sub_bold", True)))


def overlays_visual_overrides(overlays: dict[str, Any] | None) -> dict[str, Any] | None:
    """The Batch DD free style controls carried on the overlays dict
    (``sub_visual_overrides``), or None when the job made no custom
    choices — presets then render exactly as before."""
    ov = (overlays or {}).get("sub_visual_overrides")
    return dict(ov) if isinstance(ov, dict) and ov else None


def build_subtitle_visuals(
    preset: dict[str, Any],
    overrides: dict[str, Any] | None,
    canvas_h: int,
    font_px: int,
) -> dict[str, Any]:
    """Preset visuals with the user's free controls folded in — the
    one builder every PNG subtitle surface (production burn,
    preview composite, file-fallback burn) goes through."""
    visuals = _png_subs.preset_visuals(preset, canvas_h, font_px)
    return _png_subs.apply_visual_overrides(visuals, overrides, canvas_h, font_px)


def merge_preset_overrides(
    preset: dict[str, Any], overrides: dict[str, Any] | None
) -> dict[str, Any]:
    """ASS-side twin of build_subtitle_visuals: fold the free
    controls into a SUBTITLE_STYLE_PRESETS-shaped dict (ASS colour
    strings) so the .ass writer describes the same look the PNG
    burn renders."""
    if not overrides:
        return preset
    st = dict(preset)
    fill = _png_subs.hex_to_rgba(overrides.get("text_color"))
    if fill is not None:
        st["primary"] = _png_subs.rgba_to_ass_colour(fill)
    oc = _png_subs.hex_to_rgba(overrides.get("outline_color"))
    if oc is not None:
        st["outline_col"] = _png_subs.rgba_to_ass_colour(oc)
    if overrides.get("outline_width") is not None:
        try:
            st["outline"] = max(0.0, float(overrides["outline_width"]))
        except (TypeError, ValueError):
            pass
    bg = _png_subs.hex_to_rgba(overrides.get("bg_color"))
    opacity = overrides.get("bg_opacity")
    if bg is not None:
        op = 0.75 if opacity is None else max(0.0, min(1.0, float(opacity)))
        if op > 0:
            st["back_col"] = _png_subs.rgba_to_ass_colour(
                (bg[0], bg[1], bg[2], int(round(op * 255)))
            )
            st["border_style"] = 3
        else:
            st["border_style"] = 1
    elif opacity is not None and int(st.get("border_style", 1)) == 3:
        op = max(0.0, min(1.0, float(opacity)))
        if op <= 0:
            st["border_style"] = 1
        else:
            cur = _png_subs.ass_colour_to_rgba(st.get("back_col"))
            st["back_col"] = _png_subs.rgba_to_ass_colour(
                (cur[0], cur[1], cur[2], int(round(op * 255)))
            )
    if overrides.get("shadow") is not None:
        st["shadow"] = 1.2 if overrides["shadow"] else 0.0
    return st


# ── Batch DD: anti-copyright protection levels ────────────────────

PROTECTION_LEVELS = ("off", "standard", "strong")


def normalize_protection_level(value: Any) -> str:
    """Unknown/absent → "standard" (today's behaviour)."""
    v = str(value or "").strip().lower()
    return v if v in PROTECTION_LEVELS else "standard"


def protection_zoom_factor(level: Any, zoom_factor: float) -> float:
    """Effective Micro-Zoom for a protection level: Off forces the
    plain 1.0 frame (no zoom-crop); Standard and Strong keep the
    user's chosen zoom exactly as before Batch DD."""
    if normalize_protection_level(level) == "off":
        return 1.0
    return float(zoom_factor)


def apply_protection_strong(
    ffmpeg: str,
    video_path: Path,
    log: LogFn = _noop,
    render_quality: Any = "standard",
) -> Path:
    """Strong anti-copyright post-process, applied ONCE to the
    finished output:

    * light film grain (noise filter);
    * all input metadata stripped (``-map_metadata -1``).

    Batch DI (owner order): Strong is back to its pre-Batch-DD
    behaviour — it no longer retimes or pitch-shifts the output.
    The DD micro speed factor ([0.98, 1.02] via setpts + asetrate)
    audibly dulled the narration and is removed entirely; the
    audio is re-encoded untouched (no asetrate, no resample trick).

    Uses ``encoder_args_for_quality("libx264", render_quality)`` so
    the added film grain stays within the selected tier's VBV bitrate
    cap instead of spiking to 20+ Mbps at unconstrained CRF 20.

    Any failure returns the ORIGINAL file untouched — a cosmetic
    protection step must never fail a paid job.
    """
    try:
        out = video_path.with_name(video_path.stem + ".prot.mp4")
        vf = "noise=alls=6:allf=t"
        cmd = [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vf", vf,
            "-map_metadata", "-1",
            *encoder_args_for_quality("libx264", render_quality),
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            str(out),
        ]
        res = _run_media(cmd, timeout=RENDER_TIMEOUT_FLOOR_SECONDS, what="protection-strong")
        if res.returncode == 0 and out.is_file() and out.stat().st_size > 10_000:
            log("🛡️ Protection Strong applied (grain, metadata stripped)")
            return out
        log(f"⚠️ Protection Strong failed (rc={res.returncode}); keeping original output")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        return video_path
    except Exception as exc:
        log(f"⚠️ Protection Strong error ({exc}); keeping original output")
        return video_path


def manual_blur_boxes_to_canvas_boxes(
    boxes: Any, layout: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Batch DD: convert the user's manual blur-box LIST from
    picture fractions ({x: left, y: centre, w, h} of the picture —
    the exact basis the Step-2 editor draws and the single-box
    path uses) to canvas-fraction boxes for overlays["blur_boxes"].

    The conversion is source_band_to_canvas_box's — the same
    layout math as detected bands and the single user box, so
    preview == render by construction. A per-box ``strength``
    (4..40) rides along for the overlay pass. At most 5 boxes.
    With no layout the picture is assumed to fill the canvas (the
    plate's no-layout fallback rule)."""
    out: list[dict[str, Any]] = []
    for b in boxes or []:
        if not isinstance(b, dict):
            continue
        if layout is not None and int(layout.get("cw") or 0) > 0:
            cb: dict[str, Any] = dict(source_band_to_canvas_box(b, layout))
        else:
            cb = {
                "x": round(_clamp01(b.get("x"), 0.0), 4),
                "y": round(_clamp01(b.get("y"), 0.85), 4),
                "w": round(_clamp01(b.get("w"), 0.92), 4),
                "h": round(_clamp01(b.get("h"), 0.07), 4),
            }
        if b.get("strength") is not None:
            try:
                cb["strength"] = max(4, min(40, int(b["strength"])))
            except (TypeError, ValueError):
                pass
        out.append(cb)
        if len(out) >= 5:
            break
    return out


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
    "အံ့ဩတကြီး": "အံ့ဩစွာ",  # malformed 'တကြီး' adverb (jobs rc_1793525723 + rc_1793526555, owner report 2026-11-01)
    "စိုးရိမ်တကြီး": "စိုးရိမ်စွာ",  # same malformed 'တကြီး' adverb family (job rc_1793526555)
    "အန္တရာယ်ကြားဆီ": "အန္တရာယ်ထဲကို",  # broken construction (job rc_1793525723) — the natural form is ထဲကို
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


def _escape_ass_cue_text(text: str) -> str:
    """Batch AJ / L1: escape cue text for an ASS Dialogue line.

    The .ass file ships to users as a downloadable side file and
    libass players honour {...} override tags in Dialogue text, so
    cue text (which can come from user script edits) must never
    carry live tags: backslashes are doubled FIRST (a cue's literal
    ``\\N`` must not become a hard line break, nor a trailing ``\\``
    eat the next character), then braces are backslash-escaped so
    ``{\\pos(0,0)}`` / ``{\\fs999}`` render as literal text instead
    of repositioning / resizing the subtitle. The writer's OWN tags
    (the Batch AG \\pos tag) are generated separately and never pass
    through here; real newlines in the cue are converted to \\N by
    the caller AFTER this escaping, so those breaks stay live.
    """
    escaped = str(text).replace("\\", "\\\\")
    escaped = escaped.replace("{", "\\{").replace("}", "\\}")
    return escaped


def _karaoke_ass_line(c_text: str, dur_sec: float) -> str:
    """Wrap space/word tokens in ASS progressive karaoke \\kf tags."""
    tokens = [t for t in re.split(r"(\s+)", c_text) if t]
    if not tokens:
        return c_text
    total_cs = max(10, int(round(max(0.1, float(dur_sec)) * 100.0)))
    word_weights = [max(1, len(re.sub(r"\s+", "", tok))) if tok.strip() else 0 for tok in tokens]
    w_sum = sum(word_weights) or 1
    out: list[str] = []
    spent = 0
    last_word_idx = max((idx for idx, w in enumerate(word_weights) if w > 0), default=-1)
    for idx, (tok, w) in enumerate(zip(tokens, word_weights)):
        if w <= 0:
            out.append(tok)
            continue
        if idx == last_word_idx:
            cs = max(1, total_cs - spent)
        else:
            cs = max(1, int(round((w / w_sum) * total_cs)))
            spent += cs
        out.append(f"{{\\kf{cs}}}{tok}")
    return "".join(out)


def write_synced_ass_file(
    cues: list[tuple[float, float, str]],
    ass_path: Path,
    cw: int,
    ch: int,
    font_name: str = "",
    raw_font_size: int = 16,
    sub_style: str = "yellow",
    sub_lines: int = 1,
    sub_y: float | None = None,
    sub_x: float | None = None,
    subtitles_margin_v: float | None = None,
    boxes: list[dict[str, Any]] | None = None,
    layout: dict[str, Any] | None = None,
    sub_margin: float | None = None,
    sub_visual_overrides: dict[str, Any] | None = None,
    sub_effect: str = "none",
) -> Path:
    """Generate a pixel-accurate ASS subtitle file with complex Myanmar script text shaping."""
    cw = max(320, int(cw))
    ch = max(240, int(ch))
    is_vert = ch > cw
    # Resolve option keys ("serif") and legacy values ("Pyidaungsu") to a
    # bundled family name for libass HarfBuzz mym2 shaping.
    _, font_name = resolve_subtitle_font(font_name)

    # Font size: the one height-relative formula (Batch P — see
    # subtitle_font_px); the layout no longer scales it.
    font_size = subtitle_font_px(cw, ch, raw_font_size, sub_lines, layout)

    # Canvas-normalized subtitle positioning
    # sy is the center of the subtitle line as a fraction from top (0.0 to 1.0).
    # ASS Alignment 2 (bottom-center) positions text baseline at margin_v.
    # To place the subtitle center exactly at sy*ch, subtract half the rendered text height:
    text_half_height = (font_size * _subtitle_line_height_multiplier(sub_lines)) / 2.0
    # Batch AK: sub_margin (bottom-edge anchor) wins over the legacy
    # centre when both ride the overlays dict.
    centre_px = (
        subtitle_anchor_centre_px(
            cw, ch, layout, sub_margin=sub_margin,
            raw_font_size=raw_font_size, sub_lines=sub_lines, font_px=font_size,
        )
        if sub_margin is not None
        else subtitle_centre_px(cw, ch, layout, sub_y)
    )
    if centre_px is not None:
        # Batch P: picture-relative on letterboxed layouts (centre =
        # oy + sub_y × ph), sub_y of the canvas height full-frame —
        # the same anchor the preview shows (subtitle_centre_px is
        # the single implementation both sides mirror).
        pixels_from_bottom = ch - (centre_px + text_half_height)
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

    # Batch AG: an explicit non-centred sub_x pins every dialogue
    # with a \pos tag at the shared resolver's centre X (the
    # baseline Y is exactly where MarginV already places it, so the
    # vertical geometry is untouched). sub_x None or centred emits
    # no tag — the file stays byte-identical to the pre-AG writer.
    pos_tag = ""
    if sub_x is not None and abs(float(sub_x) - 0.5) > 1e-9:
        centre_x_px = subtitle_centre_x_px(cw, ch, layout, sub_x)
        if centre_x_px is not None:
            pos_tag = f"{{\\pos({int(round(centre_x_px))},{ch - margin_v})}}"

    st_key = str(sub_style or "yellow").lower()
    st = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
    # Batch DD: the user's free style controls describe the same
    # look in the side-file ASS as in the PNG burn.
    st = merge_preset_overrides(st, sub_visual_overrides)
    p_col = st["primary"]
    o_col = st["outline_col"]
    b_col = st["back_col"]
    b_style = st["border_style"]
    outl = max(1.5, float(st["outline"]) * (ch / 720.0))
    shad = max(1.0, float(st["shadow"]) * (ch / 720.0))
    # R40 item 4: size-relative guard — no preset may let the outline
    # exceed 8% of the rendered font size per side. The old Classic
    # Gold scaling reached 6.4px on a 44px font (14.5%), strangling
    # Myanmar glyph counters into black knots; the cap keeps every
    # preset at caption-practice weight at every canvas/font size.
    outl = min(outl, max(1.5, 0.08 * font_size))
    shad = min(shad, max(1.0, 0.05 * font_size))
    wrap_style = 2 if sub_lines == 1 else 0
    margin_h = max(20, int(round(cw * 0.04)))

    eff_mode = str(sub_effect or "none").strip().lower()
    sec_col = "&H00FFFFFF" if p_col.upper() != "&H00FFFFFF" else "&H00C8D4E0"
    if eff_mode == "karaoke":
        # Progressive karaoke fills from SecondaryColour -> PrimaryColour; keep Base crisp white and Highlight vivid gold/cyan.
        sec_col = "&H00EAEAEA"
        if p_col.upper() == "&H00FFFFFF":
            p_col = "&H0000D7FF"

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
Style: Default,{font_name},{font_size},{p_col},{sec_col},{o_col},{b_col},-1,0,0,0,100,100,0,0,{b_style},{outl:.2f},{shad:.2f},2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    effect_tag = ""
    if eff_mode == "fade":
        effect_tag = r"{\fad(110,80)}"
    elif eff_mode == "pop":
        effect_tag = r"{\fad(80,60)\t(0,110,\fscx106\fscy106)\t(110,220,\fscx100\fscy100)}"

    dialogues = []
    for st_sec, en_sec, cue_text in cues:
        c_text = strip_burmese_subtitle_punctuation(canonicalize_burmese_text(cue_text))
        if not c_text:
            continue
        # Batch AJ / L1: escape BEFORE the newline conversion below,
        # so the \N breaks the writer itself inserts stay live while
        # the cue's own backslashes/braces are neutralised.
        c_text = _escape_ass_cue_text(c_text)
        if eff_mode == "karaoke":
            lines_k = [c_text] if sub_lines == 1 else re.split(r"\r\n|\n", c_text)
            ass_line_text = r"\N".join(
                _karaoke_ass_line(ln.strip(), max(0.2, (en_sec - st_sec) / max(1, len(lines_k))))
                for ln in lines_k if ln.strip()
            )
        elif sub_lines == 1:
            ass_line_text = re.sub(r'\s+', ' ', c_text.replace("\r\n", " ").replace("\n", " ")).strip()
        else:
            ass_line_text = c_text.replace("\r\n", r"\N").replace("\n", r"\N").strip()
        st_str = _fmt_ass_time(st_sec)
        en_str = _fmt_ass_time(en_sec)
        dialogues.append(f"Dialogue: 0,{st_str},{en_str},Default,,0,0,0,,{pos_tag}{effect_tag}{ass_line_text}")

    ass_path.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")
    return ass_path


def _subtitle_band_boxes(scores, log: LogFn = _noop) -> list[dict[str, float]]:
    """Turn per-frame row glyph scores into subtitle band boxes.

    ``scores`` is an (n_frames, h_px) matrix whose rows are the per-frame
    "bright glyph pixels with a dark outline nearby" density of every
    image row (see detect_source_subtitle_bands). The aggregation is
    Batch AI (2026-11-05), shaped by a proven miss on a real recap
    source (YouTube GlunZpix30I): its caption strip at ~0.91 of the
    picture scored pers 0.33 / mean-agg 0.015 per row — under every
    per-row gate of the old mean-based detector — while a static title
    card mid-picture scored pers 1.00 / agg 0.174 and was the only band
    returned, so the real captions shipped uncovered as mirror-writing.

    Three changes, one per failure mechanism:

    * The temporal aggregate is the 75th percentile, not the mean.
      Dialogue is intermittent — a caption row is empty in most sampled
      frames — so the mean dilutes a genuine band below threshold;
      persistence (fraction of frames over the hit score) already
      measures "how often", the percentile measures "how strong when
      present".
    * Candidates are two-tier: strict rows (pers >= 0.35, agg >= 0.012)
      seed a band, loose rows (pers >= 0.15, agg >= 0.006) extend it.
      One-line and two-line captions put their glyphs on DIFFERENT
      rows, so no single row carries the whole band's evidence; the
      seed rule keeps scene noise from forming bands on its own, and
      the hairline guard (group extent < 0.015 of height) still kills
      thin bright lines, whose neighbours carry no loose evidence.
    * A band whose glyph pattern is (near-)identical in every sample
      is a static overlay — title card, channel banner, watermark —
      not dialogue: dialogue text changes between samples. Static
      bands are dropped ONLY when at least one genuinely changing
      band survives (dyn ratio = mean per-frame score change / mean
      score at the band's peak row; static measured <= 0.10 on the
      GlunZpix30I card vs >= 0.19 for real dialogue). When every band
      is static (looped image, slideshow), all are kept, as before.

    Batch BI (2026-11-06) adds guards against the opposite
    failure — a caption-FREE source ("Me at the zoo", noisy 240p)
    whose scene texture (face, fence rails, jacket) seeded FOUR
    dynamic bands and shipped with full-width blur strips across
    the picture:

    * Text-cluster scoring (in detect_source_subtitle_bands): only
      bright-with-outline pixels in word-stroke-like horizontal
      runs count toward a row — see _frame_glyph_row_scores.
    * Band cap: at most 2 bands ship. Real sources carry one
      caption strip (occasionally two); anything beyond the two
      strongest survivors is detection noise by definition.

    Batch BM (2026-11-06) removes BI's third guard and adds the
    zone rule (Batch BL spec, owner-approved, Option A ordering):

    * REMOVED — the prominence gate (a group's peak-row agg had to
      clear 4x the median agg across ALL rows of the matrix). The
      BL drift investigation proved it kills genuine captions on
      busy real frames: on the production-geometry crop of
      GlunZpix30I the genuine strip's rows seed and group cleanly
      (peak agg 0.0306, pers 0.58) but the frame's median agg is
      0.0222 — a full scene's texture, not a noise floor — so the
      gate's floor of 0.0889 sat ~2.9x above the strip and the
      band died (report: fix-bl-work/report-build.md). A weak
      intermittent strip on a busy frame has a peak/median ratio
      of only ~1.4x, so no purely median-relative gate weak
      enough to pass it is meaningfully a gate.
    * Caption-zone drop, applied BEFORE the cap: a surviving band
      ships only when its whole extent (y ± h/2, source fractions)
      lies inside the caption zone — bottom [0.58, 1.00] as of
      Batch BQ (owner decision 2026-11-06; BM originally also had
      a top zone [0.00, 0.24], removed by BQ — see below; the
      bottom edge is owner-moved, the BL spec's 0.60 left a
      pinned genuine band only 0.0066 of clearance). Source
      captions anchor to the picture's bottom edge; the zoo bands
      sat mid-picture (extents ~0.15–0.28 and ~0.35–0.50 — a talking
      face) and the zone drop kills them by position, which cannot
      misfire on an edge-anchored dialogue strip. The order
      matters (owner Option A, 2026-11-06): a weak strip's peak
      is an order of magnitude below busy-frame texture, so if
      the cap ranked by strength first, out-of-zone texture would
      spend both cap slots and evict the strip — measured on the
      GlunZpix30I crop, where cap-first returned [] (the strip
      ranked 4th of 4 dynamic bands) and zone-first ships it.
      The rule governs static bands too (owner Decision 1: no
      static exemption — a mid-picture static overlay is not
      blurred either; in-zone static bands are still kept exactly
      as Batch AI designed).
    * Batch BQ (2026-11-06) narrows the zone rule to the bottom
      zone only: the top zone is REMOVED. Both top-zone bands
      observed live that day were false positives — the zoo
      re-run's strip across the background above the subject's
      head and the GlunZpix30I crop's [0.000, 0.101] texture
      band — while no genuine top caption was ever observed
      through it. Top-of-frame captions remain coverable by the
      user's manual blur box (explicit boxes are authoritative;
      detection never runs for them). Everything else about the
      detector — cluster scoring, AI floors, static demotion,
      the zone-before-cap ordering, the 2-band cap — is
      unchanged.
    """
    import numpy as np

    S = np.asarray(scores, dtype=float)
    if S.ndim != 2 or S.shape[0] == 0 or S.shape[1] == 0:
        return []
    n_frames, h_px = S.shape
    pers = (S > 0.02).mean(axis=0)
    agg = np.percentile(S, 75, axis=0)
    agg_mean = S.mean(axis=0)
    dyn = np.abs(np.diff(S, axis=0)).mean(axis=0) if n_frames > 1 else np.zeros(h_px)
    strict = (pers >= 0.35) & (agg >= 0.012)
    loose = (pers >= 0.15) & (agg >= 0.006)
    if not strict.any():
        return []
    # Scan the FULL frame height (blur safefix 2026-11-03): source
    # captions also sit at the TOP of the frame in short dramas and
    # social clips, and a scan that started at 40% of the height
    # could never cover them — they ghosted through under the new
    # subtitle line. The persistence/peak thresholds are what reject
    # scene noise, not the scan range.
    gap = max(3, int(0.013 * h_px))
    groups: list[list[int]] = []
    for y in range(h_px):
        if not loose[y]:
            continue
        if groups and y - groups[-1][-1] <= gap:
            groups[-1].append(y)
        else:
            groups.append([y])
    kept: list[tuple[dict[str, float], float, float]] = []
    for grp in groups:
        if not any(strict[y] for y in grp):
            continue  # loose smear with no seed row — scene noise
        peak_row = max(grp, key=lambda y: float(agg[y]))
        if float(agg[peak_row]) < 0.02:
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
        core_mid_y = (top + bot) / 2.0
        # Pad: dialogue subs often have a second line ABOVE the persistent one,
        # and the renderer's zoom shifts the band slightly — cover both, while
        # testing caption-zone membership against core geometry so mid-frame signs are not dragged down.
        padded_top = max(0.0, top - 1.4 * core_h)
        padded_bot = min(1.0, bot + 0.6 * core_h)
        if padded_bot - padded_top > 0.16:  # safety cap: never blur a huge slab on a guess
            mid = (padded_top + padded_bot) / 2.0
            padded_top, padded_bot = mid - 0.08, mid + 0.08
        if core_mid_y < 0.58 and padded_bot >= 0.58:
            # Core lies outside the caption zone — reject before padding masks it.
            continue
        top, bot = padded_top, padded_bot
        box = {"x": 0.0, "y": round((top + bot) / 2.0, 4), "w": 1.0, "h": round(bot - top, 4)}
        dyn_ratio = float(dyn[peak_row]) / (float(agg_mean[peak_row]) + 1e-9)
        kept.append((box, dyn_ratio, float(agg[peak_row])))
    if not kept:
        return []
    dynamic = [(box, peak) for box, ratio, peak in kept if ratio > 0.10]
    if dynamic and len(dynamic) < len(kept):
        dropped = [box for box, ratio, _peak in kept if ratio <= 0.10]
        log("🔍 Static text band(s) ignored (unchanging across samples — "
            "title-card-like, while dialogue bands were found): "
            + ", ".join(f"y={b['y']:.2f} h={b['h']:.2f}" for b in dropped))
        survivors = dynamic
    else:
        survivors = [(box, peak) for box, _ratio, peak in kept]
    # Batch BM caption-zone drop (Batch BL spec, owner-approved
    # 2026-11-06, Option A ordering): a band ships only when its
    # WHOLE extent lies in a caption zone. This runs BEFORE the
    # cap so out-of-zone texture cannot spend cap slots and evict
    # a weak genuine strip (measured on the GlunZpix30I crop:
    # cap-first returned [], zone-first ships the strip — see the
    # docstring). It governs every band that reaches here, static
    # ones included (owner Decision 1: no static exemption).
    # Batch BQ (owner decision 2026-11-06): the zone is BOTTOM
    # ONLY — BM's top zone [0.00, 0.24] is removed. Both top-zone
    # bands observed live on 2026-11-06 (the zoo re-run's strip
    # above the subject's head and the GlunZpix30I crop's
    # [0.000, 0.101] texture band) were scene texture, never a
    # real caption; genuine top-of-frame captions are covered by
    # the user's manual blur box (an explicit box is
    # authoritative and detection does not run for it anyway).
    def _in_caption_zone(box: dict) -> bool:
        lo = box["y"] - box["h"] / 2.0
        hi = box["y"] + box["h"] / 2.0
        return lo >= 0.58 and hi <= 1.0

    zoned_out = [(b, p) for b, p in survivors if not _in_caption_zone(b)]
    if zoned_out:
        log("🔍 Subtitle band(s) dropped by the caption-zone rule "
            "(Batch BM/BQ — extent outside the bottom "
            "[0.58, 1.00] caption zone): "
            + ", ".join(
                f"y={b['y']:.2f} h={b['h']:.2f} "
                f"(extent {b['y'] - b['h'] / 2.0:.2f}–{b['y'] + b['h'] / 2.0:.2f})"
                for b, _p in zoned_out))
        survivors = [(b, p) for b, p in survivors if _in_caption_zone(b)]
    # Batch BI band cap: at most 2 bands ship — keep the two with
    # the highest peak agg and log what was dropped. (Batch BM: the
    # cap now ranks only caption-zone survivors — see above.)
    if len(survivors) > 2:
        ranked = sorted(survivors, key=lambda bp: bp[1], reverse=True)
        dropped = ranked[2:]
        log("🔍 Extra subtitle band(s) dropped by the 2-band cap "
            "(Batch BI): "
            + ", ".join(f"y={b['y']:.2f} h={b['h']:.2f}" for b, _p in dropped))
        survivors = ranked[:2]
    return [box for box, _peak in sorted(survivors, key=lambda bp: bp[0]["y"])]


def _clustered_glyph_row_scores(mask) -> "np.ndarray":
    """Per-row share of pixels in word-stroke-like horizontal runs.

    ``mask`` is an (h, w) boolean "bright glyph pixel with a dark
    outline nearby" mask. A pixel counts only when its horizontal
    run of mask pixels is at most 14 px long (at the 270-px sample
    scale a subtitle glyph's cross-section is 1-14 px wide, and
    separate glyphs leave gaps). Long continuous runs — fence
    rails, clothing blocks, sky bands — contribute nothing
    (Batch BI).

    The window has NO lower bound above 1: measured on the genuine
    GlunZpix30I caption strip, 53% of the band's mask mass sits in
    1-px runs (anti-aliased thin strokes at sample scale) and none
    in runs over 14 — a 2-px minimum halved the band's peak score
    (0.0241 -> 0.0120) and dropped it under the aggregator's peak
    floor, while the 14-px cap costs genuine bands nothing. Lone
    noise pixels still count, exactly as they did before Batch BI;
    the aggregator's persistence/peak gates handle them."""
    import numpy as np

    m = np.asarray(mask, dtype=bool)
    if m.ndim != 2 or m.shape[0] == 0 or m.shape[1] == 0:
        return np.zeros(0, dtype=float)
    h_px, w_px = m.shape
    padded = np.zeros((h_px, w_px + 2), dtype=np.int8)
    padded[:, 1:-1] = m
    d = np.diff(padded, axis=1)
    counts = np.zeros(h_px, dtype=float)
    for y in range(h_px):
        starts = np.flatnonzero(d[y] == 1)
        if starts.size == 0:
            continue
        ends = np.flatnonzero(d[y] == -1)
        lengths = ends - starts
        counts[y] = float(lengths[lengths <= 14].sum())
    return counts / float(w_px)


def _frame_glyph_row_scores(g) -> "np.ndarray":
    """Row glyph scores for one sampled frame (grayscale array).

    Batch BI: the score counts only bright-with-dark-outline pixels
    that sit in word-stroke-like horizontal runs (see
    _clustered_glyph_row_scores). Before, EVERY such pixel counted,
    so textured scenes (a face, fence rails, a patterned jacket on
    a noisy 240p source) scored like caption text on row after row
    and the aggregator shipped several full-width blur strips for a
    caption-free video (the zoo job, 2026-11-06)."""
    import numpy as np

    bright = g > 175
    dark = g < 110
    near_dark = np.zeros_like(bright)
    for dx in (2, 3, 4):
        near_dark[:, dx:] |= dark[:, :-dx]
        near_dark[:, :-dx] |= dark[:, dx:]
    return _clustered_glyph_row_scores(bright & near_dark)


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
    with a dark outline nearby" (the classic hard-sub look; Batch BI: only pixels in
    word-stroke-like horizontal runs count — see _frame_glyph_row_scores), then aggregate
    the per-frame row scores with _subtitle_band_boxes (Batch AI: percentile aggregate,
    two-tier candidates, static-overlay demotion; Batch BI: 2-band cap; Batch BM:
    prominence gate removed, caption-zone drop added before the cap — see its docstring).
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
        frame_scores: list = []
        for i in range(max(4, samples)):
            t = duration * (i + 0.5) / max(4, samples)
            fp = frame_dir / f"band_{i:02d}.png"
            p = subprocess.run(
                [ffmpeg, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
                 "-v", "error", "-ss", f"{t:.2f}", "-i", str(source),
                 "-frames:v", "1", "-vf", "scale=270:-2", str(fp), "-y"],
                capture_output=True, text=True, timeout=60,
            )
            if p.returncode != 0 or not fp.is_file():
                continue
            g = np.asarray(Image.open(fp).convert("L")).astype(int)
            # Batch BI: text-cluster scoring (see
            # _frame_glyph_row_scores) — only word-stroke-like runs
            # of bright-with-outline pixels count toward a row.
            row_score = _frame_glyph_row_scores(g)
            if frame_scores and row_score.shape != frame_scores[0].shape:
                continue  # inconsistent sample geometry — skip the odd frame
            frame_scores.append(row_score)
        if not frame_scores:
            return []
        boxes = _subtitle_band_boxes(np.stack(frame_scores), log=log)
        if boxes:
            log("🔍 Source subtitle band(s) detected: " + ", ".join(f"y={b['y']:.2f} h={b['h']:.2f}" for b in boxes))
        return boxes
    except Exception as exc:
        log(f"⚠️ Source subtitle band detection skipped: {exc}")
        return []


def _is_combining_or_myanmar_mark(ch: str) -> bool:
    """True for dependent vowel signs, medials, asat, tone marks, and Unicode combining marks."""
    cp = ord(ch)
    if 0x102B <= cp <= 0x103E and cp != 0x1039:
        return True
    if (
        0x1056 <= cp <= 0x1059
        or 0x105E <= cp <= 0x1060
        or 0x1062 <= cp <= 0x1064
        or 0x1067 <= cp <= 0x106D
        or 0x1071 <= cp <= 0x1074
        or 0x1082 <= cp <= 0x108D
        or cp == 0x108F
        or 0x109A <= cp <= 0x109D
    ):
        return True
    return unicodedata.category(ch) in ("Mn", "Mc", "Me")


def myanmar_grapheme_clusters(text: str) -> list[str]:
    """Segment ``text`` into visual grapheme clusters (kinzi + consonant +
    stacked consonants + medials/vowels/asat/tone marks = 1 unit).

    Never breaks inside a Myanmar syllable cluster or combining mark sequence.
    """
    t = unicodedata.normalize("NFC", str(text or ""))
    if not t:
        return []
    clusters: list[str] = []
    n = len(t)
    i = 0
    while i < n:
        # Kinzi (င်္ = 1004 + 103A + 1039) binds forward to its host consonant.
        if (
            i + 3 < n
            and t[i] == "\u1004"
            and t[i + 1] == "\u103A"
            and t[i + 2] == "\u1039"
            and ("\u1000" <= t[i + 3] <= "\u1021" or t[i + 3] == "\u103F")
        ):
            j = i + 4
        else:
            j = i + 1
        while j < n:
            if t[j] == "\u1039" and j + 1 < n:
                j += 2
                continue
            if t[j] == "\u200D" and j + 1 < n:
                j += 2
                continue
            if _is_combining_or_myanmar_mark(t[j]):
                j += 1
                continue
            break
        clusters.append(t[i:j])
        i = j
    return clusters


def grapheme_count(text: str) -> int:
    """Count visual grapheme clusters in ``text`` (ignoring line breaks)."""
    flat = re.sub(r"\s+", " ", str(text or "").replace("\r\n", " ").replace("\n", " ")).strip()
    return len(myanmar_grapheme_clusters(flat))


def _measure_text_width_px(
    line: str,
    font_px: int = 54,
    font_path: Path | None = None,
    stroke_px: int = 2,
) -> int:
    """Measure rendered ink width (px) of a single line using actual font metrics."""
    fp = Path(font_path) if font_path is not None else subtitle_font_file(DEFAULT_SUBTITLE_FONT, bold=True)
    return _png_subs.measure_line_width_px(str(line or ""), max(8, int(font_px)), fp, max(0, int(stroke_px)))


def canonicalize_burmese_text(text: str) -> str:
    """Fix common typing inversions, redundant spaces, and normalize Myanmar Unicode (NFC)."""
    if not text:
        return ""
    # 0. Strip zero-width characters FIRST: they hide inside syllables and
    #    would defeat both the mark-order repair below and the renderer's
    #    shaping. (The old pipeline removed only U+200B..U+200D, at the end.)
    t = re.sub(r'[\u200B-\u200F\uFEFF]', '', text)
    # 0b. A space can never sit between a base consonant and its combining
    #     marks; drop spaces immediately before a combining mark so each
    #     syllable is whole again before it is repaired.
    t = re.sub(r' +(?=[\u102B-\u103E])', '', t)
    # 0c. Initial Unicode NFC normalization before UTN #11 repair.
    t = unicodedata.normalize("NFC", t)
    # 1. Full syllabic mark-order repair (app.services.myanmar_text). This
    #    subsumes the old piecemeal swaps -- pre-posed 'ေ', reversed medials,
    #    'ေ' before medials, ျ/ြ order -- by re-sorting every syllable's
    #    marks into UTN #11 storage order.
    t = repair_burmese_mark_order(t)
    # 2. Normalize foreign homoglyphs and standard Myanmar orthography / spelling.
    t = normalize_burmese_orthography(t)
    # 3. Remove unwanted spaces before punctuation or dependent vowels
    t = re.sub(r'\s+([\u102B-\u103E\u104A\u104B])', r'\1', t)
    # 4. Clean up multiple spaces
    t = re.sub(r'[ \t]+', ' ', t).strip()
    # Normalize double-Myanmar period (။။) → single (။)
    t = re.sub(r'(။)+', '။', t)
    # Normalize multiple Myanmar comma (၊၊) → single (၊)
    t = re.sub(r'(၊)+', '၊', t)
    t = t.rstrip()
    # 5. Final NFC normalization (S6) guarantees canonical order even after
    #    combined rewrites so dotted-circle artifacts do not occur.
    return unicodedata.normalize("NFC", t)


def strip_burmese_subtitle_punctuation(text: str) -> str:
    """Display-only transform for subtitle outputs (PNG burn, SRT, ASS, preview).

    Removes Burmese punctuation marks ``၊`` (U+104A) and ``။`` (U+104B) and
    collapses any redundant spaces left behind, preserving explicit line breaks.
    """
    if not text:
        return ""
    lines_out: list[str] = []
    for raw_line in str(text).replace("\r\n", "\n").split("\n"):
        # Insert a space if ၊/။ separated two non-whitespace characters directly.
        s = re.sub(r"[၊။]+(?=\S)(?![”’\"'\)\]}.,!?])", " ", raw_line)
        s = re.sub(r"[၊။]+", "", s)
        s = re.sub(r"[ \t]+", " ", s).strip()
        if s:
            lines_out.append(s)
    return "\n".join(lines_out)


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
    text = unicodedata.normalize("NFC", str(text or ""))
    tokens = []
    i = 0
    particles = [
        'ပြီးတော့', 'ပြီးနောက်', 'ပြီးရင်', 'တဲ့အခါ', 'ဒါပေမဲ့', 'ဒါပေမယ့်',
        'တာကြောင့်', 'တဲ့အတွက်', 'တာနဲ့', 'တာမို့', 'ကြောင့်', 'အတွက်',
        'ပါတော့တယ်', 'ပါတယ်', 'ခဲ့တယ်', 'တယ်', 'နေတယ်', 'နေတာ',
        'ပါပဲ', 'လိုက်တယ်', 'ကတော့', 'မှာတော့', 'ကိုတော့',
        'က', 'နဲ့', 'နှင့်', 'မှာ', 'ကို', 'တော့', 'ရဲ့', '၏', 'လို့',
        'ပြီး', 'သည်', '၌', 'တွင်', 'မှ', 'သို့', 'ဖြင့်', 'လည်း',
        'ပဲ', 'ပါ', 'တွေ', 'များ', 'တို့', 'လေ', 'နော်', '၊', '။', ' ',
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
            # S1: Never split inside a grapheme cluster (kinzi, stacked consonants,
            # combining marks) or Latin alphanumeric run.
            if text[i].isascii() and text[i].isalnum():
                j = i + 1
                while j < len(text) and text[j].isascii() and text[j].isalnum():
                    j += 1
                next_idx = j
            elif (
                i + 3 < len(text)
                and text[i] == "\u1004"
                and text[i + 1] == "\u103A"
                and text[i + 2] == "\u1039"
                and ("\u1000" <= text[i + 3] <= "\u1021" or text[i + 3] == "\u103F")
            ):
                cl = myanmar_grapheme_clusters(text[i:i + 12])
                next_idx = i + len(cl[0]) if cl else i + 1
            else:
                m = BURMESE_SYLLABLE_BREAK.search(text, i + 1)
                next_idx = m.start() if m else len(text)
                # Ensure token boundary never cuts inside a single grapheme cluster
                clusters = myanmar_grapheme_clusters(text[i:next_idx + 8])
                if clusters and i + len(clusters[0]) > next_idx:
                    next_idx = i + len(clusters[0])
            tokens.append(text[i:next_idx])
            i = next_idx
    return tokens


def _subtitle_max_clusters(
    font_size: int = 16,
    sub_w: float | None = None,
    lines_per_cue: int = 1,
    is_vert: bool = False,
) -> int:
    """Font-size- and width-adaptive S1 grapheme-cluster line budget.

    - At default 16pt (1-line), returns 24 clusters/line (test-pinned target).
    - Smaller font sizes (10..15pt) expand proportionally up to 44 clusters/line;
      larger font sizes (17..26pt) shrink cleanly down to 10 clusters/line.
    - Lines >= 2 apply the 1.25x multiline glyph factor used by subtitle_font_px.
    - Explicit wrap width sub_w scales linearly relative to the 0.77 reference width.
    """
    base = 24
    try:
        fs = int(font_size) if font_size is not None else 16
    except (TypeError, ValueError):
        fs = 16
    if fs != 16:
        ratio = _subtitle_font_fraction(16) / max(0.001, _subtitle_font_fraction(fs))
        base = int(round(base * ratio))
    if int(lines_per_cue or 1) >= 2 and (fs != 16 or sub_w is not None or is_vert):
        base = int(round(base / 1.18))
    if sub_w is not None:
        try:
            w = max(0.10, min(1.0, float(sub_w)))
        except (TypeError, ValueError):
            w = 0.92
        if abs(w - 0.77) > 0.01:
            base = int(round(base * (w / 0.77)))
    return max(8, min(46, base))


def recommended_subtitle_chars_per_line(
    font_size: int = 16,
    lines_per_cue: int = 1,
    sub_w: float | None = None,
    is_vert: bool = True,
) -> int:
    """User-facing character-per-line estimate matching _subtitle_max_clusters."""
    return _subtitle_max_clusters(
        font_size=font_size,
        sub_w=sub_w,
        lines_per_cue=lines_per_cue,
        is_vert=is_vert,
    )


def _subtitle_fit_width_px(overlays: dict[str, Any] | None, picture_w: int) -> int:
    """Hard rendered PNG width cap: uses ``_sub_w_fraction`` (0.92 × picture_w by default)."""
    pw = max(240, int(picture_w))
    return max(120, int(round(_sub_w_fraction(overlays) * pw)))


_LINE_START_CONJUNCTIONS: frozenset[str] = frozenset({"ဒါပေမဲ့", "ဒါပေမယ့်"})
_PACK_EXTRA_END_PARTICLES: frozenset[str] = frozenset({
    "ကတော့", "မှာတော့", "ကိုတော့", "ပါတော့တယ်",
    "တာ", "တဲ့", "ပေါ့", "ပါပဲ", "လေ", "နော်",
})


def _split_line_units_s1(
    text: str,
    *,
    max_chars: int | None = None,
    max_clusters: int | None = None,
    max_width_px: int | None = None,
    font_px: int = 54,
    font_path: Path | None = None,
    stroke_px: int = 2,
    legacy_tail_merge: bool = False,
    pack_full_line: bool = False,
) -> list[str]:
    """Split text into single lines respecting grapheme clusters, word boundaries,
    stranded-particle rules, priority breaks (sentence -> clause/particle ->
    space -> cluster), visual width cap, and cluster/char bounds (S1).

    When ``pack_full_line=True`` (used by the Part D cross-segment packer),
    space-delimited Burmese clauses are tokenized for clean mid-clause fit,
    fill ratio is measured against the target width budget rather than truncated
    fit, and break tiers prioritize filling lines to ~72–95% without leaving
    stranded particles or sentence-starting conjunctions at line ends.
    """
    flat = re.sub(r"\s+", " ", str(text or "").replace("\r\n", " ").replace("\n", " ")).strip()
    if not flat:
        return []

    def _fits(cand: str) -> bool:
        s = cand.strip()
        if not s:
            return True
        if max_chars is not None and len(s) > max_chars:
            return False
        if max_clusters is not None and len(myanmar_grapheme_clusters(s)) > max_clusters:
            return False
        if max_width_px is not None and _measure_text_width_px(s, font_px, font_path, stroke_px) > max_width_px:
            return False
        return True

    if _fits(flat):
        return [flat]

    tokens: list[str] = []
    words = [w for w in flat.split(" ") if w]
    for w_idx, word in enumerate(words):
        has_myanmar = pack_full_line and any("\u1000" <= ch <= "\u109F" for ch in word)
        if has_myanmar or ("။" in word or "၊" in word) or not _fits(word):
            for sub in tokenize_burmese_words(word):
                if not sub:
                    continue
                if not _fits(sub) and len(myanmar_grapheme_clusters(sub)) > 1:
                    tokens.extend(myanmar_grapheme_clusters(sub))
                else:
                    tokens.append(sub)
        else:
            tokens.append(word)
        if w_idx < len(words) - 1:
            tokens.append(" ")
    if not tokens:
        return [flat]

    def _unit_len(s: str) -> int:
        if max_clusters is not None:
            return len(myanmar_grapheme_clusters(s))
        return len(s)

    def _next_nonspace(idx: int) -> str:
        j = idx
        while j < len(tokens) and not tokens[j].strip():
            j += 1
        return tokens[j].strip() if j < len(tokens) else ""

    def _prev_nonspace(end_idx: int, lo: int) -> str:
        j = end_idx - 1
        while j >= lo and not tokens[j].strip():
            j -= 1
        return tokens[j].strip() if j >= lo else ""

    chunks: list[str] = []
    start = 0
    n_tok = len(tokens)
    while start < n_tok:
        while start < n_tok and not tokens[start].strip():
            start += 1
        if start >= n_tok:
            break
        end = start
        cur = ""
        while end < n_tok:
            cand = (cur + tokens[end]).strip()
            if cur.strip() and not _fits(cand):
                break
            cur += tokens[end]
            end += 1
        if end >= n_tok:
            tail = "".join(tokens[start:]).strip()
            if tail:
                chunks.append(tail)
            break
        if end == start:
            chunks.append(tokens[start].strip())
            start += 1
            continue

        full_fit = "".join(tokens[start:end]).strip()
        full_w = max(1, _measure_text_width_px(full_fit, font_px, font_path, stroke_px) if max_width_px is not None else _unit_len(full_fit))
        target_w = max(full_w, int(max_width_px)) if (pack_full_line and max_width_px is not None) else full_w
        best_tier = -1
        best_cut = end

        for c in range(start + 1, end + 1):
            left = "".join(tokens[start:c]).strip()
            if not left:
                continue
            nxt = _next_nonspace(c)
            if nxt and (nxt in STRANDED_LINE_START_PARTICLES or nxt[0] in "။၊.,!?):]”’"):
                if not (pack_full_line and nxt in _LINE_START_CONJUNCTIONS):
                    continue
            prev_ns = _prev_nonspace(c, start)
            at_space = (tokens[c - 1] == " ") or (c < n_tok and tokens[c] == " ")
            if max_width_px is not None:
                lw = _measure_text_width_px(left, font_px, font_path, stroke_px)
            else:
                lw = _unit_len(left)
            fill = lw / target_w

            if pack_full_line:
                is_end_particle = (
                    prev_ns.endswith("၊")
                    or (
                        (prev_ns in PARTICLE_SYLS or prev_ns in _PACK_EXTRA_END_PARTICLES)
                        and prev_ns not in _LINE_START_CONJUNCTIONS
                    )
                )
                dangling_conj = prev_ns in _LINE_START_CONJUNCTIONS
                in_space_word = (
                    (not at_space and not prev_ns.endswith(("။", "၊")) and not is_end_particle and prev_ns not in STRANDED_LINE_START_PARTICLES)
                    or dangling_conj
                )
                if prev_ns.endswith("။") and fill >= 0.55:
                    tier = 7
                elif is_end_particle and fill >= 0.72:
                    tier = 6
                elif at_space and not dangling_conj and fill >= 0.72:
                    tier = 5
                elif prev_ns.endswith("။") and fill >= 0.42:
                    tier = 4
                elif is_end_particle and fill >= 0.55:
                    tier = 3
                elif at_space and not dangling_conj and fill >= 0.55:
                    tier = 2
                elif not in_space_word and fill >= 0.45:
                    tier = 1
                else:
                    tier = 0
            else:
                # Avoid breaking mid-word between non-space tokens inside one unsplit word
                in_space_word = not at_space and not prev_ns.endswith(("။", "၊")) and prev_ns not in PARTICLE_SYLS and prev_ns not in STRANDED_LINE_START_PARTICLES

                if prev_ns.endswith("။") and fill >= 0.30:
                    tier = 4
                elif (prev_ns.endswith("၊") or prev_ns in PARTICLE_SYLS or prev_ns in ("တာ", "တဲ့")) and fill >= 0.35:
                    tier = 3
                elif at_space and fill >= 0.35:
                    tier = 2
                elif not in_space_word:
                    tier = 1
                else:
                    tier = 0

            # Prefer higher priority tier; inside same tier prefer rightmost (higher c).
            if tier > best_tier or (tier == best_tier and c > best_cut):
                best_tier = tier
                best_cut = c

        chunks.append("".join(tokens[start:best_cut]).strip())
        start = best_cut

    if legacy_tail_merge and len(chunks) > 1 and len(chunks[-1]) < 12:
        sep = " " if (chunks[-2] + " " + chunks[-1]) in flat else ""
        merged_tail = (chunks[-2] + sep + chunks[-1]).strip()
        chunks[-2] = merged_tail
        chunks.pop()
    elif len(chunks) > 1 and (max_clusters is not None or max_width_px is not None):
        # Never flash a tiny 1–4 cluster tail if merging back still fits line limits.
        if len(myanmar_grapheme_clusters(chunks[-1])) <= 4:
            sep = " " if (chunks[-2] + " " + chunks[-1]) in flat else ""
            merged_tail = (chunks[-2] + sep + chunks[-1]).strip()
            if _fits(merged_tail):
                chunks[-2] = merged_tail
                chunks.pop()
            elif pack_full_line:
                prev_words = [w for w in chunks[-2].split(" ") if w]
                if len(prev_words) >= 2:
                    moved = prev_words[-1]
                    if moved not in (STRANDED_LINE_START_PARTICLES - _LINE_START_CONJUNCTIONS):
                        cand_prev = " ".join(prev_words[:-1]).strip()
                        cand_last = (moved + sep + chunks[-1]).strip()
                        if len(prev_words) >= 3 and prev_words[-2] in _LINE_START_CONJUNCTIONS:
                            cand_prev2 = " ".join(prev_words[:-2]).strip()
                            cand_last2 = (prev_words[-2] + " " + moved + sep + chunks[-1]).strip()
                            if cand_prev2 and _fits(cand_prev2) and _fits(cand_last2):
                                cand_prev, cand_last = cand_prev2, cand_last2
                        if cand_prev and _fits(cand_prev) and _fits(cand_last):
                            chunks[-2] = cand_prev
                            chunks[-1] = cand_last

    return [c for c in chunks if c] or [flat]


def _split_burmese_into_single_line_chunks(text: str, max_chars: int = 38) -> list[str]:
    """Split text into 1-line chunks without cutting grapheme clusters, words,
    or stranding particles at line starts."""
    return _split_line_units_s1(
        text,
        max_chars=max_chars,
        legacy_tail_merge=True,
    )


def _format_cue_lines_multi(text: str, lines_cnt: int) -> str:
    """Batch DD / S1: balance a cue across 3+ lines without cutting grapheme
    clusters or stranding particles at the start of any wrapped line."""
    tokens = [t for t in tokenize_burmese_words(text) if t != ""]
    if len(tokens) <= 1:
        return text
    n = min(int(lines_cnt), len(tokens))
    weights = [len(myanmar_grapheme_clusters(t)) if t.strip() else 1 for t in tokens]
    total = sum(weights)
    if total <= 0:
        return text
    bonus_after = {
        "။": 18, "၊": 14, "ပြီးတော့": 12, "ပြီးနောက်": 12, "ပြီးရင်": 12,
        "တဲ့အခါ": 12, "ဒါပေမဲ့": 12, "ဒါပေမယ့်": 12, "ကြောင့်": 12, "အတွက်": 12,
        "ပါတယ်": 8, "ခဲ့တယ်": 8, "တယ်": 8, "နေတာ": 8, "ပါပဲ": 8,
        "လိုက်တယ်": 8, "က": 6, "နဲ့": 6, "နှင့်": 6, "မှာ": 6, "ကို": 6,
        "တော့": 6, "ရဲ့": 6, "၏": 6, "လို့": 6, "ပြီး": 6, "သည်": 6,
        "လေ": 6, "နော်": 6,
    }
    cuts: list[int] = []
    start = 0
    for line_idx in range(1, n):
        ideal = total * line_idx / n
        best_cut, best_score = None, None
        cum = sum(weights[:start])
        for k in range(start + 1, len(tokens) - (n - line_idx) + 1):
            cum += weights[k - 1]
            # Veto cuts that would start the next line with a stranded particle.
            j = k
            while j < len(tokens) and not tokens[j].strip():
                j += 1
            nxt = tokens[j].strip() if j < len(tokens) else ""
            penalty = 1000 if nxt in STRANDED_LINE_START_PARTICLES else 0
            prev_ns = tokens[k - 1].strip() or (tokens[k - 2].strip() if k >= 2 else "")
            score = abs(cum - ideal) - bonus_after.get(prev_ns, 0) + penalty
            if best_score is None or score < best_score:
                best_score, best_cut = score, k
        if best_cut is None:
            break
        cuts.append(best_cut)
        start = best_cut
    if not cuts:
        return text
    parts: list[str] = []
    prev = 0
    for c in cuts + [len(tokens)]:
        part = "".join(tokens[prev:c]).strip()
        if part:
            parts.append(part)
        prev = c
    return "\n".join(parts) if len(parts) > 1 else text


def _format_cue_lines(
    text: str,
    lines_cnt: int,
    *,
    max_clusters: int | None = None,
    max_width_px: int | None = None,
    font_px: int = 54,
    font_path: Path | None = None,
    stroke_px: int = 2,
) -> str:
    """Format subtitle text into 1 or 2+ lines respecting grapheme clusters,
    priority breaks, particle attachment, and optional width/cluster caps."""
    text = canonicalize_burmese_text(text)
    if lines_cnt == 1:
        return re.sub(r"\s+", " ", text.replace("\r\n", " ").replace("\n", " ")).strip()

    if lines_cnt >= 3:
        if "\n" in text:
            return text
        return _format_cue_lines_multi(text, lines_cnt)

    if "\n" in text:
        return text

    def _line_ok(s: str) -> bool:
        if max_clusters is not None and len(myanmar_grapheme_clusters(s)) > max_clusters:
            return False
        if max_width_px is not None and _measure_text_width_px(s, font_px, font_path, stroke_px) > max_width_px:
            return False
        return True

    tokens = [t for t in tokenize_burmese_words(text) if t != ""]
    if len(tokens) <= 1:
        return text

    clusters_total = len(myanmar_grapheme_clusters(text))
    half = max(1.0, clusters_total / 2.0)
    strong_ends = ("။", "၊", "ပြီးတော့", "ပြီးနောက်", "ပြီးရင်", "တဲ့အခါ", "ဒါပေမဲ့", "ဒါပေမယ့်", "ကြောင့်", "အတွက်")
    mid_ends = ("ပါတယ်", "ခဲ့တယ်", "တယ်", "နေတာ", "ပါပဲ", "လိုက်တယ်", "က", "နဲ့", "နှင့်", "မှာ", "ကို", "တော့", "ရဲ့", "၏", "လို့", "ပြီး", "သည်")

    best_score: float | None = None
    best_pair: tuple[str, str] | None = None
    fallback_score: float | None = None
    fallback_pair: tuple[str, str] | None = None

    for k in range(1, len(tokens)):
        p1 = "".join(tokens[:k]).strip()
        p2 = "".join(tokens[k:]).strip()
        if not p1 or not p2:
            continue
        # Never break mid-word across glued syllables if spaces exist unless needed
        j = k
        while j < len(tokens) and not tokens[j].strip():
            j += 1
        nxt = tokens[j].strip() if j < len(tokens) else ""
        if nxt in STRANDED_LINE_START_PARTICLES or (nxt and nxt[0] in "။၊.,!?):]”’"):
            continue
        prev_ns = tokens[k - 1].strip() or (tokens[k - 2].strip() if k >= 2 else "")
        c1 = len(myanmar_grapheme_clusters(p1))
        diff = abs(c1 - half)
        if prev_ns.endswith("။"):
            diff -= 14
        elif prev_ns.endswith("၊") or prev_ns in strong_ends:
            diff -= 10
        elif prev_ns in mid_ends:
            diff -= 5
        at_space = (tokens[k - 1] == " ") or (tokens[k] == " ")
        if not at_space and not prev_ns.endswith(("။", "၊")) and prev_ns not in PARTICLE_SYLS:
            diff += 8
        if _line_ok(p1) and _line_ok(p2):
            if best_score is None or diff < best_score:
                best_score = diff
                best_pair = (p1, p2)
        else:
            if fallback_score is None or diff < fallback_score:
                fallback_score = diff
                fallback_pair = (p1, p2)

    chosen = best_pair or fallback_pair
    if chosen is not None:
        return f"{chosen[0]}\n{chosen[1]}"
    return text


# Batch DD: the per-cue character budget table, by canvas
# orientation and lines-per-cue. The 1/2-line entries are the
# legacy table; the 3-line entries extend it at the same per-line
# rate the 2-line entries use (80/2 = 40, 96/2 = 48 per line).
# Batch DJ keeps the HORIZONTAL entries on this legacy table —
# the 16:9 path never re-split (its budgets sit far below true
# capacity); the VERTICAL entries are superseded by the measured
# capacity ladders below and no longer read from this table.
_SUBTITLE_CUE_BUDGETS: dict[bool, dict[int, int]] = {
    True: {1: 45, 2: 80, 3: 120},
    False: {1: 52, 2: 96, 3: 144},
}

# Batch DJ (2026-11-10): MEASURED per-line character capacity of
# Burmese narration at the production burn — PngSubtitleRenderer +
# Padauk-Bold, bound = 0.92 × 1080 = 994 px on the full-frame
# 1080×1920 vertical canvas, over a real narration corpus (the
# planner's own SRT output, the pinned-post spoken script, the CX
# fixture stream), taking greedy token-fit pieces exactly as
# _cx_split_line measures them. Each value is
# floor(0.95 × median measured capacity), nudged down by at most
# 2 chars at the top of the range so the ladder stays STRICTLY
# decreasing per pt (the Batch DI invariant). Two ladders because
# Lines 2/3 burn at the ×1.25 multi-line font (subtitle_font_px),
# whose per-line capacity is smaller than the 1-line font's.
#   pt:            10  11  12  13  14  15  16  17  18  19  20  21  22  23  24  25  26
#   median (1-line font):     47  42  37  35  31  29  27  25  24  22  21  20  19  18  17  16  16
#   median (multi-line font): 36  33  30  27  25  23  22  20  19  18  16  16  15  14  13  12  12
_SUBTITLE_VERTICAL_LINE_CAPACITY: dict[int, int] = {
    10: 44, 11: 39, 12: 35, 13: 33, 14: 29, 15: 27, 16: 25,
    17: 23, 18: 22, 19: 20, 20: 19, 21: 18, 22: 17, 23: 16,
    24: 15, 25: 14, 26: 13,
}
_SUBTITLE_VERTICAL_LINE_CAPACITY_MULTILINE: dict[int, int] = {
    10: 34, 11: 31, 12: 28, 13: 25, 14: 23, 15: 21, 16: 20,
    17: 19, 18: 18, 19: 17, 20: 15, 21: 14, 22: 13, 23: 12,
    24: 11, 25: 10, 26: 9,
}


def _subtitle_max_chars(is_vert: bool, lines_per_cue: int,
                        sub_w: float | None = None,
                        font_size: int | None = None) -> int:
    """Per-cue character budget for split_burmese_subtitle_cues.

    The legacy table — 45/52 chars for one line, 80/96 for two
    (vertical / horizontal canvas) — was tuned for the historical
    92%-of-picture wrap width. Batch AG: sub_w (the user's wrap
    width as a fraction of the picture width) scales the budget
    linearly against that 0.92 reference, so a narrower column
    wraps into more, shorter cues and a wider one into fewer.
    sub_w None or exactly 0.92 reproduces the table byte-identically.

    Batch DD (Ulfa, 2026-11-09 — "size သေးလိုက်လဲ စာကြောင်း
    ရှည်မလာဘူး"): the budget is also SIZE-AWARE. A smaller font
    fits more characters per line, so the budget scales by the
    font-fraction ratio frac(16pt) / frac(size) — vertical 1-line:
    13pt ≈ 56, 16pt = 45, 19pt ≈ 38, 22pt ≈ 32. The same ratio
    applies to every table entry. font_size None (or 16) keeps the
    historical numbers; the CX/DA measured burn fit remains the
    hard safety net either way.

    Batch DJ (2026-11-10): the DD ratio OVERSTATED true vertical
    capacity by ~1.2–1.35× — DI's live matrix showed burned cues
    landing at roughly HALF the SRT cues because the width-true
    burn fit re-split most of them (simulated unsplit rate with
    the DD budgets: only ~14–28%). VERTICAL budgets now come from
    the MEASURED ladders above: budget = lines × per-line
    capacity at the chosen size (≈95% of the median measured
    capacity; font_size None means the 16pt default; sizes clamp
    to the measured 10–26 range), and the simulated unsplit rate
    is ~97–99% across the slider. The horizontal path keeps the
    legacy table + DD ratio scaling unchanged.
    """
    lines = min(3, max(1, int(lines_per_cue or 1)))
    if is_vert:
        try:
            fs = int(font_size) if font_size is not None else 16
        except (TypeError, ValueError):
            fs = 16
        fs = min(26, max(10, fs))
        ladder = (
            _SUBTITLE_VERTICAL_LINE_CAPACITY
            if lines == 1
            else _SUBTITLE_VERTICAL_LINE_CAPACITY_MULTILINE
        )
        base = lines * ladder[fs]
    else:
        base = _SUBTITLE_CUE_BUDGETS[bool(is_vert)][lines]
        if font_size is not None:
            try:
                fs = int(font_size)
            except (TypeError, ValueError):
                fs = 16
            if fs != 16:
                ratio = _subtitle_font_fraction(16) / max(0.001, _subtitle_font_fraction(fs))
                base = max(8, int(round(base * ratio)))
    if sub_w is None:
        return base
    try:
        w = float(sub_w)
    except (TypeError, ValueError):
        return base
    w = max(0.10, min(1.0, w))
    return max(8, int(round(base * w / 0.92)))


def _merge_short_chunks_for_readability(
    chunks: list[str],
    total_dur: float,
    max_chars: int,
    min_dur: float,
    orig_text: str,
) -> list[str]:
    """Merge tiny chunks with an adjacent chunk BEFORE timing when line
    budget allows, avoiding micro-flash cues without stretching timestamps."""
    out = [c for c in chunks if c]
    if len(out) <= 1 or total_dur <= 0.0 or min_dur <= 0.0:
        return out
    max_c = max(8, int(max_chars or 40))
    orig_norm = re.sub(r"\s+", " ", str(orig_text or "")).strip()
    while len(out) > 1:
        tot_chars = sum(len(c) for c in out) or 1
        shortest_idx = -1
        shortest_dur = min_dur
        for idx, ch in enumerate(out):
            est = (len(ch) / tot_chars) * total_dur
            if est < shortest_dur:
                shortest_dur = est
                shortest_idx = idx
        if shortest_idx < 0:
            break
        candidates: list[tuple[int, int, str]] = []
        for nb in (shortest_idx - 1, shortest_idx + 1):
            if 0 <= nb < len(out):
                left, right = (nb, shortest_idx) if nb < shortest_idx else (shortest_idx, nb)
                sep = " " if (out[left] + " " + out[right]) in orig_norm else ""
                merged = (out[left] + sep + out[right]).strip()
                if len(merged) <= max_c:
                    candidates.append((len(merged), left, merged))
        if not candidates:
            break
        candidates.sort(key=lambda item: item[0])
        _, left_idx, merged_text = candidates[0]
        out[left_idx] = merged_text
        out.pop(left_idx + 1)
    return out


def _word_boundary_boundary_fracs(
    units: list[str],
    word_timings: list[dict[str, Any]] | None,
) -> list[float] | None:
    """Derive monotonic internal boundary fractions in (0, 1) from Edge TTS
    WordBoundary timings when they coherently cover `units`."""
    if not word_timings or len(units) < 2:
        return None
    clean_events: list[tuple[float, float, str]] = []
    prev_s = -1.0
    for ev in word_timings:
        if not isinstance(ev, dict):
            continue
        tok = re.sub(r"\s+", "", str(ev.get("text") or ""))
        try:
            st = float(ev.get("start") or 0.0)
            en = float(ev.get("end") or st)
        except (TypeError, ValueError):
            continue
        if not tok or en < st or st < prev_s - 0.05:
            continue
        prev_s = max(prev_s, st)
        clean_events.append((max(0.0, st), max(0.0, st, en), tok))
    if len(clean_events) < 2:
        return None
    wt_max = max(en for _, en, _ in clean_events)
    if wt_max <= 0.1:
        return None

    wt_lens = [len(tok) for _, _, tok in clean_events]
    wt_total = sum(wt_lens)
    unit_lens = [len(re.sub(r"\s+", "", u)) for u in units]
    unit_total = sum(unit_lens)
    if wt_total < 4 or unit_total < 4:
        return None
    ratio = wt_total / float(unit_total)
    if ratio < 0.65 or ratio > 1.35:
        return None

    # Map each cumulative unit end char offset into WordBoundary timestamps
    fracs: list[float] = []
    cum_u = 0
    for idx in range(len(units) - 1):
        cum_u += unit_lens[idx]
        target = (cum_u / float(unit_total)) * wt_total
        seen = 0.0
        boundary_t = None
        for st, en, tok in clean_events:
            tok_l = float(len(tok))
            if seen + tok_l >= target:
                inner = max(0.0, min(1.0, (target - seen) / tok_l)) if tok_l > 0 else 1.0
                boundary_t = st + inner * (en - st)
                break
            seen += tok_l
        if boundary_t is None:
            boundary_t = clean_events[-1][1]
        fracs.append(max(0.0, min(1.0, boundary_t / wt_max)))

    # Validate strict monotonicity and guard against degenerate zero-length cues
    prop_weights = [max(1, len(u)) for u in units]
    prop_total = float(sum(prop_weights))
    prev_f = 0.0
    for i, f in enumerate(fracs):
        prop_share = prop_weights[i] / prop_total
        if f - prev_f < max(0.01, 0.35 * prop_share):
            return None
        prev_f = f
    if 1.0 - prev_f < max(0.01, 0.35 * (prop_weights[-1] / prop_total)):
        return None
    return fracs


def _allocate_cue_times(
    start_sec: float,
    end_sec: float,
    units: list[str],
    word_timings: list[dict[str, Any]] | None = None,
) -> list[tuple[float, float]]:
    """Allocate contiguous cue timestamps spanning [start_sec, end_sec] with
    zero drift and zero artificial time-stretching."""
    start_f = float(start_sec)
    end_f = max(start_f, float(end_sec))
    total_dur = end_f - start_f
    if len(units) <= 1 or total_dur <= 0.0:
        return [(round(start_f, 3), round(end_f, 3)) for _ in units] or [(round(start_f, 3), round(end_f, 3))]

    fracs = _word_boundary_boundary_fracs(units, word_timings)
    if fracs is None:
        # Pause-weighted Burmese timing for Gemini / VoxCPM / Curated voices
        # when explicit WordBoundary timestamps are absent: comma (၊) adds ~0.25s
        # pause weight and full stop (။) adds ~0.45s pause weight.
        weights = [
            max(1, len(u) + (u.count("၊") * 3) + (u.count("။") * 5))
            for u in units
        ]
        tot = float(sum(weights))
        cum = 0
        fracs = []
        for w in weights[:-1]:
            cum += w
            fracs.append(cum / tot)

    r_start = round(start_f, 3)
    r_end = round(end_f, 3)
    out: list[tuple[float, float]] = []
    cur = r_start
    for f in fracs:
        nxt = round(start_f + f * total_dur, 3)
        nxt = max(cur, min(r_end, nxt))
        out.append((cur, nxt))
        cur = nxt
    out.append((cur, r_end))
    return out


# S2: Reading-speed targets (measured in Burmese grapheme clusters per second).
SUBTITLE_CPS_IDEAL_MIN = 8.0
SUBTITLE_CPS_IDEAL_MAX = 12.0
SUBTITLE_CPS_HARD_MAX = 14.0
SUBTITLE_MIN_CUE_DURATION = 1.2
SUBTITLE_GAP_BRIDGE_SEC = 0.2
SUBTITLE_OVERHANG_SEC = 0.2


def cue_cps(duration_sec: float, text: str) -> float:
    """Measure characters-per-second (grapheme clusters / duration) for one cue."""
    dur = max(0.01, float(duration_sec or 0.0))
    return grapheme_count(text) / dur


def measure_cue_reading_speed(
    cues: list[tuple[float, float, str]],
) -> list[dict[str, Any]]:
    """Compute measured S2 reading-speed metrics per cue."""
    out: list[dict[str, Any]] = []
    for st, en, txt in cues:
        s = float(st)
        e = max(s, float(en))
        dur = max(0.01, e - s)
        clusters = grapheme_count(txt)
        cps = round(clusters / dur, 3)
        if cps > SUBTITLE_CPS_HARD_MAX:
            status = "over_max"
        elif cps > SUBTITLE_CPS_IDEAL_MAX:
            status = "above_ideal"
        else:
            status = "ok"
        out.append({
            "start": round(s, 3),
            "end": round(e, 3),
            "duration": round(dur, 3),
            "clusters": clusters,
            "cps": cps,
            "status": status,
        })
    return out


def apply_reading_speed_rules(
    cues: list[tuple[float, float, str]],
    *,
    speech_ends: list[float | None] | None = None,
    timeline_end: float | None = None,
    min_duration: float = SUBTITLE_MIN_CUE_DURATION,
    max_gap_bridge: float = SUBTITLE_GAP_BRIDGE_SEC,
    max_overhang: float = SUBTITLE_OVERHANG_SEC,
    ideal_cps_max: float = SUBTITLE_CPS_IDEAL_MAX,
) -> list[tuple[float, float, str]]:
    """S2 best-effort END-side reading-speed adjustment (D3 compliant).

    Invariants:
    - Cue START timestamps NEVER move.
    - Adjusted cues NEVER overlap the next cue's start.
    - Back-to-back zero-gap cues (e.g. §2G sub-cues) stay untouched.
    - Trims stale overhang past ``speech_end + max_overhang`` when duration >= min_duration.
    - Extends short cues toward ``min_duration`` (and fast cues toward ``ideal_cps_max``
      within silence budget) only where a real gap precedes the next cue.
    - Bridges inter-cue gaps < ``max_gap_bridge`` to eliminate visual flicker.
    """
    if not cues:
        return []
    n = len(cues)
    out: list[tuple[float, float, str]] = []
    for i, (raw_s, raw_e, txt) in enumerate(cues):
        start = round(float(raw_s), 3)
        end = max(start, round(float(raw_e), 3))
        if i + 1 < n:
            next_start: float | None = max(end, round(float(cues[i + 1][0]), 3))
        elif timeline_end is not None:
            next_start = max(end, round(float(timeline_end), 3))
        else:
            next_start = None

        sp_end = (
            max(start, round(float(speech_ends[i]), 3))
            if (speech_ends is not None and i < len(speech_ends) and speech_ends[i] is not None)
            else end
        )
        clusters = grapheme_count(txt)
        overhang_cap = round(sp_end + max_overhang, 3)

        # 1. Trim stale overhang beyond speech_end + 0.2s when already readable.
        if end > overhang_cap and (end - start) > min_duration:
            trimmed = max(overhang_cap, round(start + min_duration, 3))
            if trimmed < end:
                end = trimmed

        # 2. Best-effort extend cue END into following silence (never past next_start).
        if next_start is not None and next_start > end + 1e-4:
            cur_dur = end - start
            # Minimum cue duration 1.2s into real silence gap.
            if cur_dur < min_duration:
                end = min(next_start, round(start + min_duration, 3))
            # Reduce fast CPS within overhang_cap if gap still remains.
            cur_dur = max(0.01, end - start)
            if (clusters / cur_dur) > ideal_cps_max and end < overhang_cap:
                need_end = round(start + (clusters / ideal_cps_max), 3)
                end = min(next_start, overhang_cap, max(end, need_end))

        # 3. Anti-flicker: bridge consecutive gaps < 0.2s cleanly to next_start.
        if i + 1 < n and next_start is not None:
            gap = next_start - end
            if 1e-4 < gap < max_gap_bridge:
                end = next_start

        if next_start is not None:
            end = min(end, next_start)
        end = max(start, round(end, 3))
        out.append((start, end, txt))
    return out


def split_burmese_subtitle_cues(
    start_sec: float,
    end_sec: float,
    text: str,
    max_cue_chars: int | None = None,
    lines_per_cue: int = 1,
    word_timings: list[dict[str, Any]] | None = None,
    *,
    max_clusters_per_line: int | None = None,
    max_width_px: int | None = None,
    font_px: int | None = None,
    font_path: Path | None = None,
    stroke_px: int = 2,
) -> list[tuple[float, float, str]]:
    """
    Formats spoken segment narration into synchronized, comfortable, and readable subtitle cues.
    - S1 (default when ``max_cue_chars is None`` or cluster/width bounds passed): splits on
      Burmese grapheme clusters (20–28 clusters/line target, default 24) and rendered pixel width
      (≈840 px @ 1080 width scaled by render width), respecting sentence -> clause/particle ->
      space -> cluster break priorities and never stranding particles at line starts.
    - Explicit ``max_cue_chars=<int>`` without cluster/width overrides preserves §2G & Batch DD/DI
      character-budget splitting while enforcing grapheme-safe, particle-safe boundaries and NFC.
    - Lines 2/3 wrap each cue up to ``lines_per_cue`` lines without cutting clusters or words.
    """
    text = canonicalize_burmese_text(text)
    if not text:
        return []

    lines_cnt = min(3, max(1, int(lines_per_cue or 1)))
    total_dur = max(0.01, float(end_sec) - float(start_sec))
    use_s1_mode = (
        max_cue_chars is None
        or max_clusters_per_line is not None
        or max_width_px is not None
    )

    if not use_s1_mode:
        # Legacy explicit max_cue_chars contract (§2G, Batch DD/DI/DJ).
        assert max_cue_chars is not None
        if lines_cnt == 1:
            chunks = _split_burmese_into_single_line_chunks(text, max_chars=max_cue_chars)
            chunks = _merge_short_chunks_for_readability(
                chunks, total_dur, max_cue_chars, min_dur=0.65, orig_text=text
            )
            if len(chunks) <= 1:
                return [(round(start_sec, 3), round(end_sec, 3), _format_cue_lines(text, 1))]
            spans = _allocate_cue_times(start_sec, end_sec, chunks, word_timings=word_timings)
            return [(s, e, _format_cue_lines(chunk, 1)) for (s, e), chunk in zip(spans, chunks)]

        if "။" in text:
            sentences = [s.strip() for s in re.split(r'(?<=[။])\s*', text) if s.strip()]
            if len(sentences) >= 2:
                groups: list[str] = []
                cur_group = ""
                for s in sentences:
                    cand = f"{cur_group} {s}" if cur_group else s
                    if cur_group and len(cand) > max_cue_chars:
                        groups.append(cur_group)
                        cur_group = s
                    else:
                        cur_group = cand
                if cur_group:
                    groups.append(cur_group)
                groups = _merge_short_chunks_for_readability(
                    groups, total_dur, max_cue_chars, min_dur=1.1, orig_text=text
                )
                if len(groups) >= 2 and (total_dur / len(groups)) >= 1.8:
                    spans = _allocate_cue_times(start_sec, end_sec, groups, word_timings=word_timings)
                    return [(s, e, _format_cue_lines(grp, lines_cnt)) for (s, e), grp in zip(spans, groups)]

        formatted_text = _format_cue_lines(text, lines_cnt)
        return [(round(start_sec, 3), round(end_sec, 3), formatted_text)]

    # S1 Grapheme + Visual-Width mode
    eff_clusters = int(max_clusters_per_line) if max_clusters_per_line is not None else _subtitle_max_clusters()
    eff_font_px = int(font_px) if font_px is not None else 54
    eff_width_px = int(max_width_px) if max_width_px is not None else _subtitle_max_line_width_px(1080)

    if lines_cnt == 1:
        line_chunks = _split_line_units_s1(
            text,
            max_chars=max_cue_chars,
            max_clusters=eff_clusters,
            max_width_px=eff_width_px,
            font_px=eff_font_px,
            font_path=font_path,
            stroke_px=stroke_px,
        )
        if len(line_chunks) <= 1:
            return [(round(start_sec, 3), round(end_sec, 3), line_chunks[0] if line_chunks else text)]
        spans = _allocate_cue_times(start_sec, end_sec, line_chunks, word_timings=word_timings)
        return [(s, e, chunk) for (s, e), chunk in zip(spans, line_chunks)]

    # Multi-line cue mode (max lines_cnt lines per cue):
    # If text fits inside lines_cnt balanced lines where every line obeys S1 caps, keep 1 cue.
    flat = re.sub(r"\s+", " ", text.replace("\r\n", " ").replace("\n", " ")).strip()
    if lines_cnt in (2, 3):
        balanced = _format_cue_lines(
            flat,
            lines_cnt,
            max_clusters=eff_clusters,
            max_width_px=eff_width_px,
            font_px=eff_font_px,
            font_path=font_path,
            stroke_px=stroke_px,
        )
        b_lines = [ln for ln in balanced.split("\n") if ln]
        if len(b_lines) <= lines_cnt and all(
            len(myanmar_grapheme_clusters(ln)) <= eff_clusters
            and _measure_text_width_px(ln, eff_font_px, font_path, stroke_px) <= eff_width_px
            for ln in b_lines
        ):
            return [(round(start_sec, 3), round(end_sec, 3), balanced)]

    all_lines = _split_line_units_s1(
        flat,
        max_clusters=eff_clusters,
        max_width_px=eff_width_px,
        font_px=eff_font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    grouped_cues = [
        "\n".join(all_lines[i:i + lines_cnt])
        for i in range(0, len(all_lines), lines_cnt)
    ]
    if len(grouped_cues) <= 1:
        return [(round(start_sec, 3), round(end_sec, 3), grouped_cues[0] if grouped_cues else flat)]
    spans = _allocate_cue_times(start_sec, end_sec, grouped_cues, word_timings=word_timings)
    return [(s, e, grp) for (s, e), grp in zip(spans, grouped_cues)]


# ── Part D (2026-11-22): Sentence/clause-first width-true subtitle segmentation ──

SUBTITLE_MERGE_MAX_GAP_SEC = 0.35

# Tight adverbial modifiers where ပြီး / ကို binds forward into the verb/adjective
# and must not be treated as a clause break.
_CLAUSE_ADVERBIAL_PYI_WORDS: frozenset[str] = frozenset({
    "ထပ်ပြီး", "ပိုပြီး", "တွဲပြီး", "ပြန်ပြီး", "ဆက်ပြီး",
    "ပေါင်းပြီး", "လိုက်ပြီး", "ဝိုင်းပြီး", "စုပြီး", "ကူပြီး",
})
_CLAUSE_ADVERBIAL_KO_WORDS: frozenset[str] = frozenset({"တကယ်ကို", "အရမ်းကို", "တော်တော်ကို"})
_STRONG_CLAUSE_END_TOKS: frozenset[str] = frozenset({
    "ကိုတော့", "ကတော့", "မှာတော့", "တော့",
    "ပြီးတော့", "ပြီးနောက်", "ပြီးရင်", "ဖြစ်ပြီး", "လျက်",
    "တဲ့အခါ", "အခါ", "တာနဲ့", "တာမို့", "တာကြောင့်", "တဲ့အတွက်",
    "သော်လည်း", "ကြောင့်", "အတွက်", "လို့",
})
_STD_CLAUSE_PARTICLE_TOKS: frozenset[str] = frozenset({
    "ကို", "မှာ", "က", "နဲ့", "နှင့်", "ပါ", "လည်း", "ရော",
    "မှ", "သို့", "ဖြင့်", "ကနေ", "ရဲ့", "၏", "သည်", "၌", "တွင်",
    "တာ", "တဲ့", "ပဲ", "ပါတယ်", "ခဲ့တယ်", "တယ်", "နေတယ်", "နေတာ",
    "ပါပဲ", "လိုက်တယ်", "လိုက်တာ", "ဖြစ်တယ်", "ရှိတယ်", "သွားတယ်",
    "ပေါ့", "လေ", "နော်",
})
_CLAUSE_OPENING_TOKS: frozenset[str] = frozenset({
    "ကျွန်တော်", "ကျွန်မ", "ကျွန်တော့်", "ကျွန်မရဲ့",
    "မင်း", "မင်းတို့", "သူ", "သူက", "သူမ", "သူတို့",
    "ဒါပေမဲ့", "ဒါပေမယ့်", "ဒါကြောင့်", "အဲ့ဒါကြောင့်", "အဲဒါကြောင့်",
    "အဲဒီနောက်", "အဲ့ဒီနောက်", "ဒီတော့", "အခု",
})
_STRANDED_VERB_AUX_TOKS: frozenset[str] = frozenset({
    "သေး", "ကြ", "လိုက်", "နေ", "ခဲ့", "သွား", "ဦး",
    "ပါ", "မယ်", "တယ်", "ပြီ", "ဘူး", "ပေါ့", "တာ", "တဲ့",
})

_SHORT_SENTENCE_MAX_CLUSTERS = 6
_SHORT_SENTENCE_MAX_WIDTH_FRAC = 0.38
_SHORT_SENTENCE_MERGED_MAX_WIDTH_FRAC = 0.85
_SHORT_SENTENCE_MERGED_MAX_CLUSTERS = 14

_CLAUSE_START_CONNECTIVES: frozenset[str] = frozenset({
    "ဒါပေမဲ့", "ဒါပေမယ့်",
    "ပြီးတော့", "ပြီးနောက်", "ပြီးရင်",
    "ဒါကြောင့်", "အဲ့ဒါကြောင့်", "အဲဒါကြောင့်", "ဒါနဲ့",
    "အဲဒီနောက်", "အဲ့ဒီနောက်", "အဲ့ဒီတော့မှ", "အဲဒီတော့မှ",
    "ဒီတော့", "အခုတော့", "အခု", "နောက်ပြီး", "ဒါ့အပြင်",
})
_FRAGMENT_MIN_CLUSTERS = 4
_FRAGMENT_MIN_WORDS = 2
_MIN_FULL_WIDTH_FRAC = 0.35


def _display_text_width_px(
    text: str,
    font_px: int,
    font_path: Path | None,
    stroke_px: int,
) -> int:
    """Measure display width after stripping subtitle-hidden ``၊``/``။`` marks."""
    disp = strip_burmese_subtitle_punctuation(text)
    if not disp:
        return 0
    return _measure_text_width_px(disp, int(font_px), font_path, int(stroke_px))


def _nonspace_cluster_count(text: str) -> int:
    disp = strip_burmese_subtitle_punctuation(text)
    compact = re.sub(r"\s+", "", disp)
    return len(myanmar_grapheme_clusters(compact)) if compact else 0


def _piece_word_count(text: str) -> int:
    disp = strip_burmese_subtitle_punctuation(text)
    return len([w for w in disp.split() if w])


def _is_tiny_fragment(text: str) -> bool:
    return (
        _nonspace_cluster_count(text) < _FRAGMENT_MIN_CLUSTERS
        or _piece_word_count(text) < _FRAGMENT_MIN_WORDS
    )


def _split_burmese_sentences(flat: str) -> list[str]:
    """Segment narration into complete sentences without breaking embedded quotes."""
    if not flat:
        return []
    raw = [p.strip() for p in re.findall(r"[^။!?]+[။!?]+[”’\"'\)\]}]*|[^။!?]+", flat) if p.strip()]
    if len(raw) <= 1:
        return raw or [flat]
    out: list[str] = [raw[0]]
    for nxt in raw[1:]:
        first_word = nxt.split(" ", 1)[0]
        first_toks = tokenize_burmese_words(first_word)
        first_tok = first_toks[0] if first_toks else first_word
        if first_tok in ("လို့", "ဟု", "ဆို", "တဲ့") or nxt[0] in "”’\"')]}.,!?။၊":
            out[-1] = f"{out[-1]} {nxt}".strip()
        else:
            out.append(nxt)
    return out


def _dp_split_burmese_clause(
    text: str,
    *,
    max_width_px: int,
    font_px: int,
    font_path: Path | None,
    stroke_px: int,
) -> list[str]:
    """Split an over-wide Burmese sentence/clause at its highest-priority clause boundaries."""
    eff_w = max(80, int(max_width_px))
    if _display_text_width_px(text, font_px, font_path, stroke_px) <= eff_w:
        return [text]

    words = [w for w in text.split(" ") if w]
    tokens: list[str] = []
    tok_word: list[str] = []
    for w_idx, word in enumerate(words):
        need_sub = (
            _display_text_width_px(word, font_px, font_path, stroke_px) > eff_w
            or ("၊" in word and not word.endswith("၊"))
            or ("။" in word and not word.endswith("။"))
            or (len(words) <= 2 and len(myanmar_grapheme_clusters(word)) >= 10)
        )
        if need_sub:
            for sub in tokenize_burmese_words(word):
                if not sub:
                    continue
                if (
                    _display_text_width_px(sub, font_px, font_path, stroke_px) > eff_w
                    and len(myanmar_grapheme_clusters(sub)) > 1
                ):
                    for cl in myanmar_grapheme_clusters(sub):
                        tokens.append(cl)
                        tok_word.append(word)
                else:
                    tokens.append(sub)
                    tok_word.append(word)
        else:
            tokens.append(word)
            tok_word.append(word)
        if w_idx < len(words) - 1:
            tokens.append(" ")
            tok_word.append(" ")

    n = len(tokens)
    if n <= 1:
        return [text]

    word_tok_cache: dict[str, list[str]] = {}

    def _get_word_toks(w: str) -> list[str]:
        cached = word_tok_cache.get(w)
        if cached is None:
            cached = [t for t in tokenize_burmese_words(w) if t.strip()]
            word_tok_cache[w] = cached
        return cached

    tier_penalties = {
        6: 0.0,
        5: 10.0,
        4: 55.0,
        3: 80.0,
        2: 220.0,
        1: 420.0,
        0: 2000.0,
    }
    cut_penalty: dict[int, float] = {}
    for c in range(1, n):
        if tokens[c] == " ":
            continue
        at_space = (tokens[c - 1] == " ")
        p1_idx = c - 1
        while p1_idx >= 0 and not tokens[p1_idx].strip():
            p1_idx -= 1
        prev_unit = tokens[p1_idx].strip() if p1_idx >= 0 else ""
        prev_word = tok_word[p1_idx] if p1_idx >= 0 else ""
        next_unit = tokens[c].strip()
        next_word = tok_word[c]

        if prev_unit == prev_word:
            p_toks = _get_word_toks(prev_unit)
            prev_tok = p_toks[-1] if p_toks else prev_unit
            prev2_tok = p_toks[-2] if len(p_toks) >= 2 else ""
        else:
            prev_tok = prev_unit
            p2_idx = p1_idx - 1
            while p2_idx >= 0 and not tokens[p2_idx].strip():
                p2_idx -= 1
            prev2_tok = tokens[p2_idx].strip() if p2_idx >= 0 else ""

        if next_unit == next_word:
            n_toks = _get_word_toks(next_unit)
            next_tok = n_toks[0] if n_toks else next_unit
        else:
            next_tok = next_unit

        prev_word_clean = strip_burmese_subtitle_punctuation(prev_word)
        vetoed = False
        if next_tok in (STRANDED_LINE_START_PARTICLES - _LINE_START_CONJUNCTIONS) and (
            not at_space or len(_get_word_toks(next_word)) == 1
        ):
            vetoed = True
        elif next_unit in (STRANDED_LINE_START_PARTICLES - _LINE_START_CONJUNCTIONS):
            vetoed = True
        elif next_tok and next_tok[0] in "။၊.,!?):]”’":
            vetoed = True
        elif at_space and next_tok in _STRANDED_VERB_AUX_TOKS:
            vetoed = True
        elif prev_tok in _LINE_START_CONJUNCTIONS or prev_word_clean in _CLAUSE_START_CONNECTIVES:
            vetoed = True

        if vetoed:
            tier = 0
        elif prev_tok.endswith("၊") or prev_unit.endswith("၊") or prev_tok in (",", ";", ":", "—"):
            tier = 6
        elif (
            prev_tok in _STRONG_CLAUSE_END_TOKS
            or (prev_tok == "ပြီး" and prev_word_clean not in _CLAUSE_ADVERBIAL_PYI_WORDS)
            or (prev2_tok in ("တာ", "တဲ့", "မှု") and prev_tok in ("ကို", "က", "မှာ", "နဲ့", "နှင့်"))
            or (
                at_space
                and prev_tok in _STD_CLAUSE_PARTICLE_TOKS
                and prev_word_clean not in _CLAUSE_ADVERBIAL_KO_WORDS
                and next_tok in _CLAUSE_OPENING_TOKS
            )
        ):
            tier = 5
        elif prev_tok in _STD_CLAUSE_PARTICLE_TOKS and prev_word_clean not in _CLAUSE_ADVERBIAL_KO_WORDS:
            tier = 4 if at_space else 3
        elif at_space and next_tok in _LINE_START_CONJUNCTIONS:
            tier = 3
        elif at_space:
            tier = 2
        else:
            tier = 1

        cut_penalty[c] = tier_penalties[tier]

    valid_cuts = [0] + sorted(cut_penalty.keys()) + [n]
    m = len(valid_cuts)

    edges_from: list[list[tuple[int, str, int, int, int, float, int, int, bool]]] = [
        [] for _ in range(m)
    ]
    edge_fills_below_35: set[float] = set()
    for i in range(m - 1):
        s_idx = valid_cuts[i]
        for j in range(i + 1, m):
            e_idx = valid_cuts[j]
            piece = "".join(tokens[s_idx:e_idx]).strip()
            if not piece:
                continue
            pw = _display_text_width_px(piece, font_px, font_path, stroke_px)
            oversized = pw > eff_w
            if oversized and j > i + 1:
                break
            cl = _nonspace_cluster_count(piece)
            wc = _piece_word_count(piece)
            fill = pw / float(eff_w)
            is_short_cl = 1 if cl < _FRAGMENT_MIN_CLUSTERS else 0
            is_frag = 1 if (cl < _FRAGMENT_MIN_CLUSTERS or wc < _FRAGMENT_MIN_WORDS) else 0
            edges_from[i].append((j, piece, pw, cl, wc, fill, is_frag, is_short_cl, oversized))
            if fill < _MIN_FULL_WIDTH_FRAC:
                edge_fills_below_35.add(round(fill, 4))
            if oversized:
                break

    def _solve_with_floor(
        min_fill_floor: float,
    ) -> tuple[tuple[int, int, int, float, float] | None, list[str] | None]:
        inf_state = (10**9, 10**9, 10**9, 1e18, 1e18)
        dp: list[tuple[int, int, int, float, float]] = [inf_state] * m
        parent = [-1] * m
        parent_piece = [""] * m
        dp[0] = (0, 0, 0, 0.0, 0.0)
        for i in range(m - 1):
            st_i = dp[i]
            if st_i[0] >= 10**9:
                continue
            f_i, sc_i, k_i, cp_i, bp_i = st_i
            for j, piece, _pw, _cl, _wc, fill, is_frag, is_short_cl, oversized in edges_from[i]:
                if not oversized and fill + 1e-6 < min_fill_floor:
                    continue
                c_pen = cut_penalty[valid_cuts[j]] if j < m - 1 else 0.0
                low_pen = 60.0 * (max(0.0, 0.65 - fill) ** 2)
                cand = (f_i + is_frag, sc_i + is_short_cl, k_i + 1, cp_i + c_pen, bp_i + low_pen)
                if cand < dp[j]:
                    dp[j] = cand
                    parent[j] = i
                    parent_piece[j] = piece
        if dp[m - 1][0] >= 10**9:
            return None, None
        out_pcs: list[str] = []
        cur = m - 1
        while cur > 0:
            out_pcs.append(parent_piece[cur])
            cur = parent[cur]
        out_pcs.reverse()
        return dp[m - 1], out_pcs

    base_score, base_pcs = _solve_with_floor(0.0)
    if base_score is None or not base_pcs:
        return [text]
    best_frag, best_short_cl, best_k = base_score[0], base_score[1], base_score[2]

    candidate_floors = [_MIN_FULL_WIDTH_FRAC] + sorted(edge_fills_below_35, reverse=True)
    for fl in candidate_floors:
        if fl <= 0.0:
            break
        sc, pcs = _solve_with_floor(fl)
        if sc is not None and pcs is not None:
            if (sc[0], sc[1], sc[2]) <= (best_frag, best_short_cl, best_k):
                return pcs
    return base_pcs


def _rebalance_sentence_pieces(
    pieces: list[str],
    *,
    max_width_px: int,
    font_px: int,
    font_path: Path | None,
    stroke_px: int,
) -> list[str]:
    """Shift boundary words or merge neighboring pieces when a split left a
    tiny fragment (<2 words or <4 clusters) or an under-filled piece (<35% width)."""
    eff_w = max(80, int(max_width_px))

    def _score(pcs: list[str]) -> tuple[int, int, int, float]:
        frags = sum(1 for p in pcs if _is_tiny_fragment(p))
        short_cls = sum(1 for p in pcs if _nonspace_cluster_count(p) < _FRAGMENT_MIN_CLUSTERS)
        min_fill = min(
            (_display_text_width_px(p, font_px, font_path, stroke_px) / float(eff_w))
            for p in pcs
        ) if pcs else 0.0
        return (frags, short_cls, len(pcs), -round(min(min_fill, _MIN_FULL_WIDTH_FRAC), 4))

    out = list(pieces)
    for _ in range(8):
        if len(out) <= 1:
            break
        weak_idxs = [
            idx for idx, p in enumerate(out)
            if _is_tiny_fragment(p)
            or _display_text_width_px(p, font_px, font_path, stroke_px) < _MIN_FULL_WIDTH_FRAC * eff_w
        ]
        if not weak_idxs:
            break
        best_score = _score(out)
        best_next: list[str] | None = None
        for idx in weak_idxs:
            for nb in (idx - 1, idx + 1):
                if 0 <= nb < len(out):
                    lo, hi = (nb, idx) if nb < idx else (idx, nb)
                    merged = f"{out[lo]} {out[hi]}".strip()
                    if _display_text_width_px(merged, font_px, font_path, stroke_px) <= eff_w:
                        cand = out[:lo] + [merged] + out[hi + 1:]
                        sc = _score(cand)
                        if sc < best_score:
                            best_score = sc
                            best_next = cand
            if idx > 0:
                lwords = out[idx - 1].split()
                if len(lwords) >= 2:
                    moved = lwords[-1]
                    moved_clean = strip_burmese_subtitle_punctuation(moved)
                    l_last = strip_burmese_subtitle_punctuation(lwords[-2])
                    new_l = " ".join(lwords[:-1]).strip()
                    new_r = f"{moved} {out[idx]}".strip()
                    if (
                        new_l
                        and _display_text_width_px(new_r, font_px, font_path, stroke_px) <= eff_w
                        and l_last not in _CLAUSE_START_CONNECTIVES
                        and moved_clean not in (STRANDED_LINE_START_PARTICLES - _LINE_START_CONJUNCTIONS)
                        and moved_clean not in _STRANDED_VERB_AUX_TOKS
                    ):
                        cand = out[:idx - 1] + [new_l, new_r] + out[idx + 1:]
                        sc = _score(cand)
                        if sc < best_score:
                            best_score = sc
                            best_next = cand
            if idx + 1 < len(out):
                rwords = out[idx + 1].split()
                if len(rwords) >= 2:
                    moved = rwords[0]
                    moved_clean = strip_burmese_subtitle_punctuation(moved)
                    r_next = strip_burmese_subtitle_punctuation(rwords[1])
                    new_l = f"{out[idx]} {moved}".strip()
                    new_r = " ".join(rwords[1:]).strip()
                    if (
                        new_r
                        and _display_text_width_px(new_l, font_px, font_path, stroke_px) <= eff_w
                        and moved_clean not in _CLAUSE_START_CONNECTIVES
                        and r_next not in (STRANDED_LINE_START_PARTICLES - _LINE_START_CONJUNCTIONS)
                        and r_next not in _STRANDED_VERB_AUX_TOKS
                    ):
                        cand = out[:idx] + [new_l, new_r] + out[idx + 2:]
                        sc = _score(cand)
                        if sc < best_score:
                            best_score = sc
                            best_next = cand
        if best_next is None:
            break
        out = best_next
    return out


def _split_sentence_at_clause_boundaries(
    sentence: str,
    *,
    max_width_px: int,
    font_px: int,
    font_path: Path | None,
    stroke_px: int,
) -> list[str]:
    """Keep a fitting sentence whole; otherwise split only at natural clause boundaries."""
    eff_w = max(80, int(max_width_px))
    if _display_text_width_px(sentence, font_px, font_path, stroke_px) <= eff_w:
        return [sentence]

    dp_pcs = _rebalance_sentence_pieces(
        _dp_split_burmese_clause(
            sentence,
            max_width_px=eff_w,
            font_px=font_px,
            font_path=font_path,
            stroke_px=stroke_px,
        ),
        max_width_px=eff_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )

    if "၊" in sentence:
        raw_clauses = [p.strip() for p in re.findall(r"[^၊]+၊+|[^၊]+", sentence) if p.strip()]
        if len(raw_clauses) >= 2:
            grouped: list[str] = []
            cur = raw_clauses[0]
            for nxt in raw_clauses[1:]:
                cand = f"{cur} {nxt}".strip()
                if _display_text_width_px(cand, font_px, font_path, stroke_px) <= eff_w:
                    cur = cand
                else:
                    grouped.append(cur)
                    cur = nxt
            grouped.append(cur)
            comma_pcs: list[str] = []
            for cl in grouped:
                comma_pcs.extend(
                    _dp_split_burmese_clause(
                        cl,
                        max_width_px=eff_w,
                        font_px=font_px,
                        font_path=font_path,
                        stroke_px=stroke_px,
                    )
                )
            comma_frags = sum(1 for p in comma_pcs if _is_tiny_fragment(p))
            dp_frags = sum(1 for p in dp_pcs if _is_tiny_fragment(p))
            if (
                all(
                    _nonspace_cluster_count(p) >= _FRAGMENT_MIN_CLUSTERS
                    and _display_text_width_px(p, font_px, font_path, stroke_px) >= _MIN_FULL_WIDTH_FRAC * eff_w
                    for p in comma_pcs
                )
                and not (len(comma_pcs) == len(dp_pcs) and comma_frags > dp_frags)
            ):
                return comma_pcs

    return dp_pcs


def _split_width_true_pieces_grouped(
    text: str,
    *,
    lines_per_cue: int = 1,
    max_width_px: int,
    font_px: int,
    font_path: Path | None,
    stroke_px: int = 2,
) -> list[tuple[str, int, bool]]:
    """Segment a merged narration span and retain ``(piece, unit_id, is_complete_unit)``
    metadata so callers can enforce duration/fragment merging within sentence rules."""
    clean = canonicalize_burmese_text(text)
    if not clean:
        return []
    flat = re.sub(r"\s+", " ", clean.replace("\r\n", " ").replace("\n", " ")).strip()
    if not flat:
        return []

    lines_cnt = min(3, max(1, int(lines_per_cue or 1)))
    eff_w = max(80, int(max_width_px))
    f_px = int(font_px)
    s_px = int(stroke_px)

    sentences = _split_burmese_sentences(flat)

    def _is_very_short_sentence(sent: str) -> bool:
        cl = _nonspace_cluster_count(sent)
        w = _display_text_width_px(sent, f_px, font_path, s_px)
        return cl <= _SHORT_SENTENCE_MAX_CLUSTERS and w <= _SHORT_SENTENCE_MAX_WIDTH_FRAC * eff_w

    sentence_units: list[str] = []
    idx = 0
    while idx < len(sentences):
        if _is_very_short_sentence(sentences[idx]):
            group = sentences[idx]
            nxt_idx = idx + 1
            while nxt_idx < len(sentences) and _is_very_short_sentence(sentences[nxt_idx]):
                cand = f"{group} {sentences[nxt_idx]}"
                cand_w = _display_text_width_px(cand, f_px, font_path, s_px)
                cand_cl = _nonspace_cluster_count(cand)
                if (
                    cand_w <= _SHORT_SENTENCE_MERGED_MAX_WIDTH_FRAC * eff_w
                    and cand_cl <= _SHORT_SENTENCE_MERGED_MAX_CLUSTERS
                ):
                    group = cand
                    nxt_idx += 1
                else:
                    break
            sentence_units.append(group)
            idx = nxt_idx
        else:
            sentence_units.append(sentences[idx])
            idx += 1

    grouped: list[tuple[str, int, bool]] = []
    for unit_id, unit in enumerate(sentence_units):
        unit_lines = [
            ln.strip()
            for ln in _split_sentence_at_clause_boundaries(
                unit,
                max_width_px=eff_w,
                font_px=f_px,
                font_path=font_path,
                stroke_px=s_px,
            )
            if ln.strip()
        ]
        if not unit_lines:
            continue
        if lines_cnt == 1 or len(unit_lines) == 1:
            unit_cues = unit_lines
        else:
            unit_cues = [
                "\n".join(unit_lines[k:k + lines_cnt])
                for k in range(0, len(unit_lines), lines_cnt)
            ]
        is_complete = len(unit_cues) == 1
        for cue in unit_cues:
            grouped.append((cue, unit_id, is_complete))
    return grouped or [(flat, 0, True)]


def _split_width_true_pieces(
    text: str,
    *,
    lines_per_cue: int = 1,
    max_width_px: int,
    font_px: int,
    font_path: Path | None,
    stroke_px: int = 2,
) -> list[str]:
    """Segment a merged narration span by linguistic sentence/clause structure
    while enforcing the locked-font ``max_width_px`` cap.

    1. Segments into complete sentences at ``။`` / ``!`` / ``?``.
    2. Only merges consecutive *very short* complete sentences that together fit
       comfortably; never glues a sentence tail to the next sentence's head.
    3. Any over-wide sentence splits strictly at natural clause boundaries without
       stranding tiny single-word or <4-cluster fragments when avoidable.
    """
    return [
        piece
        for piece, _, _ in _split_width_true_pieces_grouped(
            text,
            lines_per_cue=lines_per_cue,
            max_width_px=max_width_px,
            font_px=font_px,
            font_path=font_path,
            stroke_px=stroke_px,
        )
    ]


def _coherent_segment_word_events(
    seg_text: str,
    word_timings: list[dict[str, Any]] | None,
    *,
    seg_start: float | None = None,
    seg_end: float | None = None,
) -> tuple[list[tuple[float, float, int]], float, int] | None:
    """Return ``(events[(abs_st, abs_en, cluster_len)], wt_end, wt_total)`` when
    segment WordBoundary timings coherently cover ``seg_text``; else None.

    Normalizes both segment-relative timings (produced per-segment by Edge TTS,
    starting near 0.0) and job-absolute timings onto the job timeline
    ``[seg_start, seg_end]`` using Myanmar grapheme-cluster weights so cue
    boundaries track spoken words without offset drift.
    """
    if not word_timings:
        return None
    clean_events: list[tuple[float, float, int]] = []
    prev_s = -1.0
    for ev in word_timings:
        if not isinstance(ev, dict):
            continue
        tok = re.sub(r"\s+", "", str(ev.get("text") or ""))
        try:
            st = float(ev.get("start") or 0.0)
            en = float(ev.get("end") or st)
        except (TypeError, ValueError):
            continue
        if not tok or en < st or st < prev_s - 0.05:
            continue
        prev_s = max(prev_s, st)
        tok_units = max(1, len(myanmar_grapheme_clusters(tok)))
        clean_events.append((max(0.0, st), max(0.0, st, en), tok_units))
    if len(clean_events) < 2:
        return None
    wt_min = clean_events[0][0]
    wt_max = max(en for _, en, _ in clean_events)
    if wt_max <= 0.1 or (wt_max - wt_min) <= 0.05:
        return None
    wt_total = sum(ln for _, _, ln in clean_events)
    unit_total = len(myanmar_grapheme_clusters(re.sub(r"\s+", "", seg_text or "")))
    if wt_total < 3 or unit_total < 3:
        return None
    ratio = wt_total / float(unit_total)
    if ratio < 0.65 or ratio > 1.35:
        return None

    if seg_start is not None and seg_end is not None and seg_end > seg_start:
        s_st = float(seg_start)
        s_en = float(seg_end)
        seg_dur = s_en - s_st
        is_job_absolute = (
            s_st > 0.25
            and wt_min >= s_st - 0.25
            and wt_max <= s_en + 0.35
        )
        mapped: list[tuple[float, float, int]] = []
        if is_job_absolute:
            for ev_st, ev_en, tok_u in clean_events:
                st_a = max(s_st, min(s_en, ev_st))
                en_a = max(st_a, min(s_en, ev_en))
                mapped.append((st_a, en_a, tok_u))
        else:
            base_t = wt_min if (wt_min >= 0.25 and (wt_max - wt_min) > 0.1) else 0.0
            span_t = max(0.1, wt_max - base_t)
            for ev_st, ev_en, tok_u in clean_events:
                st_a = s_st + max(0.0, min(1.0, (ev_st - base_t) / span_t)) * seg_dur
                en_a = max(st_a, s_st + max(0.0, min(1.0, (ev_en - base_t) / span_t)) * seg_dur)
                mapped.append((st_a, en_a, tok_u))
        return mapped, s_en, wt_total

    return clean_events, wt_max, wt_total


def _span_piece_raw_boundaries(
    span_segments: list[dict[str, Any]],
    piece_texts: list[str],
) -> list[float]:
    """Compute raw monotonic boundary timestamps ``[b_0, ..., b_k]`` across
    ``span_segments`` for ``piece_texts`` using coherent ``word_timings``
    where available and grapheme-cluster share otherwise."""
    if not span_segments or not piece_texts:
        return []
    span_start = round(float(span_segments[0]["start"]), 3)
    span_end = round(max(span_start, float(span_segments[-1]["end"])), 3)
    if len(piece_texts) == 1:
        return [float(span_start), float(span_end)]

    seg_cluster_spans: list[tuple[float, float, float, float, Any]] = []
    cum_seg_c = 0.0
    for s in span_segments:
        c_len = float(max(1, len(myanmar_grapheme_clusters(re.sub(r"\s+", "", s["text"])))))
        s_st_f = float(s["start"])
        s_en_f = float(s["end"])
        wt_info = _coherent_segment_word_events(
            s["text"], s.get("word_timings"), seg_start=s_st_f, seg_end=s_en_f
        )
        seg_cluster_spans.append((
            cum_seg_c,
            cum_seg_c + c_len,
            s_st_f,
            s_en_f,
            wt_info,
        ))
        cum_seg_c += c_len
    segs_total_c = max(1.0, cum_seg_c)

    def _time_at_cluster_pos(pos: float) -> float:
        clamped = max(0.0, min(segs_total_c, pos))
        for c0, c1, s_st, s_en, wt_info in seg_cluster_spans:
            if clamped <= c1 or c1 >= segs_total_c:
                seg_c = max(1.0, c1 - c0)
                u = max(0.0, min(1.0, (clamped - c0) / seg_c))
                if wt_info is not None:
                    events, _wt_end, wt_total = wt_info
                    target = u * float(wt_total)
                    seen = 0.0
                    bt: float | None = None
                    for ev_st, ev_en, tok_l in events:
                        tl = float(tok_l)
                        if seen + tl >= target:
                            inner = max(0.0, min(1.0, (target - seen) / tl)) if tl > 0 else 1.0
                            bt = ev_st + inner * (ev_en - ev_st)
                            break
                        seen += tl
                    if bt is None:
                        bt = events[-1][1]
                    return max(s_st, min(s_en, bt))
                return s_st + u * max(0.0, s_en - s_st)
        return float(span_end)

    piece_clusters = [
        max(1, len(myanmar_grapheme_clusters(re.sub(r"\s+", "", p))))
        for p in piece_texts
    ]
    pieces_total_c = float(sum(piece_clusters)) or 1.0

    boundaries: list[float] = [float(span_start)]
    cum_p = 0
    for pc in piece_clusters[:-1]:
        cum_p += pc
        raw_t = _time_at_cluster_pos((cum_p / pieces_total_c) * segs_total_c)
        boundaries.append(max(boundaries[-1], min(float(span_end), raw_t)))
    boundaries.append(float(span_end))
    return boundaries


def _allocate_span_cue_times(
    span_segments: list[dict[str, Any]],
    piece_texts: list[str],
) -> list[tuple[float, float, float]]:
    """Distribute contiguous timestamps across a merged segment span for each
    emitted cue piece, preferring coherent ``word_timings`` and otherwise
    using grapheme-cluster share across each segment window, while enforcing
    ``_CX_MIN_PIECE_SECONDS`` (0.4 s) when the span is long enough.
    """
    if not span_segments or not piece_texts:
        return []
    span_start = round(float(span_segments[0]["start"]), 3)
    span_end = round(max(span_start, float(span_segments[-1]["end"])), 3)
    last_sp_end = round(min(span_end, max(span_start, float(span_segments[-1]["speech_end"]))), 3)

    if len(piece_texts) == 1:
        return [(span_start, span_end, last_sp_end)]

    boundaries = _span_piece_raw_boundaries(span_segments, piece_texts)
    durs = [max(0.0, boundaries[i + 1] - boundaries[i]) for i in range(len(piece_texts))]
    total_dur = max(0.0, float(span_end) - float(span_start))
    if len(durs) > 1 and total_dur >= _CX_MIN_PIECE_SECONDS * len(durs):
        need = sum(max(0.0, _CX_MIN_PIECE_SECONDS - d) for d in durs)
        if need > 1e-6:
            surplus = [max(0.0, d - _CX_MIN_PIECE_SECONDS) for d in durs]
            tot_surplus = sum(surplus)
            if tot_surplus > 1e-6:
                spare = total_dur - _CX_MIN_PIECE_SECONDS * len(durs)
                durs = [
                    _CX_MIN_PIECE_SECONDS + (s / tot_surplus) * spare
                    for s in surplus
                ]

    out: list[tuple[float, float, float]] = []
    cur = float(span_start)
    for i, dur in enumerate(durs):
        p_start = round(cur, 3)
        if i == len(durs) - 1:
            p_end = span_end
            sp_end = min(p_end, last_sp_end)
        else:
            raw_end = cur + dur
            p_end = round(max(p_start, min(float(span_end), raw_end)), 3)
            sp_end = p_end
        out.append((p_start, p_end, sp_end))
        cur = float(p_end)
    return out


def _merge_sub_min_duration_pieces(
    span_segments: list[dict[str, Any]],
    grouped: list[tuple[str, int, bool]],
    *,
    lines_per_cue: int,
    max_width_px: int,
    font_px: int,
    font_path: Path | None,
    stroke_px: int,
    min_duration: float = _CX_MIN_PIECE_SECONDS,
) -> list[str]:
    """Eliminate sub-0.4 s proportional cue pieces before timestamp allocation by
    merging with an eligible neighbor (same sentence first, or adjacent complete
    sentence) or shifting a boundary word when the width cap allows."""
    if len(grouped) <= 1:
        return [p for p, _, _ in grouped]

    eff_w = max(80, int(max_width_px))
    lines_cnt = min(3, max(1, int(lines_per_cue or 1)))
    cur_grouped = list(grouped)

    def _fit_merged_text(left_txt: str, right_txt: str) -> tuple[str, int] | None:
        flat = re.sub(r"\s+", " ", f"{left_txt} {right_txt}").strip()
        if lines_cnt == 1:
            w = _display_text_width_px(flat, font_px, font_path, stroke_px)
            return (flat, w) if w <= eff_w else None
        left_lines = [ln.strip() for ln in left_txt.split("\n") if ln.strip()]
        right_lines = [ln.strip() for ln in right_txt.split("\n") if ln.strip()]
        stacked = left_lines + right_lines
        if len(stacked) <= lines_cnt and all(
            _display_text_width_px(ln, font_px, font_path, stroke_px) <= eff_w for ln in stacked
        ):
            w_max = max(_display_text_width_px(ln, font_px, font_path, stroke_px) for ln in stacked)
            return ("\n".join(stacked), w_max)
        reflowed = _dp_split_burmese_clause(
            flat,
            max_width_px=eff_w,
            font_px=font_px,
            font_path=font_path,
            stroke_px=stroke_px,
        )
        if len(reflowed) <= lines_cnt and all(
            _display_text_width_px(ln, font_px, font_path, stroke_px) <= eff_w for ln in reflowed
        ):
            w_max = max(_display_text_width_px(ln, font_px, font_path, stroke_px) for ln in reflowed)
            return ("\n".join(reflowed), w_max)
        return None

    for _ in range(len(cur_grouped) + 2):
        if len(cur_grouped) <= 1:
            break
        texts = [p for p, _, _ in cur_grouped]
        bounds = _span_piece_raw_boundaries(span_segments, texts)
        durs = [max(0.0, bounds[i + 1] - bounds[i]) for i in range(len(texts))]
        short_idxs = sorted(
            (i for i, d in enumerate(durs) if d < min_duration - 1e-6),
            key=lambda i: durs[i],
        )
        if not short_idxs:
            break

        changed = False
        for idx in short_idxs:
            txt, uid, is_complete = cur_grouped[idx]

            # 1. Prefer merging into an adjacent piece of the SAME sentence unit.
            same_cands: list[tuple[int, int, str]] = []
            for nb in (idx - 1, idx + 1):
                if 0 <= nb < len(cur_grouped) and cur_grouped[nb][1] == uid:
                    lo, hi = (nb, idx) if nb < idx else (idx, nb)
                    fitted = _fit_merged_text(cur_grouped[lo][0], cur_grouped[hi][0])
                    if fitted is not None:
                        same_cands.append((fitted[1], lo, fitted[0]))
            if same_cands:
                same_cands.sort(key=lambda item: item[0])
                _, lo, merged_txt = same_cands[0]
                remain_same = sum(
                    1 for k, (_, u, _) in enumerate(cur_grouped)
                    if u == uid and k not in (lo, lo + 1)
                )
                cur_grouped[lo] = (merged_txt, uid, remain_same == 0)
                cur_grouped.pop(lo + 1)
                changed = True
                break

            # 2. Complete short sentence: may merge with an adjacent complete sentence.
            if is_complete:
                sent_cands: list[tuple[int, int, str, int]] = []
                for nb in (idx - 1, idx + 1):
                    if 0 <= nb < len(cur_grouped) and cur_grouped[nb][2]:
                        lo, hi = (nb, idx) if nb < idx else (idx, nb)
                        fitted = _fit_merged_text(cur_grouped[lo][0], cur_grouped[hi][0])
                        if fitted is not None:
                            sent_cands.append((fitted[1], lo, fitted[0], cur_grouped[lo][1]))
                if sent_cands:
                    sent_cands.sort(key=lambda item: item[0])
                    _, lo, merged_txt, keep_uid = sent_cands[0]
                    cur_grouped[lo] = (merged_txt, keep_uid, True)
                    cur_grouped.pop(lo + 1)
                    changed = True
                    break

            # 3. Same-sentence boundary shift (1-line mode) if merging exceeded width cap.
            if lines_cnt == 1:
                best_shift: tuple[float, int, str, str] | None = None
                orig_frags = (1 if _is_tiny_fragment(txt) else 0)
                if idx > 0 and cur_grouped[idx - 1][1] == uid:
                    nb_txt = cur_grouped[idx - 1][0]
                    lwords = nb_txt.split()
                    if len(lwords) >= 2:
                        moved = lwords[-1]
                        moved_clean = strip_burmese_subtitle_punctuation(moved)
                        l_last = strip_burmese_subtitle_punctuation(lwords[-2])
                        new_l = " ".join(lwords[:-1]).strip()
                        new_r = f"{moved} {txt}".strip()
                        if (
                            new_l
                            and _display_text_width_px(new_r, font_px, font_path, stroke_px) <= eff_w
                            and l_last not in _CLAUSE_START_CONNECTIVES
                            and moved_clean not in (STRANDED_LINE_START_PARTICLES - _LINE_START_CONJUNCTIONS)
                            and moved_clean not in _STRANDED_VERB_AUX_TOKS
                        ):
                            old_pair_frags = orig_frags + (1 if _is_tiny_fragment(nb_txt) else 0)
                            new_pair_frags = (1 if _is_tiny_fragment(new_l) else 0) + (1 if _is_tiny_fragment(new_r) else 0)
                            if new_pair_frags <= old_pair_frags:
                                cand_texts = list(texts)
                                cand_texts[idx - 1] = new_l
                                cand_texts[idx] = new_r
                                c_bounds = _span_piece_raw_boundaries(span_segments, cand_texts)
                                new_min = min(c_bounds[idx] - c_bounds[idx - 1], c_bounds[idx + 1] - c_bounds[idx])
                                old_min = min(durs[idx - 1], durs[idx])
                                if new_min > old_min + 1e-4:
                                    best_shift = (new_min, idx - 1, new_l, new_r)
                if idx + 1 < len(cur_grouped) and cur_grouped[idx + 1][1] == uid:
                    nb_txt = cur_grouped[idx + 1][0]
                    rwords = nb_txt.split()
                    if len(rwords) >= 2:
                        moved = rwords[0]
                        moved_clean = strip_burmese_subtitle_punctuation(moved)
                        r_next = strip_burmese_subtitle_punctuation(rwords[1])
                        new_l = f"{txt} {moved}".strip()
                        new_r = " ".join(rwords[1:]).strip()
                        if (
                            new_r
                            and _display_text_width_px(new_l, font_px, font_path, stroke_px) <= eff_w
                            and moved_clean not in _CLAUSE_START_CONNECTIVES
                            and r_next not in (STRANDED_LINE_START_PARTICLES - _LINE_START_CONJUNCTIONS)
                            and r_next not in _STRANDED_VERB_AUX_TOKS
                        ):
                            old_pair_frags = orig_frags + (1 if _is_tiny_fragment(nb_txt) else 0)
                            new_pair_frags = (1 if _is_tiny_fragment(new_l) else 0) + (1 if _is_tiny_fragment(new_r) else 0)
                            if new_pair_frags <= old_pair_frags:
                                cand_texts = list(texts)
                                cand_texts[idx] = new_l
                                cand_texts[idx + 1] = new_r
                                c_bounds = _span_piece_raw_boundaries(span_segments, cand_texts)
                                new_min = min(c_bounds[idx + 1] - c_bounds[idx], c_bounds[idx + 2] - c_bounds[idx + 1])
                                old_min = min(durs[idx], durs[idx + 1])
                                if new_min > old_min + 1e-4 and (best_shift is None or new_min > best_shift[0]):
                                    best_shift = (new_min, idx, new_l, new_r)
                if best_shift is not None:
                    _, lo, new_a, new_b = best_shift
                    cur_grouped[lo] = (new_a, uid, False)
                    cur_grouped[lo + 1] = (new_b, uid, False)
                    changed = True
                    break

        if not changed:
            break

    return [p for p, _, _ in cur_grouped]


class SubtitleCuePacker:
    """Streaming pause-delimited Burmese subtitle packer.

    Concatenates consecutive narration segments whose inter-segment gap is
    ``<= max_gap_sec`` (0.35 s) and packs them to full measured PNG width
    at the job's locked font size. Guarantees identical output between live
    prewarm streaming and final burn/SRT/ASS emission.
    """

    def __init__(
        self,
        *,
        lines_per_cue: int = 1,
        max_width_px: int,
        font_px: int = 54,
        font_path: Path | None = None,
        stroke_px: int = 2,
        max_gap_sec: float = SUBTITLE_MERGE_MAX_GAP_SEC,
    ) -> None:
        self.lines_per_cue = min(3, max(1, int(lines_per_cue or 1)))
        self.max_width_px = max(80, int(max_width_px))
        self.font_px = int(font_px)
        self.font_path = font_path
        self.stroke_px = int(stroke_px)
        self.max_gap_sec = float(max_gap_sec)
        self._run: list[dict[str, Any]] = []

    def add_segment(
        self,
        start_sec: float,
        end_sec: float,
        text: str,
        *,
        speech_end: float | None = None,
        word_timings: list[dict[str, Any]] | None = None,
    ) -> list[tuple[float, float, str, float]]:
        clean = canonicalize_burmese_text(text)
        flat = re.sub(r"\s+", " ", clean.replace("\r\n", " ").replace("\n", " ")).strip()
        if not flat:
            return []

        st = round(float(start_sec), 3)
        en = round(max(st, float(end_sec)), 3)
        sp = round(min(en, max(st, float(speech_end if speech_end is not None else en))), 3)

        emitted: list[tuple[float, float, str, float]] = []
        if self._run and (st - float(self._run[-1]["end"])) > self.max_gap_sec + 1e-6:
            emitted = self.flush()

        self._run.append({
            "start": st,
            "end": en,
            "speech_end": sp,
            "text": flat,
            "word_timings": word_timings,
        })
        return emitted

    def flush(self) -> list[tuple[float, float, str, float]]:
        if not self._run:
            return []
        span = self._run
        self._run = []
        joined = " ".join(s["text"] for s in span).strip()
        grouped = _split_width_true_pieces_grouped(
            joined,
            lines_per_cue=self.lines_per_cue,
            max_width_px=self.max_width_px,
            font_px=self.font_px,
            font_path=self.font_path,
            stroke_px=self.stroke_px,
        )
        if not grouped:
            return []
        pieces = _merge_sub_min_duration_pieces(
            span,
            grouped,
            lines_per_cue=self.lines_per_cue,
            max_width_px=self.max_width_px,
            font_px=self.font_px,
            font_path=self.font_path,
            stroke_px=self.stroke_px,
        )
        if not pieces:
            return []
        times = _allocate_span_cue_times(span, pieces)
        out: list[tuple[float, float, str, float]] = []
        for (st, en, sp_en), txt in zip(times, pieces):
            disp_txt = strip_burmese_subtitle_punctuation(txt)
            if disp_txt:
                out.append((st, en, disp_txt, sp_en))
        return out


def pack_subtitle_cues_from_segments(
    timing_items: list[dict[str, Any]],
    *,
    lines_per_cue: int = 1,
    max_width_px: int,
    font_px: int = 54,
    font_path: Path | None = None,
    stroke_px: int = 2,
    max_gap_sec: float = SUBTITLE_MERGE_MAX_GAP_SEC,
) -> tuple[list[tuple[float, float, str]], list[float | None]]:
    """Pack a sequence of segment dicts (``audio_start``/``start``,
    ``audio_end``/``end``, ``text``, optional ``speech_duration``,
    ``word_timings``) into width-true repacked cues + aligned speech_ends.
    """
    packer = SubtitleCuePacker(
        lines_per_cue=lines_per_cue,
        max_width_px=max_width_px,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
        max_gap_sec=max_gap_sec,
    )
    packed: list[tuple[float, float, str, float]] = []
    for item in timing_items:
        raw_st = float(item.get("audio_start", item.get("start", 0.0)))
        raw_en = float(item.get("audio_end", item.get("end", raw_st)))
        sp_dur = item.get("speech_duration")
        if sp_dur is not None:
            seg_sp_end = min(raw_en, raw_st + max(0.05, float(sp_dur)))
        else:
            seg_sp_end = raw_en
        txt = str(item.get("text", "")).strip()
        if not txt:
            continue
        packed.extend(
            packer.add_segment(
                raw_st,
                raw_en,
                txt,
                speech_end=seg_sp_end,
                word_timings=item.get("word_timings"),
            )
        )
    packed.extend(packer.flush())
    cues = [(c_st, c_en, c_txt) for c_st, c_en, c_txt, _ in packed]
    speech_ends: list[float | None] = [float(sp_en) for _, _, _, sp_en in packed]
    return cues, speech_ends


def _resolve_png_subtitle_job(
    overlays: dict[str, Any], cw: int, ch: int,
    layout: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve everything the Batch Q PNG subtitle burn needs.

    Cue source, in priority order: the pipeline's ``subtitle_cues``
    (set by EdgeSmartSync beside subtitles_file); else the cues are
    parsed back out of the subtitles_file itself (.srt, or .ass —
    whose embedded style also supplies the font size/visuals, so a
    file-only call renders what the file describes). Returns None
    when there is nothing to burn.
    """
    cues: list[tuple[float, float, str]] = []
    file_font_px: int | None = None
    file_visuals: dict[str, Any] | None = None
    file_family: str | None = None
    raw_cues = overlays.get("subtitle_cues")
    if raw_cues:
        for item in raw_cues:
            try:
                a, b, t = float(item[0]), float(item[1]), str(item[2])
            except Exception:
                continue
            if t.strip() and b > a:
                cues.append((a, b, t))
    if not cues:
        sub_file = str(overlays.get("subtitles_file") or "").strip()
        spath = Path(sub_file) if sub_file else None
        if spath and spath.is_file():
            suffix = spath.suffix.lower()
            if suffix == ".srt":
                cues = _png_subs.parse_srt_cues(spath)
            elif suffix == ".ass":
                parsed = _png_subs.parse_ass_source(spath)
                if parsed:
                    cues = parsed["cues"]
                    file_font_px = parsed.get("font_px")
                    file_visuals = parsed.get("visuals")
                    file_family = parsed.get("font_family")
    if not cues:
        return None
    # Same canonicalizer the ASS writer applied, so the burned text
    # is byte-identical to the pre-Q burn (it is idempotent — the
    # pipeline's cues and the synced files are already canonical).
    cues = [
        (a, b, strip_burmese_subtitle_punctuation(canonicalize_burmese_text(t)))
        for a, b, t in cues
    ]
    cues = [(a, b, t) for a, b, t in cues if t.strip()]
    if not cues:
        return None
    # Batch CX forced the PNG burn to one line; Batch DD (Ulfa,
    # 2026-11-09) supersedes that with the user's Lines 1/2/3
    # choice: cues wrap into up to the chosen line count at the
    # locked size (the ×1.25 multi-line font factor in
    # subtitle_font_px applies again). A file-only call keeps the
    # .ass file's own font size and one-line resolution (it renders
    # what the file describes).
    raw_size = int(overlays.get("subtitles_font_size", 16))
    sub_lines = min(3, max(1, int(overlays.get("sub_lines") or 1)))
    font_px = file_font_px or subtitle_font_px(
        cw, ch, raw_size, 1 if file_font_px else sub_lines, layout
    )
    if file_visuals is not None:
        visuals = file_visuals
    else:
        st_key = str(
            overlays.get("sub_style") or overlays.get("subtitles_style") or "yellow"
        ).lower()
        preset = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
        # Batch DD: one shared font/visual resolution — the preview
        # composite resolves through the same helpers, so preview
        # == render for font and style (free controls included).
        visuals = build_subtitle_visuals(
            preset, overlays_visual_overrides(overlays), ch, font_px
        )
    if file_family:
        font_path = subtitle_font_file(str(file_family))
    else:
        font_path = overlays_font_path(overlays)
    return {
        "cues": cues,
        "font_px": int(font_px),
        "visuals": visuals,
        "font_path": font_path,
        # Batch DD: the job's chosen line count (1 for file-derived
        # jobs, which keep their historical one-line resolution).
        "sub_lines": 1 if file_font_px else sub_lines,
    }


# Batch AB: explicit ffmpeg thread cap for the production overlay
# steps. The production instance has 2 GB RAM; the libx264 quality
# presets pass "-threads 0" (auto), and at 1080×1920 each extra
# encoder/filter thread holds additional frame buffers — the single
# whole-video overlay pass OOM-killed the instance ~15 s in. The
# overlay steps append "-threads 2" AFTER the quality args (the last
# -threads wins in ffmpeg) to bound peak RSS, with negligible speed
# cost on a ≤2 vCPU instance.
# Batch AL-R re-measured on the 4 GB instance (1080×1920, 2 vCPU
# pinned): threads 4 vs 2 — wall 30.5 s vs 32.3 s two-step and
# 25.1 s vs 24.5 s single-pass (within run noise), peak RSS 376-379 MB
# vs 336-337 MB. The +40 MB is real, the speed gain is not: 2 stays.
OVERLAY_FFMPEG_THREADS = 2


# Batch AL-R: the combined single-pass overlay (blur + titles + cues in
# ONE encode) is used only when a short per-job probe encode proves its
# peak RSS fits this ceiling — the 4 GB instance must keep headroom for
# the bot/worker in the same process space. Probe failure, an over-cap
# probe, or a failed single pass all fall back to the Batch AB
# two-step path automatically.
OVERLAY_SINGLE_PASS_MAX_RSS_BYTES = int(3.2 * 1024 * 1024 * 1024)
# Batch CC: this lock is now the ONE global overlay slot — it guards
# EVERY production overlay encode in apply_production_overlays
# (single-pass, both two-step steps, and each chunked run as a unit).
# It began (Batch AL-R) as a single-pass-only lock; the 2026-11-07
# OOM loop showed two jobs' unserialised two-step passes summing
# past the 4 GB instance, so the guard widened to the whole pass.
# (The name is kept: it is the same lock object the AL-R code made.)
_OVERLAY_SINGLE_PASS_SLOT = threading.Lock()


# Batch CC: heavy-job chunked cue overlay. A job is HEAVY when its
# cue graph is big enough to threaten the instance (cue count above
# the threshold) or the video is long: the 2026-11-07 incident job
# had 312 cues / 550 s and its full-graph passes peaked at 3,306 MB
# on the 4 GB production instance. Heavy jobs skip the probe and the
# single-pass entirely (the probe command IS the full graph — its
# production peak was that same 3,306 MB, so probing recreates the
# hazard and its verdict cannot change the route) and run step 2 in
# ≤ 60 s windows, each carrying only the cues that intersect it;
# peak RSS then scales with one window's cue count, not the total.
# Thresholds confirmed against the local bench (fix-cc-work/bench):
# full graph 1,821 MB / two-step step-2 1,848 MB vs chunked ~1/3.
OVERLAY_HEAVY_CUE_COUNT = 120
OVERLAY_HEAVY_DURATION_SECONDS = 240.0
OVERLAY_CHUNK_SECONDS = 60.0


def _overlay_is_heavy(cue_count: int, duration: float | None) -> bool:
    if cue_count > OVERLAY_HEAVY_CUE_COUNT:
        return True
    try:
        return duration is not None and float(duration) > OVERLAY_HEAVY_DURATION_SECONDS
    except (TypeError, ValueError):
        return False


def _plan_overlay_chunks(
    cue_spans: list[tuple[float, float]], duration: float | None
) -> list[tuple[float, float, list[int]]]:
    """Plan the chunked cue pass: fixed ≤60 s windows from 0 to the
    media end (the caller's duration, else the last cue end), each
    carrying the indices of the cues whose display interval
    intersects it. A cue spanning a seam is carried by BOTH windows
    (its enable interval is clipped by each window's own length), so
    the composited result matches the single graph exactly."""
    total = 0.0
    try:
        total = float(duration or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    if cue_spans:
        total = max(total, max(end for _s, end in cue_spans))
    if total <= 0:
        total = OVERLAY_CHUNK_SECONDS
    windows: list[tuple[float, float, list[int]]] = []
    ws = 0.0
    while ws < total - 1e-9:
        we = min(total, ws + OVERLAY_CHUNK_SECONDS)
        idxs = [
            i for i, (s, e) in enumerate(cue_spans) if s < we and e > ws
        ]
        windows.append((ws, we, idxs))
        ws = we
    return windows


# Duplicate _concat_list_line removed — single POSIX-safe definition lives near top of module.


def _run_overlay_probe(cmd: list[str], *, timeout: float = 180.0) -> tuple[int, int, str]:
    """Run a short encode directly (NOT via _run_media) while sampling
    the child's peak RSS from /proc VmHWM. Returns (returncode,
    peak_rss_bytes, stderr_tail). Any spawn/sampling failure yields a
    non-zero rc / zero peak so callers fall back safely."""
    argv = [str(c) for c in cmd]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as exc:
        return 1, 0, f"probe spawn failed: {exc}"
    peak = 0
    deadline = time.monotonic() + max(5.0, float(timeout))
    while proc.poll() is None:
        try:
            with open(f"/proc/{proc.pid}/status", "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        peak = max(peak, int(line.split()[1]) * 1024)
                        break
        except Exception:
            pass
        if time.monotonic() > deadline:
            try:
                proc.kill()
            except Exception:
                pass
            break
        time.sleep(0.02)
    try:
        _, stderr = proc.communicate(timeout=10)
    except Exception:
        stderr = b""
    if peak <= 0:
        try:
            import resource

            peak = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * 1024
        except Exception:
            peak = 0
    rc = proc.returncode if proc.returncode is not None else 1
    return rc, peak, (stderr or b"").decode("utf-8", "ignore")[-500:]


def _karaoke_cue_spans(
    lines: list[str], cue_start: float, cue_end: float
) -> list[tuple[tuple[int, int], float, float]]:
    """Split one cue's duration across phrase/word highlight spans for Method 3."""
    flat = "\n".join(lines)
    dur = max(0.05, float(cue_end) - float(cue_start))
    # Collect non-whitespace spans along the flat string
    raw_tokens: list[tuple[int, int, str]] = []
    pos = 0
    for ln in lines:
        for m in re.finditer(r"\S+", ln):
            raw_tokens.append((pos + m.start(), pos + m.end(), m.group(0)))
        pos += len(ln) + 1
    if len(raw_tokens) <= 1 and lines:
        # Unspaced Burmese cue: partition along tokenized Burmese words (~2..4 beats)
        words = [w for w in tokenize_burmese_words(" ".join(lines)) if w.strip()]
        if len(words) >= 2:
            beats = min(4, len(words))
            per = max(1, len(words) // beats)
            spans_out: list[tuple[tuple[int, int], float, float]] = []
            char_cursor = 0
            beat_groups: list[tuple[int, int, int]] = []
            for bi in range(0, len(words), per):
                grp = words[bi : bi + per]
                grp_txt = "".join(grp)
                idx = flat.replace("\n", "").find(grp_txt, char_cursor)
                start_idx = idx if idx >= 0 else char_cursor
                end_idx = start_idx + len(grp_txt)
                char_cursor = end_idx
                beat_groups.append((start_idx, end_idx, max(1, len(grp_txt))))
            w_tot = sum(w for _, _, w in beat_groups) or 1
            cur_t = cue_start
            for i, (s_i, e_i, w) in enumerate(beat_groups):
                t_en = cue_end if i == len(beat_groups) - 1 else cur_t + dur * (w / w_tot)
                spans_out.append(((s_i, e_i), round(cur_t, 3), round(t_en, 3)))
                cur_t = t_en
            return spans_out
        return [((0, len(flat)), round(cue_start, 3), round(cue_end, 3))]

    # Group adjacent short space-separated tokens up to 4 visual beats so frame count stays modest
    groups: list[tuple[int, int, int]] = []
    if len(raw_tokens) <= 5:
        for s_i, e_i, txt in raw_tokens:
            groups.append((s_i, e_i, max(1, len(txt))))
    else:
        step = max(2, math.ceil(len(raw_tokens) / 5))
        for i in range(0, len(raw_tokens), step):
            chunk = raw_tokens[i : i + step]
            groups.append((chunk[0][0], chunk[-1][1], sum(max(1, len(c[2])) for c in chunk)))

    w_tot = sum(w for _, _, w in groups) or 1
    spans: list[tuple[tuple[int, int], float, float]] = []
    cur_t = cue_start
    for i, (s_i, e_i, w) in enumerate(groups):
        t_en = cue_end if i == len(groups) - 1 else cur_t + dur * (w / w_tot)
        spans.append(((s_i, e_i), round(cur_t, 3), round(t_en, 3)))
        cur_t = t_en
    return spans


def build_png_timeline_concat(
    cue_inputs: list[tuple[Path, int, str, float, float]],
    work_dir: Path,
    cw: int,
    ch: int,
    *,
    layout: dict[str, Any] | None = None,
    sub_x: float | None = None,
    duration: float | None = None,
) -> tuple[Path, int, int] | None:
    """Method 1 (Single-Stream Timeline Overlay): pack all cue PNGs into a
    single fixed-geometry RGBA strip ffconcat timeline so FFmpeg composites all
    subtitles through ONE overlay filter node (~90% lower memory than multi-input).
    Returns (concat_path, strip_top_px, strip_height_px).
    """
    if not cue_inputs:
        return None
    from PIL import Image

    timeline_dir = work_dir / "subtitle_timeline"
    timeline_dir.mkdir(parents=True, exist_ok=True)

    sized: list[tuple[Path, int, int, int, float, float]] = []
    strip_top = ch
    strip_bot = 0
    for png_path, top, _xe, cs, ce in sorted(cue_inputs, key=lambda x: (float(x[3]), float(x[4]))):
        if ce <= cs:
            continue
        with Image.open(png_path) as im:
            pw_png, ph_png = im.size
        t_px = max(0, min(ch - ph_png, int(top)))
        strip_top = min(strip_top, t_px)
        strip_bot = max(strip_bot, t_px + ph_png)
        sized.append((Path(png_path), pw_png, ph_png, t_px, float(cs), float(ce)))

    if not sized:
        return None
    strip_top = max(0, strip_top - (strip_top % 2))
    strip_h = max(2, strip_bot - strip_top)
    if strip_h % 2 != 0:
        strip_h += 1
    strip_w = max(2, int(cw) - (int(cw) % 2))

    blank_path = timeline_dir / "blank.png"
    if not blank_path.is_file():
        Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0)).save(str(blank_path), "PNG", compress_level=1)

    centre_x_px = subtitle_centre_x_px(cw, ch, layout, sub_x)
    ffconcat_lines: list[str] = ["ffconcat version 1.0"]
    cursor = 0.0
    last_frame_path = blank_path
    framed_cache: dict[tuple[str, int, int], Path] = {}

    for idx, (png_path, pw_png, ph_png, t_px, cs, ce) in enumerate(sized):
        if cs > cursor + 0.005:
            ffconcat_lines.append(_concat_list_line(blank_path, posix=True))
            ffconcat_lines.append(f"duration {cs - cursor:.3f}")
            cursor = cs
            last_frame_path = blank_path
        cs_eff = max(cs, cursor)
        if ce <= cs_eff + 0.005:
            continue
        if centre_x_px is not None:
            x_px = max(12, min(strip_w - pw_png - 12, int(round(centre_x_px - pw_png / 2.0))))
        else:
            x_px = max(0, (strip_w - pw_png) // 2)
        y_px = max(0, t_px - strip_top)
        ck = (str(png_path), x_px, y_px)
        framed = framed_cache.get(ck)
        if framed is None:
            framed = timeline_dir / f"tl_{len(framed_cache):04d}_{png_path.stem}.png"
            if not framed.is_file():
                canvas = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
                with Image.open(png_path) as cue_im:
                    canvas.paste(cue_im.convert("RGBA"), (x_px, y_px))
                canvas.save(str(framed), "PNG", compress_level=1)
            framed_cache[ck] = framed
        ffconcat_lines.append(_concat_list_line(framed, posix=True))
        ffconcat_lines.append(f"duration {ce - cs_eff:.3f}")
        cursor = ce
        last_frame_path = framed

    if duration is not None and float(duration) > cursor + 0.005:
        ffconcat_lines.append(_concat_list_line(blank_path, posix=True))
        ffconcat_lines.append(f"duration {float(duration) - cursor:.3f}")
        last_frame_path = blank_path

    # FFmpeg concat demuxer requires repeating the final frame after its duration.
    ffconcat_lines.append(_concat_list_line(last_frame_path, posix=True))
    concat_file = timeline_dir / "subtitles_timeline.ffconcat"
    concat_file.write_text("\n".join(ffconcat_lines) + "\n", encoding="utf-8")
    return concat_file, strip_top, strip_h


def mux_soft_subtitle_track(
    ffmpeg: str,
    video_path: Path,
    srt_path: Path,
    output_path: Path | None = None,
) -> Path:
    """Method 4 (Dual Soft Subtitles): stream-copy video/audio and embed
    an ISO-compliant UTF-8 ``mov_text`` closed-caption track into the MP4 container
    in <1s without re-encoding. Fails open back to ``video_path`` on error.
    """
    src = Path(video_path)
    srt = Path(srt_path)
    if not src.is_file() or not srt.is_file() or srt.stat().st_size == 0:
        return src
    target = Path(output_path) if output_path is not None else src
    tmp_out = target.with_name(f".{target.stem}.softsub.mp4")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-sub_charenc", "UTF-8", "-i", str(srt),
        "-map", "0:v", "-map", "0:a?", "-map", "1:0",
        "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=mya",
        "-metadata:s:s:0", "title=Myanmar",
        "-movflags", "+faststart",
        str(tmp_out),
    ]
    res = _run_media(cmd, timeout=180.0, what="Soft subtitle mov_text mux")
    if res.returncode == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 1000:
        if target == src:
            os.replace(tmp_out, src)
            return src
        os.replace(tmp_out, target)
        return target
    try:
        tmp_out.unlink(missing_ok=True)
    except OSError:
        pass
    return src


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
    duration: float | None = None,
    timings_out: dict[str, float] | None = None,
    progress_fn: Any = None,
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
    # Batch DD: the title overlay feature is REMOVED — no title_text
    # is read or rendered anywhere in the overlay pass. Legacy
    # overlays dicts carrying title_* keys are simply ignored.
    logo_enabled = bool(overlays.get("logo_enabled", True))
    logo_path = str(overlays.get("logo_path") or "").strip() if logo_enabled else ""
    logo_file = Path(logo_path) if logo_path else None
    extra_text = str(overlays.get("extra_text") or "").strip()
    subtitles_file = str(overlays.get("subtitles_file") or "").strip()
    subtitles_path = Path(subtitles_file) if subtitles_file else None
    watermark_mode = bool(overlays.get("watermark"))
    burn_subtitles_flag = bool(overlays.get("burn_subtitles", True))
    sub_render_mode = str(overlays.get("sub_render_mode") or "png").strip().lower()
    if sub_render_mode not in ("png", "timeline", "ass", "karaoke", "soft"):
        sub_render_mode = "png"
    if not burn_subtitles_flag or sub_render_mode == "soft":
        sub_job = None
    else:
        # Batch Q: subtitles burn as PNG overlays (png_subtitles), or Method 1
        # timeline / Method 2 ASS / Method 3 karaoke when selected.
        sub_job = _resolve_png_subtitle_job(
            overlays, cw, ch, {"pw": pw, "ph": ph, "ox": ox, "oy": oy}
        )
    if not boxes and not extra_text and not watermark_mode and not (logo_file and logo_file.is_file()) and not (subtitles_path and subtitles_path.is_file() and burn_subtitles_flag and sub_render_mode != "soft") and sub_job is None:
        return input_video

    # Batch AB: the overlay pass runs as TWO sequential ffmpeg steps
    # (blur boxes first, then everything else — see the step builders
    # below) to bound the peak memory of the pass, so the PNG assets
    # are rendered up front here and each step registers its own
    # inputs. Registration order is unchanged: logo, extra
    # text, watermark pattern, then one input per subtitle cue PNG.
    extra_file: Path | None = None
    logo_present = bool(logo_file and logo_file.is_file())
    if extra_text:
        extra_file = _render_extra_text_png(
            extra_text,
            work_dir / "extra_text_overlay.png",
            int(overlays.get("extra_text_font_size", 42)),
            0.18,
            max_width=text_width,
        )
    # Free-tier brand watermark v3 (2026-10-30): when the pipeline flags
    # overlays["watermark"], the extra-text panel below is REPLACED by a
    # single mark — the full-frame diagonal tiled pattern of the bot
    # username (crop-resistant; see render_brand_pattern_png). The v2
    # corner lockup is gone entirely (owner: it looked cheap and collided
    # with a pattern tile). The pattern is registered as an input here but
    # composited LAST in the graph (after subtitles), and this whole pass
    # runs after the mirror/zoom/ratio part renders, so the mark is never
    # mirrored.
    pattern_file: Path | None = None
    if watermark_mode:
        wm_opts = resolve_watermark_overlay_options(overlays)
        pattern_file = render_brand_pattern_png(
            work_dir / "brand_pattern.png",
            frame_width=pw,
            frame_height=ph,
            opacity=wm_opts["opacity"],
            style=wm_opts["style"],
            position=wm_opts["position"],
            size_pct=wm_opts["size_pct"],
        )

    # Step 1's filter graph: ONLY the blur-box chains, exactly as
    # they were built for the old single graph.
    blur_graph: list[str] = []
    blur_current = "[0:v]"
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
        # Batch DD: a box may carry its own strength (the manual
        # box list); detected/global boxes keep blur_strength.
        try:
            box_strength = max(4, min(40, int(b.get("strength")))) if b.get("strength") is not None else blur_strength
        except (TypeError, ValueError):
            box_strength = blur_strength
        safe_luma = max(2, min(box_strength, (min(w, h) // 2) - 1))
        safe_chroma = max(1, min(safe_luma // 2, 9))
        base = f"bbase{i}"; crop = f"bcrop{i}"; blur = f"bblur{i}"; out = f"bv{i}"
        blur_graph.append(f"{blur_current}split=2[{base}][{crop}]")
        blur_graph.append(
            f"[{crop}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={safe_luma}:luma_power=2:"
            f"chroma_radius={safe_chroma}:chroma_power=1[{blur}]"
        )
        blur_graph.append(f"[{base}][{blur}]overlay={x}:{y}[{out}]")
        blur_current = f"[{out}]"

    # Batch Q cue PNGs, rendered up front (the renderer cache lives in
    # work_dir as before): (png_path, block_top_px, x_expr, cue_start,
    # cue_end). Batch AG: the x expression comes from the one shared
    # horizontal resolver — centred verbatim by default.
    cue_inputs: list[tuple[Path, int, str, float, float]] = []
    ass_burn_file: Path | None = None
    timeline_spec: tuple[Path, int, int] | None = None
    if sub_job is not None:
        _t_png0 = time.monotonic()
        lay_dict = {"pw": pw, "ph": ph, "ox": ox, "oy": oy}
        if sub_render_mode == "ass" and subtitles_path and subtitles_path.is_file() and subtitles_path.suffix.lower() == ".ass":
            ass_burn_file = subtitles_path
        elif sub_render_mode == "ass":
            # Generate an on-the-fly ASS file with requested effect if only cues/.srt exist
            ass_burn_file = write_synced_ass_file(
                cues=sub_job["cues"],
                ass_path=work_dir / "recap_inline_burn.ass",
                cw=cw,
                ch=ch,
                font_name=str(overlays.get("subtitles_font") or overlays.get("sub_font") or ""),
                raw_font_size=int(overlays.get("subtitles_font_size", 16)),
                sub_style=str(overlays.get("sub_style") or overlays.get("subtitles_style") or "yellow"),
                sub_lines=sub_job["sub_lines"],
                sub_y=overlays.get("sub_y"),
                sub_margin=overlays.get("sub_margin"),
                sub_x=overlays.get("sub_x"),
                subtitles_margin_v=overlays.get("subtitles_margin_v"),
                boxes=boxes,
                layout=lay_dict,
                sub_visual_overrides=overlays_visual_overrides(overlays),
                sub_effect=str(overlays.get("sub_effect") or "none"),
            )
        else:
            renderer = _png_subs.PngSubtitleRenderer(work_dir / "subtitle_png_cache")
            x_expr = _subtitle_overlay_x_expr(cw, ch, lay_dict, overlays.get("sub_x"))
            fit_max_w = _subtitle_fit_width_px(overlays, pw)

            def _render_cue_item(cue_item):
                cue_start, cue_end, cue_text = cue_item
                pieces: list[tuple[Path, int, str, float, float]] = []
                for fit_lines, fit_px, fit_visuals, piece_start, piece_end in fit_png_cue_pieces(
                    renderer,
                    cue_text,
                    sub_job["font_px"],
                    sub_job["font_path"],
                    sub_job["visuals"],
                    fit_max_w,
                    cue_start,
                    cue_end,
                    max_lines=sub_job["sub_lines"],
                ):
                    if not fit_lines:
                        continue
                    if sub_render_mode == "karaoke":
                        for k_span, k_st, k_en in _karaoke_cue_spans(fit_lines, piece_start, piece_end):
                            k_res = renderer.render_karaoke_lines(
                                fit_lines, k_span, fit_px, sub_job["font_path"], fit_visuals
                            )
                            if k_res is None:
                                continue
                            png_path, _png_w, png_h = k_res
                            top = _subtitle_block_top_px(
                                cw, ch, lay_dict,
                                sub_y=overlays.get("sub_y"),
                                subtitles_margin_v=overlays.get("subtitles_margin_v"),
                                boxes=boxes,
                                font_px=sub_job["font_px"],
                                sub_lines=sub_job["sub_lines"],
                                block_h=png_h,
                                sub_margin=overlays.get("sub_margin"),
                            )
                            pieces.append((png_path, top, x_expr, float(k_st), float(k_en)))
                    else:
                        png_path, _png_w, png_h = renderer.render_lines(
                            fit_lines, fit_px, sub_job["font_path"], fit_visuals
                        )
                        top = _subtitle_block_top_px(
                            cw, ch, lay_dict,
                            sub_y=overlays.get("sub_y"),
                            subtitles_margin_v=overlays.get("subtitles_margin_v"),
                            boxes=boxes,
                            font_px=sub_job["font_px"],
                            sub_lines=sub_job["sub_lines"],
                            block_h=png_h,
                            sub_margin=overlays.get("sub_margin"),
                        )
                        pieces.append((png_path, top, x_expr, float(piece_start), float(piece_end)))
                return pieces

            if progress_fn and sub_job["cues"]:
                _emit_progress_stage(progress_fn, f"Rendering subtitle PNGs ({len(sub_job['cues'])} cues)")
            if len(sub_job["cues"]) > 1:
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="recap-png") as _png_pool:
                    for _pieces in _png_pool.map(_render_cue_item, sub_job["cues"]):
                        cue_inputs.extend(_pieces)
            else:
                for _cue_item in sub_job["cues"]:
                    cue_inputs.extend(_render_cue_item(_cue_item))

            if sub_render_mode in ("timeline", "karaoke") and cue_inputs:
                timeline_spec = build_png_timeline_concat(
                    cue_inputs, work_dir, cw, ch,
                    layout=lay_dict, sub_x=overlays.get("sub_x"), duration=duration,
                )
        if timings_out is not None:
            timings_out["png"] = round(max(0.0, time.monotonic() - _t_png0), 3)

    if progress_fn:
        _emit_progress_stage(progress_fn, "Compositing video overlay")

    def _build_step2_cmd(
        step_encoder: str,
        *,
        input_path: Any = None,
        graph_head: list[str] | None = None,
        start_label: str = "[0:v]",
        cue_subset: list[int] | None = None,
        window: tuple[float, float] | None = None,
        output_path: Any = None,
    ) -> list[str]:
        # Batch AL-R: with graph_head/start_label the SAME builder
        # produces the combined single-pass command (blur chains first,
        # then this step's overlays continuing from the blur output
        # label, reading the original input). Defaults reproduce the
        # classic step-2 command byte for byte.
        # Batch CC: cue_subset/window/output_path produce ONE chunked
        # window command instead — the input is sought to the window,
        # only the subset's cues are registered, their enable times
        # are shifted into window time, and the output is the chunk
        # file (an intermediate: no +faststart, like step 1's). The
        # graph construction and encoder settings are untouched.
        cues_here = cue_inputs if cue_subset is None else [cue_inputs[i] for i in cue_subset]
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if window is not None:
            cmd += ["-ss", f"{window[0]:.3f}", "-t", f"{window[1] - window[0]:.3f}"]
        cmd += ["-i", str(input_path if input_path is not None else step2_input)]
        input_index = 1
        logo_idx: int | None = None
        extra_idx: int | None = None
        pattern_idx: int | None = None
        if logo_present:
            logo_idx = input_index
            cmd += ["-i", str(logo_file)]
            input_index += 1
        if extra_file is not None:
            extra_idx = input_index
            cmd += ["-i", str(extra_file)]
            input_index += 1
        if pattern_file is not None:
            pattern_idx = input_index
            cmd += ["-i", str(pattern_file)]
            input_index += 1
        cue_indices: list[int] = []
        timeline_idx: int | None = None
        if ass_burn_file is not None:
            pass
        elif timeline_spec is not None and window is None and cue_subset is None:
            timeline_idx = input_index
            cmd += ["-f", "concat", "-safe", "0", "-i", str(timeline_spec[0])]
            input_index += 1
        else:
            for png_path, _top, _xe, _cs, _ce in cues_here:
                cue_indices.append(input_index)
                cmd += ["-i", str(png_path)]
                input_index += 1

        graph: list[str] = list(graph_head or [])
        current = start_label

        if logo_idx is not None:
            pos = overlays.get("logo_position") or [0.80, 0.06]
            lx = _clamp01(pos[0] if len(pos) > 0 else 0.80, 0.80)
            ly = _clamp01(pos[1] if len(pos) > 1 else 0.06, 0.06)
            lw = max(0.04, min(0.40, float(overlays.get("logo_width_fraction", 0.16))))
            lo = max(0.10, min(1.00, float(overlays.get("logo_opacity", 0.70))))
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

        # Batch Q: burn each cue as a Pillow+raqm PNG overlay in the
        # same graph position the libass burn occupied (after title /
        # logo / extra text, before the watermark pattern). The blur
        # chains ran in step 1; vertical position from
        # subtitle_centre_px via _subtitle_block_top_px (the Batch P
        # anchor), computed when the cue PNGs were rendered above;
        # horizontal placement is the centred expression by default,
        # or the Batch AG sub_x resolver expression when the user
        # moved the subtitle sideways.
        if ass_burn_file is not None:
            _ass_esc = str(ass_burn_file.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            _fdir_esc = str(bundled_fonts_dir().resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            graph.append(f"{current}ass=filename='{_ass_esc}':fontsdir='{_fdir_esc}'[vsubass]")
            current = "[vsubass]"
        elif timeline_idx is not None and timeline_spec is not None:
            _tl_top = int(timeline_spec[1])
            graph.append(f"{current}[{timeline_idx}:v]overlay=x=0:y={_tl_top}:eof_action=pass[vsubtl]")
            current = "[vsubtl]"
        else:
            for (png_path, top, x_expr, cue_start, cue_end), idx in zip(cues_here, cue_indices):
                if window is not None:
                    cue_start = max(0.0, cue_start - window[0])
                    cue_end = cue_end - window[0]
                out = f"vsub{idx}"
                graph.append(
                    f"{current}[{idx}:v]overlay=x='{x_expr}':y={top}:"
                    f"eof_action=repeat:enable='between(t,{cue_start:.3f},{cue_end:.3f})'[{out}]"
                )
                current = f"[{out}]"

        if pattern_idx is not None:
            # Diagonal tiled pattern over the WHOLE picture area — centre and
            # edges alike, so no crop can remove it. It is composited over the
            # burned subtitles at ~0.19 opacity: visible as a brand texture,
            # never enough to hurt subtitle readability.
            graph.append(f"{current}[{pattern_idx}:v]overlay=x='{ox}':y='{oy}':eof_action=repeat[vpat]")
            current = "[vpat]"

        if bool(overlays.get("protection_strong")):
            graph.append(f"{current}noise=alls=6:allf=t,format=yuv420p[vout]")
        else:
            graph.append(f"{current}format=yuv420p[vout]")
        cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map_metadata", "-1", "-an"] + encoder_args_for_quality(step_encoder, render_quality) + ["-threads", str(OVERLAY_FFMPEG_THREADS)]
        if output_path is None:
            cmd += ["-movflags", "+faststart", str(output_video)]
        else:
            cmd += [str(output_path)]
        return cmd

    def _run_overlay_step(build_cmd, step_encoder: str):
        """Run one overlay step; on an NVENC failure rebuild that step
        with libx264 (same thread cap) and report libx264 as the
        encoder in use from then on."""
        p = _run_media(build_cmd(step_encoder), timeout=ffmpeg_timeout(duration), what="Production overlay render")
        if p.returncode != 0 and step_encoder == "h264_nvenc":
            log("⚠️ Overlay NVENC failed — libx264 fallback")
            p = _run_media(build_cmd("libx264"), timeout=ffmpeg_timeout(duration), what="Production overlay render")
            return "libx264", p
        return step_encoder, p

    def _run_chunked_step2() -> None:
        """Batch CC: the heavy-job cue/title step, in ≤60 s windows.

        Each window re-runs the step-2 builder restricted to the cues
        intersecting it (the full-duration title / logo / extra text /
        watermark pattern ride in every window, exactly as the single
        graph applies them), then the windows are concatenated with
        stream copy — identical encoder settings throughout, so the
        output is the step-2 output produced at a fraction of the
        peak memory. Runs as one unit inside the caller's overlay
        slot acquisition; failures raise exactly like step 2's."""
        nonlocal used_encoder
        total = duration
        if not total:
            # The pipeline always passes its audio duration; a caller
            # that did not gets the post-blur input probed rather
            # than a window plan truncated at the last cue's end.
            try:
                total = probe_duration(
                    resolve_binary(None, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"]),
                    step2_input,
                )
            except Exception:
                total = None
        windows = _plan_overlay_chunks(
            [(cs, ce) for _p, _t, _x, cs, ce in cue_inputs], total
        )
        log(
            f"🧩 Overlay: heavy job ({len(cue_inputs)} cues) — chunked cue pass "
            f"in {len(windows)} windows of ≤{OVERLAY_CHUNK_SECONDS:.0f}s"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        chunk_files: list[Path] = []
        list_path = work_dir / "overlay_chunks.txt"
        try:
            for k, (ws, we, idxs) in enumerate(windows):
                chunk_path = work_dir / f"overlay_chunk_{k:03d}.mp4"

                def _chunk_cmd(enc: str, _ws=ws, _we=we, _idxs=idxs, _out=chunk_path) -> list[str]:
                    return _build_step2_cmd(
                        enc,
                        cue_subset=_idxs,
                        window=(_ws, _we),
                        output_path=_out,
                    )

                used_encoder, p = _run_overlay_step(_chunk_cmd, used_encoder)
                if p.returncode != 0 or not chunk_path.is_file() or chunk_path.stat().st_size < 5000:
                    raise RuntimeError("Production overlay render failed: " + p.stderr[-3000:])
                chunk_files.append(chunk_path)
            list_path.write_text(
                # Absolute entries: the concat demuxer resolves
                # relative ones against the LIST's directory, not the
                # process CWD — a relative work_dir would double its
                # prefix and open nothing.
                "".join(_concat_list_line(cpath.resolve()) + "\n" for cpath in chunk_files),
                encoding="utf-8",
            )
            concat_cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-map_metadata", "-1", "-c", "copy",
                "-movflags", "+faststart", str(output_video),
            ]
            pc = _run_media(concat_cmd, timeout=ffmpeg_timeout(duration), what="Production overlay render")
            if pc.returncode != 0 or not output_video.is_file() or output_video.stat().st_size < 5000:
                raise RuntimeError("Production overlay render failed: " + pc.stderr[-3000:])
        finally:
            for chunk_path in chunk_files:
                try:
                    chunk_path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                list_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _attempt_single_pass() -> bool:
        """Batch AL-R: combined blur+overlay encode, gated by a short
        RAM probe. Returns True only when the single pass produced the
        final output; every failure path returns False and the caller
        runs the Batch AB two-step path unchanged. Batch CC: the
        caller holds the global overlay slot for the whole pass, so
        the attempt no longer acquires it (or falls back on busy)."""
        nonlocal used_encoder
        try:
            def _single_cmd(enc: str) -> list[str]:
                return _build_step2_cmd(
                    enc, input_path=input_video,
                    graph_head=blur_graph, start_label=blur_current,
                )

            work_dir.mkdir(parents=True, exist_ok=True)
            # Single-stream Timeline (ffconcat) & vector ASS peak at ~250 MB RSS
            # regardless of cue count — skip the redundant 10s full-res probe encode
            # to save ~10-30s on every recap job (legacy multi-input PNG keeps the probe).
            if timeline_spec is not None or ass_burn_file is not None:
                log("⚡ Overlay fast single-pass (timeline/ASS low-RSS path — skipping 10s probe)")
            else:
                probe_path = work_dir / "overlay_probe.mp4"
                probe_seconds = 10.0
                if duration:
                    probe_seconds = min(10.0, max(1.0, float(duration)))
                probe_cmd = _single_cmd(used_encoder)[:-1] + ["-t", f"{probe_seconds:.3f}", str(probe_path)]
                rc, peak, _tail = _run_overlay_probe(probe_cmd, timeout=180)
                try:
                    probe_path.unlink(missing_ok=True)
                except OSError:
                    pass
                cap = OVERLAY_SINGLE_PASS_MAX_RSS_BYTES
                if rc != 0 or peak <= 0 or peak > cap:
                    log(
                        f"🧪 Overlay probe: rc={rc}, peak RSS {peak / 1048576:.0f} MB "
                        f"(cap {cap / 1048576:.0f} MB) — two-step path"
                    )
                    return False
                log(f"🧪 Overlay probe OK: peak RSS {peak / 1048576:.0f} MB — single-pass overlay")
            used_encoder, p_single = _run_overlay_step(_single_cmd, used_encoder)
            if p_single.returncode != 0 or not output_video.is_file() or output_video.stat().st_size < 5000:
                log("⚠️ Overlay single pass failed — two-step fallback")
                return False
            return True
        except Exception as single_exc:
            log(f"⚠️ Overlay single-pass attempt error ({single_exc}) — two-step fallback")
            return False

    used_encoder = encoder
    intermediate: Path | None = None
    step2_input = input_video
    single_done = False
    # When using Single-Stream Timeline or ASS vector burn, peak RSS is ~250MB
    # even for 300+ cues — skip the slow 60s chunked re-encode loop!
    heavy = (
        False
        if (timeline_spec is not None or ass_burn_file is not None)
        else _overlay_is_heavy(len(cue_inputs), duration)
    )
    # Batch CC: EVERY production overlay encode below runs under the
    # one global overlay slot (it previously serialised single-pass
    # only, so two jobs' two-step passes could sum past the instance
    # memory). A second job's overlay waits here — its stage text is
    # already up — instead of OOM-ing both. Heavy jobs skip the
    # single-pass attempt (see the OVERLAY_HEAVY_* note above) and
    # route their step 2 to the chunked pass.
    _t_ov0 = time.monotonic()
    with _OVERLAY_SINGLE_PASS_SLOT:
        if boxes and not heavy:
            single_done = _attempt_single_pass()
        if boxes and not single_done:
            # STEP 1: blur boxes only, into a scratch intermediate (no
            # +faststart — it is re-read immediately by step 2 and deleted
            # on success; a failed pass re-runs both steps from scratch).
            intermediate = work_dir / "overlay_step1_blurred.mp4"
            work_dir.mkdir(parents=True, exist_ok=True)

            def _build_step1_cmd(step_encoder: str) -> list[str]:
                graph1 = blur_graph + [f"{blur_current}format=yuv420p[vout]"]
                return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_video)] + ["-filter_complex", ";".join(graph1), "-map", "[vout]", "-map_metadata", "-1", "-an"] + encoder_args_for_quality(step_encoder, render_quality) + ["-threads", str(OVERLAY_FFMPEG_THREADS), str(intermediate)]

            used_encoder, p1 = _run_overlay_step(_build_step1_cmd, used_encoder)
            if p1.returncode != 0:
                raise RuntimeError("Production overlay render failed: " + p1.stderr[-3000:])
            step2_input = intermediate

        if not single_done:
            if heavy:
                # STEP 2 (chunked): the same title / logo / extra text
                # / subtitle cues / watermark pattern, in ≤60 s
                # windows over the (possibly pre-blurred) video.
                _run_chunked_step2()
            else:
                # STEP 2: title / logo / extra text / subtitle cues /
                # watermark pattern over the (possibly pre-blurred)
                # video.
                used_encoder, p = _run_overlay_step(_build_step2_cmd, used_encoder)
                if p.returncode != 0 or not output_video.is_file() or output_video.stat().st_size < 5000:
                    raise RuntimeError("Production overlay render failed: " + p.stderr[-3000:])
    if timings_out is not None:
        timings_out["overlay"] = round(max(0.0, time.monotonic() - _t_ov0), 3)
    if intermediate is not None:
        try:
            intermediate.unlink()
        except OSError:
            pass
    return output_video


# Batch AK (premium audit P2/N4): ONE sample sentence for every
# preview surface — the engine preview's default and the fallback
# below both use it, and the Mini App no longer keeps a different
# sentence of its own (its canvas shows this engine composite).
PREVIEW_SAMPLE_TEXT = "ဒီတစ်ခါမှာတော့ ဘယ်သူမှ မထင်မှတ်ထားတဲ့ အဖြစ်အပျက် ဖြစ်လာပါတယ်"


def extract_preview_with_overlays(
    ffmpeg: str,
    source_video: Path | str,
    output_image: Path | str,
    timestamp: float = 3.0,
    overlays: dict[str, Any] | None = None,
    output_ratio: str = "Original",
    frame_fill: str = "blur",
    mirror_video: bool = True,
    zoom_factor: float = 1.1,
    sample_text: str = PREVIEW_SAMPLE_TEXT,
    work_dir: Path | str | None = None,
    ffprobe: str | None = None,
    crop_top: float = 0.12,
    crop_bottom: float = 0.12,
    crop_left: float = 0.0,
    crop_right: float = 0.0,
    max_short_side: int | None = None,
) -> Path:
    """Extracts a single representative frame with layout, blur box, and sample 1-line subtitle applied."""
    source = Path(source_video).resolve()
    if str(output_ratio or "").strip().lower() == "original":
        crop_top = crop_bottom = crop_left = crop_right = 0.0
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
        max_short_side=max_short_side,
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

        # Batch DD: a box may carry its own strength (the manual
        # box list); detected/global boxes keep blur_strength.
        try:
            box_strength = max(4, min(40, int(b.get("strength")))) if b.get("strength") is not None else blur_strength
        except (TypeError, ValueError):
            box_strength = blur_strength
        safe_luma = max(2, min(box_strength, (min(w, h) // 2) - 1))
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

    # Batch DD: the title overlay feature is REMOVED — production
    # no longer burns it, so the preview no longer shows it either
    # (preview == render). Legacy title_* overlay keys are ignored.

    cmd_inputs: list[str] = ["-ss", f"{seek_sec:.2f}", "-i", str(source)]
    _next_idx = 1
    logo_idx: int | None = None
    if logo_file:
        logo_idx = _next_idx
        _next_idx += 1
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
        # Batch DD: the sample honours the user's Lines 1/2/3
        # choice, exactly like the production burn (CX's forced
        # one-line is superseded); an overflowing sample shows its
        # first fitting piece at the chosen line count.
        sub_lines = min(3, max(1, int(overlays.get("sub_lines") or 1)))
        sample = str(sample_text or "").strip()
        if not sample:
            sample = PREVIEW_SAMPLE_TEXT
        sample = sample.replace("\n", " ").strip()

        # Batch Q: the sample subtitle is a Pillow+raqm PNG overlay
        # (png_subtitles), exactly like the production burn — the
        # preview frame shows the same shaping, size table and
        # anchor the finished video will carry.
        _, sub_font = resolve_subtitle_font(
            str(overlays.get("subtitles_font") or overlays.get("sub_font") or ensure_system_myanmar_fonts())
        )
        font_px = subtitle_font_px(
            cw, ch, int(overlays.get("subtitles_font_size", 16)), sub_lines, layout
        )
        st_key = str(
            overlays.get("sub_style") or overlays.get("subtitles_style") or "yellow"
        ).lower()
        preset = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
        # Batch DD: same shared resolution as the production burn
        # (build_subtitle_visuals + overlays_font_path) — preview
        # == render for font, style and free controls.
        visuals = build_subtitle_visuals(
            preset, overlays_visual_overrides(overlays), ch, font_px
        )
        font_file = overlays_font_path(overlays)
        renderer = _png_subs.PngSubtitleRenderer(temp_dir / "subtitle_png_cache")
        # Batch AG: the sample goes through the same hard fit as the
        # production burn (same bound: sub_w of the picture width),
        # so the preview frame never shows a wider line than the
        # finished video will carry.
        fit_lines, fit_px, fit_visuals = fit_png_cue_lines(
            renderer,
            strip_burmese_subtitle_punctuation(canonicalize_burmese_text(sample)),
            sub_lines, font_px,
            font_file, visuals,
            _subtitle_fit_width_px(overlays, pw),
        )
        rendered = None
        if fit_lines:
            if str(overlays.get("sub_render_mode") or "").lower() == "karaoke":
                first_w = fit_lines[0].split()[0] if " " in fit_lines[0] else fit_lines[0][: max(1, len(fit_lines[0]) // 3)]
                rendered = renderer.render_karaoke_lines(
                    fit_lines, (0, max(1, len(first_w))), fit_px, font_file, fit_visuals
                )
            else:
                rendered = renderer.render_lines(
                    fit_lines, fit_px, font_file, fit_visuals
                )
        if rendered is not None:
            png_path, _png_w, png_h = rendered
            sub_idx = _next_idx
            _next_idx += 1
            cmd_inputs += ["-i", str(png_path)]
            top = _subtitle_block_top_px(
                cw, ch, layout,
                sub_y=overlays.get("sub_y"),
                subtitles_margin_v=overlays.get("subtitles_margin_v"),
                boxes=boxes,
                font_px=font_px,
                sub_lines=sub_lines,
                block_h=png_h,
                sub_margin=overlays.get("sub_margin"),
            )
            out = "vsub"
            graph.append(f"{current}[{sub_idx}:v]overlay=x='{_subtitle_overlay_x_expr(cw, ch, layout, overlays.get('sub_x'))}':y={top}[{out}]")
            current = f"[{out}]"

    # Admin Watermark Controls: preview composite renders the watermark
    # identically to the delivered video (same style, position, opacity,
    # size for the viewing account's tier).
    if bool(overlays.get("watermark")):
        wm_opts = resolve_watermark_overlay_options(overlays)
        wm_png = render_brand_pattern_png(
            temp_dir / "preview_watermark.png",
            frame_width=pw,
            frame_height=ph,
            opacity=wm_opts["opacity"],
            style=wm_opts["style"],
            position=wm_opts["position"],
            size_pct=wm_opts["size_pct"],
        )
        wm_idx = _next_idx
        _next_idx += 1
        cmd_inputs += ["-i", str(wm_png)]
        out = "vwm"
        graph.append(f"{current}[{wm_idx}:v]overlay=x='{ox}':y='{oy}'[{out}]")
        current = f"[{out}]"

    graph.append(f"{current}format=yuv420p[vout]")

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
    ] + cmd_inputs + [
        "-filter_complex", ";".join(graph),
        "-map", "[vout]",
        "-frames:v", "1",
        "-q:v", "6",
        "-strict", "-2",
        "-update", "1",
        str(out_img),
    ]
    p = _run_media(cmd, timeout=PROBE_TIMEOUT_SECONDS, what="Preview extraction")
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
    # Batch DP: class-level defaults so partially-constructed
    # instances (older tests build EdgeSmartSync via __new__ and set
    # only the attributes they exercise) read as a FREE job with no
    # voice-choice mode; __init__ shadows these per instance.
    paid_tier: bool = False
    voice_choice_mode: str | None = None

    def __init__(
        self,
        *,
        job_dir: str = "",
        source_path: str | None = None,
        plan_path: str | None = None,
        voice: str,
        edge_tts_rate: str = DEFAULT_SPEED,
        edge_tts_pitch: str = "+0Hz",
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        max_video_speed: float = 1.25,
        max_video_slow: float = 1.35,
        mirror_video: bool = True,
        zoom_factor: float = 1.1,
        smooth_freeze_fallback: bool = False,
        max_freeze_hold: float = 0.75,
        render_final_video: bool = True,
        output_ratio: str = "16:9",
        frame_fill: str = "blur",
        crop_top: float = 0.12,
        crop_bottom: float = 0.12,
        crop_left: float = 0.0,
        crop_right: float = 0.0,
        overlays: dict[str, Any] | None = None,
        log: LogFn = _noop,
        progress: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        max_run_seconds: float | None = None,
        tts_workers: int | None = None,
        gemini_route: Any = None,
        paid_tier: bool = False,
        voice_choice_mode: str | None = None,
    ) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.source_path = Path(source_path).resolve() if source_path else (self.job_dir / "source.mp4")
        self.plan_path = Path(plan_path).resolve() if plan_path else (self.job_dir / "smart_sync_plan.json")
        self.voice = voice
        self.edge_tts_rate = resolve_speed_rate(edge_tts_rate or DEFAULT_SPEED)
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
        if self.output_ratio == "Original":
            self.crop_top = 0.0
            self.crop_bottom = 0.0
            self.crop_left = 0.0
            self.crop_right = 0.0
        else:
            self.crop_top = float(crop_top or 0.0)
            self.crop_bottom = float(crop_bottom or 0.0)
            self.crop_left = float(crop_left or 0.0)
            self.crop_right = float(crop_right or 0.0)
        self.smooth_freeze_fallback = bool(smooth_freeze_fallback)
        self.max_freeze_hold = max(0.15, min(float(max_freeze_hold), 1.50))
        self.render_final_video = bool(render_final_video)
        # Batch CG: the BYOK route context for Gemini narration
        # (GeminiRouteContext or None). A plain in-memory attribute —
        # it must NEVER ride through ``overlays``, which the pipeline
        # persists into checkpoint/plan JSON on disk. Finalised into
        # self._gemini_outcome after the TTS stage for the caller's
        # FREE taste accounting.
        self.gemini_route = gemini_route
        self._gemini_outcome: Optional[dict] = None
        self._gemini_routed: Any = None
        # Batch DP: park-and-ask plumbing. ``paid_tier`` marks a job
        # whose owner pays (PURCHASED or admin): its VoxCPM walks use
        # the paid reserve and quota exhaustion PARKS the job instead
        # of degrading a segment. ``voice_choice_mode`` is the owner's
        # resumed choice ("edge_all" / "edge_mix") or None; run()
        # also adopts a mode persisted in the checkpoint when the
        # caller passes none, so a resumed run can never drift back.
        self.paid_tier = bool(paid_tier)
        self.voice_choice_mode = (
            voice_choice_mode
            if voice_choice_mode in ("edge_all", "edge_mix")
            else None
        )
        # Short pauses: see trim_edge_silence. Trims excessive TTS dead air while keeping natural breath.
        self.short_pauses = True
        self.overlays = dict(overlays or {})
        self.overlays.setdefault("protection_level", "strong")
        if "protection_strong" not in self.overlays:
            self.overlays["protection_strong"] = (
                normalize_protection_level(self.overlays.get("protection_level")) == "strong"
            )
        # Render quality preset (overlays key set by recap_pipeline);
        # "standard" == the historical encode, so old callers are
        # unaffected. Included in the video-part checkpoint fingerprint
        # below so changing quality re-renders instead of reusing parts
        # encoded at another quality.
        self.render_quality = normalize_render_quality(self.overlays.get("render_quality"))
        self.log = log
        self.progress = progress or (lambda a, b, c: None)
        self.should_stop = should_stop or (lambda: False)
        # Whole-job time budget (audit fix 2026-11-03): run() arms the
        # deadline from the source duration; _check_run_deadline turns
        # an over-budget job into a failure (refund path) instead of
        # an endless hang. None = derive from the source duration.
        self.max_run_seconds = max_run_seconds
        self._run_started: float | None = None
        self._run_budget: float | None = None
        # Batch AL: TTS worker-pool width. Explicit argument wins, then
        # the RECAP_TTS_WORKERS env override, then DEFAULT_TTS_WORKERS.
        # 1 = the pre-AL strictly serial stage (same code path, one
        # worker), kept as the operational rollback.
        if tts_workers is None:
            _env_workers = str(os.getenv("RECAP_TTS_WORKERS", "") or "").strip()
            try:
                tts_workers = int(_env_workers) if _env_workers else DEFAULT_TTS_WORKERS
            except ValueError:
                tts_workers = DEFAULT_TTS_WORKERS
        self.tts_workers = max(1, min(int(tts_workers), 16))

    def _produce_segment_audio(
        self,
        idx: int,
        seg: dict[str, Any],
        *,
        paths: Any,
        checkpoint: dict[str, Any],
        fingerprint_voice: str,
        engine_provider: str,
        use_gemini: bool,
        gem_engine: Any,
        gemini_voice_name: str,
        use_voxcpm: bool,
        vox_engine: Any,
        persona_ref_wav: Any,
        persona_ref_text: str,
        _global_preset_v: Any,
        voxcpm_speed_float: float,
        effective_rate: str,
        effective_pitch: str,
        base_edge_voice: str,
        engine: Any,
        short_pauses: bool,
        total_segments: int,
    ) -> dict[str, Any]:
        """Produce ONE segment's narration audio (Batch AL extraction).

        This is the pre-AL serial loop body, moved verbatim so the TTS
        worker pool can run it concurrently: it only READS the shared
        checkpoint (reuse check) and produces the segment file; the
        caller (single committer thread) owns checkpoint writes,
        fallback bookkeeping and timing assembly, so per-segment audio
        content, durations and subtitle timing are identical to the
        serial stage — only WHEN each file arrives changes.

        Returns {"raw_duration", "duration", "part", "record",
        "fell_back"}: ``record`` is the checkpoint row to store (None
        on a cache reuse), ``raw_duration`` the pre-trim probed
        duration (the serial loop's tts_total input).
        """
        base = segment_basename(seg)
        dst = paths.tts_parts / f"{base}.wav"
        fp = segment_fingerprint(seg, fingerprint_voice)
        rec = checkpoint["audio"].get(base, {})
        _seg_word_timings: list[dict[str, Any]] = []
        # Batch DP: an "edge_all" resume redoes EVERY segment in the
        # Edge/persona voice (one consistent voice — the owner's
        # explicit choice), so finished clone segments are NOT reused.
        reusable = (
            self.voice_choice_mode != "edge_all"
            and rec.get("fingerprint") == fp
            and dst.is_file()
            and dst.stat().st_size > 1000
        )
        record: dict[str, Any] | None = None
        fell_back = False
        if reusable:
            duration = probe_duration(self.ffprobe, dst)
            self.log(f"♻️ [{idx}/{total_segments}] {base}.wav reuse ({duration:.2f}s)")
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
            if use_gemini and self.voice_choice_mode is None:
                if gem_engine is not None:
                    self.progress(idx - 1, total_segments, f"Gemini TTS {idx}/{total_segments}")
                    self.log(f"✨ [{idx}/{total_segments}] {base} generate via Gemini TTS ({gemini_voice_name})")
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
                        fell_back = True
                        seg_provider = "edge_fallback"
                else:
                    fell_back = True
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
            if use_voxcpm and vox_engine and not use_gemini and self.voice_choice_mode is None:
                self.progress(idx - 1, total_segments, f"VoxCPM 2 {idx}/{total_segments}")
                self.log(f"🎙️ [{idx}/{total_segments}] {base} generate via VoxCPM 2 Cloud AI")
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
                    budget_seconds=vox_failover_budget(
                        float(seg["video_end"]) - float(seg["video_start"]), len(speak),
                        paid=self.paid_tier,
                    ),
                )
                if vox_err is not None:
                    # Batch DP (Item 3): a PAID job never silently
                    # degrades — quota exhaustion parks the whole job
                    # and asks its owner (the driver turns this into
                    # WAITING_CHOICE with the charge held). A resumed
                    # edge_* run never reaches this step (the VoxCPM
                    # step is gated off above), and free jobs keep the
                    # per-segment fallback below, unchanged.
                    if (
                        self.paid_tier
                        and _VoxCPMQuotaExhaustedError is not None
                        and isinstance(vox_err, _VoxCPMQuotaExhaustedError)
                    ):
                        raise RecapParkRequested(
                            f"VoxCPM quota exhausted at segment {idx}; "
                            "parking the paid job for its owner's choice"
                        )
                    self.log(
                        f"⚠️ EDGE_FALLBACK_SEGMENT: line {idx} fell back to Studio Persona (Edge TTS) "
                        f"after retries ({vox_err}); other lines keep VoxCPM 2"
                    )
                    fell_back = True
                    seg_provider = "edge_fallback"
                else:
                    seg_provider = "voxcpm"

            # 2. Studio Persona TTS (consistent fallback preserving persona timbre & pitch)
            if not produced or not Path(produced).is_file():
                # Batch DP: on an edge_all / edge_mix resume the
                # persona step is the owner's CHOSEN voice for this
                # segment — recorded with the existing fallback
                # bookkeeping so the completion transparency fields
                # (fallback_used / fallback_segments) name exactly
                # the segments that are not clone-voiced.
                if self.voice_choice_mode in ("edge_all", "edge_mix"):
                    fell_back = True
                    seg_provider = "edge_fallback"
                curr_persona = preset_v or (str(self.voice).replace("voxcpm_", "") if str(self.voice).startswith("voxcpm_") else str(self.voice))
                self.progress(idx - 1, total_segments, f"Studio TTS {idx}/{total_segments}")
                self.log(f"🗣️ [{idx}/{total_segments}] {base} generate via Studio Persona ({curr_persona})")
                _edge_tls = getattr(engine, "_tls", None)
                _seg_word_timings: list[dict[str, Any]] = []
                try:
                    from app.services.curated_voices import synthesize_curated_persona, get_curated_voice
                    if get_curated_voice(curr_persona):
                        synthesize_curated_persona(
                            text=speak,
                            persona_id=curr_persona,
                            output_wav=dst,
                            speed=effective_rate,
                            log_fn=self.log,
                        )
                        produced = dst
                    else:
                        if _edge_tls is not None:
                            _edge_tls.last_word_timings = []
                        produced = engine.generate_channel99_voice(speak, base_edge_voice)
                        _seg_word_timings = list(getattr(_edge_tls, "last_word_timings", None) or [])
                except Exception as persona_err:
                    self.log(f"⚠️ Persona TTS notice: {persona_err} — using standard engine")
                    try:
                        if _edge_tls is not None:
                            _edge_tls.last_word_timings = []
                        produced = engine.generate_channel99_voice(speak, base_edge_voice)
                    except Exception as first:
                        plain = without_english(speak)
                        if is_edge_refused(first) or plain == speak or not any(ch.isalpha() for ch in plain):
                            raise
                        # A line mixing English and Burmese sometimes gets no audio at all
                        self.log(f"⚠️ [{idx}/{total_segments}] no audio — trying again without the English words "
                                 "(Edit Text can rewrite the line)")
                        if _edge_tls is not None:
                            _edge_tls.last_word_timings = []
                        produced = engine.generate_channel99_voice(plain, base_edge_voice)
                    _seg_word_timings = list(getattr(_edge_tls, "last_word_timings", None) or [])
                    if not produced or not Path(produced).is_file():
                        raise RuntimeError("TTS output path မရပါ။")
                except Exception as exc:
                    if is_edge_refused(exc):
                        raise RuntimeError(f"TTS stopped at line {idx}/{total_segments}.\n\n{exc}") from exc
                    raise RuntimeError(f"TTS FAILED at segment {idx} ({base}): {exc}\n\n"
                                       f"Text: {seg['narration'][:200]}") from exc
            produced_path = Path(produced).resolve()
            if produced_path != dst.resolve():
                shutil.copy2(produced_path, dst)
                produced_path.unlink(missing_ok=True)
            duration = probe_duration(self.ffprobe, dst)
            if duration <= 0.05:
                raise RuntimeError(f"Generated audio duration invalid: {dst}")
            record = {
                "fingerprint": fp,
                "provider": seg_provider if (use_voxcpm or use_gemini) else "edge_tts",
                "fallback": bool((use_voxcpm or use_gemini) and seg_provider == "edge_fallback"),
                "file": str(dst),
                "duration": round(duration, 3),
                "video_start": seg["video_start"],
                "video_end": seg["video_end"],
                "text": seg["narration"],
            }
            if _seg_word_timings:
                record["word_timings"] = _seg_word_timings
            self.log(f"✅ {base}.wav = {duration:.2f}s")

        part = dst
        raw_duration = duration
        if short_pauses:
            window = float(seg["video_end"]) - float(seg["video_start"])
            part, duration = trim_edge_silence(dst, paths.tts_parts.parent / "tts_parts_short" / dst.name,
                                               min_duration=window / self.max_video_speed)
        return {
            "raw_duration": raw_duration,
            "duration": duration,
            "part": part,
            "record": record,
            "word_timings": record.get("word_timings") if isinstance(locals().get("record"), dict) else None,
            "fell_back": fell_back,
        }


    def _check_run_deadline(self, stage: str) -> None:
        if self._run_started is None or self._run_budget is None:
            return
        if time.monotonic() - self._run_started > self._run_budget:
            raise TimeoutError(
                f"Recap render exceeded its {int(self._run_budget)}s job time budget "
                f"at {stage} — failing the job so it can be refunded/retried "
                f"instead of hanging."
            )

    def _checkpoint_load(self, paths: PipelinePaths) -> dict[str, Any]:
        if paths.checkpoint.is_file():
            try:
                data = load_json(paths.checkpoint)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"version": 1, "audio": {}, "video": {}}

    def _log_sync_check(
        self,
        paths: "PipelinePaths",
        segments: list[dict[str, Any]],
        checkpoint: dict[str, Any],
        *,
        tolerance: float = SYNC_DRIFT_TOLERANCE_SECONDS,
    ) -> tuple[float, float, float]:
        """Hard post-concat sync-invariant gate written to the run log.

        Narration is the clock; every video part is frame-locked to it. So if the
        concatenated picture and the narration track have the same length (within
        ``tolerance``), every segment starts on its own narration line. Drift beyond
        ``tolerance`` raises ``SyncDriftError`` BEFORE overlays or ``-shortest`` mux
        so mismatched footage is never silently truncated and shipped.
        """
        video_len = probe_duration(self.ffprobe, paths.synced_video_noaudio)
        audio_len = probe_duration(self.ffprobe, paths.voice)
        diff = video_len - audio_len
        trimmed = 0
        for seg in segments:
            sync = ((checkpoint.get("video") or {}).get(segment_basename(seg)) or {}).get("sync") or {}
            if float(sync.get("applied_factor", 0.0) or 0.0) > float(sync.get("desired_factor", 0.0) or 0.0) + 1e-6:
                trimmed += 1
        max_tol = max(0.05, float(tolerance))
        status = "✅ SYNC OK" if abs(diff) <= max_tol else "❌ SYNC DRIFT"
        self.log(
            f"{status}: picture={video_len:.2f}s · narration={audio_len:.2f}s · diff={diff:+.3f}s "
            f"| {len(segments)} segments, {trimmed} speed-capped ({self.max_video_speed:g}x) + footage trimmed"
        )
        if abs(diff) > max_tol:
            raise SyncDriftError(
                f"Smart Sync drift exceeded tolerance ({max_tol:.2f}s): "
                f"picture={video_len:.3f}s vs narration={audio_len:.3f}s (diff={diff:+.3f}s, "
                f"segments={len(segments)})"
            )
        return video_len, audio_len, diff

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
        budget_seconds: float | None = None,
    ) -> tuple[Any, Exception | None]:
        """Per-segment VoxCPM attempt with retries (fix 2026-10-29).

        The engine itself fails over across spaces/tokens; this adds one
        segment-level retry on top. Returns (produced_path, None) on
        success or (None, last_error) when the segment — and ONLY this
        segment — should fall back to the Studio Persona engine.

        Batch AL: a quota-exhaustion failure gets NO second walk — the
        job's accounts are all ruled out, so re-walking could only burn
        the same refusals again; the segment falls back immediately.
        ``budget_seconds`` (scaled to the segment by the caller) is
        passed through as the engine's total failover budget.
        """
        # Batch CW: VoxCPM pronounces digit runs the English way, so
        # the line it receives gets its digits rewritten as Myanmar
        # number words (recap narration is full of years and counts).
        # This is the VoxCPM-only choke point: the caller's shared
        # ``speak`` line also feeds the Gemini/Edge paths, which are
        # out of CW scope and keep the original digits, and the
        # segment fingerprint/cache stay keyed on the raw narration.
        speak = normalize_numbers_for_speech(speak)
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
                    total_timeout=budget_seconds,
                    tier="paid" if self.paid_tier else "free",
                )
                return produced, None
            except Exception as exc:
                last_err = exc
                if (
                    _VoxCPMQuotaExhaustedError is not None
                    and isinstance(exc, _VoxCPMQuotaExhaustedError)
                ):
                    self.log(
                        "🚫 VoxCPM 2 quota exhausted for this job — segment falls "
                        "back without a second failover walk"
                    )
                    break
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
        _stage_timings: dict[str, float] = {
            str(k): round(float(v), 3)
            for k, v in (plan.get("stage_timings") or {}).items()
            if isinstance(v, (int, float))
        }
        segments = normalize_segments(plan)
        # Arm the whole-job deadline (audit fix 2026-11-03).
        self._run_started = time.monotonic()
        _source_duration = float(segments[-1]["video_end"]) if segments else 0.0
        self._run_budget = (
            float(self.max_run_seconds) if self.max_run_seconds
            else float(recap_job_timeout(_source_duration))
        )
        paths = make_paths(self.job_dir)
        checkpoint = self._checkpoint_load(paths)
        checkpoint.setdefault("audio", {})
        checkpoint.setdefault("video", {})
        # Batch DP: adopt a persisted owner choice when the caller
        # passed none, and persist an explicit one, so the mode
        # survives the park/resume boundary inside the job directory
        # itself (the DB choice-state stays the resumer's truth; this
        # is the in-job guard against a drifted re-entry).
        if self.voice_choice_mode is None:
            _saved_mode = checkpoint.get("voice_choice_mode")
            if _saved_mode in ("edge_all", "edge_mix"):
                self.voice_choice_mode = _saved_mode
        if self.voice_choice_mode is not None:
            checkpoint["voice_choice_mode"] = self.voice_choice_mode
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
            max_short_side=(1080 if self.paid_tier else 720),
        )
        crop_msg = (
            f" | Crop={self.crop_top*100:.0f}%T/{self.crop_bottom*100:.0f}%B/{self.crop_left*100:.0f}%L/{self.crop_right*100:.0f}%R"
            if (self.crop_top > 0 or self.crop_bottom > 0 or self.crop_left > 0 or self.crop_right > 0)
            else ""
        )
        self.log(f"🪞 Mirror={'ON' if self.mirror_video else 'OFF'} | Ratio={layout['ratio']} "
                 f"{layout['cw']}x{layout['ch']} ({layout['fill']}){crop_msg} from {source_w}x{source_h} | Zoom={self.zoom_factor:.3f}x")

        # Unified speed contract: respect the exact resolved user speed (including explicit '+0%' == 1.00x).
        effective_rate = resolve_speed_rate(self.edge_tts_rate)
        effective_pitch = self.edge_tts_pitch or "+0Hz"

        engine = EdgeTTSEngine(
            ffmpeg=self.ffmpeg,
            work_dir=paths.work / "edge_tts_temp",
            rate=effective_rate,
            pitch=effective_pitch,
            log=self.log,
        )

        # Subtitle-only / No-voice mode: skip all voice synthesis, render video at original
        # segment pacing with synchronized burned/soft Myanmar subtitles (0 voice minutes).
        subtitle_only_mode = str(self.overlays.get("tts_engine") or "").strip().lower() in (
            "none", "subtitle_only", "no_voice"
        )
        # Method 1: Check if VoxCPM Cloud Engine is requested
        use_voxcpm = (
            not subtitle_only_mode
            and (
                str(self.voice).lower().startswith(("voxcpm", "vox_"))
                or str(self.overlays.get("tts_engine") or "").lower() == "voxcpm"
            )
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
        use_gemini = (not subtitle_only_mode) and str(self.overlays.get("tts_engine") or "").lower() == "gemini"
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
                if self.gemini_route is not None:
                    # Batch CG: route narration through the caller's
                    # own key first, the pool second (same composite
                    # as the TTS tab). Neither side available → the
                    # engine stays None and every segment uses the
                    # persona fallback, exactly like the no-keys case;
                    # the outcome records the taste block for the
                    # caller's caption/accounting.
                    try:
                        from app.services.recap_engine.gemini_tts_engine import (
                            RoutedGeminiEngine,
                        )
                    except ImportError:
                        from gemini_tts_engine import (  # type: ignore
                            RoutedGeminiEngine,
                        )
                    _ctx = self.gemini_route
                    _own_engine = None
                    if _ctx.has_usable_own_key:
                        try:
                            _own_engine = GeminiTTSEngine(
                                ffmpeg=self.ffmpeg,
                                work_dir=paths.work / "gemini_tts",
                                rate=effective_rate,
                                keys=[_ctx.own_key],
                                # Batch DN: user-owned key — its usage
                                # never enters the shared pool ledger.
                                record_usage=False,
                                log=self.log,
                            )
                        except Exception:
                            _own_engine = None
                    _pool_engine = None
                    if _ctx.pool_allowed and _gem_keys:
                        _pool_engine = GeminiTTSEngine(
                            ffmpeg=self.ffmpeg,
                            work_dir=paths.work / "gemini_tts",
                            rate=effective_rate,
                            keys=_gem_keys,
                            log=self.log,
                        )
                    if _own_engine is not None or _pool_engine is not None:
                        gem_engine = RoutedGeminiEngine(
                            _own_engine, _pool_engine, log=self.log
                        )
                        self._gemini_routed = gem_engine
                        self.log(f"✨ Gemini TTS engine ready (voice={gemini_voice_name}, routed own-key→pool); persona engine stays as per-segment fallback")
                    else:
                        self.log("⚠️ Gemini TTS selected but no Gemini route is available for this user — every line will use the persona engine (reported as fallback)")
                elif _gem_keys:
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
        voxcpm_speed_float = resolve_speed_tempo(effective_rate)

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
            elif persona_ref_wav:
                # Batch BV: a reference WITHOUT a transcript is the
                # user's own-voice sample (persona samples always
                # carry one) — it still clones, via the spaces that
                # accept transcript-free references; the old "voice
                # design only" wording was wrong for this case.
                self.log(f"🎯 VoxCPM 2 cloning from own-voice sample: {Path(persona_ref_wav).name} (no transcript)")
            else:
                self.log("⚠️ No persona sample transcript — VoxCPM voice design (voice may vary slightly)")

        # Batch BV: adapt the narration reference ONCE per job (Batch
        # BU's 50 s Space cap). synthesize() adapts on every call, so
        # without this pre-pass each concurrent segment worker would
        # probe the same over-cap sample — and race the shared
        # digest-cached trim file. Adapting here, before the segment
        # loop, makes every per-segment adapt a no-op (the adapted
        # file is ≤50 s) and yields the job's single
        # reference_trimmed truth for the checkpoint / plan JSON.
        reference_trimmed = False
        if use_voxcpm and persona_ref_wav and vox_engine is not None:
            try:
                _ref_before = str(persona_ref_wav)
                persona_ref_wav = str(
                    vox_engine._adapt_reference_for_clone(Path(persona_ref_wav))
                )
                reference_trimmed = bool(
                    getattr(vox_engine, "last_reference_trimmed", False)
                    or persona_ref_wav != _ref_before
                )
            except Exception as adapt_err:
                self.log(f"⚠️ Reference pre-adapt skipped ({adapt_err}) — per-segment adapt still applies")
        checkpoint["reference_trimmed"] = bool(reference_trimmed)

        timing: list[dict[str, Any]] = []
        audio_parts: list[Path] = []
        cursor = 0.0
        tts_total = 0.0
        # Batch AL: job-scoped VoxCPM quota memory + per-slot worker
        # engines. Worker slot N binds to HF account N (token_bind_index)
        # and all slots share ONE JobQuotaMemory, so a quota refusal
        # learned by any worker is honoured by every later segment.
        vox_job_memory = None
        _vox_engines_by_slot: dict[int, Any] = {}
        if use_voxcpm and vox_engine is not None:
            _vox_engines_by_slot[0] = vox_engine
            if _JobQuotaMemory is not None:
                vox_job_memory = _JobQuotaMemory()
                if hasattr(vox_engine, "job_memory"):
                    vox_engine.job_memory = vox_job_memory
                    vox_engine.token_bind_index = 0

        def _vox_engine_for_slot(_slot: int) -> Any:
            _eng = _vox_engines_by_slot.get(_slot)
            if _eng is None:
                try:
                    _eng = VoxCPMEngine(
                        work_dir=paths.work / "voxcpm_temp", log_fn=self.log,
                        job_memory=vox_job_memory, token_bind_index=_slot,
                    )
                except TypeError:
                    _eng = VoxCPMEngine(work_dir=paths.work / "voxcpm_temp", log_fn=self.log)
                    if vox_job_memory is not None and hasattr(_eng, "job_memory"):
                        _eng.job_memory = vox_job_memory
                        _eng.token_bind_index = _slot
                _vox_engines_by_slot[_slot] = _eng
            return _eng

        # Batch AL: pipelined part rendering state. One renderer thread
        # consumes parts in segment order while TTS continues; encodes
        # stay strictly one-at-a-time (4 GB instance RAM safety).
        _checkpoint_lock = threading.Lock()
        _render_queue: "queue.Queue[Any] | None" = None
        _render_abort = threading.Event()
        _render_state: dict[str, Any] = {"error": None}
        _sync_acc: dict[str, float] = {"parts_sec": 0.0}
        _renderer_thread: threading.Thread | None = None
        render_pipelined = bool(self.render_final_video)
        # Batch AB: NVENC must be listed AND probe-usable — the
        # listing alone lies on GPU-less hosts (see _select_render_encoder).
        # (Selected before the TTS stage so the pipelined renderer can
        # start as soon as the first part's audio lands.)
        encoder = _select_render_encoder(self.ffmpeg, checkpoint) if render_pipelined else "libx264"
        video_parts: list[Path] = []
        # Cumulative frame lock: each part gets round(audio_end*30) - frames_so_far
        # frames, so video never drifts more than half a frame from the narration.
        audio_cursor = 0.0
        frames_done = 0

        def _render_parts_from_queue(_q: "queue.Queue[Any]") -> None:
            """Render video parts from (idx, seg, timing_item) entries.

            This is the pre-AL serial video-parts loop, verbatim: the
            frame-count/cursor math, fingerprinting, NVENC first-part
            fallback and checkpoint rows are unchanged — only the
            source of work items differs (a queue filled in segment
            order by the TTS committer, or by the video stage itself
            when no renderer thread is running).
            """
            nonlocal encoder, audio_cursor, frames_done
            while True:
                _entry = _q.get()
                if _entry is None:
                    return
                idx, seg, item = _entry
                if _render_abort.is_set():
                    continue
                if self.should_stop():
                    raise InterruptedError("STOP requested — video parts checkpoint ထဲကနေ Resume လုပ်နိုင်ပါတယ်။")
                self._check_run_deadline("video render")
                _part_t0 = time.monotonic()
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
                    _use_intermediate = bool(
                        self.overlays.get("blur_boxes")
                        or str(self.overlays.get("logo_path") or "").strip()
                        or str(self.overlays.get("extra_text") or "").strip()
                        or (self.overlays.get("burn_subtitles", True) and str(self.overlays.get("sub_render_mode") or "png").strip().lower() != "soft")
                    )
                    try:
                        metrics = render_video_part(
                            self.ffmpeg, source, dst, seg, target, encoder,
                            self.mirror_video, self.zoom_factor,
                            self.max_video_speed, self.max_video_slow,
                            self.smooth_freeze_fallback, self.max_freeze_hold,
                            frame_count=frame_count, layout=layout,
                            render_quality=self.render_quality,
                            intermediate=_use_intermediate,
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
                                intermediate=_use_intermediate,
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
                    with _checkpoint_lock:
                        atomic_write_json(paths.checkpoint, checkpoint)
                _sync_acc["parts_sec"] += max(0.0, time.monotonic() - _part_t0)
                video_parts.append(dst)
                self.progress(idx, len(segments), f"Video {idx}/{len(segments)}")

        def _render_parts_guarded(_q: "queue.Queue[Any]") -> None:
            try:
                _render_parts_from_queue(_q)
            except BaseException as _exc:  # surfaced by the video stage
                _render_state["error"] = _exc

        def _start_part_renderer() -> None:
            nonlocal _render_queue, _renderer_thread
            _q: "queue.Queue[Any]" = queue.Queue()
            _t = threading.Thread(
                target=_render_parts_guarded, args=(_q,),
                name="recap-part-renderer", daemon=True,
            )
            _t.start()
            _render_queue = _q
            _renderer_thread = _t

        def _stop_part_renderer() -> None:
            """Stop the pipelined part renderer and wait until it is
            really dead (Batch DB).

            The Batch CS restart stopped it with join(timeout=30)
            and carried on even when the thread was still rendering
            (a real job's render backlog always outlasts 30 s); the
            parts wipe then unlinked the file under the in-flight
            ffmpeg, whose +faststart pass re-opens the output by
            name — the job died on the renderer's stale error
            (2026-11-09: the restart's first live firings killed
            4 paid jobs). Set the abort flag first so the thread
            skips its backlog, then join in slices until it exits:
            a single part render is bounded by ffmpeg_timeout, so
            this always terminates. The abort flag is cleared on
            the way out so a later pass can start a fresh renderer
            that does not skip its queue."""
            nonlocal _renderer_thread, _render_queue
            if _renderer_thread is None:
                return
            _render_abort.set()
            if _render_queue is not None:
                _render_queue.put(None)
            while _renderer_thread.is_alive():
                _renderer_thread.join(timeout=5)
            _renderer_thread = None
            _render_queue = None
            _render_abort.clear()


        _prewarm_pool: ThreadPoolExecutor | None = None
        _prewarm_spec: dict[str, Any] | None = None
        _prewarm_renderer = None
        _prewarm_params: tuple[int, int, int] | None = None
        _prewarm_wait_sec = 0.0
        _srm_pre = str(self.overlays.get("sub_render_mode") or "png").strip().lower()
        _overlays_will_reencode = bool(
            self.overlays.get("blur_boxes")
            or str(self.overlays.get("logo_path") or "").strip()
            or str(self.overlays.get("extra_text") or "").strip()
            or (self.overlays.get("burn_subtitles", True) and _srm_pre != "soft")
        )
        _prewarm_packer: SubtitleCuePacker | None = None
        if render_pipelined and self.overlays.get("burn_subtitles", True) and _srm_pre in ("png", "timeline"):
            try:
                _cw_pre = int(layout.get("cw", 1920))
                _ch_pre = int(layout.get("ch", 1080))
                _pw_pre = int(layout.get("pw", _cw_pre))
                _lpc_pre = int(self.overlays.get("sub_lines", 1))
                _fs_pre = int(self.overlays.get("subtitles_font_size", 16))
                _prewarm_spec = _resolve_png_subtitle_job(
                    {**self.overlays, "subtitle_cues": [(0.0, 1.0, "c")]},
                    _cw_pre, _ch_pre, layout,
                )
                if _prewarm_spec is not None:
                    _prewarm_renderer = _png_subs.PngSubtitleRenderer(paths.work / "subtitle_png_cache")
                    _prewarm_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recap-png-pre")
                    _fit_w_pre = _subtitle_fit_width_px(self.overlays, _pw_pre)
                    _pad2_pre = 2 * _png_subs.png_pad_x(
                        _prewarm_spec["visuals"], _prewarm_spec["font_px"],
                        int(_prewarm_spec["visuals"].get("stroke_px", 1)),
                    )
                    _ink_w_pre = max(80, _fit_w_pre - _pad2_pre)
                    _max_cl_pre = _subtitle_max_clusters(_fs_pre, self.overlays.get("sub_w"))
                    _prewarm_params = (_max_cl_pre, _lpc_pre, _fit_w_pre, _ink_w_pre)
                    _prewarm_packer = SubtitleCuePacker(
                        lines_per_cue=_lpc_pre,
                        max_width_px=_ink_w_pre,
                        font_px=_prewarm_spec["font_px"],
                        font_path=_prewarm_spec["font_path"],
                        stroke_px=int(_prewarm_spec["visuals"].get("stroke_px", 1)),
                    )
            except Exception:
                _prewarm_pool = None
                _prewarm_spec = None
                _prewarm_packer = None

        def _submit_prewarm_cues(emitted: list[tuple[float, float, str, float]]) -> None:
            if not emitted or _prewarm_pool is None or _prewarm_spec is None or _prewarm_renderer is None or _prewarm_params is None:
                return
            _, _, _fit_mw, _ = _prewarm_params
            def _do_warm(_cues=emitted) -> None:
                try:
                    for c_st, c_en, c_txt, _ in _cues:
                        for f_lines, f_px, f_vis, _ps, _pe in fit_png_cue_pieces(
                            _prewarm_renderer,
                            c_txt,
                            _prewarm_spec["font_px"],
                            _prewarm_spec["font_path"],
                            _prewarm_spec["visuals"],
                            _fit_mw,
                            c_st,
                            c_en,
                            max_lines=_prewarm_spec["sub_lines"],
                        ):
                            if f_lines:
                                _prewarm_renderer.render_lines(
                                    f_lines, f_px, _prewarm_spec["font_path"], f_vis
                                )
                except Exception:
                    pass
            try:
                _prewarm_pool.submit(_do_warm)
            except Exception:
                pass

        def _prewarm_segment_cues(
            st: float,
            en: float,
            seg_text: str,
            wt: list[dict[str, Any]] | None,
            sp_dur: float | None = None,
        ) -> None:
            if _prewarm_packer is None or _prewarm_pool is None:
                return
            try:
                sp_en = min(en, st + max(0.05, float(sp_dur))) if sp_dur is not None else en
                emitted = _prewarm_packer.add_segment(
                    st, en, seg_text, speech_end=sp_en, word_timings=wt
                )
            except Exception:
                return
            if emitted:
                _submit_prewarm_cues(emitted)

        def _flush_prewarm_cues() -> None:
            if _prewarm_packer is None:
                return
            try:
                emitted = _prewarm_packer.flush()
            except Exception:
                return
            if emitted:
                _submit_prewarm_cues(emitted)

        def _reset_prewarm_packer() -> None:
            nonlocal _prewarm_packer
            if _prewarm_spec is None or _prewarm_params is None:
                return
            _, _lpc, _, _ink_w = _prewarm_params
            _prewarm_packer = SubtitleCuePacker(
                lines_per_cue=_lpc,
                max_width_px=_ink_w,
                font_px=_prewarm_spec["font_px"],
                font_path=_prewarm_spec["font_path"],
                stroke_px=int(_prewarm_spec["visuals"].get("stroke_px", 1)),
            )

        def _shutdown_prewarm(wait: bool) -> float:
            nonlocal _prewarm_pool
            if _prewarm_pool is None:
                return 0.0
            _tw0 = time.monotonic()
            try:
                _prewarm_pool.shutdown(wait=wait)
            except Exception:
                pass
            _prewarm_pool = None
            return max(0.0, time.monotonic() - _tw0)

        _t_tts_stage0 = time.monotonic()
        if subtitle_only_mode:
            checkpoint["tts_provider"] = "subtitle_only"
            checkpoint["fallback_used"] = False
            checkpoint["fallback_segments"] = []
            checkpoint["reference_trimmed"] = False
            timing = []
            audio_parts = []
            cursor = 0.0
            tts_total = 0.0
            fallback_segments = []
            for _idx, _seg in enumerate(segments, 1):
                _dur = max(0.2, float(_seg.get("video_end", 0.0)) - float(_seg.get("video_start", 0.0)))
                _start = cursor
                _end = _start + _dur
                cursor = _end
                timing.append({
                    "segment_id": _seg["segment_id"],
                    "source_index": _seg["source_index"],
                    "video_start": _seg["video_start"],
                    "video_end": _seg["video_end"],
                    "audio_start": round(_start, 3),
                    "audio_end": round(_end, 3),
                    "audio_duration": round(_dur, 3),
                    "speech_duration": round(_dur, 3),
                    "text": _seg["narration"],
                    "audio_file": None,
                })
            _stage_timings["tts"] = 0.0
            checkpoint["stage_timings"] = dict(_stage_timings)
            atomic_write_json(paths.checkpoint, checkpoint)
            self.log(f"📝 Subtitle-Only mode (no voice synthesis) · {len(timing)} segments · duration={cursor:.2f}s")
        else:
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

            if render_pipelined and _renderer_thread is None:
                _start_part_renderer()

            _tts_results: dict[int, dict[str, Any]] = {}
            _tts_errors: dict[int, BaseException] = {}
            _tts_abort = threading.Event()
            _commit_lock = threading.Lock()
            _assemble_next = [1]

            def _drain_timing() -> None:
                # Timing assembly stays strictly in segment order with
                # the serial loop's exact cursor math; a part is handed
                # to the pipelined renderer the moment its timing row
                # exists (Batch AL-C).
                nonlocal cursor, tts_total
                while _assemble_next[0] in _tts_results:
                    _idx = _assemble_next[0]
                    _seg = segments[_idx - 1]
                    _res = _tts_results[_idx]
                    _dur = float(_res["duration"])
                    tts_total += float(_res["raw_duration"])
                    _start = cursor
                    _end = _start + _dur
                    cursor = _end
                    _cached_wt = (
                        _res.get("word_timings")
                        or ((checkpoint.get("audio") or {}).get(segment_basename(_seg)) or {}).get("word_timings")
                    )
                    _item = {
                        "segment_id": _seg["segment_id"],
                        "source_index": _seg["source_index"],
                        "video_start": _seg["video_start"],
                        "video_end": _seg["video_end"],
                        "audio_start": round(_start, 3),
                        "audio_end": round(_end, 3),
                        "audio_duration": round(_dur, 3),
                        "speech_duration": round(float(_res.get("raw_duration") or _dur), 3),
                        "text": _seg["narration"],
                        "audio_file": str(_res["part"]),
                    }
                    if _cached_wt:
                        _item["word_timings"] = _cached_wt
                    timing.append(_item)
                    audio_parts.append(_res["part"])
                    if _render_queue is not None:
                        _render_queue.put((_idx, _seg, _item))
                    _prewarm_segment_cues(
                        _start, _end, _seg["narration"], _cached_wt,
                        sp_dur=float(_res.get("raw_duration") or _dur),
                    )
                    _assemble_next[0] += 1

            def _produce_and_commit(_slot: int, _idx: int, _seg: dict[str, Any]) -> None:
                if _tts_abort.is_set():
                    return
                try:
                    if self.should_stop():
                        raise InterruptedError("STOP requested — လက်ရှိပြီးထားတာတွေ checkpoint ထဲသိမ်းထားပါတယ်။")
                    self._check_run_deadline("TTS")
                    _worker_vox = _vox_engine_for_slot(_slot) if use_voxcpm else None
                    _res = self._produce_segment_audio(
                        _idx, _seg,
                        paths=paths, checkpoint=checkpoint,
                        fingerprint_voice=fingerprint_voice,
                        engine_provider=engine_provider,
                        use_gemini=use_gemini, gem_engine=gem_engine,
                        gemini_voice_name=gemini_voice_name,
                        use_voxcpm=use_voxcpm, vox_engine=_worker_vox,
                        persona_ref_wav=persona_ref_wav,
                        persona_ref_text=persona_ref_text,
                        _global_preset_v=_global_preset_v,
                        voxcpm_speed_float=voxcpm_speed_float,
                        effective_rate=effective_rate,
                        effective_pitch=effective_pitch,
                        base_edge_voice=base_edge_voice,
                        engine=engine, short_pauses=short_pauses,
                        total_segments=len(segments),
                    )
                except BaseException as _exc:
                    _tts_errors[_idx] = _exc
                    _tts_abort.set()
                    return
                with _commit_lock:
                    _tts_results[_idx] = _res
                    _record = _res.get("record")
                    if _record is not None:
                        with _checkpoint_lock:
                            checkpoint["audio"][segment_basename(_seg)] = _record
                            atomic_write_json(paths.checkpoint, checkpoint)
                    if _res.get("fell_back"):
                        fallback_segments.append(_idx)
                    self.progress(_idx, len(segments), f"Audio {_idx}/{len(segments)}")
                    _drain_timing()

            _worker_count = max(1, min(self.tts_workers, len(segments)))
            if use_gemini and gem_engine is not None:
                _own_eng = getattr(gem_engine, "own_engine", None)
                if (
                    _own_eng is not None
                    and not getattr(gem_engine, "_own_spent", True)
                    and len(getattr(_own_eng, "keys", ())) <= 1
                ) or (
                    _own_eng is None
                    and not getattr(gem_engine, "_record_usage", True)
                    and len(getattr(gem_engine, "keys", ())) <= 1
                ):
                    _worker_count = min(_worker_count, 2)
            if _worker_count <= 1:
                for _idx, _seg in enumerate(segments, 1):
                    _produce_and_commit(0, _idx, _seg)
                    if _tts_abort.is_set():
                        break
            else:
                _free_slots: "queue.Queue[int]" = queue.Queue()
                for _slot in range(_worker_count):
                    _free_slots.put(_slot)

                def _pool_task(_idx: int, _seg: dict[str, Any]) -> None:
                    _slot = _free_slots.get()
                    try:
                        _produce_and_commit(_slot, _idx, _seg)
                    finally:
                        _free_slots.put(_slot)

                with ThreadPoolExecutor(max_workers=_worker_count, thread_name_prefix="recap-tts") as _pool:
                    _futures = [_pool.submit(_pool_task, _i, _s) for _i, _s in enumerate(segments, 1)]
                    for _fut in as_completed(_futures):
                        _fut.result()
            fallback_segments.sort()
            if _tts_errors:
                _stop_part_renderer()
                _shutdown_prewarm(wait=False)
                raise _tts_errors[min(_tts_errors)]

            if (
                _tts_pass == 0
                and use_gemini
                and fallback_segments
                and len(fallback_segments) < len(segments)
            ):
                # Batch CS: the Gemini pass fell back on SOME lines
                # (pool exhaustion mid-job) — shipping it would mix
                # two voices in one narration. Restart the TTS stage
                # uniformly on the Studio Persona engine instead: the
                # whole-job rule the TTS tab adopted in Batch CE.
                self.log(
                    f"🔁 Gemini TTS fell back on {len(fallback_segments)}/{len(segments)} "
                    "lines — restarting narration uniformly on the Studio Persona "
                    "engine so the recap speaks with one voice"
                )
                # Preserve pass 0's routed Gemini draws for the CG
                # settle before the route context is dropped.
                if self.gemini_route is not None and self._gemini_routed is not None:
                    _routed0 = self._gemini_routed
                    self._gemini_outcome = {
                        "route": "uniform_restart",
                        "pool_seconds": float(_routed0.pool_seconds),
                        "pool_requests": int(_routed0.pool_requests),
                        "own_key_invalid": bool(_routed0.own_key_invalid),
                        "taste_blocked": False,
                    }
                restart_uniform = True
                use_gemini = False
                # The persona engine is also the VoxCPM branch's
                # fallback target; pass 2 must run it as the PRIMARY
                # engine, not as 100% "fallback" bookkeeping.
                use_voxcpm = False
                gem_engine = None
                checkpoint["uniform_restart"] = True
                # The pipelined part renderer consumes timings with
                # cumulative cursor state — it cannot absorb a second
                # pass. Stop it, drop its pass-0 parts (they embed
                # pass-0 audio durations) and let pass 2 start fresh.
                _stop_part_renderer()
                _sync_acc["parts_sec"] = 0.0
                audio_cursor = 0.0
                frames_done = 0
                # Clear Pass 0's accumulated part paths so Pass 2 starts
                # from a clean slate (prevents duplicate prefix concat and
                # restores Pass 2's NVENC first-part fallback check).
                video_parts.clear()
                # Fresh error state for pass 2 (Batch DB): an error
                # the old thread stored while it was being stopped
                # must not poison the fresh pass's final raise.
                _render_state = {"error": None}
                checkpoint["video"] = {}
                for _part in paths.video_parts.glob("*.mp4"):
                    try:
                        _part.unlink()
                    except OSError:
                        pass
                # Also drop Pass 0's silence-trimmed WAV cache so Pass 2
                # can never hit a stale trimmed clip on same-second mtime.
                _short_dir = paths.tts_parts.parent / "tts_parts_short"
                if _short_dir.is_dir():
                    for _short_wav in _short_dir.glob("*.wav"):
                        try:
                            _short_wav.unlink()
                        except OSError:
                            pass
                _reset_prewarm_packer()

            if not restart_uniform:
                _flush_prewarm_cues()
                break
          with _checkpoint_lock:
            checkpoint["fallback_used"] = bool(fallback_segments)
            checkpoint["fallback_segments"] = list(fallback_segments)
            checkpoint["reference_trimmed"] = bool(reference_trimmed)
            checkpoint["tts_provider"] = (
                "gemini" if use_gemini and not fallback_segments
                else "gemini+persona_fallback" if use_gemini
                else "voxcpm" if use_voxcpm and not fallback_segments
                else "voxcpm+edge_fallback" if use_voxcpm
                else "edge_tts"
            )
            _stage_timings["tts"] = round(max(0.0, time.monotonic() - _t_tts_stage0), 3)
            checkpoint["stage_timings"] = dict(_stage_timings)
            atomic_write_json(paths.checkpoint, checkpoint)

        # Batch CG: finalise the BYOK route outcome now that every
        # segment's audio exists. Successful draws only (the routed
        # engine counts nothing for failed attempts); the caller
        # debits the FREE taste from these numbers. When no route was
        # available at all (taste spent, no key, no pool) the outcome
        # records the block so the completion caption can name the
        # own-key option. In-memory only — never in the checkpoint.
        if use_gemini and self.gemini_route is not None:
            _routed = self._gemini_routed
            if _routed is not None:
                if _routed.pool_requests and _routed.own_requests:
                    _route = "own_pool_mix"
                elif _routed.pool_requests:
                    _route = "pool"
                elif _routed.own_requests:
                    _route = "own_key"
                else:
                    _route = "edge"
                self._gemini_outcome = {
                    "route": _route,
                    "pool_seconds": float(_routed.pool_seconds),
                    "pool_requests": int(_routed.pool_requests),
                    "own_key_invalid": bool(_routed.own_key_invalid),
                    "taste_blocked": False,
                }
            else:
                self._gemini_outcome = {
                    "route": "edge",
                    "pool_seconds": 0.0,
                    "pool_requests": 0,
                    "own_key_invalid": False,
                    "taste_blocked": bool(self.gemini_route.taste_blocked),
                }

        if subtitle_only_mode:
            final_audio_duration = 0.0
            paths.voice.parent.mkdir(parents=True, exist_ok=True)
            _sil_dur = max(0.2, float(cursor or _source_duration or 1.0))
            subprocess.run(
                [
                    self.ffmpeg, "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={_sil_dur:.3f}",
                    "-c:a", "pcm_s16le", str(paths.voice),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=ffmpeg_timeout(_sil_dur),
            )
        else:
            if short_pauses:
                self.log(f"✂️ Short pauses: Edge TTS silence trimmed · voice {tts_total / 60:.1f} → {cursor / 60:.1f} min "
                         f"(~0.25 s between lines; picture never cut for it)")
            self.log("🔗 Timestamp audio parts concat...")
            concat_audio(self.ffmpeg, audio_parts, paths.voice, paths.work, total_duration=cursor)
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
        _actual_edge_voice = str(base_edge_voice or "my-MM-ThihaNeural")
        if _global_preset_v:
            try:
                from app.services.curated_voices import get_curated_voice as _get_cv_actual

                _cv_actual = _get_cv_actual(_global_preset_v)
                if _cv_actual and _cv_actual.get("edge_voice"):
                    _actual_edge_voice = str(_cv_actual["edge_voice"])
            except Exception:
                pass
        _actual_tts_provider = str(checkpoint.get("tts_provider") or "edge_tts")
        _all_segments_fell_back = bool(segments) and len(fallback_segments) >= len(segments)
        if _actual_tts_provider == "subtitle_only":
            _actual_voice = ""
        elif _actual_tts_provider == "gemini":
            _actual_voice = str(gemini_voice_name or "")
        elif _actual_tts_provider == "gemini+persona_fallback":
            _actual_voice = _actual_edge_voice if _all_segments_fell_back else str(gemini_voice_name or "")
        elif _actual_tts_provider == "voxcpm":
            _actual_voice = str(self.voice)
        elif _actual_tts_provider == "voxcpm+edge_fallback":
            _actual_voice = _actual_edge_voice if _all_segments_fell_back else str(self.voice)
        else:
            _actual_voice = _actual_edge_voice
        _voice_outcome: dict[str, Any] = {
            "actual_tts_provider": _actual_tts_provider,
            "actual_voice": _actual_voice,
            "uniform_restart": bool(checkpoint.get("uniform_restart")),
            "fallback_used": bool(fallback_segments),
            "fallback_segments": list(fallback_segments),
            "fallback_count": len(fallback_segments),
            "total_segments": len(segments),
            "tts_voice": str(self.voice),
        }
        checkpoint["actual_voice"] = _actual_voice
        checkpoint["total_segments"] = len(segments)

        out_plan = dict(plan)
        out_plan["segments"] = [dict(seg) for seg in plan.get("segments", [])]
        out_plan["tts_provider"] = _actual_tts_provider
        out_plan["actual_voice"] = _actual_voice
        out_plan["total_segments"] = len(segments)
        out_plan["uniform_restart"] = bool(checkpoint.get("uniform_restart"))
        out_plan["fallback_used"] = bool(fallback_segments)
        out_plan["fallback_segments"] = list(fallback_segments)
        out_plan["reference_trimmed"] = bool(reference_trimmed)
        out_plan["tts_voice"] = self.voice
        out_plan["actual_audio_duration"] = round(final_audio_duration, 3)
        out_plan["tts_timing_segments"] = timing
        out_plan["sync_segments"] = sync_segments
        out_plan["subtitle_segments"] = [
            {"start": item["audio_start"], "end": item["audio_end"], "text": item["text"]}
            for item in timing
        ]
        out_plan["production_overlays"] = self.overlays
        out_plan["stage_timings"] = dict(_stage_timings)
        atomic_write_json(paths.plan, out_plan)

        # Generate synchronized Myanmar SRT & ASS subtitle files
        srt_file = paths.root / "recap_myanmar_synced.srt"
        ass_file = paths.root / "recap_myanmar_synced.ass"
        all_sub_cues: list[tuple[float, float, str]] = []
        all_speech_ends: list[float | None] = []
        try:
            srt_lines = []
            cue_counter = 1
            cw_val = int(layout.get("cw", 1920))
            ch_val = int(layout.get("ch", 1080))
            pw_val = int(layout.get("pw", cw_val))
            lines_per_cue = int(self.overlays.get("sub_lines", 1))
            raw_font_size = int(self.overlays.get("subtitles_font_size", 16))
            st_key = str(self.overlays.get("sub_style") or self.overlays.get("subtitles_style") or "yellow").lower()
            preset_cfg = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
            burn_font_px = subtitle_font_px(cw_val, ch_val, raw_font_size, lines_per_cue, layout)
            burn_font_path = overlays_font_path(self.overlays)
            burn_visuals = build_subtitle_visuals(
                preset_cfg, overlays_visual_overrides(self.overlays), ch_val, burn_font_px
            )
            burn_stroke = int(burn_visuals.get("stroke_px", 1))
            fit_max_w = _subtitle_fit_width_px(self.overlays, pw_val)
            pad_2 = 2 * _png_subs.png_pad_x(burn_visuals, burn_font_px, burn_stroke)
            max_line_ink_w = max(80, fit_max_w - pad_2)
            max_clusters_line = _subtitle_max_clusters(
                font_size=int(self.overlays.get("subtitles_font_size", 16)),
                sub_w=self.overlays.get("sub_w"),
                lines_per_cue=lines_per_cue,
                is_vert=(ch_val > cw_val),
            )

            all_sub_cues, all_speech_ends = pack_subtitle_cues_from_segments(
                timing,
                lines_per_cue=lines_per_cue,
                max_width_px=max_line_ink_w,
                font_px=burn_font_px,
                font_path=burn_font_path,
                stroke_px=burn_stroke,
            )

            # S2: best-effort END-side / gap-only reading-speed polish BEFORE
            # emitting SRT, ASS, and burn-in subtitle_cues so all outputs match.
            all_sub_cues = apply_reading_speed_rules(
                all_sub_cues,
                speech_ends=all_speech_ends,
                timeline_end=final_audio_duration,
            )

            for c_st, c_en, c_txt in all_sub_cues:
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
                sub_margin=self.overlays.get("sub_margin"),
                sub_x=self.overlays.get("sub_x"),
                subtitles_margin_v=self.overlays.get("subtitles_margin_v"),
                boxes=self.overlays.get("blur_boxes") or self.overlays.get("boxes"),
                layout=layout,
                sub_visual_overrides=overlays_visual_overrides(self.overlays),
                sub_effect=str(self.overlays.get("sub_effect") or ("karaoke" if str(self.overlays.get("sub_render_mode") or "").lower() == "karaoke" else "none")),
            )
            self.log(f"📝 Synced Myanmar ASS saved: {ass_file.name} (font={active_font})")
        except Exception as srt_err:
            self.log(f"⚠️ Failed to write synced subtitles: {srt_err}")

        if self.overlays.get("burn_subtitles"):
            self.overlays["subtitles_file"] = str(ass_file) if ass_file.is_file() else (str(srt_file) if srt_file.is_file() else None)
            # Batch Q: the burn consumes the cue list directly (PNG
            # overlays); the .ass/.srt files remain the downloadable
            # side-files and the parse-back fallback for file-only
            # callers of apply_production_overlays.
            self.overlays["subtitle_cues"] = [
                [float(c_st), float(c_en), c_txt]
                for c_st, c_en, c_txt in all_sub_cues
            ]

        if not self.render_final_video:
            _shutdown_prewarm(wait=False)
            self.log("✅ Audio + Smart Sync plan READY (final video render skipped).")
            return {
                "audio": str(paths.voice),
                "plan": str(paths.plan),
                "final": None,
                "subtitles_srt": str(srt_file) if srt_file.is_file() else None,
                "subtitles_ass": str(ass_file) if ass_file.is_file() else None,
                "segments": len(timing),
                "stage_timings": dict(_stage_timings),
                "voice_outcome": dict(_voice_outcome),
            }

        self.log("🎬 Smart Sync video parts render စမယ်...")
        if _renderer_thread is not None:
            # Batch AL: parts were rendered pipelined during the TTS
            # stage; collect the thread and surface any render error.
            if _render_queue is not None:
                _render_queue.put(None)
            _renderer_thread.join()
            if _render_state.get("error") is not None:
                raise _render_state["error"]
        else:
            _serial_q: "queue.Queue[Any]" = queue.Queue()
            for _i, (_s, _it) in enumerate(zip(segments, timing), 1):
                _serial_q.put((_i, _s, _it))
            _serial_q.put(None)
            _render_parts_from_queue(_serial_q)

        # 1B-1 + 1B-2: Build the concat input list deterministically from
        # ``segments`` and enforce the 1-to-1 part count/identity invariant
        # before concatenating so stale/duplicated in-memory paths can never
        # slip into video_parts.txt.
        expected_video_parts = [paths.video_parts / f"{segment_basename(seg)}.mp4" for seg in segments]
        if len(video_parts) != len(expected_video_parts) or [
            p.resolve() for p in video_parts
        ] != [p.resolve() for p in expected_video_parts]:
            raise SyncDriftError(
                f"Smart Sync video part mismatch before concat: rendered {len(video_parts)} part(s) "
                f"for {len(expected_video_parts)} segment(s)"
            )
        missing_parts = [
            p.name for p in expected_video_parts if not p.is_file() or p.stat().st_size <= 0
        ]
        if missing_parts:
            raise SyncDriftError(
                f"Smart Sync video part(s) missing before concat: {', '.join(missing_parts[:5])}"
            )

        # Late stages (concat -> overlay -> mux) are checkpointed by
        # input fingerprint (audit fix 2026-11-03): a resumed run whose
        # parts, overlays and voice are unchanged skips straight to the
        # finished outputs instead of redoing a full-length overlay
        # re-encode. Any input change — a re-rendered part, an edited
        # overlay setting, a replaced voice/BGM file — changes the
        # fingerprint and re-runs exactly the affected stages.
        late = checkpoint.setdefault("late_stage", {})
        part_fps = [
            (checkpoint.get("video") or {}).get(segment_basename(seg), {}).get("fingerprint")
            for seg in segments
        ]
        expected_concat_duration = round(float(audio_cursor), 3)
        concat_fp = _stage_fingerprint("concat", {
            "parts": part_fps,
            "part_count": len(expected_video_parts),
            "expected_duration": expected_concat_duration,
            "encoder": encoder,
        })
        synced = paths.synced_video_noaudio
        _reuse_concat = False
        if late.get("concat") == concat_fp and synced.is_file() and synced.stat().st_size > 5000:
            try:
                _cached_vid_dur = probe_duration(self.ffprobe, synced)
                _expected_aud_dur = probe_duration(self.ffprobe, paths.voice)
                if abs(_cached_vid_dur - _expected_aud_dur) <= SYNC_DRIFT_TOLERANCE_SECONDS:
                    _reuse_concat = True
                else:
                    self.log(
                        f"⚠️ Cached concat rejected (picture={_cached_vid_dur:.2f}s vs "
                        f"narration={_expected_aud_dur:.2f}s) — rebuilding concat"
                    )
            except Exception as _cache_probe_exc:
                self.log(f"⚠️ Cached concat probe failed ({_cache_probe_exc}) — rebuilding concat")
        if _reuse_concat:
            self.log("♻️ Smart Sync concat reuse (inputs unchanged)")
            _stage_timings["sync"] = round(max(0.0, _sync_acc["parts_sec"]), 3)
            self._log_sync_check(paths, segments, checkpoint)
        else:
            self._check_run_deadline("concat")
            self.log("🔗 Smart Sync video parts concat...")
            _t_concat0 = time.monotonic()
            concat_video_parts(self.ffmpeg, expected_video_parts, synced, paths.work, total_duration=audio_cursor, faststart=not _overlays_will_reencode)
            _concat_sec = max(0.0, time.monotonic() - _t_concat0)
            # Gate BEFORE persisting late["concat"] so a drifted concat is never cached.
            self._log_sync_check(paths, segments, checkpoint)
            late["concat"] = concat_fp
            _stage_timings["sync"] = round(max(0.0, _sync_acc["parts_sec"] + _concat_sec), 3)
            checkpoint["stage_timings"] = dict(_stage_timings)
            atomic_write_json(paths.checkpoint, checkpoint)
        _prewarm_wait_sec = _shutdown_prewarm(wait=True)
        video_for_mux = paths.synced_video_noaudio
        overlays_active = bool(
            self.overlays.get("blur_boxes")
            or str(self.overlays.get("logo_path") or "").strip()
            or str(self.overlays.get("extra_text") or "").strip()
            or str(self.overlays.get("subtitles_file") or "").strip()
        )
        overlay_fp: str | None = None
        if overlays_active:
            overlay_fp = _stage_fingerprint("overlay", {
                "concat": concat_fp,
                "overlays": self.overlays,
                "files": {
                    key: _file_identity(self.overlays.get(key))
                    for key in ("logo_path", "subtitles_file", "bgm_path")
                    if self.overlays.get(key)
                },
                "encoder": encoder,
                "quality": self.render_quality,
                "frame": [layout["cw"], layout["ch"], layout["ratio"], layout["fill"]],
            })
            decorated = paths.decorated_video_noaudio
            if late.get("overlay") == overlay_fp and decorated.is_file() and decorated.stat().st_size > 5000:
                self.log("♻️ Production overlay reuse (inputs unchanged)")
                video_for_mux = decorated
                _stage_timings.setdefault("png", round(_prewarm_wait_sec, 3))
                _stage_timings.setdefault("overlay", 0.0)
            else:
                self._check_run_deadline("overlay")
                self.log(f"🎨 Production overlay pass — Blur / Title PNG / Logo / Subtitles... [encoder={encoder}]")
                _overlay_started = time.monotonic()
                _ov_timings: dict[str, float] = {}
                import inspect as _inspect_mod
                _ov_kw: dict[str, Any] = {
                    "layout": layout,
                    "render_quality": self.render_quality,
                    "duration": audio_cursor,
                }
                _ov_params = _inspect_mod.signature(apply_production_overlays).parameters
                if "timings_out" in _ov_params:
                    _ov_kw["timings_out"] = _ov_timings
                if "progress_fn" in _ov_params:
                    _ov_kw["progress_fn"] = lambda _msg: self.progress(1, 1, _msg)
                video_for_mux = apply_production_overlays(
                    self.ffmpeg, paths.synced_video_noaudio, paths.decorated_video_noaudio,
                    self.overlays, encoder, paths.work, self.log, frame_size=(layout["cw"], layout["ch"]),
                    **_ov_kw,
                )
                late["overlay"] = overlay_fp
                _stage_timings["png"] = round(max(0.0, _prewarm_wait_sec + float(_ov_timings.get("png", 0.0))), 3)
                _stage_timings["overlay"] = round(
                    float(_ov_timings.get("overlay", max(0.0, time.monotonic() - _overlay_started))),
                    3,
                )
                checkpoint["stage_timings"] = dict(_stage_timings)
                atomic_write_json(paths.checkpoint, checkpoint)
                # Batch AB: a completion line with elapsed time — the
                # OOM loop left NO log trace of the pass ever finishing
                # (or not), which is what made it undebuggable.
                self.log(f"✅ Production overlay pass complete in {time.monotonic() - _overlay_started:.1f}s [encoder={encoder}]")
        self.log("🎧 Final audio mux (with limiter & mastering chain)...")
        volume_percent = int(getattr(self, "voice_volume_percent", 100) or 100)
        if volume_percent != 100:
            self.log(f"🔊 Voice volume {volume_percent}% applied at final mux (limiter on)")
        bgm_file = self.overlays.get("bgm_path")
        bgm_vol = float(self.overlays.get("bgm_volume", 0.18))
        if bgm_file and Path(bgm_file).is_file():
            self.log(f"🎵 Studio BGM sidechain ducking applied ({Path(bgm_file).name}, vol={bgm_vol:.2f})")
        mux_fp = _stage_fingerprint("mux", {
            "video": overlay_fp if overlays_active else concat_fp,
            "voice": _file_identity(paths.voice),
            "volume": volume_percent,
            "bgm": _file_identity(bgm_file) if bgm_file else None,
            "bgm_volume": bgm_vol,
        })
        _srm = str(self.overlays.get("sub_render_mode") or "png").strip().lower()
        _want_soft_cc = bool(
            (_srm == "soft" or bool(self.overlays.get("embed_soft_subs", True)))
            and srt_file.is_file()
        )
        _soft_embedded_inline = False
        if late.get("mux") == mux_fp and paths.final.is_file() and paths.final.stat().st_size > 5000:
            self.log("♻️ Final mux reuse (inputs unchanged)")
            _stage_timings.setdefault("mux", 0.0)
        else:
            self._check_run_deadline("mux")
            self.progress(1, 1, "Muxing final video")
            _t_mux0 = time.monotonic()
            _soft_embedded_inline = bool(
                mux_audio(
                    self.ffmpeg,
                    video_for_mux,
                    paths.voice,
                    paths.final,
                    volume_percent,
                    bgm_path=Path(bgm_file) if bgm_file else None,
                    bgm_volume=bgm_vol,
                    duration=audio_cursor,
                    soft_srt_path=srt_file if _want_soft_cc else None,
                )
            )
            if _soft_embedded_inline:
                self.log("💬 Embedded soft Myanmar CC track (mov_text) inline during final mux")
            late["mux"] = mux_fp
            _stage_timings["mux"] = round(max(0.0, time.monotonic() - _t_mux0), 3)

        # Fallback only if final was reused from cache without soft subs.
        if _want_soft_cc and not _soft_embedded_inline and paths.final.is_file() and late.get("mux") == mux_fp:
            pass
        elif _want_soft_cc and not _soft_embedded_inline and paths.final.is_file():
            try:
                mux_soft_subtitle_track(self.ffmpeg, paths.final, srt_file)
                self.log("💬 Embedded soft Myanmar CC track (mov_text) into MP4 container")
            except Exception as _soft_exc:
                self.log(f"⚠️ Soft subtitle track mux skipped: {_soft_exc}")
        checkpoint["stage_timings"] = dict(_stage_timings)
        out_plan["stage_timings"] = dict(_stage_timings)
        atomic_write_json(paths.checkpoint, checkpoint)
        atomic_write_json(paths.plan, out_plan)
        final_duration = probe_duration(self.ffprobe, paths.final)
        _expected_narration_dur = (
            float(cursor) if subtitle_only_mode else float(final_audio_duration)
        )
        if _expected_narration_dur > 0.05 and abs(final_duration - _expected_narration_dur) > SYNC_DRIFT_TOLERANCE_SECONDS:
            _mux_diff = final_duration - _expected_narration_dur
            self.log(
                f"❌ SYNC DRIFT (post-mux): final={final_duration:.2f}s · "
                f"narration={_expected_narration_dur:.2f}s · diff={_mux_diff:+.3f}s"
            )
            raise SyncDriftError(
                f"Post-mux duration mismatch exceeded tolerance ({SYNC_DRIFT_TOLERANCE_SECONDS:.2f}s): "
                f"final={final_duration:.3f}s vs narration={_expected_narration_dur:.3f}s "
                f"(diff={_mux_diff:+.3f}s)"
            )

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

        # Export standalone MP3 of the voiceover audio for separate delivery (skipped in subtitle-only mode)
        final_mp3 = None
        if not subtitle_only_mode:
            final_mp3 = paths.final.with_suffix(".mp3")
            try:
                cmd_mp3 = [
                    self.ffmpeg, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
                    "-y", "-i", str(paths.voice),
                    "-vn", "-af", "highpass=f=60,lowpass=f=12000,alimiter=limit=0.95:level=disabled",
                    "-c:a", "libmp3lame", "-b:a", "192k",
                    "-map_metadata", "-1",
                    str(final_mp3)
                ]
                subprocess.run(cmd_mp3, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=ffmpeg_timeout(final_duration))
            except Exception as e_mp3:
                self.log(f"⚠️ Standalone MP3 export warning: {e_mp3}")
                final_mp3 = paths.voice

        actual_voice_secs = (
            0
            if (subtitle_only_mode or not use_voxcpm)
            else max(0, int(math.ceil(float(final_audio_duration or 0.0))))
        )

        self.log(f"✅ FINAL READY: {paths.final}")
        self.log(f"⏱️ Final duration: {final_duration:.2f}s")
        return {
            "audio": None if subtitle_only_mode else str(final_mp3 if (isinstance(final_mp3, Path) and final_mp3.is_file()) else paths.voice),
            "voice_wav": None if subtitle_only_mode else str(paths.voice),
            "plan": str(paths.plan),
            "final": str(paths.final),
            "subtitles_srt": str(final_srt) if final_srt.is_file() else (str(srt_file) if srt_file.is_file() else None),
            "subtitles_ass": str(final_ass) if final_ass.is_file() else (str(ass_file) if ass_file.is_file() else None),
            "segments": len(timing),
            "duration": final_duration,
            "actual_voice_seconds": actual_voice_secs,
            "tts_runtime_seconds": float(_stage_timings.get("tts") or 0.0),
            # Batch CG: BYOK route outcome (None for non-routed runs).
            "gemini_route": self._gemini_outcome,
            "voice_outcome": dict(_voice_outcome),
            "stage_timings": dict(_stage_timings),
            "protection_strong_baked": bool(overlays_active and self.overlays.get("protection_strong")),
        }
