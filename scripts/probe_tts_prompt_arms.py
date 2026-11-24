#!/usr/bin/env python3
"""Phase 0 Standalone Probe: Gemini TTS Prompt Arms A/B/C1/C2 Leak & Quality Audit.

Standalone script (not imported by the app) intended to run inside the production
container on the VPS where the Gemini pool keys live.

Evaluates 4 prompt arms x 2 GA TTS models x 10 fixed Burmese chunks (hard cap: 80 calls):
  - Arm A  (baseline) : current narrator_prompt_short
  - Arm B  (sys_inst) : line 1 in GenerateContentConfig(system_instruction=...),
                        contents = "[expressive storytelling] {text}"
  - Arm C1 (tag_only) : contents = "[warm, lively storytelling] {text}"
  - Arm C2 (bare)     : contents = "{text}"

Usage:
  python scripts/probe_tts_prompt_arms.py [--arms A,B,C1,C2] [--models gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts] [--list]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any
import wave

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services import key_rotation as kr  # noqa: E402
from app.services.recap_engine import gemini_tts_engine as gte  # noqa: E402
from app.services import tts_leak_gate as leak_gate  # noqa: E402

HARD_MAX_API_CALLS = 80
DEFAULT_ARMS = ("A", "B", "C1", "C2")
DEFAULT_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")

ARM_DESCRIPTIONS: dict[str, str] = {
    "A": "Baseline narrator_prompt_short (line 1 English prose + [expressive storytelling] in contents)",
    "B": "Line 1 moved to GenerateContentConfig(system_instruction=...); contents='[expressive storytelling] {text}'",
    "C1": "Tag-only zero prose: contents='[warm, lively storytelling] {text}'",
    "C2": "Bare cleaned transcript only: contents='{text}'",
}

# 10 fixed Burmese chunks: 5 plain-TTS (~380-400 chars) + 5 Recap (~80-120 chars).
# Voices alternate 5 Charon + 5 Kore; covers digits, English loanwords, dialogue, and sad/slow mood.
PROBE_CHUNKS: tuple[dict[str, Any], ...] = (
    {
        "idx": 1,
        "kind": "plain_tts",
        "voice": "Charon",
        "tag": "narrative_opening",
        "text": (
            "မိုးသက်လေပြင်းတွေ တိုက်ခတ်နေတဲ့ ညဉ့်နက်သန်းခေါင်ယံမှာ မြို့စွန်က အိမ်အိုကြီးတစ်လုံးရဲ့ တံခါးဝရှေ့ကို "
            "လူငယ်တစ်ယောက် တိတ်တဆိတ် ရောက်လာခဲ့ပါတယ်။ သူ့လက်ထဲမှာ နှစ်ပေါင်းများစွာ သိမ်းဆည်းထားခဲ့တဲ့ သော့ဟောင်းလေးတစ်ချောင်းကို "
            "တင်းတင်းကျပ်ကျပ် ဆုပ်ကိုင်ထားပြီး ရင်ခုန်သံတွေကလည်း မိုးသံနဲ့အပြိုင် မြန်ဆန်နေခဲ့ပါတယ်။ ဒီအိမ်ကြီးထဲမှာ ဘယ်လိုလျှို့ဝှက်ချက်တွေက "
            "သူ့ကို စောင့်ကြိုနေမလဲဆိုတာ ဘယ်သူမှ မသိနိုင်သေးပါဘူး။"
        ),
    },
    {
        "idx": 2,
        "kind": "plain_tts",
        "voice": "Kore",
        "tag": "digits_and_loanwords",
        "text": (
            "2026 ခုနှစ် နည်းပညာ ပြပွဲကြီးမှာ YouTube နဲ့ TikTok ဖန်တီးသူပေါင်း 15000 ကျော် တက်ရောက်ခဲ့ပြီး AI Studio "
            "စနစ်သစ်ကို လက်တွေ့ စမ်းသပ်ခဲ့ကြပါတယ်။ တစ်နေ့တာ အသုံးပြုသူ အရေအတွက်က 350000 အထိ စံချိန်တင် မြင့်တက်သွားသလို "
            "မြန်မာဘာသာနဲ့ အသံဖန်တီးမှု ကဏ္ဍမှာလည်း အရည်အသွေး သိသိသာသာ တိုးတက်လာတာ တွေ့မြင်ခဲ့ရပါတယ်။ ဒီပြောင်းလဲမှုဟာ "
            "လူငယ်ဖန်တီးသူတွေအတွက် အခွင့်အလမ်းသစ်တွေ ဖွင့်ပေးလိုက်တာပဲ ဖြစ်ပါတယ်။"
        ),
    },
    {
        "idx": 3,
        "kind": "plain_tts",
        "voice": "Charon",
        "tag": "quoted_dialogue",
        "text": (
            "အခန်းထဲကို ဝင်လိုက်တာနဲ့ ဦးမင်းသူက စားပွဲပေါ်က စာအိတ်ကို လက်ညှိုးထိုးပြပြီး \"မင်း ဒီအမှန်တရားကို တကယ်ပဲ သိချင်နေပြီလား\" "
            "လို့ လေးနက်တဲ့ အသံနဲ့ မေးလိုက်ပါတယ်။ ကျွန်တော်လည်း တစ်ချက်မှ နောက်မဆုတ်ဘဲ \"ဟုတ်ကဲ့၊ ဘာပဲဖြစ်နေပါစေ ကျွန်တော် အဆုံးထိ "
            "ရင်ဆိုင်ဖို့ ဆုံးဖြတ်ပြီးပါပြီ\" လို့ ပြတ်ပြတ်သားသား ပြန်ဖြေလိုက်တဲ့အခါ သူ့မျက်နှာပေါ်မှာ အံ့အားသင့်တဲ့ အရိပ်အယောင်တွေ "
            "ချက်ချင်း ပေါ်လာခဲ့ပါတော့တယ်။"
        ),
    },
    {
        "idx": 4,
        "kind": "plain_tts",
        "voice": "Kore",
        "tag": "sad_slow_passage",
        "text": (
            "ဘူတာရုံလေးရဲ့ စင်္ကြံပေါ်မှာ နောက်ဆုံးရထား ထွက်ခွာသွားတဲ့အထိ သူမ တစ်ယောက်တည်း ငြိမ်သက်စွာ ရပ်ကြည့်နေမိပါတယ်။ "
            "ပြန်မလာနိုင်တော့တဲ့ အတိတ်နေ့ရက်တွေကို သတိရတိုင်း ပါးပြင်ပေါ်ကို မျက်ရည်စတွေ တိတ်တဆိတ် စီးကျလာပေမယ့် နှုတ်ဆက်စကားတစ်ခွန်းတောင် "
            "ပြောခွင့်မရလိုက်တဲ့ နောင်တက ရင်ထဲမှာ ပိုပြီး နာကျင်စေခဲ့ပါတယ်။ ဆောင်းဦးလေအေးလေးကသာ သူမရဲ့ အထီးကျန်မှုကို တိုးတိုးလေး "
            "နှစ်သိမ့်ပေးနေသလို ရှိနေပါတော့တယ်။"
        ),
    },
    {
        "idx": 5,
        "kind": "plain_tts",
        "voice": "Charon",
        "tag": "action_suspense",
        "text": (
            "ရုတ်တရက် အချက်ပေးခေါင်းလောင်းသံကြီး မြည်ဟိန်းသွားပြီး လုံခြုံရေးတံခါးတွေ တစ်ချပ်ပြီးတစ်ချပ် အလိုအလျောက် ပိတ်ကျလာခဲ့ပါတယ်။ "
            "အချိန်က စက္ကန့် 30 ပဲ ကျန်တော့တာမို့ အဖွဲ့သားအားလုံး အနီးဆုံး ထွက်ပေါက်ဆီကို အသက်လုပြီး အမြန် ပြေးခဲ့ကြရပါတယ်။ နောက်ဆုံးအကွေ့မှာ "
            "မီးခိုးလုံးကြီးတွေ အပြည့် ဖုံးလွှမ်းလာပေမယ့် ခေါင်းဆောင်ရဲ့ တည်ငြိမ်တဲ့ လမ်းညွှန်မှုကြောင့် အားလုံး အချိန်မီ လွတ်မြောက်သွားခဲ့ပါတယ်။"
        ),
    },
    {
        "idx": 6,
        "kind": "recap_short",
        "voice": "Kore",
        "tag": "recap_hook",
        "text": "ဒီဇာတ်ကားရဲ့ အစမှာတော့ သာမန်ကျောင်းသားလေးတစ်ယောက်က မထင်မှတ်ဘဲ ထူးဆန်းတဲ့ နာရီတစ်လုံးကို ကောက်ရသွားခဲ့ပါတယ်။",
    },
    {
        "idx": 7,
        "kind": "recap_short",
        "voice": "Charon",
        "tag": "recap_digits_loanword",
        "text": "Facebook ပေါ်မှာ 24 နာရီအတွင်း ကြည့်ရှုသူ 500000 ကျော်သွားတဲ့ ဒီဗီဒီယိုရဲ့ နောက်ကွယ်က အဖြစ်မှန်က ဘာလဲ။",
    },
    {
        "idx": 8,
        "kind": "recap_short",
        "voice": "Kore",
        "tag": "recap_dialogue",
        "text": "သူမက တံခါးကို ဖွင့်ကြည့်လိုက်ရင်း \"နင် ဒီနေရာကို ဘယ်လိုလုပ် ရောက်လာတာလဲ\" လို့ တုန်ရီစွာ မေးလိုက်ပါတယ်။",
    },
    {
        "idx": 9,
        "kind": "recap_short",
        "voice": "Charon",
        "tag": "recap_sad_beat",
        "text": "အားလုံး ပြီးဆုံးသွားချိန်မှာတော့ သူ တန်ဖိုးအထားရဆုံး သူငယ်ချင်းဟာ သူ့အနားမှာ မရှိနိုင်တော့ပါဘူး။",
    },
    {
        "idx": 10,
        "kind": "recap_short",
        "voice": "Kore",
        "tag": "recap_cliffhanger",
        "text": "ဒါပေမဲ့ မြေအောက်ခန်းထဲက သေတ္တာကို ဖွင့်လိုက်တဲ့အခါ သူတို့ ထင်ထားသလို ရတနာတွေ လုံးဝ မဟုတ်နေခဲ့ပါဘူး။",
    },
)


def build_arm_request(arm: str, text: str, voice: str) -> tuple[str | None, str]:
    """Return ``(system_instruction, contents)`` for the given arm."""
    arm_u = arm.strip().upper()
    cleaned = gte._clean_tts_text(text)
    short_full = gte.narrator_prompt_short(text, voice)
    line1 = short_full.split("\n", 1)[0].strip()

    if arm_u == "A":
        return None, short_full
    if arm_u == "B":
        return line1, f"[expressive storytelling] {cleaned}"
    if arm_u == "C1":
        return None, f"[warm, lively storytelling] {cleaned}"
    if arm_u == "C2":
        return None, cleaned
    raise ValueError(f"Unknown arm '{arm}'. Valid arms: {', '.join(DEFAULT_ARMS)}")


def _write_wav(path: Path, pcm: bytes, channels: int, sampwidth: int, framerate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(pcm)


def _print_usage_footer() -> None:
    print("\nUsage: python scripts/probe_tts_prompt_arms.py [--arms A,B] [--models ...]")


def _list_configuration(arms: list[str], models: list[str]) -> int:
    print("=== Gemini TTS Phase 0 Probe — Dry-Run Inventory ===")
    print(f"Configured arms   ({len(arms)}): {', '.join(arms)}")
    for a in arms:
        print(f"  - Arm {a}: {ARM_DESCRIPTIONS[a]}")
    print(f"Configured models ({len(models)}): {', '.join(models)}")
    print(f"Embedded chunks   ({len(PROBE_CHUNKS)}):")
    for item in PROBE_CHUNKS:
        cleaned = gte._clean_tts_text(item["text"])
        print(
            f"  - #{item['idx']:02d} [{item['kind']:11s}] voice={item['voice']:6s} "
            f"chars={len(cleaned):3d} tag={item['tag']}"
        )
    planned = min(HARD_MAX_API_CALLS, len(arms) * len(models) * len(PROBE_CHUNKS))
    print(f"Planned API calls : {planned} (hard cap: {HARD_MAX_API_CALLS})")
    _print_usage_footer()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 0 standalone A/B probe for Gemini TTS prompt arms (hard cap: 80 calls)."
    )
    parser.add_argument(
        "--arms",
        default=",".join(DEFAULT_ARMS),
        help="Comma-separated prompt arms to test (default: A,B,C1,C2)",
    )
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated Gemini TTS models to test",
    )
    parser.add_argument(
        "--out-dir",
        default="probe_out",
        help="Directory for WAV artifacts and results.json (default: probe_out)",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=HARD_MAX_API_CALLS,
        help=f"Maximum API calls (clamped to <= {HARD_MAX_API_CALLS})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Validate imports and list arms/models/chunks without making network calls",
    )
    args = parser.parse_args(argv)

    arms = [a.strip().upper() for a in str(args.arms).split(",") if a.strip()]
    for a in arms:
        if a not in ARM_DESCRIPTIONS:
            print(f"ERROR: unknown arm '{a}'. Expected subset of {DEFAULT_ARMS}", file=sys.stderr)
            return 2

    models = [m.strip() for m in str(args.models).split(",") if m.strip()]
    if not models:
        print("ERROR: at least one model must be specified.", file=sys.stderr)
        return 2

    if args.list:
        return _list_configuration(arms, models)

    pool_keys = gte.collect_gemini_keys()
    if not pool_keys:
        print("ERROR: collect_gemini_keys() found 0 configured Gemini keys in environment.", file=sys.stderr)
        _print_usage_footer()
        return 1

    from google.genai import types

    max_calls = max(1, min(HARD_MAX_API_CALLS, int(args.max_calls)))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tts_health = kr.health_for("gemini_tts")

    print(
        f"Loaded {len(pool_keys)} pool key(s) "
        f"(digests: {', '.join(kr.key_id(k) for k in pool_keys[:5])}{'...' if len(pool_keys) > 5 else ''})"
    )

    records: list[dict[str, Any]] = []
    call_count = 0
    key_cursor = 0

    budget_reached = False
    for arm in arms:
        if budget_reached:
            break
        for model in models:
            if budget_reached:
                break
            ordered_keys = tts_health.order_for_model(list(pool_keys), model) or list(pool_keys)
            for item in PROBE_CHUNKS:
                if call_count >= max_calls:
                    print(f"Hard call budget ({max_calls}) reached; stopping cleanly.")
                    budget_reached = True
                    break

                call_count += 1
                idx = int(item["idx"])
                voice = str(item["voice"])
                raw_text = str(item["text"])
                cleaned_text = gte._clean_tts_text(raw_text)
                cleaned_chars = len(cleaned_text)
                sys_inst, contents = build_arm_request(arm, raw_text, voice)

                chosen_key = ordered_keys[key_cursor % len(ordered_keys)]
                key_cursor += 1
                key_digest = kr.key_id(chosen_key)

                cfg_kwargs: dict[str, Any] = {
                    "response_modalities": ["AUDIO"],
                    "speech_config": types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                        )
                    ),
                }
                if sys_inst is not None:
                    cfg_kwargs["system_instruction"] = sys_inst
                config = types.GenerateContentConfig(**cfg_kwargs)

                t0 = time.monotonic()
                rec: dict[str, Any] = {
                    "call_no": call_count,
                    "arm": arm,
                    "model": model,
                    "voice": voice,
                    "chunk_idx": idx,
                    "chunk_kind": item["kind"],
                    "chunk_tag": item["tag"],
                    "cleaned_chars": cleaned_chars,
                    "key_digest": key_digest,
                    "http_status": 200,
                    "error_class": None,
                    "error_detail": None,
                    "pcm_seconds": None,
                    "chars_per_sec": None,
                    "duration_guard_bound": round(gte._max_plausible_seconds(cleaned_chars), 3),
                    "duration_guard_verdict": None,
                    "asr_verdict": None,
                    "asr_reason": None,
                    "asr_head_transcript": None,
                    "leaked": False,
                    "wav_file": None,
                    "elapsed_sec": None,
                }

                try:
                    client = gte._client(chosen_key)
                    resp = client.models.generate_content(model=model, contents=contents, config=config)
                    audio_bytes = None
                    for part in resp.candidates[0].content.parts:
                        data = getattr(getattr(part, "inline_data", None), "data", None)
                        if data:
                            audio_bytes = data
                            break
                    if not audio_bytes:
                        rec["http_status"] = 200
                        rec["error_class"] = "EmptyAudio"
                        rec["error_detail"] = "No inline audio part returned"
                    else:
                        pcm, ch, sw, fr = gte._unwrap_tts_audio(audio_bytes)
                        secs = gte._pcm_seconds(pcm, channels=ch, sampwidth=sw, framerate=fr)
                        cps = (cleaned_chars / secs) if secs > 0 else 0.0
                        dq_leak = secs > gte._max_plausible_seconds(cleaned_chars)
                        asr_res = leak_gate.verify_tts_pcm_head(
                            pcm,
                            cleaned_text=cleaned_text,
                            channels=ch,
                            sampwidth=sw,
                            framerate=fr,
                        )
                        safe_model = model.replace("/", "_")
                        wav_name = f"{arm}_{safe_model}_{voice}_{idx:02d}.wav"
                        wav_path = out_dir / wav_name
                        _write_wav(wav_path, pcm, ch, sw, fr)

                        rec["pcm_seconds"] = round(secs, 3)
                        rec["chars_per_sec"] = round(cps, 3)
                        rec["duration_guard_verdict"] = "dq_reject" if dq_leak else "pass"
                        rec["asr_verdict"] = asr_res.verdict
                        rec["asr_reason"] = asr_res.reason
                        rec["asr_head_transcript"] = asr_res.head_transcript[:200]
                        rec["leaked"] = bool(dq_leak or asr_res.verdict == "leak")
                        rec["wav_file"] = wav_name

                        try:
                            tts_health.record_usage(chosen_key, model)
                        except Exception:
                            pass
                except Exception as exc:
                    code = gte._code(exc)
                    rec["http_status"] = code or 0
                    rec["error_class"] = type(exc).__name__
                    rec["error_detail"] = str(exc)[:240]

                rec["elapsed_sec"] = round(time.monotonic() - t0, 3)
                records.append(rec)
                status_str = (
                    f"ERR({rec['http_status']}:{rec['error_class']})"
                    if rec["error_class"]
                    else f"secs={rec['pcm_seconds']:.2f}s cps={rec['chars_per_sec']:.1f} leaked={rec['leaked']} ({rec['asr_reason']})"
                )
                print(
                    f"[{call_count:02d}/{max_calls}] arm={arm:2s} model={model} voice={voice:6s} "
                    f"chunk={idx:02d} key=#{key_digest[:8]} -> {status_str}",
                    flush=True,
                )

    summary_by_arm: dict[str, Any] = {}
    for arm in arms:
        arm_rows = [r for r in records if r["arm"] == arm]
        ok_rows = [r for r in arm_rows if r["error_class"] is None]
        err_rows = [r for r in arm_rows if r["error_class"] is not None]
        leak_rows = [r for r in ok_rows if r["leaked"]]
        cps_vals = [float(r["chars_per_sec"]) for r in ok_rows if r["chars_per_sec"] is not None]
        summary_by_arm[arm] = {
            "total_calls": len(arm_rows),
            "ok_calls": len(ok_rows),
            "error_calls": len(err_rows),
            "error_rate": round(len(err_rows) / len(arm_rows), 4) if arm_rows else 0.0,
            "leak_calls": len(leak_rows),
            "leak_rate": round(len(leak_rows) / len(ok_rows), 4) if ok_rows else None,
            "median_chars_per_sec": round(statistics.median(cps_vals), 3) if cps_vals else None,
        }

    summary_by_arm_model: dict[str, Any] = {}
    for arm in arms:
        for model in models:
            sub = [r for r in records if r["arm"] == arm and r["model"] == model]
            if not sub:
                continue
            ok_sub = [r for r in sub if r["error_class"] is None]
            err_sub = [r for r in sub if r["error_class"] is not None]
            leak_sub = [r for r in ok_sub if r["leaked"]]
            cps_sub = [float(r["chars_per_sec"]) for r in ok_sub if r["chars_per_sec"] is not None]
            summary_by_arm_model[f"{arm}:{model}"] = {
                "total_calls": len(sub),
                "ok_calls": len(ok_sub),
                "error_rate": round(len(err_sub) / len(sub), 4),
                "leak_rate": round(len(leak_sub) / len(ok_sub), 4) if ok_sub else None,
                "median_chars_per_sec": round(statistics.median(cps_sub), 3) if cps_sub else None,
            }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_calls": call_count,
        "arms": arms,
        "models": models,
        "summary_by_arm": summary_by_arm,
        "summary_by_arm_model": summary_by_arm_model,
        "records": records,
    }
    results_path = out_dir / "results.json"
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved summary + {len(records)} call record(s) to {results_path}")
    _print_usage_footer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
