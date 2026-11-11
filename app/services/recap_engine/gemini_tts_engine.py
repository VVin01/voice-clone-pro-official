"""Gemini TTS narrator engine (production-grade programme, 2026-10-29).

An ADDITIONAL narration engine behind the same interface EdgeSmartSync
already uses for Edge TTS (``generate_channel99_voice(text, voice)`` ->
48 kHz mono WAV), ported from the desktop Recapter tool's
``gemini_tts.py`` and adapted to this server codebase:

* Keys come from the process environment with the SAME precedence the
  recap planner already uses for its Gemini text calls
  (GEMINI_IMAGE_PROJECT_<N>_KEY -> GEMINI_API_KEY_<N> -> GEMINI_API_KEYS
  -> GEMINI_API_KEY), so no new secret or config is introduced — the
  option works wherever the existing Gemini translation already works.
  Keys are never logged or printed.
* Every line is a separate request carrying the same narrator
  direction (``narrator_prompt``): one prebuilt voice, one speaker
  identity, pace and pitch from the first line to the last — Gemini
  otherwise drifts between requests.
* A key that hits quota/auth errors (429/400/401/403) is skipped for
  the rest of the run for that model; 5xx "busy" responses retry in
  rounds (3 x 20 s), the pattern the desktop tool ships.
* Circuit breaker (server adaptation): when every key/model is
  exhausted, or Gemini reports the host location as unsupported, the
  engine marks itself exhausted and later lines fail FAST. The caller
  (EdgeSmartSync) falls back per segment to the persona engine and
  records the segment in fallback_segments — the failure is visible in
  job status, never silent, and one dead engine cannot stall a whole
  recap re-trying every line.
* Finished lines are cached in the job work dir (same fingerprint
  scheme as the desktop tool), so a resumed run reuses them.
* Batch U (2026-11-04): the starting key rotates per call through
  KeyHealth.order() on this engine's own "gemini_tts" registry
  (Batch O had deliberately left the engine on a stable order), and
  the starting TTS model rotates per call across TTS_MODELS — each
  TTS model carries its own per-project quota bucket, and
  rotation spreads lines across all of them instead of draining the
  first bucket first. Cooled keys are tried last, never dropped;
  failover inside a call is unchanged.
* Batch DN (2026-11-10): TTS_MODELS migrated ahead of the preview
  models' 2026-11-17 retirement (see the constant), and the key walk
  is quota-aware: per model, keys are ordered by remaining daily
  quota (KeyHealth.order_for_model on counted successes) and every
  success is recorded via record_usage. BYOK engines built for a
  user's OWN key pass record_usage=False — their quota is theirs and
  never enters the shared pool ledger.
* Batch DO (2026-11-11): the prompt is built PER MODEL. Both GA 3.8
  models READ the full narrator_prompt AUDIO-PROFILE aloud instead
  of following it (proven firsthand on the DN deploy — 130+ s of
  profile-reading per line, ASR-verified), so the 3.8 family gets
  the compact narrator_prompt_short shape; the preview models keep
  the full profile they follow correctly.

Quota note: Gemini TTS models carry their own per-project daily
quota, separate from the text models the planner uses. Measured caps
(AI Studio rate-limit table, Free tier, 2026-11-10): every model in
TTS_MODELS allows 10 requests/day per project at 3 RPM / 10K TPM.
When quota runs out mid-recap, the remaining lines degrade to the
persona engine with fallback_used=true — by design, and surfaced to
the user.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Optional

# Audit fix E: the Gemini pool is built by ONE shared helper
# (app.services.key_rotation) so TTS sees the same union of keys as the
# recap planner and the group admin. Defensive import: if the app
# package is unavailable, collect_gemini_keys falls back to its
# original first-non-empty-source behaviour below.
try:
    from app.services import key_rotation as _key_rotation
except Exception:  # pragma: no cover - standalone load
    _key_rotation = None  # type: ignore[assignment]

VOICES = [("Charon", "male"), ("Algenib", "male"), ("Orus", "male"), ("Iapetus", "male"), ("Puck", "male"),
          ("Gacrux", "female"), ("Sulafat", "female"), ("Kore", "female"), ("Achernar", "female"), ("Leda", "female")]
GEMINI_VOICE_NAMES: tuple[str, ...] = tuple(name for name, _ in VOICES)
GEMINI_VOICE_GENDER: dict[str, str] = dict(VOICES)
# Burmese display names for the Gemini voices — the canonical map.
# These are the names users see in the Mini App Gemini voice picker
# (static/index.html <option> labels) and hear in the Group AI Admin
# knowledge sheet; user-facing job messages must name a Gemini-voiced
# recap with THESE names, never the raw engine slug (R40 item 1).
GEMINI_VOICE_DISPLAY_MY: dict[str, str] = {
    "Charon": "ကိုသီဟ",
    "Algenib": "ကိုအာကာ",
    "Orus": "ကိုမင်းခန့်",
    "Iapetus": "ကိုလင်းခန့်",
    "Puck": "ကိုသော်တာ",
    "Kore": "မစန္ဒာ",
    "Leda": "မနှင်းဆီ",
    "Sulafat": "မမေသူ",
    "Gacrux": "မသီရိ",
    "Achernar": "မသွန်းရည်",
}
# Batch DN (2026-11-10): model migration ahead of the 2026-11-17
# retirement of BOTH preview models on the Gemini API. Rotation order:
# the GA replacement first (best quality), the current primary next
# (its remaining pre-retirement days still serve), the GA lite model
# (fastest), then the current reserve. After Nov 17 the two previews
# simply fail per call and the existing failover moves on — no second
# migration is needed at the deadline. gemini-2.5-pro-preview-tts is
# deliberately ABSENT: its free-tier limits are 0/0/0 (it 429s on
# every key), so it can never serve a line.
TTS_MODELS = (
    "gemini-3.8-flash-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-3.8-flash-lite-tts",
    "gemini-2.5-flash-preview-tts",
)
# Batch U: process-level starting-model rotation cursor (in-memory
# only, the same lifetime as KeyHealth). Advanced once per _ask() call.
_TTS_MODEL_LOCK = threading.Lock()
_TTS_MODEL_CURSOR = 0


def _rotated_tts_models() -> tuple[str, ...]:
    """TTS_MODELS rotated so consecutive calls start on different
    models; failover inside the call walks the remaining model(s)."""
    global _TTS_MODEL_CURSOR
    if len(TTS_MODELS) < 2:
        return TTS_MODELS
    with _TTS_MODEL_LOCK:
        start = _TTS_MODEL_CURSOR % len(TTS_MODELS)
        _TTS_MODEL_CURSOR += 1
    return TTS_MODELS[start:] + TTS_MODELS[:start]
PROFILE_VERSION = "single_narrator_v3"
DEFAULT_DIRECTION = ("An energetic, ALIVE Burmese storyteller PERFORMING the story for listeners — warm, vivid and "
                     "gripping, never flat or robotic: the pace breathes with the scene (quicker and tighter at "
                     "danger and action, easing off in calm or sad moments), dramatic beats land with real "
                     "emphasis, questions sound like questions, hooks rise with excitement, and the voice rises "
                     "and falls naturally, like someone telling a story they care about. Diction stays crisp and "
                     "the story always stays clear — energy comes from pace, emphasis and timing, never shouting.")
# Batch CY: busy patience trimmed 3 rounds x 20 s -> 2 rounds x 10 s.
# On a busy (5xx) day the old values parked every affected line for
# up to 60 s of pure sleep (including a wait AFTER the final round,
# which could only delay the persona fallback) before the walk
# repeated; CY keeps the same key/model rotation order and the same
# gemini_tts health-registry semantics — only the stall patience
# shrinks, so a busy line fails over to the next round (and then to
# the persona engine) sooner.
BUSY_ROUNDS, BUSY_WAIT_SECONDS = 2, 10
# Batch AJ / L20: hard cap on the HTTP attempts ONE _ask() call may
# make for ONE narration line. The loop's own structure bounded it
# only loosely: quota/auth deaths are marked dead, 5xx answers set
# `busy` and repeat whole rounds, and — confirmed from the code —
# errors that are NEITHER (e.g. 404/409/422 or code-less transport
# errors) were logged and retried on every busy round without ever
# being marked dead, for up to BUSY_ROUNDS x models x keys attempts
# (60 for a 10-key pool) at the client's 180 s timeout. Unclassified
# errors are now marked dead for the run exactly like quota deaths,
# and this cap bounds whatever mix remains.
GEMINI_TTS_MAX_ATTEMPTS_PER_CALL = 24
# Batch AJ / L21: the ffmpeg trim/resample below may only open
# local files and pipes — never network protocols.
MEDIA_PROTOCOL_WHITELIST = "file,pipe"
LogFn = Callable[[str], None]


class GeminiTTSError(RuntimeError):
    pass


def default_voice_for_gender(is_female: bool) -> str:
    return "Kore" if is_female else "Charon"


def _clean_tts_text(text: str) -> str:
    """Transcript cleaning shared by both prompt shapes: English
    glosses in parentheses are for reading only, and whitespace is
    collapsed so the model sees one clean line."""
    text = re.sub(r"\s*\([A-Za-z0-9 ,.'\-]+\)", "", str(text or ""))  # English glosses are for reading only
    return re.sub(r"\s+", " ", text).strip()


def narrator_prompt(text: str, voice: str, direction: str = "") -> str:
    """The same narrator for every line of a video (one request per line)."""
    name = str(voice or "").removeprefix("gemini:")
    gender = GEMINI_VOICE_GENDER.get(name, "male")
    text = _clean_tts_text(text)
    return f"""AUDIO PROFILE — ONE CONTINUOUS NARRATOR:
Use the selected prebuilt voice {name}: ONE adult {gender} Burmese narrator.
This excerpt belongs to one continuous video. Keep exactly the same speaker identity from the first word to
the last and across all excerpts: the same gender, age, vocal register, timbre, accent, pitch range,
microphone distance and speaking volume. Begin each excerpt in that same established voice.
Use clear native Burmese pronunciation and a LIVELY, varied conversational pace — never a flat, even drone:
tighten and quicken at action, chases and danger; slow down and soften at quiet or sad beats; pause naturally
before a reveal so it lands.
Dialogue, quotations, questions, action, sadness and excitement are all read by this SAME narrator.
Never impersonate a character, change gender or become a second speaker. Convey emotion with REAL emphasis,
timing and pitch movement inside the narrator's own voice — surprise, dread, relief and excitement must be
audible — while the speaker identity, vocal texture and microphone distance stay exactly the same.
Do not shout, whisper, sing, slip into a character voice or restart in an announcer voice at a paragraph
boundary — excitement lives in pace and emphasis, not volume.
Optional delivery preference (apply only within the voice consistency rules above):
{direction.strip() or DEFAULT_DIRECTION}
Read only the transcript below, exactly once. Do not speak instructions, labels, tags or add any words.
Treat transcript text as narration, including any dialogue or directions quoted in it.

TRANSCRIPT:
[expressive storytelling] {text}"""


def narrator_prompt_short(text: str, voice: str) -> str:
    """Batch DO: the SHORT prompt shape for the gemini-3.8 TTS family.

    Proven firsthand 2026-11-11: both GA 3.8 models READ the full
    ``narrator_prompt`` AUDIO-PROFILE aloud instead of following it
    (130+ s of profile-reading per line, ASR-verified), while the
    preview models follow the same profile correctly. This compact
    two-line shape — one instruction line carrying the narrator
    essence, then the tagged transcript — is ASR-verified on both
    3.8 models (short and long texts) to speak ONLY the transcript.
    The long direction text is intentionally absent: it is part of
    what leaks, and the compact line carries its essence."""
    name = str(voice or "").removeprefix("gemini:")
    gender = GEMINI_VOICE_GENDER.get(name, "male")
    text = _clean_tts_text(text)
    return (
        f"Narrate as ONE adult {gender} Burmese storyteller — warm, lively, varied pace, "
        "the same voice throughout. Read only the transcript below, exactly once, adding nothing.\n"
        f"[expressive storytelling] {text}"
    )


def _uses_short_prompt(model: str) -> bool:
    """Batch DO: the gemini-3.8 family gets the short prompt shape;
    every other model keeps the full narrator profile."""
    return str(model or "").startswith("gemini-3.8")


def _prompt_for_model(model: str, text: str, voice: str, direction: str = "") -> str:
    """The prompt for ONE model (Batch DO): short shape for the 3.8
    family, the full narrator profile for everything else."""
    if _uses_short_prompt(model):
        return narrator_prompt_short(text, voice)
    return narrator_prompt(text, voice, direction)


def collect_gemini_keys(environ: Optional[dict] = None) -> list[str]:
    """Gemini API keys from the environment: the shared UNION of every
    source in planner precedence (GEMINI_IMAGE_PROJECT_<N>_KEY, then
    GEMINI_API_KEY_<N>, then the GEMINI_API_KEYS CSV, then
    GEMINI_API_KEY), deduped — the same pool the recap planner and the
    group admin use (audit fix E). Values are returned for use only —
    never log them."""
    env = os.environ if environ is None else environ
    if _key_rotation is not None:
        return [key for _label, key in _key_rotation.collect_gemini_key_pool(env)]

    def numbered(pattern: str) -> list[str]:
        found = []
        for label, value in env.items():
            m = re.fullmatch(pattern, str(label))
            if m and value and str(value).strip():
                found.append((int(m.group(1)), str(value).strip()))
        return [v for _n, v in sorted(found)]

    keys = numbered(r"GEMINI_IMAGE_PROJECT_(\d+)_KEY")
    if not keys:
        keys = numbered(r"GEMINI_API_KEY_(\d+)")
    if not keys and env.get("GEMINI_API_KEYS"):
        keys = [p.strip() for p in str(env["GEMINI_API_KEYS"]).split(",") if p.strip()]
    if not keys and env.get("GEMINI_API_KEY"):
        keys = [str(env["GEMINI_API_KEY"]).strip()]
    return list(dict.fromkeys(k for k in keys if k))


def _proxy() -> str:
    configured = str(os.getenv("GEMINI_PROXY") or "").strip()
    if configured:
        return "" if configured.lower() == "none" else configured
    return ""


def _client(key: str):
    from google import genai
    from google.genai import types

    via = _proxy()
    return genai.Client(api_key=key, http_options=types.HttpOptions(
        timeout=180_000, retry_options=types.HttpRetryOptions(attempts=1),
        client_args={"proxy": via} if via else None, async_client_args={"proxy": via} if via else None))


def _code(exc: Exception) -> int:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        found = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
        return int(found.group(1)) if found else 0


class GeminiTTSEngine:
    """Same interface as EdgeTTSEngine: generate_channel99_voice(text, voice) -> WAV path."""

    def __init__(self, *, ffmpeg: str, work_dir: Path, rate: str = "+0%", keys: Optional[list[str]] = None,
                 direction: str = "", log: LogFn = print, record_usage: bool = True, **_ignored: Any) -> None:
        self.ffmpeg, self.work_dir, self.log = ffmpeg, Path(work_dir), log
        try:
            self.rate = int(str(rate or "+0%").strip().rstrip("%"))
        except ValueError:
            self.rate = 0
        self.direction = str(direction or "").strip()
        self.keys = list(keys) if keys is not None else collect_gemini_keys()
        if not self.keys:
            raise GeminiTTSError("Gemini TTS: no Gemini API key configured (GEMINI_API_KEY / GEMINI_API_KEYS).")
        self._dead: set[tuple[str, int]] = set()
        self._last_key = 0
        self.exhausted = False  # circuit breaker: total failure -> later lines fail fast
        # Batch CG: the last HTTP status + short error text seen per
        # key index, so callers routing a SINGLE user-owned key can
        # classify its failure (auth-class → the key is invalid;
        # 429/quota → the key is merely spent) without ever logging
        # the key itself. Inert for the shared pool path.
        self.key_error_codes: dict[int, int] = {}
        self.key_error_texts: dict[int, str] = {}
        # Batch DN: whether successful syntheses are counted into the
        # shared "gemini_tts" usage ledger. Pool engines keep the
        # default (True); engines built around ONE user-owned key
        # (Batch CG routes) pass False — the user's quota is theirs
        # alone and must not distort the pool's remaining-quota math.
        # Cooldown bookkeeping (mark_success / mark_failure) and the
        # CG key_error_codes classification are unaffected either way.
        self._record_usage = bool(record_usage)

    def fingerprint(self, voice: str) -> str:
        profile = narrator_prompt("", voice, self.direction)
        return voice + "|gemini|" + hashlib.sha256(f"{PROFILE_VERSION}|{profile}|{self.rate}".encode()).hexdigest()

    def _key_order_for_model(self, model: str, tts_health: Any) -> list[int]:
        """Candidate key indices for ONE TTS model (Batch DN).

        Starts from the _last_key offset, then re-sorts by the health
        registry's quota-aware order for THIS model: the registry
        omits cooled keys (tail-appended here, never dropped — Batch
        U semantics) and sorts the rest richest-quota-first with
        rotation preserved inside ties. The order is recomputed per
        model because remaining quota is per (key, model): a key
        drained on one model still leads on another model's untouched
        bucket. Any registry failure degrades to the plain rotated
        offset order."""
        order = list(range(len(self.keys)))
        order = order[self._last_key:] + order[:self._last_key]
        if tts_health is None:
            return order
        try:
            ranked_keys = tts_health.order_for_model(list(self.keys), model)
            _seen_keys = set(ranked_keys)
            ranked_keys += [k for k in self.keys if k not in _seen_keys]
            rank = {key: pos for pos, key in enumerate(ranked_keys)}
            return sorted(order, key=lambda i: rank[self.keys[i]])
        except Exception:
            return order

    def _ask(self, text: str, voice_name: str) -> bytes:
        # Batch DO: callers pass the RAW transcript; the prompt is
        # built PER MODEL inside the model loop (short shape for the
        # 3.8 family, full narrator profile for the previews).
        from google.genai import types

        config = types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name))))
        # TTS keeps its OWN registry ("gemini_tts"): the TTS models'
        # per-key quota is separate from the text models', so a
        # TTS-dead key must not sideline the planner.
        tts_health = _key_rotation.health_for("gemini_tts") if _key_rotation is not None else None
        models = _rotated_tts_models()
        last = ""
        attempts = 0
        for _round_no in range(BUSY_ROUNDS):
            busy = False
            for model in models:
                # Batch DO: per-model prompt shape — the 3.8 family
                # must never receive the full AUDIO-PROFILE text (it
                # reads it aloud); previews keep the full profile.
                prompt = _prompt_for_model(model, text, voice_name, self.direction)
                # Batch DN: per-model quota-aware key order (richest
                # key first for THIS model) — see _key_order_for_model.
                order = self._key_order_for_model(model, tts_health)
                for i in order:
                    if (model, i) in self._dead:
                        continue
                    # Batch AJ / L20: total-attempt cap (see the
                    # constant's comment) — checked before every
                    # request so no error mix can exceed it.
                    attempts += 1
                    if attempts > GEMINI_TTS_MAX_ATTEMPTS_PER_CALL:
                        self.exhausted = True
                        raise GeminiTTSError(
                            "Gemini TTS: attempt budget exhausted "
                            f"({GEMINI_TTS_MAX_ATTEMPTS_PER_CALL} requests for one "
                            f"line) — remaining lines will use the persona engine "
                            f"instead. ({last})"
                        )
                    try:
                        client = _client(self.keys[i])  # kept in a variable: a dropped genai Client closes itself
                        response = client.models.generate_content(model=model, contents=prompt, config=config)
                        for part in response.candidates[0].content.parts:
                            data = getattr(getattr(part, "inline_data", None), "data", None)
                            if data:
                                self._last_key = i
                                if tts_health is not None:
                                    tts_health.mark_success(self.keys[i])
                                    # Batch DN: count the success
                                    # against this (key, model)'s
                                    # daily cap so later calls start
                                    # from a richer key. BYOK engines
                                    # (record_usage=False) skip this —
                                    # a user's own quota never enters
                                    # the shared pool ledger.
                                    if self._record_usage:
                                        try:
                                            tts_health.record_usage(self.keys[i], model)
                                        except Exception:
                                            pass
                                return data
                        last = f"{model}: no audio in the answer"
                    except Exception as exc:  # noqa: BLE001
                        code = _code(exc)
                        self.key_error_codes[i] = code
                        self.key_error_texts[i] = str(exc)[:200]
                        last = f"{model} key {i + 1}: {code or ''} {str(exc)[:160]}"
                        if "location is not supported" in str(exc).lower():
                            self.exhausted = True
                            raise GeminiTTSError(
                                "Gemini TTS: this server location is not supported by Gemini TTS — "
                                "narration lines will use the persona engine instead."
                            ) from exc
                        if code == 429 or code in (400, 401, 403):
                            self._dead.add((model, i))
                            # Audit fix E: quota/auth deaths (not 400,
                            # which can be request-specific) also cool
                            # the key for later runs, not just this one.
                            if tts_health is not None and code != 400:
                                tts_health.mark_failure(self.keys[i])
                            continue
                        if code in (500, 502, 503, 504):
                            busy = True
                            self.log(f"⏳ Gemini TTS {model} is busy ({code}) — next key/model")
                            break
                        # Batch AJ / L20: unclassified errors (not
                        # 429/auth, not 5xx) used to fall through
                        # here with NO dead-marking, so the same
                        # (model, key) was retried on every busy
                        # round. Mark it dead for this run — the
                        # request-specific failure gets one try per
                        # model, exactly like a quota death, and the
                        # attempt cap above bounds the rest.
                        self._dead.add((model, i))
                        self.log(f"⚠️ Gemini TTS key {i + 1}: {str(exc)[:100]}")
            if not busy:
                break
            # Batch CY: no wait after the FINAL round — the call is
            # about to fail over to the persona engine, and sleeping
            # first was pure stall.
            if _round_no < BUSY_ROUNDS - 1:
                self.log(f"⏳ Gemini TTS busy — waiting {BUSY_WAIT_SECONDS} s")
                time.sleep(BUSY_WAIT_SECONDS)
        self.exhausted = True
        raise GeminiTTSError(
            "Gemini TTS: all keys' quota is exhausted or unavailable for today — "
            f"remaining lines will use the persona engine instead. ({last})"
        )

    def generate_channel99_voice(self, text: str, voice: str) -> Path:
        """The line as 48 kHz mono WAV, silence at both ends trimmed, speed applied (cached in the job)."""
        if self.exhausted:
            raise GeminiTTSError("Gemini TTS exhausted for this run — persona fallback.")
        name = str(voice or "").removeprefix("gemini:")
        if name not in GEMINI_VOICE_NAMES:
            raise GeminiTTSError(f"Unknown Gemini voice '{voice}'. Use one of: {', '.join(GEMINI_VOICE_NAMES)}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{self.fingerprint(voice)}|{text}".encode("utf-8")).hexdigest()[:16]
        out = self.work_dir / f"gemini_{key}.wav"
        if out.is_file() and out.stat().st_size > 1000:
            return out
        pcm = self._ask(text, name)
        raw = out.with_suffix(".24k.wav")
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,"
                "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.12,areverse")
        tempo = f",atempo={max(0.5, min(2.0, 1 + self.rate / 100)):.3f}" if self.rate else ""
        # Bounded like every engine media call (audit fix 2026-11-03;
        # 120s == core.PROBE_TIMEOUT_SECONDS — one TTS line is seconds
        # long, so this is a hang backstop, never a real limit).
        subprocess.run([self.ffmpeg, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
                        "-y", "-v", "error", "-i", str(raw), "-af", trim + tempo, "-ar", "48000",
                        "-ac", "1", "-c:a", "pcm_s16le", str(out)], check=True, timeout=120)
        raw.unlink(missing_ok=True)
        return out


# ── Batch CG: bring-your-own-key routing ─────────────────────────────
# A user's own Gemini key is tried FIRST for that user's Gemini TTS;
# the service pool only tops up. The composite below exposes the same
# generate_channel99_voice interface as GeminiTTSEngine so both
# callers (the TTS tab and recap narration) route identically. The
# key VALUE never appears here beyond the engine that already holds
# it — nothing in this section logs, formats or returns a key.

def wav_duration_seconds(path: Path) -> float:
    """Duration of a PCM WAV via the stdlib wave module (the engines
    write plain PCM, so no ffprobe round-trip is needed for the
    delivered-seconds accounting). 0.0 on any unreadable file."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            if rate <= 0:
                return 0.0
            return w.getnframes() / float(rate)
    except Exception:
        return 0.0


def classify_gemini_key_failure(engine: Any, exc: BaseException) -> str:
    """Classify a SINGLE-key engine's failure for BYOK routing:
    "invalid" (auth-class — Google rejected the key itself: 401/403,
    "API key not valid", permission-denied), "spent" (429 / quota —
    the key is genuine but out of daily quota), or "error" (anything
    else). Only "invalid" may mark a user's key invalid; every class
    still steps the current job to the pool — a bad key never fails
    a job."""
    codes = getattr(engine, "key_error_codes", None) or {}
    texts = " ".join((getattr(engine, "key_error_texts", None) or {}).values())
    haystack = f"{texts} {exc}".lower()
    if (
        any(c in (401, 403) for c in codes.values())
        or "api key not valid" in haystack
        or "api_key_invalid" in haystack
        or "permission_denied" in haystack
        or "permission denied" in haystack
    ):
        return "invalid"
    if (
        429 in codes.values()
        or "quota" in haystack
        or "resource_exhausted" in haystack
        or "exhausted" in haystack
    ):
        return "spent"
    return "error"


class RoutedGeminiEngine:
    """own-key → pool composite over two GeminiTTSEngine instances.

    Per call: the own-key engine serves while it can; a GeminiTTSError
    from it is classified (invalid sets ``own_key_invalid`` so the
    caller can mark the stored key + tell the user) and the call
    steps to the pool engine. The pool engine's own failures
    propagate unchanged (the caller's exhaustion handling — CE
    fallback / recap per-segment fallback — decides). Successful
    draws are counted per source (requests + delivered WAV seconds)
    for the taste/budget accounting; failed attempts count nothing.
    Thread-safe: recap narration calls from a worker pool.
    """

    def __init__(
        self,
        own_engine: Any = None,
        pool_engine: Any = None,
        *,
        log: LogFn = print,
    ) -> None:
        self.own_engine = own_engine
        self.pool_engine = pool_engine
        self.log = log
        self.exhausted = False
        self.own_key_invalid = False
        self._own_spent = own_engine is None
        self.own_requests = 0
        self.own_seconds = 0.0
        self.pool_requests = 0
        self.pool_seconds = 0.0
        self._lock = threading.Lock()

    def generate_channel99_voice(self, text: str, voice: str) -> Path:
        own_exc: BaseException | None = None
        if self.own_engine is not None and not self._own_spent:
            try:
                out = self.own_engine.generate_channel99_voice(text, voice)
                with self._lock:
                    self.own_requests += 1
                    self.own_seconds += wav_duration_seconds(out)
                return out
            except GeminiTTSError as exc:
                own_exc = exc
                cls = classify_gemini_key_failure(self.own_engine, exc)
                with self._lock:
                    if cls == "invalid":
                        self.own_key_invalid = True
                    self._own_spent = True
                self.log(f"🔑 Own Gemini key stepped aside ({cls}) — pool takes over")
        if self.pool_engine is not None:
            out = self.pool_engine.generate_channel99_voice(text, voice)
            with self._lock:
                self.pool_requests += 1
                self.pool_seconds += wav_duration_seconds(out)
            return out
        self.exhausted = True
        if own_exc is not None:
            raise own_exc
        raise GeminiTTSError("Gemini TTS: no Gemini route available for this call.")
