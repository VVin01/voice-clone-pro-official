"""PNG-overlay subtitle renderer (Batch Q, 2026-11-04).

Replaces the libass subtitle burn (ASS + SRT force_style + engine
preview) with per-cue transparent PNGs drawn by Pillow through the
RAQM layout engine (HarfBuzz complex-text shaping) and composited
with ffmpeg ``overlay`` — the recapter construction (VVin01/recapter
renders its subtitles exactly this way).

Why: the diagnosis in ~/workspace/subtitle-compare/shaping proved
our subtitle TEXT was always codepoint-correct; the "looks
misspelled" impression was legibility — small Regular captions
rasterised softly by libass. A PNG is drawn once, in-process, with a
font loaded BY PATH from the bundled static builds, so the shaping
and rasterisation are identical on every host and visibly crisper
at caption sizes. The downloadable .srt side-file is produced
separately (EdgeSmartSync) and is untouched by this module.

Hard rule: RAQM or nothing. The basic Pillow layout engine does NOT
shape Myanmar clusters (medials/kinzi would render detached), so a
missing libraqm is a loud failure, never a silent fallback.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_RAQM_OK = False


def assert_raqm() -> None:
    """Fail loud unless Pillow can shape text with RAQM/HarfBuzz."""
    global _RAQM_OK
    if _RAQM_OK:
        return
    from PIL import features

    if not features.check("raqm"):
        raise RuntimeError(
            "Pillow RAQM layout engine unavailable (libraqm missing): "
            "refusing to render Burmese subtitle PNGs with the basic "
            "layout engine, which mis-shapes complex clusters."
        )
    _RAQM_OK = True


def ass_colour_to_rgba(colour: Any) -> tuple[int, int, int, int]:
    """ASS ``&HAABBGGRR`` → Pillow ``(R, G, B, A)``.

    ASS alpha is a TRANSPARENCY byte (00 = opaque, FF = invisible);
    Pillow alpha is opacity, hence the inversion.
    """
    a = str(colour or "").strip().replace("&H", "").replace("&h", "")
    a = (a or "00000000").rjust(8, "0")[-8:]
    try:
        aa, bb, gg, rr = (int(a[i:i + 2], 16) for i in (0, 2, 4, 6))
    except ValueError:
        return (255, 255, 255, 255)
    return (rr, gg, bb, 255 - aa)


def subtitle_outline_px(preset: dict[str, Any], canvas_h: int, font_px: int) -> float:
    """The outline width (px) libass would have burned for a preset.

    Mirrors write_synced_ass_file exactly: preset outline scaled by
    canvas height relative to 720p, floored at 1.5px, then the R40
    guard caps it at 8% of the rendered font size per side.
    """
    ch = max(240, int(canvas_h))
    outl = max(1.5, float(preset.get("outline", 1.3)) * (ch / 720.0))
    return min(outl, max(1.5, 0.08 * max(1, int(font_px))))


def preset_visuals(
    preset: dict[str, Any], canvas_h: int, font_px: int
) -> dict[str, Any]:
    """Map a SUBTITLE_STYLE_PRESETS entry to PNG drawing data.

    primary → fill; outline_col → stroke colour; outline → stroke
    width (subtitle_outline_px × 0.75: Pillow strokes straddle the
    glyph edge where libass outlines grow outward, so 0.75 keeps the
    perceived weight equal — the prototype's calibration);
    border_style 3 (band/boxed) → a filled rounded rectangle behind
    the text in back_col; shadow is dropped (the stroke does the
    contrast job, as in the prototype).
    """
    stroke = max(
        1, int(round(subtitle_outline_px(preset, canvas_h, font_px) * 0.75))
    )
    band = None
    if int(preset.get("border_style", 1)) == 3:
        band = ass_colour_to_rgba(preset.get("back_col"))
    return {
        "fill": ass_colour_to_rgba(preset.get("primary")),
        "stroke_colour": ass_colour_to_rgba(preset.get("outline_col")),
        "stroke_px": stroke,
        "band": band,
    }


def visuals_from_ass_style(
    primary: Any, outline_col: Any, back_col: Any,
    border_style: int, outline_px: float,
) -> dict[str, Any]:
    """Visuals for cues parsed back out of an .ass file: the file's
    outline is already the final scaled+capped px value, so only the
    ×0.75 Pillow straddle calibration is applied."""
    band = None
    if int(border_style) == 3:
        band = ass_colour_to_rgba(back_col)
    return {
        "fill": ass_colour_to_rgba(primary),
        "stroke_colour": ass_colour_to_rgba(outline_col),
        "stroke_px": max(1, int(round(float(outline_px) * 0.75))),
        "band": band,
    }


def normalize_cue_lines(text: Any, sub_lines: int) -> list[str]:
    """Display lines for a cue — the same text the ASS path burned.

    Callers pass cue text that already went through
    canonicalize_burmese_text (split_burmese_subtitle_cues output, or
    text read back from the synced .srt/.ass files, which were
    canonical at write time); this mirrors write_synced_ass_file's
    post-canonicalize handling only: 1-line mode collapses all
    whitespace to single spaces; 2-line mode keeps the line break
    chosen by _format_cue_lines.
    """
    t = str(text or "")
    if int(sub_lines or 1) == 1:
        line = re.sub(r"\s+", " ", t.replace("\r\n", " ").replace("\n", " ")).strip()
        return [line] if line else []
    lines = [ln.strip() for ln in t.replace("\r\n", "\n").split("\n")]
    return [ln for ln in lines if ln]


_SRT_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _srt_ts(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt_cues(path: Path) -> list[tuple[float, float, str]]:
    """Parse an .srt file back into (start, end, text) cues — used
    when a job's subtitles_file points at the .srt (the pre-Q
    force_style fallback case); the burn is now PNG either way."""
    cues: list[tuple[float, float, str]] = []
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except Exception:
        return cues
    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            m = _SRT_TIME_RE.search(ln)
            if m:
                start = _srt_ts(*m.groups()[:4])
                end = _srt_ts(*m.groups()[4:])
                text = "\n".join(lines[i + 1:]).strip()
                if text:
                    cues.append((start, end, text))
                break
    return cues


def _ass_ts(stamp: str) -> float:
    m = re.match(r"(\d+):(\d{2}):(\d{2})\.(\d{1,3})", stamp.strip())
    if not m:
        return 0.0
    h, mi, s, cs = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(cs.ljust(2, "0")[:2]) / 100.0


def parse_ass_source(path: Path) -> dict[str, Any] | None:
    """Parse a write_synced_ass_file .ass back into cues + style.

    Only used when a subtitles_file points at an .ass without the
    pipeline's subtitle_cues (defensive: the burn must never fall
    back to libass). Returns None when the file has no dialogues.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except Exception:
        return None
    font_px: int | None = None
    family: str | None = None
    visuals: dict[str, Any] | None = None
    cues: list[tuple[float, float, str]] = []
    for line in raw.splitlines():
        if line.startswith("Style:"):
            parts = [p.strip() for p in line[len("Style:"):].split(",")]
            # Format: Name, Fontname, Fontsize, PrimaryColour,
            # SecondaryColour, OutlineColour, BackColour, Bold, ...,
            # BorderStyle(15), Outline(16), Shadow, Alignment, ...
            if len(parts) >= 18:
                family = parts[1]
                try:
                    font_px = int(float(parts[2]))
                except ValueError:
                    font_px = None
                try:
                    border_style = int(float(parts[15]))
                    outline_px = float(parts[16])
                except ValueError:
                    border_style, outline_px = 1, 1.5
                visuals = visuals_from_ass_style(
                    parts[3], parts[5], parts[6], border_style, outline_px
                )
        elif line.startswith("Dialogue:"):
            fields = line[len("Dialogue:"):].split(",", 9)
            if len(fields) == 10:
                start, end = _ass_ts(fields[1]), _ass_ts(fields[2])
                text = fields[9].replace("\\N", "\n").replace("\\n", "\n").strip()
                if text and end > start:
                    cues.append((start, end, text))
    if not cues:
        return None
    return {
        "cues": cues,
        "font_px": font_px,
        "font_family": family,
        "visuals": visuals,
    }


class PngSubtitleRenderer:
    """Renders subtitle cues to transparent PNGs, hash-cached.

    One PNG per unique (lines, font size, font file, visuals) — the
    recapter SubtitlePNGCache idea: a repeated cue costs one dict
    lookup, and a 3-minute recap's ~40–80 cues render in well under
    a second total.
    """

    def __init__(self, cache_dir: Path):
        assert_raqm()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._fonts: dict[tuple[str, int], Any] = {}

    def _font(self, font_path: Path, font_px: int):
        from PIL import ImageFont

        key = (str(font_path), int(font_px))
        font = self._fonts.get(key)
        if font is None:
            font = ImageFont.truetype(
                str(font_path), int(font_px),
                layout_engine=ImageFont.Layout.RAQM,
            )
            self._fonts[key] = font
        return font

    def render_lines(
        self,
        lines: list[str],
        font_px: int,
        font_path: Path,
        visuals: dict[str, Any],
    ) -> tuple[Path, int, int]:
        """Render pre-split display lines → (png_path, width, height)."""
        from PIL import Image, ImageDraw

        lines = [ln for ln in lines if ln]
        if not lines:
            raise ValueError("empty subtitle cue")
        font_px = max(8, int(font_px))
        font_path = Path(font_path)
        stroke = int(visuals.get("stroke_px", 1))
        key_src = json.dumps(
            {
                "lines": lines,
                "px": font_px,
                "font": font_path.name,
                "fsize": font_path.stat().st_size if font_path.is_file() else 0,
                "fill": visuals.get("fill"),
                "sc": visuals.get("stroke_colour"),
                "sp": stroke,
                "band": visuals.get("band"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:24]
        out = self.cache_dir / f"sub_{digest}.png"
        if out.is_file() and out.stat().st_size > 0:
            with Image.open(out) as im:
                return out, im.width, im.height

        font = self._font(font_path, font_px)
        spacing = max(2, int(round(font_px * 0.15)))
        joined = "\n".join(lines)
        dummy = Image.new("RGBA", (1, 1))
        d = ImageDraw.Draw(dummy)
        bbox = d.multiline_textbbox(
            (0, 0), joined, font=font, stroke_width=stroke,
            spacing=spacing, align="center",
        )
        bbox = tuple(int(round(v)) for v in bbox)
        ink_w = max(1, bbox[2] - bbox[0])
        ink_h = max(1, bbox[3] - bbox[1])
        band = visuals.get("band")
        if band is not None:
            pad_x = max(stroke + 4, int(round(font_px * 0.35)))
            pad_y = max(stroke + 3, int(round(font_px * 0.16)))
        else:
            pad_x = pad_y = stroke + max(2, font_px // 24)
        w = ink_w + 2 * pad_x
        h = ink_h + 2 * pad_y
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        if band is not None:
            draw.rounded_rectangle(
                [0, 0, w - 1, h - 1],
                radius=max(6, int(round(font_px * 0.18))),
                fill=tuple(band),
            )
        draw.multiline_text(
            (pad_x - bbox[0], pad_y - bbox[1]),
            joined,
            font=font,
            fill=tuple(visuals["fill"]),
            stroke_width=stroke,
            stroke_fill=tuple(visuals["stroke_colour"]),
            spacing=spacing,
            align="center",
        )
        img.save(str(out), "PNG")
        return out, w, h

    def render_text(
        self,
        text: Any,
        sub_lines: int,
        font_px: int,
        font_path: Path,
        visuals: dict[str, Any],
    ) -> tuple[Path, int, int] | None:
        """Normalize a cue's text (ASS-identical line handling) and
        render it; None when the cue has no visible text."""
        lines = normalize_cue_lines(text, sub_lines)
        if not lines:
            return None
        return self.render_lines(lines, font_px, font_path, visuals)
