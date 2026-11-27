"""VoiceClonePro's own minimal VoxCPM2 Space (built 2026-11-05).

Implements the official openbmb/VoxCPM-Demo `/generate` contract that our
engine's default branch calls:

    generate(text_input, control_instruction, reference_wav_path_input,
             use_prompt_text, prompt_text_input, cfg_value_input,
             do_normalize, denoise) -> ((sample_rate, waveform), gpu_seconds_used)

Generation mirrors the proven upstream settings: model
openbmb/VoxCPM2 with load_denoiser=False, cfg_value default 2.0,
inference_timesteps 15, sentence-chunked synthesis with 0.5 s joins and
peak normalisation to 0.95. The whole endpoint is one ZeroGPU function
with a dynamic duration request sized to the input text length.
"""

import gc
import os
import re
import time

import gradio as gr
import numpy as np
import spaces
import torch

MODEL_ID = "openbmb/VoxCPM2"

# Same patches as the upstream Space app: torch.compile off (ZeroGPU
# workers restart per call) and a soundfile-based torchaudio.load.
torch.compile = lambda model, *a, **k: model  # noqa: E731
try:
    import torch._dynamo

    torch._dynamo.config.disable = True
except Exception:
    pass


def _patch_torchaudio_load():
    import soundfile as sf
    import torchaudio

    def safe_torchaudio_load(filepath, *args, **kwargs):
        data, sr = sf.read(filepath)
        if data.ndim > 1:
            data = data.mean(axis=1)
        return torch.from_numpy(data).float().unsqueeze(0), sr

    torchaudio.load = safe_torchaudio_load


_patch_torchaudio_load()

_model = None


def load_model():
    global _model
    if _model is None:
        from voxcpm import VoxCPM

        print("Loading VoxCPM model...")
        _model = VoxCPM.from_pretrained(MODEL_ID, load_denoiser=False)
        print("Model loaded.")
    return _model


def split_text(text: str) -> list[str]:
    clean = re.sub(r"\[.*?\]", "", text or "")
    clean = re.sub(r"\(.*?\)", "", clean)
    smart = (
        clean.replace("။", "။\n")
        .replace(".", ".\n")
        .replace("?", "?\n")
        .replace("!", "!\n")
    )
    return [t.strip() for t in smart.split("\n") if t.strip()]




def _dyn_duration(*args, **kwargs):
    """ZeroGPU reservation sized to the text: fixed model-load overhead
    plus a per-char synthesis allowance, clamped to [25, 55] seconds."""
    try:
        text = kwargs.get("text_input") or (args[0] if args else "") or ""
        n = len(str(text).strip())
    except Exception:
        n = 100
    return min(55, max(25, 18 + int(n * 0.15)))

def _session_duration(*args, **kwargs):
    """ZeroGPU reservation for a whole session call: one model load plus
    a per-char synthesis allowance over up to ~1,100 chars, clamped to
    [30, 120] seconds."""
    try:
        text = kwargs.get("text_input") or (args[0] if args else "") or ""
        n = len(str(text).strip())
    except Exception:
        n = 100
    return min(120, max(30, 18 + int(n * 0.085)))


@spaces.GPU(duration=_session_duration)
def generate_session(
    text_input,
    control_instruction,
    reference_wav_path_input,
    use_prompt_text,
    prompt_text_input,
    cfg_value_input,
    do_normalize,
    denoise,
):
    """Session-style generation (test variant, 2026-11-26): identical body
    to generate() — the difference is the caller sends a LONG text
    (~1,100 chars) so every sentence of the slice is synthesized inside
    ONE ZeroGPU call, on one worker, with one model load (the Colab
    pattern), instead of one engine-chunk per call."""
    t0 = time.perf_counter()
    gpu_seconds_used = 0.0
    try:
        sample_rate, wav = _generate_impl(
            text_input,
            control_instruction,
            reference_wav_path_input,
            use_prompt_text,
            prompt_text_input,
            cfg_value_input,
            do_normalize,
            denoise,
        )
    finally:
        gpu_seconds_used = round(max(0.0, time.perf_counter() - t0), 2)
    return ((sample_rate, wav), gpu_seconds_used)


def _generate_impl(
    text_input,
    control_instruction,
    reference_wav_path_input,
    use_prompt_text,
    prompt_text_input,
    cfg_value_input,
    do_normalize,
    denoise,
):
    """Official-demo contract: clone when a reference wav is given,
    control-instruction TTS otherwise. `denoise` is accepted for contract
    compatibility; the model is loaded without a denoiser (same as the
    proven upstream Space), so it does not alter the audio."""
    tts_model = load_model()
    sample_rate = int(tts_model.tts_model.sample_rate)

    text = (text_input or "").strip()
    if do_normalize:
        text = re.sub(r"\s+", " ", text)
    if not text:
        return (sample_rate, np.zeros(100, dtype=np.float32))

    ref_audio = reference_wav_path_input
    has_ref = bool(ref_audio) and os.path.exists(ref_audio)

    gen_kwargs = {
        "cfg_value": float(cfg_value_input or 2.0),
        "inference_timesteps": 15,
    }
    style = None
    if has_ref:
        gen_kwargs["prompt_wav_path"] = ref_audio
        if use_prompt_text and prompt_text_input and prompt_text_input.strip():
            gen_kwargs["prompt_text"] = prompt_text_input.strip()
    else:
        style = (control_instruction or "").strip() or None

    silence = np.zeros(int(sample_rate * 0.5), dtype=np.float32)
    parts: list[np.ndarray] = []
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        if len(chunk) < 2:
            continue
        safe_text = f"({style}) {chunk}" if style else chunk
        safe_text = safe_text + " "
        with torch.inference_mode():
            wav = tts_model.generate(text=safe_text, **gen_kwargs)
        if isinstance(wav, tuple):
            wav = wav[0]
        if isinstance(wav, torch.Tensor):
            wav = wav.detach().cpu().numpy()
        wav = wav.astype(np.float32).flatten()
        peak = np.max(np.abs(wav)) if wav.size else 0.0
        if peak > 0:
            wav = (wav / peak) * 0.95
        parts.append(wav)
        if i < len(chunks) - 1:
            parts.append(silence)
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

    if not parts:
        return (sample_rate, np.zeros(100, dtype=np.float32))
    return (sample_rate, np.concatenate(parts))


@spaces.GPU(duration=_dyn_duration)
def generate(
    text_input,
    control_instruction,
    reference_wav_path_input,
    use_prompt_text,
    prompt_text_input,
    cfg_value_input,
    do_normalize,
    denoise,
):
    t0 = time.perf_counter()
    gpu_seconds_used = 0.0
    try:
        sample_rate, wav = _generate_impl(
            text_input,
            control_instruction,
            reference_wav_path_input,
            use_prompt_text,
            prompt_text_input,
            cfg_value_input,
            do_normalize,
            denoise,
        )
    finally:
        gpu_seconds_used = round(max(0.0, time.perf_counter() - t0), 2)
    return ((sample_rate, wav), gpu_seconds_used)


@spaces.GPU(duration=198)
def probe_quota():
    t0 = time.perf_counter()
    gpu_seconds_used = 0.0
    try:
        torch.ones(1, device="cuda").sum().item()
    finally:
        gpu_seconds_used = round(max(0.0, time.perf_counter() - t0), 2)
    return gpu_seconds_used


with gr.Blocks(title="VoiceClonePro VoxCPM2") as demo:
    gr.Markdown("## VoiceClonePro — VoxCPM2 (own build)")
    with gr.Row():
        with gr.Column():
            text_in = gr.Textbox(label="Text", lines=3)
            control_in = gr.Textbox(label="Control instruction")
            ref_in = gr.Audio(label="Reference audio", type="filepath")
            use_pt = gr.Checkbox(label="Use prompt text", value=True)
            prompt_in = gr.Textbox(label="Prompt text")
            cfg_in = gr.Slider(label="CFG value", minimum=1.0, maximum=3.0, value=2.0)
            norm_in = gr.Checkbox(label="Normalize", value=True)
            denoise_in = gr.Checkbox(label="Denoise", value=False)
            btn = gr.Button("Generate")
        with gr.Column():
            out = gr.Audio(label="Output")
            gpu_s_out = gr.Number(label="GPU Seconds Used", visible=False)
            probe_gpu_s_out = gr.Number(label="Probe GPU Seconds Used", visible=False)
    btn.click(
        fn=generate,
        inputs=[text_in, control_in, ref_in, use_pt, prompt_in, cfg_in, norm_in, denoise_in],
        outputs=[out, gpu_s_out],
        api_name="generate",
    )
    btn2 = gr.Button("Generate (session, long text)")
    btn2.click(
        fn=generate_session,
        inputs=[text_in, control_in, ref_in, use_pt, prompt_in, cfg_in, norm_in, denoise_in],
        outputs=[out, gpu_s_out],
        api_name="generate_session",
    )
    probe_btn = gr.Button("Probe Quota", visible=False)
    probe_btn.click(
        fn=probe_quota,
        inputs=[],
        outputs=[probe_gpu_s_out],
        api_name="probe_quota",
    )

if __name__ == "__main__":
    demo.launch()
