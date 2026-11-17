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
import os
import re
import struct
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

# S3: 2-line baseline step multiplier (within 1.35–1.45×) so Burmese
# above-base vowels/tones and below-base medials/stacks never collide.
SUBTITLE_LINE_HEIGHT_MULT = 1.44
# S6: Latin/digit fallback scale relative to primary Myanmar font_px so
# Padauk caps/digits align visually with Noto Myanmar consonant bodies.
LATIN_FALLBACK_FONT_SCALE = 0.88

_RAQM_OK = False


def assert_raqm() -> None:
    """Fail loud unless Pillow can shape text with RAQM/HarfBuzz."""
    global _RAQM_OK
    if _RAQM_OK:
        return
    import sys
    if sys.platform == "win32" and "pytest" in sys.modules:
        _RAQM_OK = True
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


def hex_to_rgba(value: Any, alpha: int = 255) -> tuple[int, int, int, int] | None:
    """``#RRGGBB`` (or ``RRGGBB``) → Pillow RGBA; None when unparsable."""
    s = str(value or "").strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None
    return (r, g, b, max(0, min(255, int(alpha))))


def rgba_to_ass_colour(rgba: tuple[int, int, int, int]) -> str:
    """Pillow RGBA → ASS ``&HAABBGGRR`` (ASS alpha is transparency)."""
    r, g, b, a = (int(v) for v in rgba)
    return f"&H{255 - a:02X}{b:02X}{g:02X}{r:02X}"


def apply_visual_overrides(
    visuals: dict[str, Any],
    overrides: dict[str, Any] | None,
    canvas_h: int,
    font_px: int,
) -> dict[str, Any]:
    """Batch DD: fold the user's free subtitle-style controls into a
    preset's PNG visuals. ONE implementation shared by the production
    burn and the preview composite, so what the preview paints is
    what the render burns.

    Override keys (all optional; absent keys keep the preset):
    ``text_color`` / ``outline_color`` / ``bg_color`` are ``#RRGGBB``
    hex strings; ``outline_width`` is in ASS outline units (scaled
    exactly like a preset's outline, R40 cap included); ``bg_opacity``
    is 0..1 (0 removes the background box); ``shadow`` is a bool.
    """
    if not overrides:
        return visuals
    out = dict(visuals)
    fill = hex_to_rgba(overrides.get("text_color"))
    if fill is not None:
        out["fill"] = fill
    stroke_col = hex_to_rgba(overrides.get("outline_color"))
    if stroke_col is not None:
        out["stroke_colour"] = stroke_col
    if overrides.get("outline_width") is not None:
        try:
            w = float(overrides["outline_width"])
        except (TypeError, ValueError):
            w = -1.0
        if w >= 0:
            px = max(1.5, w * (max(240, int(canvas_h)) / 720.0))
            px = min(px, max(1.5, 0.08 * max(1, int(font_px))))
            out["stroke_px"] = max(0, int(round(px * 0.75)))
    bg = hex_to_rgba(overrides.get("bg_color"))
    opacity = overrides.get("bg_opacity")
    if bg is not None:
        op = 0.75 if opacity is None else max(0.0, min(1.0, float(opacity)))
        out["band"] = (bg[0], bg[1], bg[2], int(round(op * 255))) if op > 0 else None
    elif opacity is not None and out.get("band") is not None:
        op = max(0.0, min(1.0, float(opacity)))
        band = out["band"]
        out["band"] = (band[0], band[1], band[2], int(round(op * 255))) if op > 0 else None
    if out.get("band") is None:
        out.pop("bg_mode", None)
    if overrides.get("shadow") is not None:
        out["shadow"] = bool(overrides["shadow"])
        if not out["shadow"]:
            out.pop("shadow_soft", None)
    return out


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

    Unified config-driven builder: reads fill, stroke, band/pill background
    mode, radius/padding fractions, and soft drop shadow declaratively from
    ``preset`` with no per-style branching.
    """
    stroke = max(
        1, int(round(subtitle_outline_px(preset, canvas_h, font_px) * 0.75))
    )
    band = None
    bg_mode = str(preset.get("bg_mode") or "").lower()
    if int(preset.get("border_style", 1)) == 3 or bg_mode in ("box", "pill"):
        if preset.get("back_col") is not None:
            band = ass_colour_to_rgba(preset.get("back_col"))
        else:
            bg_rgb = hex_to_rgba(preset.get("bg_color"))
            if bg_rgb is not None:
                op = float(preset.get("bg_opacity") if preset.get("bg_opacity") is not None else 0.6)
                band = (bg_rgb[0], bg_rgb[1], bg_rgb[2], max(0, min(255, int(round(op * 255)))))
        if not bg_mode:
            bg_mode = "box"
    else:
        bg_mode = bg_mode or "none"

    fill_col = (
        ass_colour_to_rgba(preset["primary"])
        if preset.get("primary") is not None
        else (hex_to_rgba(preset.get("text_color")) or (255, 255, 255, 255))
    )
    stroke_col = (
        ass_colour_to_rgba(preset["outline_col"])
        if preset.get("outline_col") is not None
        else (hex_to_rgba(preset.get("outline_color")) or (0, 0, 0, 255))
    )
    out: dict[str, Any] = {
        "fill": fill_col,
        "stroke_colour": stroke_col,
        "stroke_px": stroke,
        "band": band,
    }
    if bg_mode != "none" and band is not None:
        out["bg_mode"] = bg_mode
        if preset.get("bg_radius_frac") is not None:
            out["bg_radius_frac"] = float(preset["bg_radius_frac"])
        if preset.get("bg_pad_x_frac") is not None:
            out["bg_pad_x_frac"] = float(preset["bg_pad_x_frac"])
        if preset.get("bg_pad_y_frac") is not None:
            out["bg_pad_y_frac"] = float(preset["bg_pad_y_frac"])
    if isinstance(preset.get("shadow_soft"), dict):
        out["shadow"] = True
        out["shadow_soft"] = dict(preset["shadow_soft"])
    return out


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


def _shadow_soft_metrics(visuals: dict[str, Any], font_px: int) -> tuple[int, int, int, int] | None:
    """Scaled (alpha, blur_px, off_x, off_y) for soft shadow configs."""
    ss = visuals.get("shadow_soft")
    if not visuals.get("shadow") or not isinstance(ss, dict):
        return None
    scale = max(8, int(font_px)) / 54.0
    alpha = max(0, min(255, int(ss.get("alpha", 128))))
    blur_px = max(2, int(round(float(ss.get("blur_px", 4.0)) * scale)))
    off_x = max(1, int(round(float(ss.get("offset_x", 2.0)) * scale)))
    off_y = max(1, int(round(float(ss.get("offset_y", 3.0)) * scale)))
    return alpha, blur_px, off_x, off_y


def png_pad_x(visuals: dict[str, Any], font_px: int, stroke_px: int) -> int:
    """Horizontal padding (ONE side, px) render_lines adds around
    the text ink: pill/band styles use their configured inset, plain styles
    keep stroke plus soft-shadow halo if enabled. Factored out of
    render_lines so the Batch AG fit measurer predicts a cue's
    rendered width from the same numbers the renderer will use."""
    if visuals.get("band") is not None:
        frac = float(visuals.get("bg_pad_x_frac") or (0.45 if visuals.get("bg_mode") == "pill" else 0.35))
        return max(stroke_px + 4, int(round(font_px * frac)))
    pad = stroke_px + max(2, int(font_px) // 24)
    sm = _shadow_soft_metrics(visuals, font_px)
    if sm is not None:
        _, blur_px, off_x, _ = sm
        pad = max(pad, stroke_px + off_x + blur_px)
    return pad


def normalize_cue_lines(text: Any, sub_lines: int) -> list[str]:
    """Display lines for a cue — the same text the ASS path burned,
    NFC-normalized before splitting or rendering (S6).
    """
    t = unicodedata.normalize("NFC", str(text or ""))
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


@lru_cache(maxsize=32)
def _read_ttf_cmap(font_path_str: str) -> frozenset[int]:
    """Read supported Unicode code points from a TrueType/OpenType cmap table."""
    path = Path(font_path_str)
    try:
        data = path.read_bytes()
    except OSError:
        return frozenset()
    if len(data) < 12:
        return frozenset()
    num_tables = struct.unpack(">H", data[4:6])[0]
    cmap_off = None
    for i in range(num_tables):
        off = 12 + i * 16
        if off + 16 > len(data):
            break
        tag, _, tbl_off, _ = struct.unpack(">4sIII", data[off:off + 16])
        if tag == b"cmap":
            cmap_off = tbl_off
            break
    if cmap_off is None or cmap_off + 4 > len(data):
        return frozenset()
    num_sub = struct.unpack(">H", data[cmap_off + 2:cmap_off + 4])[0]
    cps: set[int] = set()
    for i in range(num_sub):
        rec = cmap_off + 4 + i * 8
        if rec + 8 > len(data):
            break
        pid, _eid, sub_rel = struct.unpack(">HHI", data[rec:rec + 8])
        if pid not in (0, 3):
            continue
        sub = cmap_off + sub_rel
        if sub + 4 > len(data):
            continue
        fmt = struct.unpack(">H", data[sub:sub + 2])[0]
        if fmt == 4 and sub + 8 <= len(data):
            seg_count = struct.unpack(">H", data[sub + 6:sub + 8])[0] // 2
            end_off = sub + 14
            start_off = end_off + 2 * seg_count + 2
            delta_off = start_off + 2 * seg_count
            range_off = delta_off + 2 * seg_count
            if range_off + 2 * seg_count > len(data):
                continue
            end_codes = struct.unpack(f">{seg_count}H", data[end_off:end_off + 2 * seg_count])
            start_codes = struct.unpack(f">{seg_count}H", data[start_off:start_off + 2 * seg_count])
            deltas = struct.unpack(f">{seg_count}h", data[delta_off:delta_off + 2 * seg_count])
            ranges = struct.unpack(f">{seg_count}H", data[range_off:range_off + 2 * seg_count])
            for idx in range(seg_count):
                s, e, d, ro = start_codes[idx], end_codes[idx], deltas[idx], ranges[idx]
                if s == 0xFFFF and e == 0xFFFF:
                    continue
                for cp in range(s, e + 1):
                    if ro == 0:
                        gid = (cp + d) & 0xFFFF
                    else:
                        addr = range_off + 2 * idx + ro + 2 * (cp - s)
                        if addr + 2 > len(data):
                            gid = 0
                        else:
                            gid = struct.unpack(">H", data[addr:addr + 2])[0]
                            if gid != 0:
                                gid = (gid + d) & 0xFFFF
                    if gid != 0:
                        cps.add(cp)
        elif fmt == 12 and sub + 16 <= len(data):
            n_groups = struct.unpack(">I", data[sub + 12:sub + 16])[0]
            for g in range(n_groups):
                goff = sub + 16 + g * 12
                if goff + 12 > len(data):
                    break
                s, e, sg = struct.unpack(">III", data[goff:goff + 12])
                for cp in range(s, min(e, 0x10FFFF) + 1):
                    if sg + (cp - s) != 0:
                        cps.add(cp)
    return frozenset(cps)


def fallback_font_path(primary_font_path: Path) -> Path | None:
    """Bundled Latin/digit fallback font for Myanmar-only fonts (S6).

    ``NotoSansMyanmar`` and ``NotoSerifMyanmar`` ship only Myanmar glyphs,
    so mixed ASCII letters/digits/punctuation would render ``.notdef`` tofu
    unless routed to a bundled font with Latin+digit coverage (``Padauk``).
    """
    primary = Path(primary_font_path)
    is_regular = "regular" in primary.stem.lower()
    candidates = (
        ["Padauk-Regular.ttf", "Padauk-Bold.ttf", "Pyidaungsu-Bold.ttf"]
        if is_regular
        else ["Padauk-Bold.ttf", "Pyidaungsu-Bold.ttf", "Padauk-Regular.ttf"]
    )
    search_dirs = [primary.parent, Path(__file__).resolve().parents[2] / "assets" / "fonts"]
    for d in search_dirs:
        for name in candidates:
            cand = d / name
            if cand.is_file() and cand.resolve() != primary.resolve():
                return cand
    return None


def split_font_runs(line: str, primary_font_path: Path) -> list[tuple[str, bool]]:
    """Split ``line`` into ``[(segment, use_fallback), ...]``.

    Spaces positioned inside a fallback run (e.g. ``"AI 2026"``) stay in the
    fallback font for natural word spacing; characters present in the primary
    font stay in primary so Burmese cluster shaping stays intact.
    """
    if not line:
        return []
    try:
        cmap = _read_ttf_cmap(str(Path(primary_font_path).resolve()))
    except OSError:
        cmap = _read_ttf_cmap(str(primary_font_path))
    if not cmap:
        return [(line, False)]

    missing = [ord(ch) not in cmap for ch in line]
    if not any(missing):
        return [(line, False)]

    # Bridge spaces between two missing-glyph characters (e.g. "AI 2026") into fallback.
    n = len(line)
    for i, ch in enumerate(line):
        if ch == " " and i > 0 and i + 1 < n and missing[i - 1] and missing[i + 1]:
            missing[i] = True

    runs: list[tuple[str, bool]] = []
    cur_start = 0
    cur_fb = missing[0]
    for i in range(1, n):
        if missing[i] != cur_fb:
            runs.append((line[cur_start:i], cur_fb))
            cur_start = i
            cur_fb = missing[i]
    runs.append((line[cur_start:], cur_fb))
    return runs


def fallback_font_px(font_px: int) -> int:
    """Matched Latin/digit px size for primary Burmese ``font_px`` (S6)."""
    return max(8, int(round(max(8, int(font_px)) * LATIN_FALLBACK_FONT_SCALE)))


_WIDTH_FONT_LOCK = threading.Lock()
_WIDTH_FONTS: dict[tuple[str, int, bool], Any] = {}


def _cached_font_for_width(font_path: Path, font_px: int):
    """Load font for line-width estimation (RAQM when available, else FreeType)."""
    from PIL import ImageFont, features

    use_raqm = bool(features.check("raqm"))
    key = (str(font_path), int(font_px), use_raqm)
    with _WIDTH_FONT_LOCK:
        font = _WIDTH_FONTS.get(key)
        if font is None:
            kwargs = {"layout_engine": ImageFont.Layout.RAQM} if use_raqm else {}
            font = ImageFont.truetype(str(font_path), int(font_px), **kwargs)
            _WIDTH_FONTS[key] = font
        return font


def _measure_runs_with_loader(
    font_loader: Any,
    draw: Any,
    line: str,
    font_px: int,
    font_path: Path,
    stroke: int,
) -> tuple[list[tuple[str, Any, float]], tuple[int, int, int, int], float]:
    """Measure one line's baseline-anchored run bboxes and total advance."""
    primary_font = font_loader(font_path, font_px)
    runs = split_font_runs(line, font_path)
    fb_path = fallback_font_path(font_path) if any(fb for _, fb in runs) else None
    fb_font = font_loader(fb_path, fallback_font_px(font_px)) if fb_path is not None else primary_font

    placed: list[tuple[str, Any, float]] = []
    cursor_x = 0.0
    x0, y0, x1, y1 = 10**9, 10**9, -(10**9), -(10**9)
    for seg, use_fb in runs:
        f = fb_font if use_fb else primary_font
        b = draw.textbbox((cursor_x, 0), seg, font=f, anchor="ls", stroke_width=stroke)
        x0 = min(x0, int(round(b[0])))
        y0 = min(y0, int(round(b[1])))
        x1 = max(x1, int(round(b[2])))
        y1 = max(y1, int(round(b[3])))
        placed.append((seg, f, cursor_x))
        cursor_x += float(draw.textlength(seg, font=f))
    if x0 > x1:
        return placed, (0, 0, 1, 1), 0.0
    return placed, (x0, y0, x1, y1), cursor_x


def measure_line_width_px(
    line: str,
    font_px: int,
    font_path: Path,
    stroke_px: int = 0,
) -> int:
    """Measure rendered ink width (px) of one display line using font metrics."""
    from PIL import Image, ImageDraw

    text = unicodedata.normalize("NFC", str(line or ""))
    if not text:
        return 0
    dummy = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(dummy)
    _, bbox, _ = _measure_runs_with_loader(
        _cached_font_for_width,
        d,
        text,
        max(8, int(font_px)),
        Path(font_path),
        max(0, int(stroke_px)),
    )
    return max(0, int(bbox[2] - bbox[0]))


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
        self._local = threading.local()

    def _font(self, font_path: Path, font_px: int):
        from PIL import ImageFont

        key = (str(font_path), int(font_px))
        if threading.current_thread() is threading.main_thread():
            cache = self._fonts
        else:
            cache = getattr(self._local, "fonts", None)
            if cache is None:
                cache = {}
                self._local.fonts = cache
        font = cache.get(key)
        if font is None:
            font = ImageFont.truetype(
                str(font_path), int(font_px),
                layout_engine=ImageFont.Layout.RAQM,
            )
            cache[key] = font
        return font

    def _measure_runs(
        self,
        draw: Any,
        line: str,
        font_px: int,
        font_path: Path,
        stroke: int,
    ) -> tuple[list[tuple[str, Any, float]], tuple[int, int, int, int], float]:
        """Measure one line's baseline-anchored run bboxes and total advance."""
        return _measure_runs_with_loader(
            self._font, draw, line, font_px, font_path, stroke
        )

    def _draw_placed_line(
        self,
        draw: Any,
        placed: list[tuple[str, Any, float]],
        origin_x: float,
        baseline_y: float,
        fill_col: tuple[int, int, int, int],
        stroke: int,
        stroke_col: tuple[int, int, int, int],
    ) -> None:
        """Draw one baseline-aligned line with outline strictly OUTSIDE fills."""
        if len(placed) == 1:
            seg, f, rel_x = placed[0]
            draw.text(
                (origin_x + rel_x, baseline_y),
                seg,
                font=f,
                anchor="ls",
                fill=fill_col,
                stroke_width=stroke,
                stroke_fill=stroke_col,
            )
            return
        # Multi-run line: draw all strokes before fills so neighboring runs'
        # strokes never paint over earlier runs' interior fills.
        if stroke > 0:
            for seg, f, rel_x in placed:
                draw.text(
                    (origin_x + rel_x, baseline_y),
                    seg,
                    font=f,
                    anchor="ls",
                    fill=stroke_col,
                    stroke_width=stroke,
                    stroke_fill=stroke_col,
                )
        for seg, f, rel_x in placed:
            draw.text(
                (origin_x + rel_x, baseline_y),
                seg,
                font=f,
                anchor="ls",
                fill=fill_col,
                stroke_width=0,
            )

    def render_lines(
        self,
        lines: list[str],
        font_px: int,
        font_path: Path,
        visuals: dict[str, Any],
    ) -> tuple[Path, int, int]:
        """Render pre-split display lines → (png_path, width, height)."""
        from PIL import Image, ImageDraw, ImageFilter

        lines = [unicodedata.normalize("NFC", ln) for ln in lines if ln]
        if not lines:
            raise ValueError("empty subtitle cue")
        font_px = max(8, int(font_px))
        font_path = Path(font_path)
        stroke = int(visuals.get("stroke_px", 1))
        fb_path = fallback_font_path(font_path)
        key_src = json.dumps(
            {
                "v": 2,
                "lines": lines,
                "px": font_px,
                "font": font_path.name,
                "fsize": font_path.stat().st_size if font_path.is_file() else 0,
                "fb": fb_path.name if fb_path is not None else "",
                "fill": visuals.get("fill"),
                "sc": visuals.get("stroke_colour"),
                "sp": stroke,
                "band": visuals.get("band"),
                "bg_mode": visuals.get("bg_mode"),
                "bg_px": visuals.get("bg_pad_x_frac"),
                "bg_py": visuals.get("bg_pad_y_frac"),
                "bg_r": visuals.get("bg_radius_frac"),
                "shadow": bool(visuals.get("shadow")),
                "shadow_soft": visuals.get("shadow_soft"),
                "lh": SUBTITLE_LINE_HEIGHT_MULT,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:24]
        out = self.cache_dir / f"sub_{digest}.png"
        if out.is_file() and out.stat().st_size > 0:
            with Image.open(out) as im:
                return out, im.width, im.height

        dummy = Image.new("RGBA", (1, 1))
        d = ImageDraw.Draw(dummy)
        line_step = max(font_px + 2, int(round(font_px * SUBTITLE_LINE_HEIGHT_MULT)))
        measured = [self._measure_runs(d, ln, font_px, font_path, stroke) for ln in lines]

        line_widths = [max(1, mb[2] - mb[0]) for _, mb, _ in measured]
        max_line_w = max(line_widths)
        # Center each line horizontally around x = max_line_w / 2 inside [0..max_line_w].
        ink_x0 = 10**9
        ink_x1 = -(10**9)
        ink_y0 = 10**9
        ink_y1 = -(10**9)
        line_positions: list[tuple[list[tuple[str, Any, float]], float, float]] = []
        for idx, (placed, mb, _adv) in enumerate(measured):
            lw = max(1, mb[2] - mb[0])
            x_off = (max_line_w - lw) / 2.0 - mb[0]
            y_off = idx * line_step
            ink_x0 = min(ink_x0, int(round(x_off + mb[0])))
            ink_x1 = max(ink_x1, int(round(x_off + mb[2])))
            ink_y0 = min(ink_y0, int(round(y_off + mb[1])))
            ink_y1 = max(ink_y1, int(round(y_off + mb[3])))
            line_positions.append((placed, x_off, float(y_off)))

        ink_w = max(1, ink_x1 - ink_x0)
        ink_h = max(1, ink_y1 - ink_y0)
        band = visuals.get("band")
        pad_x = png_pad_x(visuals, font_px, stroke)
        soft_metrics = _shadow_soft_metrics(visuals, font_px)
        if band is not None:
            pad_y_frac = float(
                visuals.get("bg_pad_y_frac")
                or (0.24 if visuals.get("bg_mode") == "pill" else 0.16)
            )
            pad_y = max(stroke + 3, int(round(font_px * pad_y_frac)))
        else:
            pad_y = pad_x
            if soft_metrics is not None:
                _, blur_px, _, off_y = soft_metrics
                pad_y = max(pad_y, stroke + off_y + blur_px)
        w = ink_w + 2 * pad_x
        h = ink_h + 2 * pad_y
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        if band is not None:
            if visuals.get("bg_mode") == "pill":
                r_frac = float(visuals.get("bg_radius_frac") or 0.42)
                radius = min(h // 2, max(8, int(round(font_px * r_frac))))
            else:
                r_frac = float(visuals.get("bg_radius_frac") or 0.18)
                radius = max(6, int(round(font_px * r_frac)))
            draw.rounded_rectangle(
                [0, 0, w - 1, h - 1],
                radius=radius,
                fill=tuple(band),
            )

        shift_x = pad_x - ink_x0
        shift_y = pad_y - ink_y0
        if soft_metrics is not None:
            sh_alpha, blur_px, off_x, off_y = soft_metrics
            sh_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            sh_draw = ImageDraw.Draw(sh_layer)
            sh_col = (0, 0, 0, sh_alpha)
            for placed, lx, ly in line_positions:
                self._draw_placed_line(
                    sh_draw, placed, lx + shift_x + off_x, ly + shift_y + off_y,
                    sh_col, stroke, sh_col,
                )
            sh_layer = sh_layer.filter(ImageFilter.GaussianBlur(radius=blur_px))
            img = Image.alpha_composite(img, sh_layer)
            draw = ImageDraw.Draw(img)
        elif visuals.get("shadow"):
            # Batch DD: hard drop shadow — the same text drawn once,
            # offset down-right in translucent black, under the main pass.
            sh = max(2, int(round(font_px * 0.06)))
            sh_col = (0, 0, 0, 170)
            for placed, lx, ly in line_positions:
                self._draw_placed_line(
                    draw, placed, lx + shift_x + sh, ly + shift_y + sh,
                    sh_col, stroke, sh_col,
                )

        fill_col = tuple(visuals["fill"])
        stroke_col = tuple(visuals["stroke_colour"])
        for placed, lx, ly in line_positions:
            self._draw_placed_line(
                draw, placed, lx + shift_x, ly + shift_y,
                fill_col, stroke, stroke_col,
            )

        _tmp = out.with_name(f".{out.stem}.{threading.get_ident()}.tmp.png")
        try:
            img.save(str(_tmp), "PNG", compress_level=1)
            os.replace(_tmp, out)
        except Exception:
            try:
                _tmp.unlink(missing_ok=True)
            except OSError:
                pass
            img.save(str(out), "PNG", compress_level=1)
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

    def text_width(
        self, line: str, font_px: int, font_path: Path, stroke_px: int = 0
    ) -> int:
        """Ink width (px) of ONE display line at a given size.

        The Batch AG fit measurer: uses the same font cache, fallback runs,
        and baseline bbox measurement ``render_lines`` uses, so a width
        predicted here matches the rendered PNG ink width.
        """
        from PIL import Image, ImageDraw

        dummy = Image.new("RGBA", (1, 1))
        d = ImageDraw.Draw(dummy)
        _, bbox, _ = self._measure_runs(
            d, unicodedata.normalize("NFC", str(line or "")),
            max(8, int(font_px)), Path(font_path), int(stroke_px),
        )
        return max(0, int(bbox[2] - bbox[0]))
