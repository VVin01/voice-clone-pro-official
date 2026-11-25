"""Gemini TTS Head-Only ASR Prompt-Leak Verification Gate & Telemetry Ring Buffer.

Inspects the first 12 seconds of raw PCM returned by Gemini TTS inside ``_ask()``
using local ``faster-whisper`` (``base`` int8 on CPU, max 2 concurrent) to catch
spoken English instruction prose or unexpected English preambles before audio is
accepted, cached, or speed-adjusted.

Modes (dynamic setting ``gemini_tts_leak_gate_mode``, read per call):
  - ``off``     : skip ASR verification entirely
  - ``shadow``  : run gate, log structured verdict + capture leak sample, accept audio (default)
  - ``enforce`` : reject leaked audio so ``_ask()`` strips prompt prose and retries/escalates
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
import wave

import numpy as np

logger = logging.getLogger(__name__)

VALID_LEAK_GATE_MODES: tuple[str, ...] = ("off", "shadow", "enforce")
DEFAULT_LEAK_GATE_MODE: str = "shadow"

HEAD_CHECK_SECONDS: float = 12.0
TARGET_SAMPLE_RATE: int = 16000
WHISPER_MAX_CONCURRENCY: int = 2
RECAP_ALWAYS_VERIFY_CHARS: int = 160
RECAP_DURATION_DIVISOR: float = 15.0
RECAP_DURATION_SLACK_SECONDS: float = 1.2
MYANMAR_OPENING_WINDOW_CHARS: int = 80
MYANMAR_SCRIPT_RATIO_THRESHOLD: float = 0.85
UNEXPECTED_EN_PROB_THRESHOLD: float = 0.80
UNEXPECTED_EN_MIN_WORDS: int = 4

LEAK_RING_MAX_ENTRIES: int = 20
LEAK_RING_MAX_BYTES: int = 8 * 1024 * 1024  # 8 MB cap
LEAK_TEXT_SNIPPET_MAX_CHARS: int = 40

# Targeted instruction vocabulary from shipped narrator_prompt_short / narrator_prompt shapes.
INSTRUCTION_KEYWORDS: frozenset[str] = frozenset(
    {
        "narrate",
        "storyteller",
        "burmese",
        "lively",
        "varied",
        "throughout",
        "transcript",
        "adding",
        "nothing",
        "expressive",
        "storytelling",
        "continuous",
        "narrator",
        "prebuilt",
        "impersonate",
        "excerpt",
        "microphone",
        "pronunciation",
    }
)

INSTRUCTION_BIGRAMS: frozenset[tuple[str, str]] = frozenset(
    {
        ("narrate", "as"),
        ("one", "adult"),
        ("adult", "male"),
        ("adult", "female"),
        ("male", "burmese"),
        ("female", "burmese"),
        ("burmese", "storyteller"),
        ("warm", "lively"),
        ("lively", "varied"),
        ("varied", "pace"),
        ("same", "voice"),
        ("voice", "throughout"),
        ("read", "only"),
        ("only", "the"),
        ("the", "transcript"),
        ("transcript", "below"),
        ("exactly", "once"),
        ("adding", "nothing"),
        ("expressive", "storytelling"),
        ("lively", "storytelling"),
        ("audio", "profile"),
        ("continuous", "narrator"),
        ("prebuilt", "voice"),
        ("speaker", "identity"),
        ("microphone", "distance"),
    }
)

_ALL_PROMPT_TERMS_GE7: tuple[str, ...] = tuple(
    sorted(
        {
            w
            for w in (
                set(INSTRUCTION_KEYWORDS)
                | {tok for bg in INSTRUCTION_BIGRAMS for tok in bg}
            )
            if len(w) >= 7
        }
    )
)

_ASCII_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_DIGIT_CHAR_RE = re.compile(r"[0-9\u1040-\u1049]")

_WHISPER_MODEL: Any = None
_WHISPER_LOCK = threading.Lock()
_WHISPER_SEM = threading.Semaphore(WHISPER_MAX_CONCURRENCY)
_WHISPER_FAIL_LOGGED: bool = False

_RING_LOCK = threading.Lock()
_RING_SEQ: int = 0

_MISS_SENTINEL = object()
_MODE_CACHE_LOCK = threading.Lock()
_MODE_CACHE_TS: float = 0.0
_MODE_CACHE_VAL: object = None
_MODE_SYNC_TTL_SECONDS: float = 2.0


@dataclass(frozen=True)
class LeakGateResult:
    verdict: str  # "clean" | "leak"
    reason: str
    head_transcript: str = ""
    matched_bigrams: tuple[str, ...] = ()
    matched_keywords: tuple[str, ...] = ()
    detected_language: str = ""
    language_probability: float = 0.0


def reset_leak_gate_state_for_tests() -> None:
    """Reset lazy Whisper state & mode cache for isolated unit tests."""
    global _WHISPER_MODEL, _WHISPER_FAIL_LOGGED, _MODE_CACHE_TS, _MODE_CACHE_VAL
    with _WHISPER_LOCK:
        _WHISPER_MODEL = None
        _WHISPER_FAIL_LOGGED = False
    with _MODE_CACHE_LOCK:
        _MODE_CACHE_TS = 0.0
        _MODE_CACHE_VAL = None


def _normalize_mode(raw: Any) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip().lower()
    if val in VALID_LEAK_GATE_MODES:
        return val
    return None


def get_leak_gate_mode() -> str:
    """Resolve ``gemini_tts_leak_gate_mode`` per call without restart.

    Precedence:
      1. Persisted ``BotSetting`` row via sync store (2 s cache when present)
      2. In-memory ``settings.gemini_tts_leak_gate_mode``
      3. ``GEMINI_TTS_LEAK_GATE_MODE`` env var
      4. Default ``shadow`` on missing/invalid/unreachable state
    """
    global _MODE_CACHE_TS, _MODE_CACHE_VAL
    try:
        now = time.monotonic()
        cached_hit = False
        db_raw: str | None = None
        with _MODE_CACHE_LOCK:
            if _MODE_CACHE_VAL is not None and (now - _MODE_CACHE_TS) < _MODE_SYNC_TTL_SECONDS:
                cached_hit = True
                db_raw = None if _MODE_CACHE_VAL is _MISS_SENTINEL else str(_MODE_CACHE_VAL)
        if not cached_hit:
            try:
                from app.services import key_rotation as _kr

                fetched = _kr.botsetting_sync_read("gemini_tts_leak_gate_mode")
            except Exception:
                fetched = None
            with _MODE_CACHE_LOCK:
                _MODE_CACHE_TS = now
                _MODE_CACHE_VAL = _MISS_SENTINEL if fetched is None else fetched
            db_raw = fetched
        norm_db = _normalize_mode(db_raw)
        if norm_db is not None:
            return norm_db
    except Exception:
        pass

    try:
        from app.config import settings as _settings

        norm_cfg = _normalize_mode(getattr(_settings, "gemini_tts_leak_gate_mode", None))
        if norm_cfg is not None:
            return norm_cfg
    except Exception:
        pass

    norm_env = _normalize_mode(os.getenv("GEMINI_TTS_LEAK_GATE_MODE"))
    if norm_env is not None:
        return norm_env
    return DEFAULT_LEAK_GATE_MODE


def should_verify_chunk(cleaned_chars: int, raw_secs: float) -> bool:
    """Recap risk-tiering: always check >=160 chars; check shorter chunks only when slow."""
    if int(cleaned_chars) >= RECAP_ALWAYS_VERIFY_CHARS:
        return True
    return float(raw_secs) > (float(cleaned_chars) / RECAP_DURATION_DIVISOR + RECAP_DURATION_SLACK_SECONDS)


def count_digit_chars(text: str) -> int:
    return len(_DIGIT_CHAR_RE.findall(str(text or "")))


def opening_myanmar_ratio(text: str, window: int = MYANMAR_OPENING_WINDOW_CHARS) -> float:
    head = str(text or "")[: max(1, int(window))]
    non_ws = [ch for ch in head if not ch.isspace()]
    if not non_ws:
        return 0.0
    mm_count = sum(1 for ch in non_ws if 0x1000 <= ord(ch) <= 0x109F)
    return mm_count / float(len(non_ws))


def _levenshtein_le_1(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by at most 1 edit (insert/delete/substitute)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diffs = sum(1 for ca, cb in zip(a, b) if ca != cb)
        return diffs <= 1
    # Ensure a is shorter
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = diffs = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            diffs += 1
            if diffs > 1:
                return False
            j += 1
    return True


def _matches_any_word(token: str, vocab: set[str] | frozenset[str]) -> bool:
    if token in vocab:
        return True
    if len(token) >= 6:
        for cand in vocab:
            if len(cand) >= 7 and _levenshtein_le_1(token, cand):
                return True
    return False


def _canonicalize_prompt_term(token: str) -> str:
    if len(token) < 6:
        return token
    for target in _ALL_PROMPT_TERMS_GE7:
        if token == target or _levenshtein_le_1(token, target):
            return target
    return token


def pcm_head_to_16k_float32(
    pcm: bytes,
    *,
    channels: int = 1,
    sampwidth: int = 2,
    framerate: int = 24000,
    max_seconds: float = HEAD_CHECK_SECONDS,
) -> np.ndarray:
    """Slice first ``max_seconds`` of PCM and resample to 16 kHz mono float32 in numpy."""
    if not pcm or sampwidth <= 0 or channels <= 0 or framerate <= 0:
        return np.zeros(0, dtype=np.float32)

    frame_bytes = int(channels) * int(sampwidth)
    max_frames = int(round(float(framerate) * float(max_seconds)))
    sliced = pcm[: max_frames * frame_bytes]
    usable_bytes = (len(sliced) // frame_bytes) * frame_bytes
    if usable_bytes <= 0:
        return np.zeros(0, dtype=np.float32)
    sliced = sliced[:usable_bytes]

    if sampwidth == 2:
        arr = np.frombuffer(sliced, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 1:
        arr = (np.frombuffer(sliced, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 4:
        arr = np.frombuffer(sliced, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        return np.zeros(0, dtype=np.float32)

    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)

    if arr.size == 0 or framerate == TARGET_SAMPLE_RATE:
        return arr.astype(np.float32, copy=False)

    target_len = max(1, int(round(arr.size * (TARGET_SAMPLE_RATE / float(framerate)))))
    src_idx = np.linspace(0.0, float(arr.size - 1), num=target_len, dtype=np.float64)
    resampled = np.interp(src_idx, np.arange(arr.size, dtype=np.float64), arr)
    return resampled.astype(np.float32)


def evaluate_transcript_leak(
    en_transcript: str,
    *,
    cleaned_text: str,
    auto_transcript: str | None = None,
    detected_lang: str = "",
    detected_lang_prob: float = 0.0,
) -> LeakGateResult:
    """Apply dual-signal English fingerprint & unexpected-English leak rules."""
    user_ascii_words = {w.lower() for w in _ASCII_WORD_RE.findall(str(cleaned_text or ""))}
    raw_tokens = [w.lower() for w in _ASCII_WORD_RE.findall(str(en_transcript or ""))]

    # Mask any ASCII word that belongs to the user's own cleaned_text so user content
    # (names, loanwords, or English words like "storyteller"/"transcript") never triggers.
    masked_tokens: list[str] = []
    for tok in raw_tokens:
        if _matches_any_word(tok, user_ascii_words):
            masked_tokens.append("<USER>")
        else:
            masked_tokens.append(_canonicalize_prompt_term(tok))

    # Rule (a): >=1 instruction bigram OR >=2 distinct instruction keywords
    hit_bigrams: list[str] = []
    for idx in range(len(masked_tokens) - 1):
        w1, w2 = masked_tokens[idx], masked_tokens[idx + 1]
        if w1 == "<USER>" or w2 == "<USER>":
            continue
        if (w1, w2) in INSTRUCTION_BIGRAMS:
            label = f"{w1}_{w2}"
            if label not in hit_bigrams:
                hit_bigrams.append(label)

    hit_keywords: list[str] = []
    for tok in masked_tokens:
        if tok == "<USER>":
            continue
        if tok in INSTRUCTION_KEYWORDS and tok not in hit_keywords:
            hit_keywords.append(tok)

    if hit_bigrams or len(hit_keywords) >= 2:
        reason_parts = []
        if hit_bigrams:
            reason_parts.append(f"bigrams={','.join(hit_bigrams[:3])}")
        if hit_keywords:
            reason_parts.append(f"keywords={','.join(hit_keywords[:4])}")
        return LeakGateResult(
            verdict="leak",
            reason="fingerprint:" + ";".join(reason_parts),
            head_transcript=str(en_transcript or "").strip(),
            matched_bigrams=tuple(hit_bigrams),
            matched_keywords=tuple(hit_keywords),
            detected_language=detected_lang,
            language_probability=float(detected_lang_prob or 0.0),
        )

    # Rule (b): opening 80 chars >85% Myanmar script AND Whisper detects English (p>=0.80)
    # with >=4 non-user English words.
    mm_ratio = opening_myanmar_ratio(cleaned_text, window=MYANMAR_OPENING_WINDOW_CHARS)
    lang_norm = str(detected_lang or "").strip().lower()
    prob = float(detected_lang_prob or 0.0)
    if mm_ratio > MYANMAR_SCRIPT_RATIO_THRESHOLD and lang_norm == "en" and prob >= UNEXPECTED_EN_PROB_THRESHOLD:
        check_text = auto_transcript if auto_transcript is not None else en_transcript
        auto_tokens = [w.lower() for w in _ASCII_WORD_RE.findall(str(check_text or ""))]
        unexpected_words = [
            tok for tok in auto_tokens if not _matches_any_word(tok, user_ascii_words)
        ]
        if len(unexpected_words) >= UNEXPECTED_EN_MIN_WORDS:
            return LeakGateResult(
                verdict="leak",
                reason=f"unexpected_en:prob={prob:.2f};words={len(unexpected_words)};mm_ratio={mm_ratio:.2f}",
                head_transcript=str(check_text or "").strip(),
                detected_language=lang_norm,
                language_probability=prob,
            )

    return LeakGateResult(
        verdict="clean",
        reason="clean",
        head_transcript=str(en_transcript or "").strip(),
        detected_language=lang_norm,
        language_probability=prob,
    )


def _get_whisper_model() -> Any:
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    with _WHISPER_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        from faster_whisper import WhisperModel

        _WHISPER_MODEL = WhisperModel(
            "base",
            device="cpu",
            compute_type="int8",
        )
        return _WHISPER_MODEL


def _log_whisper_fail_once(exc: BaseException) -> None:
    global _WHISPER_FAIL_LOGGED
    with _WHISPER_LOCK:
        if _WHISPER_FAIL_LOGGED:
            return
        _WHISPER_FAIL_LOGGED = True
    logger.warning(
        "tts_leak_gate: faster-whisper unavailable or failed (%s: %s) — failing open",
        type(exc).__name__,
        exc,
    )


def verify_tts_pcm_head(
    pcm: bytes,
    *,
    cleaned_text: str,
    channels: int = 1,
    sampwidth: int = 2,
    framerate: int = 24000,
    whisper_loader: Callable[[], Any] | None = None,
) -> LeakGateResult:
    """Run local faster-whisper ``base`` on the first 12 s of raw PCM; fail-open on error."""
    try:
        audio_16k = pcm_head_to_16k_float32(
            pcm,
            channels=channels,
            sampwidth=sampwidth,
            framerate=framerate,
            max_seconds=HEAD_CHECK_SECONDS,
        )
        if audio_16k.size == 0:
            return LeakGateResult(verdict="clean", reason="empty_audio")

        loader = whisper_loader or _get_whisper_model
        with _WHISPER_SEM:
            model = loader()
            common_kw = {
                "beam_size": 1,
                "best_of": 1,
                "vad_filter": False,
                "word_timestamps": False,
                "condition_on_previous_text": False,
                "temperature": 0.0,
            }
            segs_en, info_en = model.transcribe(audio_16k, language="en", **common_kw)
            en_text = " ".join(str(getattr(s, "text", "") or "").strip() for s in segs_en).strip()

            # Check fingerprint first before spending a language-detect pass.
            prelim = evaluate_transcript_leak(en_text, cleaned_text=cleaned_text)
            if prelim.verdict == "leak":
                return prelim

            # Run auto language-detect pass only when opening Myanmar ratio qualifies for rule (b).
            if opening_myanmar_ratio(cleaned_text, window=MYANMAR_OPENING_WINDOW_CHARS) > MYANMAR_SCRIPT_RATIO_THRESHOLD:
                segs_auto, info_auto = model.transcribe(audio_16k, language=None, **common_kw)
                auto_text = " ".join(str(getattr(s, "text", "") or "").strip() for s in segs_auto).strip()
                det_lang = str(getattr(info_auto, "language", "") or "")
                det_prob = float(getattr(info_auto, "language_probability", 0.0) or 0.0)
                return evaluate_transcript_leak(
                    en_text,
                    cleaned_text=cleaned_text,
                    auto_transcript=auto_text,
                    detected_lang=det_lang,
                    detected_lang_prob=det_prob,
                )

            det_lang = str(getattr(info_en, "language", "") or "")
            det_prob = float(getattr(info_en, "language_probability", 0.0) or 0.0)
            return LeakGateResult(
                verdict="clean",
                reason="clean",
                head_transcript=en_text,
                detected_language=det_lang,
                language_probability=det_prob,
            )
    except Exception as exc:
        _log_whisper_fail_once(exc)
        return LeakGateResult(verdict="clean", reason="whisper_error_fail_open")


def format_tts_telemetry_line(
    *,
    model: str,
    voice: str,
    cleaned_text: str,
    pcm_secs: float,
    gate_mode: str,
    verdict: str,
    reason: str,
) -> str:
    cleaned = str(cleaned_text or "")
    digits = count_digit_chars(cleaned)
    return (
        f"tts_leak_telemetry model={model} voice={voice} "
        f"cleaned_chars={len(cleaned)} digit_chars={digits} "
        f"pcm_secs={float(pcm_secs):.2f} mode={gate_mode} "
        f"verdict={verdict} reason={reason}"
    )


def log_tts_telemetry(
    *,
    model: str,
    voice: str,
    cleaned_text: str,
    pcm_secs: float,
    gate_mode: str,
    verdict: str,
    reason: str,
) -> str:
    line = format_tts_telemetry_line(
        model=model,
        voice=voice,
        cleaned_text=cleaned_text,
        pcm_secs=pcm_secs,
        gate_mode=gate_mode,
        verdict=verdict,
        reason=reason,
    )
    logger.info(line)
    return line


def _default_sample_dir() -> Path:
    try:
        from app.config import settings as _settings

        root = Path(getattr(_settings, "storage_root", Path("storage")))
    except Exception:
        root = Path("storage")
    return root / "temp" / "tts_leak_samples"


def record_leak_sample(
    pcm: bytes,
    *,
    model: str,
    voice: str,
    cleaned_text: str,
    pcm_secs: float,
    reason: str,
    channels: int = 1,
    sampwidth: int = 2,
    framerate: int = 24000,
    sample_dir: Path | None = None,
    max_entries: int = LEAK_RING_MAX_ENTRIES,
    max_bytes: int = LEAK_RING_MAX_BYTES,
) -> Path | None:
    """Save 12 s head WAV + meta.json into bounded ring buffer (<=20 items, <=8 MB)."""
    global _RING_SEQ
    if not pcm:
        return None
    try:
        target_dir = Path(sample_dir) if sample_dir is not None else _default_sample_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        frame_bytes = max(1, int(channels) * int(sampwidth))
        head_frames = int(round(float(framerate) * HEAD_CHECK_SECONDS))
        head_bytes = pcm[: head_frames * frame_bytes]
        head_bytes = head_bytes[: (len(head_bytes) // frame_bytes) * frame_bytes]
        if not head_bytes:
            return None

        with _RING_LOCK:
            _RING_SEQ += 1
            seq = _RING_SEQ
            ts_ms = int(time.time() * 1000)
            stem = f"leak_{ts_ms}_{seq:05d}"
            wav_path = target_dir / f"{stem}.wav"
            meta_path = target_dir / f"{stem}.meta.json"

            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(max(1, int(channels)))
                wf.setsampwidth(max(1, int(sampwidth)))
                wf.setframerate(max(8000, int(framerate)))
                wf.writeframes(head_bytes)

            meta = {
                "model": str(model or ""),
                "voice": str(voice or ""),
                "chars": len(str(cleaned_text or "")),
                "secs": round(float(pcm_secs), 3),
                "reason": str(reason or ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "text_head": str(cleaned_text or "")[:LEAK_TEXT_SNIPPET_MAX_CHARS],
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

            _evict_ring_buffer_locked(target_dir, max_entries=max_entries, max_bytes=max_bytes)
            return wav_path
    except Exception as exc:
        logger.debug("tts_leak_gate: ring buffer write skipped: %s", exc)
        return None


def _evict_ring_buffer_locked(
    target_dir: Path,
    *,
    max_entries: int,
    max_bytes: int,
) -> None:
    wav_files = sorted(target_dir.glob("leak_*.wav"), key=lambda p: (p.stat().st_mtime_ns, p.name))

    def _pair_files(wav: Path) -> list[Path]:
        meta = wav.with_suffix(".meta.json")
        return [p for p in (wav, meta) if p.exists()]

    def _total_bytes() -> int:
        total = 0
        for f in target_dir.iterdir():
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    while wav_files and (len(wav_files) > max_entries or _total_bytes() > max_bytes):
        oldest = wav_files.pop(0)
        for f in _pair_files(oldest):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
