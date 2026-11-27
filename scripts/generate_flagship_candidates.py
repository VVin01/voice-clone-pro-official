#!/usr/bin/env python3
"""Part 4 (2026-11-27): Generate raw (`skip_mastering=True`) flagship reference candidates.

Synthesizes 3–4 raw takes each for `komin` and `mahnin` on a FIXED greeting-free
Burmese mid-narration passage (12–18 s; zero greetings, zero self-names, zero
audience particles) via `VoxCPMEngine.synthesize(..., skip_mastering=True)`.

Outputs are written ONLY under `persona_candidates_2026-11-27/` as 44.1 kHz 16-bit
mono PCM WAVs (sample-rate/PCM container normalized without any EQ/compression/
atempo/formant mastering) alongside exact `.txt` sidecars and a `manifest.json`.
Does NOT install references or touch `samples/{komin,mahnin}.mp3`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Fixed 3-sentence mid-narration passage (~14-16.5 s raw; zero greetings,
# zero self-names, zero audience particles like ရှင် / ခင်ဗျာ / နော်).
FLAGSHIP_MID_NARRATION_PASSAGE = (
    "ညနေခင်း လေပြည်အေးလေး တိုက်ခတ်လာချိန်မှာ တောင်တန်းတွေပေါ် မြူခိုးပါးပါးလေးတွေ ဖုံးလွှမ်းလာပါတယ်။ "
    "တိတ်ဆိတ်တဲ့ ပတ်ဝန်းကျင်ထဲမှာ သစ်ရွက်လှုပ်သံကို အတိုင်းသား ကြားနေရပါတယ်။ "
    "သဘာဝရဲ့ အလှအပဟာ စိတ်ကို အေးချမ်းစေပါတယ်။"
)

FORBIDDEN_SUBSTRINGS = ("မင်္ဂလာပါ", "ကိုမင်း", "မင်းမင်း", "နှင်းနှင်း", "မနှင်း", "ခင်ဗျာ", "ရှင့်", "နော်")


def _to_44k_pcm16_mono_unmastered(raw_wav: Path, dst_wav: Path) -> float:
    """Convert container/sample-rate ONLY (44.1 kHz 16-bit mono PCM) with ZERO audio filters."""
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(raw_wav),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(dst_wav),
        ],
        check=True,
    )
    with wave.open(str(dst_wav), "rb") as wf:
        return round(wf.getnframes() / float(wf.getframerate()), 3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "persona_candidates_2026-11-27",
        help="Output folder for raw candidate WAVs + .txt sidecars.",
    )
    parser.add_argument(
        "--takes",
        type=int,
        default=4,
        help="Number of raw takes per flagship persona (3-4 recommended).",
    )
    parser.add_argument(
        "--personas",
        default="komin,mahnin",
        help="Comma-separated flagship persona IDs (default: komin,mahnin).",
    )
    parser.add_argument(
        "--space",
        default="",
        help="Optional single Space slug override (e.g. yunaisbusy/voxcpm2-9).",
    )
    args = parser.parse_args(argv)

    for bad in FORBIDDEN_SUBSTRINGS:
        if bad in FLAGSHIP_MID_NARRATION_PASSAGE:
            raise SystemExit(f"Passage contains forbidden token: {bad}")

    from app.services.voxcpm_engine import VoxCPMEngine

    out_dir: Path = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    takes = max(3, int(args.takes))
    manifest_rows: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="vcp_flagship_cands_") as tmp_s:
        tmp_dir = Path(tmp_s)
        spaces_kw = [args.space.strip()] if args.space.strip() else None
        eng = VoxCPMEngine(work_dir=tmp_dir, spaces=spaces_kw)

        for pid in personas:
            collected = 0
            attempt = 0
            max_tries = takes + 4
            while collected < takes and attempt < max_tries:
                attempt += 1
                raw_out = tmp_dir / f"{pid}_raw_try{attempt:02d}.wav"
                eng.synthesize(
                    text=FLAGSHIP_MID_NARRATION_PASSAGE,
                    output_wav=raw_out,
                    preset_voice=pid,
                    speed="+0%",
                    tier="paid",
                    skip_mastering=True,
                    allow_fallback=False,
                    session_mode=False,
                    reference_only=False,
                )
                take_idx = collected + 1
                cand_stem = f"{pid}_candidate_{take_idx:02d}"
                cand_wav = out_dir / f"{cand_stem}.wav"
                cand_txt = out_dir / f"{cand_stem}.txt"
                dur_s = _to_44k_pcm16_mono_unmastered(raw_out, cand_wav)
                if not (12.0 <= dur_s <= 18.0):
                    print(
                        f"  [retry] {pid} try {attempt}: duration {dur_s:.2f}s outside [12.0, 18.0]s",
                        flush=True,
                    )
                    cand_wav.unlink(missing_ok=True)
                    continue
                cand_txt.write_text(FLAGSHIP_MID_NARRATION_PASSAGE + "\n", encoding="utf-8")
                collected += 1
                row = {
                    "persona": pid,
                    "candidate": cand_stem,
                    "wav": cand_wav.name,
                    "txt": cand_txt.name,
                    "duration_s": dur_s,
                    "chars": len(FLAGSHIP_MID_NARRATION_PASSAGE),
                    "chars_per_s": round(len(FLAGSHIP_MID_NARRATION_PASSAGE) / dur_s, 2),
                    "skip_mastering": True,
                }
                manifest_rows.append(row)
                print(
                    f"  [ok] {cand_wav.name} ({dur_s:.2f}s, {row['chars_per_s']:.2f} chars/s)",
                    flush=True,
                )

            if collected < 3:
                raise SystemExit(
                    f"Could only collect {collected} valid [12-18s] raw takes for {pid} after {attempt} tries."
                )

    (out_dir / "passage.txt").write_text(FLAGSHIP_MID_NARRATION_PASSAGE + "\n", encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "passage": FLAGSHIP_MID_NARRATION_PASSAGE,
                "skip_mastering": True,
                "candidates": manifest_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Saved {len(manifest_rows)} raw flagship candidates to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
