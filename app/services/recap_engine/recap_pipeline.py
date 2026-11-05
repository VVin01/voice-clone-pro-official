"""
recap_pipeline.py - High-level execution pipeline for Recap English -> Myanmar
Combines RecapPlanBuilder (Whisper + Gemini Story Memory + Translation)
and EdgeSmartSync (Edge TTS Myanmar + Smart Sync video setpts + Mux).
"""

from __future__ import annotations

import os
import sys
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from recap_planner import RecapPlanBuilder
from core import EdgeSmartSync, probe_duration, probe_video_size, frame_layout, source_band_to_canvas_box, user_blur_box_to_canvas_box, extract_preview_with_overlays, detect_source_subtitle_bands, resolve_subtitle_font, subtitle_plate_box, reconcile_source_bands, subtitle_anchor_y, subtitle_anchor_x, ensure_plate_contains_anchor, _layout_content_box

logger = logging.getLogger(__name__)


def find_binary(name: str) -> str:
    """Find ffmpeg or ffprobe executable on the system."""
    for cand in (
        shutil.which(name),
        rf"C:\ffmpeg\bin\{name}.exe",
        str(HERE.parents[1] / "ffmpeg" / f"{name}.exe"),
        str(HERE.parents[1] / "ffmpeg" / "bin" / f"{name}.exe"),
    ):
        if cand and Path(cand).is_file():
            return str(Path(cand).resolve())
    return name


# ── Blur-box assembly (blur safefix 2026-11-03) ─────────────────────
#
# core.apply_production_overlays blurs ONCE PER BOX, sequentially —
# each box consumes the previous box's output — so any two boxes that
# overlap double-blur their intersection. Box assembly therefore ends
# with a canonical merge pass, and every box source (plate, detected
# bands, the user's fallback hint, the watermark box) flows through
# the one helper below, shared by the render and the preview.

_WATERMARK_BLUR_BOX = {"x": 0.74, "y": 0.03, "w": 0.24, "h": 0.12}

# Batch O: user-facing blur strength (boxblur radius). 18 is the
# historical hardcoded value; core.apply_production_overlays clamps
# to the same 4–40 range when it builds the filter.
DEFAULT_BLUR_STRENGTH = 18
BLUR_STRENGTH_MIN = 4
BLUR_STRENGTH_MAX = 40


def clamp_blur_strength(value) -> int:
    """Coerce a blur-strength input to the supported 4–40 range."""
    try:
        strength = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BLUR_STRENGTH
    return max(BLUR_STRENGTH_MIN, min(BLUR_STRENGTH_MAX, strength))


def _boxes_near_or_overlapping(a: dict, b: dict) -> bool:
    """Merge predicate for the final pass, mirroring the centre-gap
    convention reconcile_source_bands uses against the plate
    (half-extents + 0.02 of slack), applied on both axes."""
    v_gap = abs(float(a["y"]) - float(b["y"]))
    v_reach = float(a["h"]) / 2.0 + float(b["h"]) / 2.0 + 0.02
    a_cx = float(a["x"]) + float(a["w"]) / 2.0
    b_cx = float(b["x"]) + float(b["w"]) / 2.0
    h_gap = abs(a_cx - b_cx)
    h_reach = float(a["w"]) / 2.0 + float(b["w"]) / 2.0 + 0.02
    return v_gap <= v_reach and h_gap <= h_reach


def _union_blur_box(a: dict, b: dict) -> dict:
    left = min(float(a["x"]), float(b["x"]))
    right = max(float(a["x"]) + float(a["w"]), float(b["x"]) + float(b["w"]))
    top = min(float(a["y"]) - float(a["h"]) / 2.0, float(b["y"]) - float(b["h"]) / 2.0)
    bot = max(float(a["y"]) + float(a["h"]) / 2.0, float(b["y"]) + float(b["h"]) / 2.0)
    return {
        "x": round(left, 4),
        "y": round((top + bot) / 2.0, 4),
        "w": round(right - left, 4),
        "h": round(bot - top, 4),
    }


def merge_overlapping_blur_boxes(boxes: list[dict]) -> list[dict]:
    """Merge ANY two overlapping (or near-touching) boxes into their
    bounding union, until the list is pairwise disjoint. Earlier
    boxes absorb later ones, so the caller's ordering (plate first)
    wins."""
    merged: list[dict] = [dict(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            hit = None
            for j in range(i + 1, len(merged)):
                if _boxes_near_or_overlapping(merged[i], merged[j]):
                    hit = j
                    break
            if hit is not None:
                merged[i] = _union_blur_box(merged[i], merged[hit])
                del merged[hit]
                changed = True
                break
    return merged


def assemble_blur_boxes(
    *,
    blur_subtitles: bool,
    burn_subtitles: bool,
    plate: Optional[dict],
    detected_bands: Optional[list[dict]] = None,
    fallback_box: Optional[dict] = None,
    user_box: Optional[dict] = None,
    blur_watermark: bool = False,
    layout: Optional[dict] = None,
    canvas_w: int = 1920,
    canvas_h: int = 1080,
    anchor_y: Optional[float] = None,
    anchor_x: Optional[float] = None,
) -> tuple[list[dict], bool]:
    """One canonical blur-box assembly for the render AND the preview.

    ``detected_bands`` are CANVAS-fraction boxes (already mapped via
    source_band_to_canvas_box); ``None`` means detection did not run
    or failed. ``fallback_box`` is the user's blur_y hint box, built
    by the caller only when the hint was moved away from sub_y.

    ``user_box`` (Batch AA) is the EXPLICIT user-set blur box, already
    converted to canvas fractions via user_blur_box_to_canvas_box.
    When it is present it is AUTHORITATIVE for the source-subtitle
    band: it is the sole source-band box — detection results and the
    fallback hint are not consulted, and the reconcile can neither
    merge it into the plate, drop it into letterbox padding, nor
    move it. It ships exactly as drawn; the only pass that can still
    touch it is the final Batch J overlap merge, which folds it with
    the plate/watermark ONLY when they actually overlap (the overlay
    pass blurs once per box, so an overlap would double-blur) — the
    union still covers the drawn box exactly. The own-subtitle plate
    continues through the normal path below. Callers skip detection
    entirely when a user box is present.

    Returns ``(boxes, source_band_covered)``:

    * ``blur_subtitles`` is the master gate — OFF means ZERO boxes.
      (Before, detection was gated on ``burn_subtitles``, so Blur OFF
      still blurred every detected band, and Burn OFF + Blur ON still
      shipped a frosted plate strip under no text.)
    * Detected bands are reconciled against the plate (the existing
      merge/drop rules); the fallback hint — consulted only when
      detection found nothing — now goes through the SAME reconcile
      instead of being appended raw, so a small slider nudge can no
      longer double-blur the plate's edge strip.
    * With ``burn_subtitles`` OFF the plate is not emitted as a
      standalone strip: it ships only when a detected band or the
      fallback merged into it (a real source band lives there).
    * The assembled list ends with merge_overlapping_blur_boxes(), so
      extras are also checked against EACH OTHER and against the
      watermark box — no pair of shipped boxes can overlap.
    * ``source_band_covered`` is True when a detected band was folded
      in (merged into the plate, kept as an extra, or dropped into
      the already-blurred letterbox fill) or the fallback hint covers
      an area — callers warn the user when blur was requested without
      any source-band coverage.
    """
    if not blur_subtitles:
        return [], False

    covered = False
    extras: list[dict] = []
    plate_out = dict(plate) if plate is not None else None
    plate_merged = False

    if user_box is not None:
        # Batch AA: the explicit user box IS the source-subtitle
        # blur — verbatim, as the sole extra. It bypasses the
        # reconcile (which could merge it into the plate, drop it
        # into letterbox padding, or shift it) and the plate is
        # never fed into it; the final overlap-merge below still
        # applies, exactly as for every other shipped box.
        covered = True
        extras = [dict(user_box)]
    elif detected_bands:
        covered = True
        if plate_out is not None:
            new_plate, extras = reconcile_source_bands(
                plate_out, detected_bands, layout, canvas_w, canvas_h
            )
            plate_merged = bool(new_plate is not None and new_plate != plate)
            plate_out = new_plate
        else:
            extras = [dict(b) for b in detected_bands]
    elif fallback_box is not None:
        # Detection found nothing (or never ran): the user's own
        # blur_y hint is the source-band box — reconciled against the
        # plate exactly like a detected band.
        if plate_out is not None:
            # Reconcile drops a band ONLY when it lies fully inside
            # the letterbox padding; a hint that merges (even one
            # fully contained in the plate, which leaves the plate's
            # values unchanged) or survives as an extra covers its
            # area. Detect the drop with the same content-box rule
            # reconcile uses, not by comparing plate values.
            _pw, _ph, _ox, _oy = _layout_content_box(layout, canvas_w, canvas_h)
            _c_top = _oy / float(canvas_h)
            _c_bot = (_oy + _ph) / float(canvas_h)
            _fb_top = float(fallback_box["y"]) - float(fallback_box["h"]) / 2.0
            _fb_bot = float(fallback_box["y"]) + float(fallback_box["h"]) / 2.0
            if not (_fb_bot <= _c_top + 0.002 or _fb_top >= _c_bot - 0.002):
                covered = True
            new_plate, fb_extras = reconcile_source_bands(
                plate_out, [fallback_box], layout, canvas_w, canvas_h
            )
            plate_merged = bool(new_plate is not None and new_plate != plate)
            plate_out = new_plate
            extras.extend(fb_extras)
        else:
            extras.append(dict(fallback_box))
            covered = True

    include_plate = plate_out is not None and (burn_subtitles or plate_merged)
    if include_plate and burn_subtitles and anchor_y is not None:
        # Belt-and-braces (R40 item 3): the shipped plate must contain
        # the subtitle anchor. Applied to the reconciled plate,
        # before the final merge pass, exactly where the render path
        # always applied it. Batch AG: anchor_x (given only when the
        # user set sub_x) extends the same containment sideways.
        plate_out = ensure_plate_contains_anchor(plate_out, anchor_y, anchor_x)

    boxes: list[dict] = []
    if include_plate:
        boxes.append(plate_out)
    if blur_watermark:
        boxes.append(dict(_WATERMARK_BLUR_BOX))
    boxes.extend(extras)
    return merge_overlapping_blur_boxes(boxes), covered


def run_recap_pipeline(
    source_video_path: str | Path,
    job_dir: Optional[str | Path] = None,
    voice: str = "my-MM-ThihaNeural",
    narrative_style: str = "narrator",
    voice_speed: str = "+12%",
    tts_engine: str = "edge_tts",
    source_language: str = "auto",
    render_quality: str = "standard",
    gemini_voice: Optional[str] = None,
    voxcpm_control: Optional[str] = None,
    voxcpm_ref_audio: Optional[str] = None,
    is_female: bool = False,
    bgm_path: Optional[str] = None,
    bgm_volume: float = 0.18,
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    env_path: Optional[str | Path] = None,
    output_ratio: str = "Original",
    frame_fill: str = "blur",
    crop_top: float = 0.0,
    crop_bottom: float = 0.0,
    crop_left: float = 0.0,
    crop_right: float = 0.0,
    mirror_video: bool = True,
    zoom_factor: float = 1.06,
    blur_subtitles: bool = True,
    blur_y: float = 0.85,
    blur_h: float = 0.07,
    blur_x: Optional[float] = None,
    blur_w: Optional[float] = None,
    blur_box_explicit: bool = False,
    blur_strength: int = DEFAULT_BLUR_STRENGTH,
    blur_watermark: bool = False,
    burn_subtitles: bool = True,
    sub_y: Optional[float] = 0.85,
    sub_x: Optional[float] = None,
    sub_w: Optional[float] = None,
    sub_lines: int = 1,
    sub_font_size: int = 16,
    sub_style: str = "yellow",
    sub_font: str = "padauk",
    title_text: Optional[str] = None,
    title_position: Optional[list[float]] = None,
    logo_path: Optional[str] = None,
    logo_position: Optional[list[float]] = None,
    logo_scale: float = 0.16,
    logo_opacity: float = 0.70,
    extra_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executes the full Recap English -> Myanmar workflow:
    1. Whisper Word-level Transcription
    2. Whole-sentence segmentation
    3. Gemini Story Context Memory
    4. Gemini English -> Myanmar translation + audit
    5. Microsoft Edge TTS Myanmar narration (Thiha or Nilar)
    6. Smart Sync re-timing (30 fps frame-lock, max 1.25x speed, distributed freeze)
    7. Video transformations (Mirror horizontal, Zoom crop, Ratio)
    8. Original subtitle & watermark blur boxes
    9. Custom branding (Title badge, Logo) & Synced Myanmar subtitles burn
    10. Final Muxing to MP4

    Returns dictionary with final_video path, audio path, subtitles_srt, plan, and metrics.
    """
    source = Path(source_video_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source video not found: {source}")

    def _log(msg: str) -> None:
        logger.info(str(msg))
        if log_fn:
            try:
                log_fn(str(msg))
            except Exception:
                pass

    def _progress(current: int, total: int, label: str) -> None:
        if progress_fn:
            try:
                progress_fn(current, total, label)
            except Exception:
                pass

    stop_check = should_stop or (lambda: False)

    ffmpeg_bin = find_binary("ffmpeg")
    ffprobe_bin = find_binary("ffprobe")

    root_dir = HERE.parent
    if not job_dir:
        import hashlib
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in source.stem)[:40] or "recap_job"
        sig = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
        target_job_dir = root_dir / "downloads" / f"recap_{safe_name}_{sig}"
    else:
        target_job_dir = Path(job_dir).resolve()

    target_job_dir.mkdir(parents=True, exist_ok=True)
    _log(f"🎬 Starting Recap English → Myanmar: {source.name}")
    _log(f"📁 Job Workspace: {target_job_dir}")
    _engine_label = {"voxcpm": "VOXCPM", "gemini": "GEMINI TTS"}.get(str(tts_engine).lower(), "Edge TTS")
    _log(f"🎙️ TTS Engine: {_engine_label} | Voice: {voice}")

    gemini_env = Path(env_path) if env_path else root_dir / ".env"

    # Step 1: PLANNER (Whisper -> Gemini Context -> Translation -> Smart Sync Plan)
    has_groq = bool(os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY"))
    groq_label = "⚡ [1/4] Groq Cloud Whisper Turbo Transcription..." if has_groq else "🎙️ [1/4] Neural Whisper Audio Transcription..."
    _progress(1, 4, groq_label)
    planner = RecapPlanBuilder(
        runtime_root=HERE,
        gemini_env_path=gemini_env if gemini_env.is_file() else None,
        ffprobe_path=ffprobe_bin,
        log=_log,
        progress=lambda cur, tot, lbl: _progress(1 + int(cur * 2 / max(1, tot)), 4, lbl),
        should_stop=stop_check,
        gemini_text_model=os.getenv("GEMINI_TEXT_MODEL", "gemini-3.6-flash"),
        gemini_request_timeout_seconds=45,
        narrative_style=narrative_style,
        is_female=bool(is_female),
        source_language=source_language,
    )

    plan_path, plan = planner.build(
        source=source,
        job_dir=target_job_dir,
        source_title=title_text or source.stem,
        reuse=True,
    )

    if stop_check():
        raise InterruptedError("Recap stopped after planning stage.")

    # Build production overlays (Blur boxes, Title badge, Logo, Synced subtitles)
    overlays: dict[str, Any] = {}
    # R39: the render layout is computed once, up front — the subtitle
    # plate geometry (92% band, line-fitted height, letterbox rim
    # anchor) and the source-band conversion below both need it.
    _layout = None
    try:
        _src_w0, _src_h0 = probe_video_size(ffprobe_bin, source)
        _layout = frame_layout(
            output_ratio, frame_fill, _src_w0, _src_h0,
            crop_top=crop_top, crop_bottom=crop_bottom,
            crop_left=crop_left, crop_right=crop_right,
        )
    except Exception as layout_err:
        _log(f"⚠️ Layout probe skipped ({layout_err}); subtitle plate keeps the legacy box")
    _plate = None
    if blur_subtitles:
        # R39 plate: fixed ~92%-wide centred band, height fitted to
        # the wrapped subtitle lines (never below the caller's
        # blur_h), anchored at the content rim on letterboxed
        # layouts. The plate geometry is fixed — the old blur_x/blur_w
        # parameters never shaped it and were removed in batch N (the
        # legacy-box fallback below inlines their 0.05/0.90 values);
        # blur-strength semantics are unchanged
        # (overlays["blur_strength"] below).
        # R40 item 3: the plate is fed the SUBTITLE's sub_y — before,
        # it was fed blur_y, so a job with sub_y=67/blur_y=85 shipped
        # its subtitle floating on sharp picture at 67% while the
        # plate blurred an empty strip at 85%. blur_y/blur_h keep
        # their documented UI meaning: the user's hint for where the
        # SOURCE caption sits (fallback box in assemble_blur_boxes).
        _anchor_sub_y = float(sub_y) if sub_y is not None else float(blur_y)
        if _layout is not None:
            _plate = subtitle_plate_box(
                _layout["cw"], _layout["ch"], _layout,
                raw_font_size=sub_font_size, sub_lines=sub_lines,
                sub_y=_anchor_sub_y, min_h=blur_h,
                sub_x=sub_x, sub_w=sub_w,
            )
        else:
            _plate = {"x": 0.05, "y": _anchor_sub_y, "w": 0.90, "h": float(blur_h)}
    # Batch AA: an EXPLICIT user-set blur box is authoritative for
    # the source band — converted from picture fractions with the
    # same layout math the preview paints it with, it replaces
    # detection entirely (detection is not even run below).
    _user_box = None
    if blur_subtitles and blur_box_explicit:
        _user_box = user_blur_box_to_canvas_box(blur_y, blur_h, _layout, blur_x=blur_x, blur_w=blur_w)
    # Auto-cover the SOURCE video's own burned-in subtitle band. The
    # plate only covers the Myanmar subtitle position (y≈0.85); source
    # dialogue subs usually sit higher (≈0.70) and would otherwise
    # survive — mirrored into unreadable mirror-writing by the
    # default mirror pass.
    # Blur safefix (2026-11-03): detection is gated on
    # blur_subtitles — the master Blur toggle — not on burn_subtitles:
    # with Blur OFF the old gate still blurred every detected band,
    # and with Burn OFF + Blur ON it shipped a frosted plate strip
    # under no text.
    _canvas_bands: Optional[list[dict]] = None
    _detection_failed = False
    _band_layout = None
    if blur_subtitles and _user_box is None:
        try:
            # Detected bands are SOURCE-frame fractions; the preset
            # boxes above and every blur-box consumer use CANVAS
            # fractions. Convert through the render layout first so
            # the overlay pass places the box on the picture area it
            # actually describes (round-4 tear fix).
            _band_layout = _layout
            if _band_layout is None:
                _src_w, _src_h = probe_video_size(ffprobe_bin, source)
                _band_layout = frame_layout(
                    output_ratio, frame_fill, _src_w, _src_h,
                    crop_top=crop_top, crop_bottom=crop_bottom,
                    crop_left=crop_left, crop_right=crop_right,
                )
            _canvas_bands = [
                source_band_to_canvas_box(band, _band_layout)
                for band in detect_source_subtitle_bands(ffmpeg_bin, ffprobe_bin, source, target_job_dir, log=_log)
            ]
        except Exception as det_err:
            _detection_failed = True
            _log(f"⚠️ Source subtitle band auto-blur skipped: {det_err}")
    # The user's blur_y hint becomes the source-band box only when
    # they actually moved it away from the subtitle position, so the
    # default state gains no second band. assemble_blur_boxes
    # consults it solely when detection found nothing, and reconciles
    # it against the plate like a detected band. Batch AA: this is
    # the LEGACY path only — an explicit user box (_user_box above)
    # supersedes it, and assemble_blur_boxes ignores it then.
    _fallback_box = None
    if sub_y is not None and abs(float(blur_y) - float(sub_y)) > 0.01:
        _fallback_box = {"x": 0.05, "y": float(blur_y), "w": 0.90, "h": float(blur_h)}
    _anchor = None
    if _layout is not None:
        _anchor = subtitle_anchor_y(
            _layout["cw"], _layout["ch"], _layout,
            sub_y=float(sub_y) if sub_y is not None else float(blur_y),
            raw_font_size=sub_font_size, sub_lines=sub_lines,
        )
    # Batch AG: the horizontal anchor exists only when the user set
    # sub_x — an unset sub_x keeps the plate containment vertical-only,
    # exactly as before.
    _anchor_x = None
    if _layout is not None and sub_x is not None:
        _anchor_x = subtitle_anchor_x(
            _layout["cw"], _layout["ch"], _layout, sub_x=float(sub_x),
        )
    _asm_layout = _band_layout or _layout or {}
    blur_boxes, _band_covered = assemble_blur_boxes(
        blur_subtitles=blur_subtitles,
        burn_subtitles=burn_subtitles,
        plate=_plate,
        detected_bands=_canvas_bands,
        fallback_box=_fallback_box,
        user_box=_user_box,
        blur_watermark=blur_watermark,
        layout=_band_layout,
        canvas_w=int(_asm_layout.get("cw") or 1920),
        canvas_h=int(_asm_layout.get("ch") or 1080),
        anchor_y=_anchor,
        anchor_x=_anchor_x,
    )
    # No-coverage warning (blur safefix item 6): blur was requested
    # but nothing covers a source band — detection found no band and
    # the user's hint coincides with the subtitle position (or was
    # dropped), or detection itself failed. Surfaced on the job
    # result via the return dict; the render is unaffected.
    _job_warnings: list[str] = []
    if blur_subtitles and (_detection_failed or not _band_covered):
        _job_warnings.append("blur_no_source_band")
    if blur_boxes:
        overlays["blur_boxes"] = blur_boxes
        overlays["blur_strength"] = clamp_blur_strength(blur_strength)

    if title_text and str(title_text).strip():
        overlays["title_text"] = str(title_text).strip()
        # Batch AH: caller-supplied placement — a [tx, ty] pair of
        # 0..1 title-CENTRE fractions (validated/clamped here so a
        # malformed value degrades to the default instead of failing
        # the render); absent keeps the historical [0.5, 0.12].
        _tp = title_position
        if isinstance(_tp, (list, tuple)) and len(_tp) == 2:
            try:
                overlays["title_position"] = [
                    max(0.0, min(1.0, float(_tp[0]))),
                    max(0.0, min(1.0, float(_tp[1]))),
                ]
            except (TypeError, ValueError):
                overlays["title_position"] = [0.5, 0.12]
        else:
            overlays["title_position"] = [0.5, 0.12]

    if logo_path and Path(logo_path).is_file():
        overlays["logo_path"] = str(logo_path)
        overlays["logo_enabled"] = True
        overlays["logo_position"] = logo_position or [0.82, 0.06]
        overlays["logo_width_fraction"] = float(logo_scale or 0.16)
        overlays["logo_opacity"] = float(logo_opacity or 0.70)

    if extra_text and str(extra_text).strip():
        overlays["extra_text"] = str(extra_text).strip()
        # Free-tier brand watermark v3 (2026-10-30): the only production
        # caller passes extra_text for free-tier branding (webapp/api.py,
        # non-VIP jobs). core.apply_production_overlays renders
        # overlays["watermark"] as a full-frame diagonal tiled pattern of
        # the bot username "@voiceclonepro_bot" (crop-resistant — the v1
        # corner-only mark could simply be cropped away, and the v2
        # corner lockup looked cheap; both were rejected by the owner),
        # instead of the old centered title panel at [0.50, 0.68] / 0.88
        # opacity. See watermark-v3-2026-10-30.md.
        overlays["watermark"] = True
        overlays["watermark_pattern_opacity"] = 0.19

    overlays["burn_subtitles"] = bool(burn_subtitles)
    if sub_y is not None:
        overlays["sub_y"] = float(sub_y)
    # Batch AG: horizontal subtitle placement rides the same overlays
    # dict — EdgeSmartSync reads sub_x for the ASS \pos tag and sub_w
    # for the cue-wrap budgets; apply_production_overlays reads sub_x
    # for the PNG overlay x expression.
    if sub_x is not None:
        overlays["sub_x"] = float(sub_x)
    if sub_w is not None:
        overlays["sub_w"] = float(sub_w)
    overlays["sub_lines"] = int(sub_lines)
    overlays["subtitles_font_size"] = int(sub_font_size)
    overlays["sub_style"] = str(sub_style or "yellow")
    _font_key, _font_family = resolve_subtitle_font(sub_font)
    overlays["sub_font"] = _font_key
    overlays["subtitles_font"] = _font_family
    overlays["tts_engine"] = str(tts_engine or "edge_tts")
    # Production-grade options, consumed by EdgeSmartSync (core.py):
    # render_quality picks the encoder preset; gemini_voice names the
    # Gemini TTS voice when tts_engine == "gemini".
    overlays["render_quality"] = str(render_quality or "standard").lower()
    if gemini_voice:
        overlays["gemini_voice"] = str(gemini_voice)
    if voxcpm_control:
        overlays["voxcpm_control"] = str(voxcpm_control)
    if voxcpm_ref_audio:
        overlays["voxcpm_ref_audio"] = str(voxcpm_ref_audio)
    overlays["is_female"] = bool(is_female)
    if bgm_path:
        overlays["bgm_path"] = str(bgm_path)
        overlays["bgm_volume"] = float(bgm_volume or 0.18)

    # Step 2: RENDER (Edge TTS or VoxCPM 2 + Smart Sync Video)
    tts_lbl = {"voxcpm": "VoxCPM 2 Cloud Voice", "gemini": "Gemini TTS"}.get(str(tts_engine).lower(), "Microsoft Edge TTS")
    _progress(3, 4, f"🗣️ [3/4] {tts_lbl} & Smart Sync Render...")
    runner = EdgeSmartSync(
        job_dir=target_job_dir,
        source_path=source,
        plan_path=plan_path,
        voice=voice,
        edge_tts_rate=voice_speed,
        edge_tts_pitch="+0Hz",
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
        max_video_speed=1.35,
        max_video_slow=1.20,
        mirror_video=bool(mirror_video),
        zoom_factor=float(zoom_factor),
        smooth_freeze_fallback=False,
        max_freeze_hold=0.75,
        render_final_video=True,
        output_ratio=output_ratio,
        frame_fill=frame_fill,
        crop_top=crop_top,
        crop_bottom=crop_bottom,
        crop_left=crop_left,
        crop_right=crop_right,
        overlays=overlays,
        log=_log,
        progress=lambda cur, tot, lbl: _progress(3, 4, f"Smart Sync: {lbl}"),
        should_stop=stop_check,
    )
    runner.voice_volume_percent = 100
    runner.short_pauses = True

    result = runner.run()
    _progress(4, 4, "✅ Final Recap Video Ready!")
    _log(f"🎉 Final Video: {result.get('final')}")

    return {
        "final_video": result.get("final"),
        "audio": result.get("audio"),
        "subtitles_srt": result.get("subtitles_srt"),
        "plan_path": str(plan_path),
        "job_dir": str(target_job_dir),
        "segments": result.get("segments", len(plan.get("segments", []))),
        "duration": result.get("duration", 0.0),
        "warnings": _job_warnings,
    }


def build_recap_script_plan(
    source_video_path: str | Path,
    job_dir: Optional[str | Path] = None,
    narrative_style: str = "narrator",
    is_female: bool = False,
    source_title: Optional[str] = None,
    source_language: str = "auto",
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[int, int, str], None]] = None,
    env_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Phase 1 of the two-phase recap (script review flow, 2026-10-29).

    Runs ONLY the planner — Whisper transcription, Gemini story context and
    translation — and stops with ``smart_sync_plan.json`` (+ checkpoints)
    written into *job_dir*, so the user can review/edit the Burmese
    narration before any TTS or rendering happens (the desktop Recapter
    tool's "Edit Text" step).

    A later :func:`run_recap_pipeline` call with the same *job_dir* reuses
    this plan verbatim through the planner's reuse path — including any
    user edits saved into the plan file — so phase 2 never re-transcribes
    or re-translates.
    """
    source = Path(source_video_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source video not found: {source}")

    def _log(msg: str) -> None:
        logger.info(str(msg))
        if log_fn:
            try:
                log_fn(str(msg))
            except Exception:
                pass

    ffprobe_bin = find_binary("ffprobe")
    root_dir = HERE.parent
    if not job_dir:
        import hashlib
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in source.stem)[:40] or "recap_job"
        sig = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
        target_job_dir = root_dir / "downloads" / f"recap_{safe_name}_{sig}"
    else:
        target_job_dir = Path(job_dir).resolve()
    target_job_dir.mkdir(parents=True, exist_ok=True)

    gemini_env = Path(env_path) if env_path else root_dir / ".env"
    planner = RecapPlanBuilder(
        runtime_root=HERE,
        gemini_env_path=gemini_env if gemini_env.is_file() else None,
        ffprobe_path=ffprobe_bin,
        log=_log,
        progress=progress_fn or (lambda cur, tot, lbl: None),
        should_stop=lambda: False,
        gemini_text_model=os.getenv("GEMINI_TEXT_MODEL", "gemini-3.6-flash"),
        gemini_request_timeout_seconds=45,
        narrative_style=narrative_style,
        is_female=bool(is_female),
        source_language=source_language,
    )
    plan_path, plan = planner.build(
        source=source,
        job_dir=target_job_dir,
        source_title=source_title or source.stem,
        reuse=True,
    )
    return {
        "plan_path": str(plan_path),
        "job_dir": str(target_job_dir),
        "segments": len(plan.get("segments", [])),
        "plan": plan,
    }


def generate_recap_preview(
    source_video_path: str | Path,
    output_image_path: Optional[str | Path] = None,
    blur_subtitles: bool = True,
    blur_y: float = 0.85,
    blur_h: float = 0.07,
    blur_x: Optional[float] = None,
    blur_w: Optional[float] = None,
    blur_box_explicit: bool = False,
    blur_strength: int = DEFAULT_BLUR_STRENGTH,
    burn_subtitles: bool = True,
    sub_y: Optional[float] = 0.85,
    sub_x: Optional[float] = None,
    sub_w: Optional[float] = None,
    sub_lines: int = 1,
    sub_font_size: int = 16,
    sub_style: str = "yellow",
    sub_font: str = "padauk",
    sample_text: str = "",
    output_ratio: str = "Original",
    frame_fill: str = "blur",
    crop_top: float = 0.0,
    crop_bottom: float = 0.0,
    crop_left: float = 0.0,
    crop_right: float = 0.0,
    mirror_video: bool = True,
    zoom_factor: float = 1.06,
    at_seconds: float = 3.5,
    logo_enabled: bool = False,
    logo_path: Optional[str] = None,
    logo_position: Optional[list[float]] = None,
    logo_scale: float = 0.16,
    logo_opacity: float = 0.70,
    ffmpeg_path: Optional[str] = None,
    ffprobe_path: Optional[str] = None,
) -> str:
    """Generate a single preview frame with mirror/zoom, blur box, logo, and sample subtitle applied."""
    src = Path(source_video_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Source video not found: {src}")

    ffmpeg_bin = ffmpeg_path or find_binary("ffmpeg")
    ffprobe_bin = ffprobe_path or find_binary("ffprobe")

    if not output_image_path:
        output_image_path = src.parent / f"preview_{src.stem}.jpg"
    out_path = Path(output_image_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    overlays: dict[str, Any] = {}
    blur_boxes: list[dict] = []
    if blur_subtitles:
        # R39: same plate geometry as the render path, so the preview
        # frame shows the plate the finished video will carry.
        # R40 item 3: anchored to the subtitle's sub_y, like the render.
        # Blur safefix (2026-11-03): the preview now builds its boxes
        # through the SAME assembly as the render — plate + source-band
        # detection + reconcile + fallback hint + overlap merge — so
        # the frame shows every band the finished video will blur,
        # not just the plate.
        _p_layout = None
        _p_plate = None
        try:
            _p_src_w, _p_src_h = probe_video_size(ffprobe_bin, src)
            _p_layout = frame_layout(
                output_ratio, frame_fill, _p_src_w, _p_src_h,
                crop_top=crop_top, crop_bottom=crop_bottom,
                crop_left=crop_left, crop_right=crop_right,
            )
            _p_plate = subtitle_plate_box(
                _p_layout["cw"], _p_layout["ch"], _p_layout,
                raw_font_size=sub_font_size, sub_lines=sub_lines,
                sub_y=float(sub_y) if sub_y is not None else float(blur_y), min_h=blur_h,
                sub_x=sub_x, sub_w=sub_w,
            )
        except Exception:
            _p_plate = {"x": 0.05, "y": float(sub_y) if sub_y is not None else float(blur_y), "w": 0.90, "h": float(blur_h)}
        # Batch AA: same rule as the render — an explicit user box
        # is authoritative and detection is not run at all.
        _p_user_box = None
        if blur_box_explicit:
            _p_user_box = user_blur_box_to_canvas_box(blur_y, blur_h, _p_layout, blur_x=blur_x, blur_w=blur_w)
        _p_bands: Optional[list[dict]] = None
        _p_band_layout = _p_layout
        if _p_user_box is None:
            try:
                if _p_band_layout is None:
                    _p_src_w2, _p_src_h2 = probe_video_size(ffprobe_bin, src)
                    _p_band_layout = frame_layout(
                        output_ratio, frame_fill, _p_src_w2, _p_src_h2,
                        crop_top=crop_top, crop_bottom=crop_bottom,
                        crop_left=crop_left, crop_right=crop_right,
                    )
                _p_bands = [
                    source_band_to_canvas_box(band, _p_band_layout)
                    for band in detect_source_subtitle_bands(
                        ffmpeg_bin, ffprobe_bin, src,
                        out_path.parent / f"preview_subband_{src.stem}",
                        log=lambda _m: None,
                    )
                ]
            except Exception:
                _p_bands = None
        _p_fallback = None
        if sub_y is not None and abs(float(blur_y) - float(sub_y)) > 0.01:
            _p_fallback = {"x": 0.05, "y": float(blur_y), "w": 0.90, "h": float(blur_h)}
        _p_anchor = None
        if _p_layout is not None:
            _p_anchor = subtitle_anchor_y(
                _p_layout["cw"], _p_layout["ch"], _p_layout,
                sub_y=float(sub_y) if sub_y is not None else float(blur_y),
                raw_font_size=sub_font_size, sub_lines=sub_lines,
            )
        _p_anchor_x = None
        if _p_layout is not None and sub_x is not None:
            _p_anchor_x = subtitle_anchor_x(
                _p_layout["cw"], _p_layout["ch"], _p_layout, sub_x=float(sub_x),
            )
        _p_asm_layout = _p_band_layout or _p_layout or {}
        blur_boxes, _p_covered = assemble_blur_boxes(
            blur_subtitles=blur_subtitles,
            burn_subtitles=burn_subtitles,
            plate=_p_plate,
            detected_bands=_p_bands,
            fallback_box=_p_fallback,
            user_box=_p_user_box,
            blur_watermark=False,
            layout=_p_band_layout,
            canvas_w=int(_p_asm_layout.get("cw") or 1920),
            canvas_h=int(_p_asm_layout.get("ch") or 1080),
            anchor_y=_p_anchor,
            anchor_x=_p_anchor_x,
        )
    if blur_boxes:
        overlays["blur_boxes"] = blur_boxes
        overlays["blur_strength"] = clamp_blur_strength(blur_strength)
    overlays["burn_subtitles"] = bool(burn_subtitles)
    if sub_y is not None:
        overlays["sub_y"] = float(sub_y)
    # Batch AG: the preview's sample subtitle honours sub_x through
    # the same overlays dict (extract_preview_with_overlays resolves
    # it via the shared centre-X resolver).
    if sub_x is not None:
        overlays["sub_x"] = float(sub_x)
    if sub_w is not None:
        overlays["sub_w"] = float(sub_w)
    overlays["sub_lines"] = int(sub_lines)
    overlays["subtitles_font_size"] = int(sub_font_size)
    overlays["sub_style"] = str(sub_style or "yellow")
    _p_font_key, _p_font_family = resolve_subtitle_font(sub_font)
    overlays["sub_font"] = _p_font_key
    overlays["subtitles_font"] = _p_font_family

    if logo_enabled and logo_path:
        overlays["logo_enabled"] = True
        overlays["logo_path"] = str(logo_path)
        overlays["logo_position"] = logo_position or [0.80, 0.05]
        overlays["logo_width_fraction"] = float(logo_scale or 0.16)
        overlays["logo_opacity"] = float(logo_opacity or 0.70)

    res = extract_preview_with_overlays(
        ffmpeg=ffmpeg_bin,
        source_video=src,
        output_image=out_path,
        timestamp=at_seconds,
        overlays=overlays,
        output_ratio=output_ratio,
        frame_fill=frame_fill,
        crop_top=crop_top,
        crop_bottom=crop_bottom,
        crop_left=crop_left,
        crop_right=crop_right,
        mirror_video=mirror_video,
        zoom_factor=zoom_factor,
        sample_text=sample_text,
        ffprobe=ffprobe_bin,
    )
    return str(res)

