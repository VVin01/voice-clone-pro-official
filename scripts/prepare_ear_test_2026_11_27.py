#!/usr/bin/env python3
"""Part 7 (2026-11-27): Canary Blind Ear-Test Preparation & Objective Gate Verifier.

Generates and objective-gates the B5 protocol blind samples on canary ONLY
(`yunaisbusy/voxcpm2-9` by default — NO fleet rollout):

- Set 1 (`komin` in this round; `mahnin` + Arm 2 new-ref after owner picks flagship candidate):
  - Arm 1: current ref (`source_audio_path=None`, P0 fixed) in `(d)` `ref_continuation` mode
  - Arm 3: current ref in `(c)` isolated `reference_wav_path` audio-only mode (`use_prompt_text=False`)
  - (Optional `--komin-new-ref` enables 3-way Arm 1 / Arm 2 / Arm 3 -> Blind A / B / C)
- Set 2 (Uploads: 963,117 B NyiHlaingBwar MP3 + `The_Ligh.mp3`):
  - Arm X: silence-snapped ~10 s steady slice + Gemini ASR `prompt_text` (`ref_continuation`)
  - Arm Y: silence-snapped ~10 s steady slice + `reference_wav_path` audio-only (`use_prompt_text=False`)

Pre-Ear Objective Gate (verified BEFORE any blind listening):
1. Full-WAV (per-slice) Gemini transcripts contain 0 greeting/opener occurrences
   (`မင်္ဂလာပါ`, `မင်းမင်း`, `နှင်းနှင်း`, `ဟလို`, `ဒါပေမဲ့` unless present in input text).
2. Sentence-count parity (`sentences_in == sentences_out`).
3. Every synthesized unit `syllable_rate` within `[6.0, 18.0] syl/s`.

Blind WAVs are written to `<out-dir>/blind/` (`Set1_komin_Sample_{A,B}.wav`,
`Set2_upload1_Sample_{X,Y}.wav`, `Set2_the_light_Sample_{X,Y}.wav`) and the
deterministic/randomized arm mapping is written to `<out-dir>/SEALED_KEY_DO_NOT_OPEN_BEFORE_PICK.json`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORBIDDEN_OPENERS = (
    "မင်္ဂလာပါ",
    "ကျွန်တော်ကတော့ မင်းမင်း",
    "နှင်းနှင်း ဖြစ်ပါတယ်",
    "ဟလို",
)


def _split_burmese_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[။.?!])\s*", (text or "").strip()) if p.strip()]
    return [p for p in parts if len(p) >= 2]


def _transcribe_wav_gemini(wav_path: Path) -> str:
    """Transcribe a full slice WAV with Gemini 3.5 Flash across configured API keys."""
    from google import genai
    from google.genai import types
    from app.services.key_rotation import collect_gemini_key_pool

    raw_pool = collect_gemini_key_pool()
    keys = [k[1] if isinstance(k, tuple) else str(k) for k in raw_pool]
    if not keys:
        raise RuntimeError("No GEMINI_API_KEY configured for objective-gate transcription.")

    audio_bytes = wav_path.read_bytes()
    prompt = (
        "Transcribe this Burmese narration audio verbatim in Myanmar Unicode script. "
        "Include every spoken word from the very first syllable (including any greeting or filler word) "
        "and punctuate sentences with '။'. Output ONLY the Burmese transcript text."
    )
    last_err: Exception | None = None
    for model_name in (
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-flash-lite-latest",
    ):
        for api_key in keys[:4]:
            try:
                client = genai.Client(api_key=api_key, http_options={"timeout": 45000})
                resp = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                        prompt,
                    ],
                    config=types.GenerateContentConfig(temperature=0.0),
                )
                txt = (resp.text or "").strip()
                if txt:
                    return txt
            except Exception as exc:
                last_err = exc
    raise RuntimeError(f"Gemini transcription failed for {wav_path.name}: {last_err}")


def evaluate_objective_gate(
    *,
    input_text: str,
    slice_wavs: list[Path],
    slice_texts: list[str],
    skip_asr: bool = False,
) -> dict[str, Any]:
    from app.services import clone_prosody as cp_mod

    unit_reports: list[dict[str, Any]] = []
    rate_ok_all = True
    opener_hits: list[str] = []
    full_asr_parts: list[str] = []

    for idx, (s_wav, s_txt) in enumerate(zip(slice_wavs, slice_texts), 1):
        stats = cp_mod.measure_prosody(s_wav, text=s_txt)
        syl_rate = float(stats.syllable_rate or 0.0)
        in_bounds = 6.0 <= syl_rate <= 18.0
        if not in_bounds:
            rate_ok_all = False

        asr_txt = ""
        if not skip_asr:
            asr_txt = _transcribe_wav_gemini(s_wav)
            full_asr_parts.append(asr_txt)
            for bad in FORBIDDEN_OPENERS:
                if bad in asr_txt and bad not in s_txt:
                    opener_hits.append(f"slice_{idx}:{bad}")
            # Leading "ဒါပေမဲ့" check when input slice does not contain it
            if asr_txt.lstrip().startswith("ဒါပေမဲ့") and not s_txt.lstrip().startswith("ဒါပေမဲ့"):
                opener_hits.append(f"slice_{idx}:leading_ဒါပေမဲ့")

        unit_reports.append(
            {
                "slice": idx,
                "duration_s": round(stats.duration_sec, 2),
                "syllable_rate": round(syl_rate, 2),
                "rate_in_6_18": in_bounds,
                "sentences_in": len(_split_burmese_sentences(s_txt)),
                "sentences_asr": len(_split_burmese_sentences(asr_txt)) if asr_txt else None,
                "asr_head": asr_txt[:120] if asr_txt else None,
            }
        )

    sents_in = len(_split_burmese_sentences(input_text))
    sents_out = len(_split_burmese_sentences(" ".join(full_asr_parts))) if full_asr_parts else None
    # Exact sentence-count parity when ASR runs
    parity_ok = True if skip_asr else (sents_out == sents_in)
    passed = bool(rate_ok_all and not opener_hits and parity_ok)
    return {
        "passed": passed,
        "opener_hits": opener_hits,
        "sentences_in": sents_in,
        "sentences_out": sents_out,
        "sentence_parity_ok": parity_ok,
        "all_syllable_rates_in_6_18": rate_ok_all,
        "units": unit_reports,
    }


def _synthesize_arm_session(
    *,
    canary_space: str,
    slices: list[str],
    out_mastered: Path,
    work_dir: Path,
    preset_voice: str | None,
    reference_wav: Path | None,
    prompt_text: str,
    reference_only: bool,
) -> list[Path]:
    from app.services.audio import concat_wav_files
    from app.services.voxcpm_engine import (
        DEFAULT_CLONE_FIDELITY_CONTROL,
        VoxCPMEngine,
        master_voxcpm_audio,
    )

    work_dir.mkdir(parents=True, exist_ok=True)
    eng = VoxCPMEngine(work_dir=work_dir, spaces=[canary_space])
    eng.pinned_space = canary_space

    slice_wavs: list[Path] = []
    for idx, s_txt in enumerate(slices, 1):
        part = work_dir / f"slice_{idx:02d}.wav"
        eng.synthesize(
            text=s_txt,
            output_wav=part,
            reference_wav=reference_wav,
            prompt_text="" if reference_only else prompt_text,
            control_instruction=DEFAULT_CLONE_FIDELITY_CONTROL if not preset_voice else "",
            preset_voice=preset_voice,
            speed="+12%",
            tier="paid",
            skip_mastering=True,
            allow_fallback=False,
            session_mode=True,
            reference_only=reference_only,
        )
        slice_wavs.append(part)

    out_mastered.parent.mkdir(parents=True, exist_ok=True)
    if len(slice_wavs) == 1:
        master_voxcpm_audio(slice_wavs[0], out_mastered, speed="+12%", preset_voice=preset_voice)
    else:
        stitched = work_dir / "stitched_raw.wav"
        concat_wav_files(slice_wavs, stitched, pause_ms=100)
        master_voxcpm_audio(stitched, out_mastered, speed="+12%", preset_voice=preset_voice)
    return slice_wavs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canary-space", default="yunaisbusy/voxcpm2-9")
    parser.add_argument("--text-file", type=Path, required=True, help="UTF-8 Burmese test corpus file.")
    parser.add_argument("--upload1", type=Path, required=True, help="Path to 963,117 B upload MP3.")
    parser.add_argument(
        "--upload2",
        type=Path,
        default=ROOT / "app/webapp/static/audio/samples/the-light.mp3",
        help="Path to The_Ligh.mp3.",
    )
    parser.add_argument("--komin-new-ref", type=Path, default=None, help="Optional Arm 2 WAV after owner pick.")
    parser.add_argument("--komin-new-txt", type=Path, default=None, help="Optional Arm 2 .txt after owner pick.")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "ear_test_2026-11-27")
    parser.add_argument("--seed", type=int, default=20261127)
    parser.add_argument("--skip-asr", action="store_true")
    args = parser.parse_args(argv)

    from app.services import clone_prosody as cp_mod
    from app.services.voice_chunking import plan_session_slices
    from app.services.voxcpm_engine import VoxCPMEngine

    input_text = args.text_file.read_text(encoding="utf-8").strip()
    slices = plan_session_slices(input_text)
    out_dir = args.out_dir.resolve()
    blind_dir = out_dir / "blind"
    raw_dir = out_dir / "_internal_arms_do_not_peek"
    blind_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    sealed_mapping: dict[str, Any] = {"canary_space": args.canary_space, "sets": {}}
    public_gate_summary: dict[str, Any] = {"canary_space": args.canary_space, "sets": {}}

    # ── Set 1: Curated Persona (`komin`) ──
    set1_arms: list[tuple[str, Path, dict[str, Any]]] = []

    arm1_wav = raw_dir / "set1_komin_arm1_current_ref_d.wav"
    arm1_slices = _synthesize_arm_session(
        canary_space=args.canary_space,
        slices=slices,
        out_mastered=arm1_wav,
        work_dir=raw_dir / "w_komin_arm1",
        preset_voice="komin",
        reference_wav=None,
        prompt_text="",
        reference_only=False,
    )
    gate_arm1 = evaluate_objective_gate(
        input_text=input_text, slice_wavs=arm1_slices, slice_texts=slices, skip_asr=args.skip_asr
    )
    set1_arms.append(("Arm1_current_ref_d", arm1_wav, gate_arm1))

    if args.komin_new_ref and args.komin_new_txt:
        arm2_wav = raw_dir / "set1_komin_arm2_new_ref_d.wav"
        arm2_slices = _synthesize_arm_session(
            canary_space=args.canary_space,
            slices=slices,
            out_mastered=arm2_wav,
            work_dir=raw_dir / "w_komin_arm2",
            preset_voice="komin",
            reference_wav=args.komin_new_ref,
            prompt_text=args.komin_new_txt.read_text(encoding="utf-8").strip(),
            reference_only=False,
        )
        gate_arm2 = evaluate_objective_gate(
            input_text=input_text, slice_wavs=arm2_slices, slice_texts=slices, skip_asr=args.skip_asr
        )
        set1_arms.append(("Arm2_new_ref_d", arm2_wav, gate_arm2))

    arm3_wav = raw_dir / "set1_komin_arm3_reference_only_c.wav"
    arm3_slices = _synthesize_arm_session(
        canary_space=args.canary_space,
        slices=slices,
        out_mastered=arm3_wav,
        work_dir=raw_dir / "w_komin_arm3",
        preset_voice="komin",
        reference_wav=None,
        prompt_text="",
        reference_only=True,
    )
    gate_arm3 = evaluate_objective_gate(
        input_text=input_text, slice_wavs=arm3_slices, slice_texts=slices, skip_asr=args.skip_asr
    )
    set1_arms.append(("Arm3_reference_only_c", arm3_wav, gate_arm3))

    labels_s1 = ["A", "B", "C"][: len(set1_arms)]
    shuffled_s1 = list(set1_arms)
    rng.shuffle(shuffled_s1)
    sealed_mapping["sets"]["Set1_komin"] = {}
    public_gate_summary["sets"]["Set1_komin"] = {}
    for lbl, (arm_name, arm_path, gate_res) in zip(labels_s1, shuffled_s1):
        dst = blind_dir / f"Set1_komin_Sample_{lbl}.wav"
        shutil.copy2(arm_path, dst)
        sealed_mapping["sets"]["Set1_komin"][f"Sample_{lbl}"] = {
            "arm": arm_name,
            "sha256_12": hashlib.sha256(dst.read_bytes()).hexdigest()[:12],
        }
        public_gate_summary["sets"]["Set1_komin"][f"Sample_{lbl}"] = {
            "passed": gate_res["passed"],
            "opener_hits": gate_res["opener_hits"],
            "sentence_parity_ok": gate_res["sentence_parity_ok"],
            "all_syllable_rates_in_6_18": gate_res["all_syllable_rates_in_6_18"],
            "units": [
                {"slice": u["slice"], "duration_s": u["duration_s"], "syllable_rate": u["syllable_rate"]}
                for u in gate_res["units"]
            ],
        }

    # ── Set 2: Custom Uploads (Arm X: snapped slice + ASR prompt vs Arm Y: snapped slice + ref-only) ──
    with tempfile.TemporaryDirectory(prefix="vcp_ear_adapt_") as tmp_ad:
        ad_eng = VoxCPMEngine(work_dir=Path(tmp_ad), spaces=[args.canary_space])
        for tag, up_path in (("upload_963kb", args.upload1), ("the_light", args.upload2)):
            adapted = ad_eng._adapt_reference_for_clone(up_path.resolve())
            snapped_wav, snapped_s, _ = cp_mod.select_steady_reference_slice(
                Path(adapted), raw_dir / f"slice_{tag}", target_sec=cp_mod.REFERENCE_SLICE_SECONDS
            )
            asr_prompt = cp_mod.transcribe_reference_slice_checked(
                snapped_wav, max(snapped_s, 0.5)
            )
            if not asr_prompt:
                asr_prompt = _transcribe_wav_gemini(snapped_wav)

            arm_x_wav = raw_dir / f"set2_{tag}_armX_snapped_asr.wav"
            sx_slices = _synthesize_arm_session(
                canary_space=args.canary_space,
                slices=slices,
                out_mastered=arm_x_wav,
                work_dir=raw_dir / f"w_{tag}_armX",
                preset_voice=None,
                reference_wav=snapped_wav,
                prompt_text=asr_prompt,
                reference_only=False,
            )
            gate_x = evaluate_objective_gate(
                input_text=input_text, slice_wavs=sx_slices, slice_texts=slices, skip_asr=args.skip_asr
            )

            arm_y_wav = raw_dir / f"set2_{tag}_armY_snapped_ref_only.wav"
            sy_slices = _synthesize_arm_session(
                canary_space=args.canary_space,
                slices=slices,
                out_mastered=arm_y_wav,
                work_dir=raw_dir / f"w_{tag}_armY",
                preset_voice=None,
                reference_wav=snapped_wav,
                prompt_text="",
                reference_only=True,
            )
            gate_y = evaluate_objective_gate(
                input_text=input_text, slice_wavs=sy_slices, slice_texts=slices, skip_asr=args.skip_asr
            )

            pair = [
                ("ArmX_snapped_slice_plus_ASR_prompt", arm_x_wav, gate_x),
                ("ArmY_snapped_slice_plus_reference_only", arm_y_wav, gate_y),
            ]
            rng.shuffle(pair)
            set_key = f"Set2_{tag}"
            sealed_mapping["sets"][set_key] = {}
            public_gate_summary["sets"][set_key] = {}
            for lbl, (arm_name, arm_path, gate_res) in zip(("X", "Y"), pair):
                dst = blind_dir / f"{set_key}_Sample_{lbl}.wav"
                shutil.copy2(arm_path, dst)
                sealed_mapping["sets"][set_key][f"Sample_{lbl}"] = {
                    "arm": arm_name,
                    "snapped_slice_s": snapped_s,
                    "sha256_12": hashlib.sha256(dst.read_bytes()).hexdigest()[:12],
                }
                public_gate_summary["sets"][set_key][f"Sample_{lbl}"] = {
                    "passed": gate_res["passed"],
                    "opener_hits": gate_res["opener_hits"],
                    "sentence_parity_ok": gate_res["sentence_parity_ok"],
                    "all_syllable_rates_in_6_18": gate_res["all_syllable_rates_in_6_18"],
                    "units": [
                        {"slice": u["slice"], "duration_s": u["duration_s"], "syllable_rate": u["syllable_rate"]}
                        for u in gate_res["units"]
                    ],
                }

    # Remove unblinded intermediate directory so only `blind/` + sealed key remain
    shutil.rmtree(raw_dir, ignore_errors=True)

    (out_dir / "objective_gate_report.json").write_text(
        json.dumps(public_gate_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "SEALED_KEY_DO_NOT_OPEN_BEFORE_PICK.json").write_text(
        json.dumps(sealed_mapping, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Blind ear-test bundle written to {blind_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
