"""Isolated Applio inference bridge.

Executed by the Applio virtualenv, not by the bot virtualenv.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import sys


def _force_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_WAV_HEADER_BYTES = 12


def _looks_like_wav(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(_WAV_HEADER_BYTES)
    except OSError:
        return False
    return (
        len(header) == _WAV_HEADER_BYTES
        and header[:4] in (b"RIFF", b"RF64")
        and header[8:12] == b"WAVE"
    )


def _validate_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, str]:
    model = Path(args.model)
    input_audio = Path(args.input)
    output_audio = Path(args.output)
    if not model.is_file():
        raise FileNotFoundError(f"APPLIO_MODEL_NOT_FOUND: {model}")
    if not input_audio.is_file():
        raise FileNotFoundError(f"APPLIO_INPUT_NOT_FOUND: {input_audio}")
    if not _looks_like_wav(input_audio):
        raise RuntimeError(f"APPLIO_INPUT_NOT_WAV: {input_audio}")

    index = args.index or ""
    if index and not Path(index).is_file():
        print(f"APPLIO_INDEX_MISSING_FALLBACK_NO_INDEX={index}")
        index = ""
    return model, input_audio, output_audio, index


def main() -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Voice Clone Pro Applio inference bridge")
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", default="")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--speaker-id", type=int, default=0)
    parser.add_argument("--pitch", type=int, default=0)
    parser.add_argument("--f0-method", default="rmvpe")
    parser.add_argument("--index-rate", type=float, default=0.75)
    parser.add_argument("--protect", type=float, default=0.5)
    parser.add_argument("--volume-envelope", type=float, default=1.0)
    args = parser.parse_args()

    model, input_audio, output_audio, index = _validate_paths(args)
    output_audio.parent.mkdir(parents=True, exist_ok=True)

    applio_root = Path(os.getcwd()).resolve()
    if str(applio_root) not in sys.path:
        sys.path.insert(0, str(applio_root))

    from rvc.infer.infer import VoiceConverter

    converter = VoiceConverter()
    converter.convert_audio(
        audio_input_path=str(input_audio),
        audio_output_path=str(output_audio),
        model_path=str(model),
        index_path=index or "",
        pitch=args.pitch,
        f0_method=args.f0_method,
        index_rate=args.index_rate,
        volume_envelope=args.volume_envelope,
        protect=args.protect,
        sid=args.speaker_id,
        export_format="WAV",
    )

    if not output_audio.is_file():
        raise RuntimeError(f"APPLIO_OUTPUT_NOT_FOUND: {output_audio}")
    print(f"APPLIO_OUTPUT_OK={output_audio}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
