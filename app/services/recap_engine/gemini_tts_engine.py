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

Quota note: Gemini TTS preview models carry their own per-key daily
quota, separate from the text models the planner uses; when it runs
out mid-recap, the remaining lines degrade to the persona engine with
fallback_used=true — by design, and surfaced to the user.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import wave
from pathlib import Path
from typing import Callable, Optional

VOICES = [("Charon", "male"), ("Algenib", "male"), ("Orus", "male"), ("Iapetus", "male"), ("Puck", "male"),
          ("Gacrux", "female"), ("Sulafat", "female"), ("Kore", "female"), ("Achernar", "female"), ("Leda", "female")]
GEMINI_VOICE_NAMES: tuple[str, ...] = tuple(name for name, _ in VOICES)
GEMINI_VOICE_GENDER: dict[str, str] = dict(VOICES)
TTS_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
PROFILE_VERSION = "single_narrator_v3"
DEFAULT_DIRECTION = ("An energetic, ALIVE Burmese storyteller PERFORMING the story for listeners — warm, vivid and "
                     "gripping, never flat or robotic: the pace breathes with the scene (quicker and tighter at "
                     "danger and action, easing off in calm or sad moments), dramatic beats land with real "
                     "emphasis, questions sound like questions, hooks rise with excitement, and the voice rises "
                     "and falls naturally, like someone telling a story they care about. Diction stays crisp and "
                     "the story always stays clear — energy comes from pace, emphasis and timing, never shouting.")
BUSY_ROUNDS, BUSY_WAIT_SECONDS = 3, 20
LogFn = Callable[[str], None]


class GeminiTTSError(RuntimeError):
    pass


def default_voice_for_gender(is_female: bool) -> str:
    return "Kore" if is_female else "Charon"


def narrator_prompt(text: str, voice: str, direction: str = "") -> str:
    """The same narrator for every line of a video (one request per line)."""
    name = str(voice or "").removeprefix("gemini:")
    gender = GEMINI_VOICE_GENDER.get(name, "male")
    text = re.sub(r"\s*\([A-Za-z0-9 ,.'\-]+\)", "", str(text or ""))  # English glosses are for reading only
    text = re.sub(r"\s+", " ", text).strip()
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


def collect_gemini_keys(environ: Optional[dict] = None) -> list[str]:
    """Gemini API keys from the environment, planner precedence:
    GEMINI_IMAGE_PROJECT_<N>_KEY, then GEMINI_API_KEY_<N>, then the
    GEMINI_API_KEYS CSV, then GEMINI_API_KEY. Values are returned for
    use only — never log them."""
    env = os.environ if environ is None else environ

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
                 direction: str = "", log: LogFn = print) -> None:
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

    def fingerprint(self, voice: str) -> str:
        profile = narrator_prompt("", voice, self.direction)
        return voice + "|gemini|" + hashlib.sha256(f"{PROFILE_VERSION}|{profile}|{self.rate}".encode()).hexdigest()

    def _ask(self, prompt: str, voice_name: str) -> bytes:
        from google.genai import types

        config = types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name))))
        order = list(range(len(self.keys)))
        order = order[self._last_key:] + order[:self._last_key]
        last = ""
        for _round in range(BUSY_ROUNDS):
            busy = False
            for model in TTS_MODELS:
                for i in order:
                    if (model, i) in self._dead:
                        continue
                    try:
                        client = _client(self.keys[i])  # kept in a variable: a dropped genai Client closes itself
                        response = client.models.generate_content(model=model, contents=prompt, config=config)
                        for part in response.candidates[0].content.parts:
                            data = getattr(getattr(part, "inline_data", None), "data", None)
                            if data:
                                self._last_key = i
                                return data
                        last = f"{model}: no audio in the answer"
                    except Exception as exc:  # noqa: BLE001
                        code = _code(exc)
                        last = f"{model} key {i + 1}: {code or ''} {str(exc)[:160]}"
                        if "location is not supported" in str(exc).lower():
                            self.exhausted = True
                            raise GeminiTTSError(
                                "Gemini TTS: this server location is not supported by Gemini TTS — "
                                "narration lines will use the persona engine instead."
                            ) from exc
                        if code == 429 or code in (400, 401, 403):
                            self._dead.add((model, i))
                            continue
                        if code in (500, 502, 503, 504):
                            busy = True
                            self.log(f"⏳ Gemini TTS {model} is busy ({code}) — next key/model")
                            break
                        self.log(f"⚠️ Gemini TTS key {i + 1}: {str(exc)[:100]}")
            if not busy:
                break
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
        pcm = self._ask(narrator_prompt(text, voice, self.direction), name)
        raw = out.with_suffix(".24k.wav")
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,"
                "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.12,areverse")
        tempo = f",atempo={max(0.5, min(2.0, 1 + self.rate / 100)):.3f}" if self.rate else ""
        subprocess.run([self.ffmpeg, "-y", "-v", "error", "-i", str(raw), "-af", trim + tempo, "-ar", "48000",
                        "-ac", "1", "-c:a", "pcm_s16le", str(out)], check=True)
        raw.unlink(missing_ok=True)
        return out
