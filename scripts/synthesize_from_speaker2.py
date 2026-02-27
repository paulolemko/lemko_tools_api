#!/usr/bin/env python3
"""Standalone StyleTTS2 synthesis from text + speaker id (0/1).

Reference wavs and local model assets are auto-discovered across common
project layouts (so the script survives directory moves).
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import torch
from munch import Munch

# Keep notebook-like deterministic settings.
torch.manual_seed(0)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True
random.seed(0)
np.random.seed(0)

# Only values currently used from config_ft_demo.yml.
ASR_MODEL_CONFIG = "Utils/ASR/config.yml"
ASR_MODEL_PATH = "Utils/ASR/epoch_00080.pth"
F0_MODEL_PATH = "Utils/JDC/bst.t7"
PLBERT_DIR = "Utils/PLBERT_multi/"
PRETRAINED_MODEL = "Models/lemko_finetune/epoch_2nd_00044_nmndt_p2.pth"

MODEL_PARAMS = {
    "multispeaker": True,
    "dim_in": 64,
    "hidden_dim": 512,
    "max_conv_dim": 512,
    "n_layer": 3,
    "n_mels": 80,
    "n_token": 178,
    "max_dur": 50,
    "style_dim": 128,
    "dropout": 0.2,
    "decoder": {
        "type": "hifigan",
        "resblock_kernel_sizes": [3, 7, 11],
        "upsample_rates": [10, 5, 3, 2],
        "upsample_initial_channel": 512,
        "resblock_dilation_sizes": [[1, 3, 5], [1, 3, 5], [1, 3, 5]],
        "upsample_kernel_sizes": [20, 10, 6, 4],
    },
    "slm": {
        "model": "microsoft/wavlm-base-plus",
        "sr": 16000,
        "hidden": 768,
        "nlayers": 13,
        "initial_channel": 64,
    },
    "diffusion": {
        "embedding_mask_proba": 0.1,
        "transformer": {
            "num_layers": 3,
            "num_heads": 8,
            "head_features": 64,
            "multiplier": 2,
        },
        "dist": {
            "sigma_data": 0.2,
            "estimate_sigma_data": True,
            "mean": -3.0,
            "std": 1.0,
        },
    },
}

def recursive_munch(d):
    if isinstance(d, dict):
        return Munch((k, recursive_munch(v)) for k, v in d.items())
    if isinstance(d, list):
        return [recursive_munch(v) for v in d]
    return d


def _unique_resolved_paths(paths: list[Path | None]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p is None:
            continue
        rp = p.expanduser().resolve()
        if rp not in seen:
            unique.append(rp)
            seen.add(rp)
    return unique


def collect_styletts_roots(styletts_dir: Path, project_root: Path, invocation_cwd: Path) -> list[Path]:
    """Candidate directories that can contain StyleTTS2 assets."""
    return _unique_resolved_paths(
        [
            styletts_dir,
            project_root / "StyleTTS2",
            project_root.parent / "StyleTTS2",
            invocation_cwd / "StyleTTS2",
            invocation_cwd,
        ]
    )


def resolve_local_asset(relative_path: str, roots: list[Path], expect_dir: bool = False) -> Path:
    """Resolve a relative asset path by searching through candidate roots."""
    rel = Path(relative_path)
    searched: list[Path] = []

    for root in roots:
        candidate = (root / rel).resolve()
        searched.append(candidate)
        if expect_dir and candidate.is_dir():
            return candidate
        if not expect_dir and candidate.is_file():
            return candidate

    kind = "directory" if expect_dir else "file"
    searched_msg = "\n".join(str(p) for p in searched)
    raise FileNotFoundError(f"Could not find {kind} '{relative_path}'. Searched:\n{searched_msg}")


def resolve_refs_root(
    explicit_root: Path | None,
    speaker: int,
    num_refs: int,
    candidate_roots: list[Path],
) -> Path:
    """
    Choose a root directory containing <root>/<speaker>/...wav.
    If explicit_root is set, use only that location.
    """
    if explicit_root is not None:
        root = explicit_root.expanduser().resolve()
        speaker_dir = root / str(speaker)
        if not speaker_dir.is_dir():
            raise FileNotFoundError(f"Speaker directory not found: {speaker_dir}")
        return root

    checked: list[Path] = []

    # First pass: prefer locations that already have enough wavs.
    for root in candidate_roots:
        speaker_dir = root / str(speaker)
        checked.append(speaker_dir)
        if speaker_dir.is_dir() and len(list(speaker_dir.rglob("*.wav"))) >= num_refs:
            return root

    # Second pass: fallback to first existing speaker dir (error for too few wavs is shown later).
    for root in candidate_roots:
        speaker_dir = root / str(speaker)
        if speaker_dir.is_dir():
            return root

    checked_msg = "\n".join(str(p) for p in checked)
    raise FileNotFoundError(
        f"Speaker directory for id={speaker} was not found in any candidate root.\nChecked:\n{checked_msg}"
    )


def resolve_dirs() -> tuple[Path, Path]:
    """Return (styletts2_dir, project_root_dir)."""
    start = Path(__file__).resolve().parent
    candidates = [start] + list(start.parents)

    for c in candidates:
        if (c / "models.py").is_file() and (c / "Modules").is_dir():
            return c, c.parent

        if (c / "StyleTTS2" / "models.py").is_file() and (c / "StyleTTS2" / "Modules").is_dir():
            return c / "StyleTTS2", c

    searched = "\n".join(str(p) for p in candidates)
    raise FileNotFoundError(
        "Cannot find StyleTTS2 project directory; searched:\n" + searched
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthesize speech from text and speaker id (0/1) using StyleTTS2."
    )
    parser.add_argument("text", help="Text to synthesize.")
    parser.add_argument("speaker", type=int, choices=[0, 1], help="Speaker id (0 or 1).")
    parser.add_argument(
        "--preset",
        choices=["default", "less", "more"],
        default="default",
        help=(
            "Inference preset from notebook cells 50/52/54: "
            "default=(0.3,0.7), less=(0.1,0.3), more=(0.5,0.95)."
        ),
    )
    parser.add_argument(
        "--num-refs",
        type=int,
        default=3,
        help="Number of reference wav files to average (default: 3).",
    )
    parser.add_argument(
        "--refs-root",
        type=Path,
        default=None,
        help="Directory containing speaker folders '0' and '1' (default: auto-discovery).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output m4a path (default: <project_root>/synth_speaker_<id>.m4a).",
    )
    parser.add_argument(
        "--trim-in-ms",
        type=int,
        default=600,
        help="Trim duration in milliseconds removed from the beginning (default: %(default)s).",
    )
    parser.add_argument(
        "--trim-out-ms",
        type=int,
        default=200,
        help="Trim duration in milliseconds removed from the end (default: %(default)s).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    invocation_cwd = Path.cwd().resolve()

    base_dir, project_root = resolve_dirs()
    os.chdir(base_dir)
    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))

    # Imports that rely on StyleTTS2 cwd/module path.
    import librosa
    import soundfile as sf
    import torchaudio
    from nltk.tokenize import word_tokenize

    from lem_phonemizer2 import (
        LANG_BG,
        LANG_PL,
        LANG_UK,
        RAW_RULES,
        build_backends,
        build_rule_index,
        build_rules,
        phonemize_chunks,
        split_with_rules,
    )
    from models import build_model, load_ASR_models, load_F0_models
    from text_utils import TextCleaner
    from Utils.PLBERT.util import load_plbert
    from Modules.diffusion.sampler import DiffusionSampler, ADPM2Sampler, KarrasSchedule

    styletts_roots = collect_styletts_roots(base_dir, project_root, invocation_cwd)
    asr_model_config_path = resolve_local_asset(ASR_MODEL_CONFIG, styletts_roots)
    asr_model_path = resolve_local_asset(ASR_MODEL_PATH, styletts_roots)
    f0_model_path = resolve_local_asset(F0_MODEL_PATH, styletts_roots)
    plbert_dir_path = resolve_local_asset(PLBERT_DIR, styletts_roots, expect_dir=True)
    pretrained_model_path = resolve_local_asset(PRETRAINED_MODEL, styletts_roots)

    device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    textclenaer = TextCleaner()

    # Notebook settings up to cell 54.
    preset_to_params = {
        "default": {"alpha": 0.3, "beta": 0.7, "diffusion_steps": 10, "embedding_scale": 1.0},
        "less": {"alpha": 0.1, "beta": 0.3, "diffusion_steps": 10, "embedding_scale": 1.0},
        "more": {"alpha": 0.5, "beta": 0.95, "diffusion_steps": 10, "embedding_scale": 1.0},
    }
    infer_params = preset_to_params[args.preset]

    text_aligner = load_ASR_models(str(asr_model_path), str(asr_model_config_path))
    pitch_extractor = load_F0_models(str(f0_model_path))
    plbert = load_plbert(str(plbert_dir_path))

    model_params = recursive_munch(MODEL_PARAMS)
    model = build_model(model_params, text_aligner, pitch_extractor, plbert)
    _ = [model[key].eval() for key in model]
    _ = [model[key].to(device) for key in model]

    params_whole = torch.load(str(pretrained_model_path), map_location="cpu")
    params = params_whole["net"]

    for key in model:
        if key in params:
            try:
                model[key].load_state_dict(params[key])
            except Exception:
                from collections import OrderedDict

                state_dict = params[key]
                new_state_dict = OrderedDict()
                for k, v in state_dict.items():
                    name = k[7:]  # remove `module.`
                    new_state_dict[name] = v
                model[key].load_state_dict(new_state_dict, strict=False)

    _ = [model[key].eval() for key in model]

    sampler = DiffusionSampler(
        model.diffusion.diffusion,
        sampler=ADPM2Sampler(),
        sigma_schedule=KarrasSchedule(sigma_min=0.0001, sigma_max=3.0, rho=9.0),
        clamp=False,
    )

    phoneme_rules = build_rules(RAW_RULES)
    phoneme_rule_index = build_rule_index(phoneme_rules)
    phoneme_backends = build_backends(
        langs={LANG_PL, LANG_UK, LANG_BG},
        pl_code="pl",
        uk_code="uk",
        bg_code="bg",
    )

    def phonemize_for_synthesis(text: str) -> str:
        chunks = split_with_rules(text, phoneme_rule_index)
        ipa = phonemize_chunks(chunks, phoneme_backends)
        # Keep compatibility with the ы-handling logic from lem_phonemizer.py.
        return ipa.replace("ɨɯ", "ɯ").replace("iɡrɛkɯ", "ɯ")

    to_mel = torchaudio.transforms.MelSpectrogram(
        n_mels=80, n_fft=2048, win_length=1200, hop_length=300
    )
    mean, std = -4, 4

    def length_to_mask(lengths: torch.Tensor) -> torch.Tensor:
        mask = (
            torch.arange(lengths.max())
            .unsqueeze(0)
            .expand(lengths.shape[0], -1)
            .type_as(lengths)
        )
        mask = torch.gt(mask + 1, lengths.unsqueeze(1))
        return mask

    def preprocess(wave: np.ndarray) -> torch.Tensor:
        wave_tensor = torch.from_numpy(wave).float()
        mel_tensor = to_mel(wave_tensor)
        mel_tensor = (torch.log(1e-5 + mel_tensor.unsqueeze(0)) - mean) / std
        return mel_tensor

    def compute_style(path: str) -> torch.Tensor:
        wave, sr = librosa.load(path, sr=24000)
        audio, _ = librosa.effects.trim(wave, top_db=30)
        if sr != 24000:
            audio = librosa.resample(audio, sr, 24000)
        mel_tensor = preprocess(audio).to(device)

        with torch.no_grad():
            ref_s = model.style_encoder(mel_tensor.unsqueeze(1))
            ref_p = model.predictor_encoder(mel_tensor.unsqueeze(1))

        return torch.cat([ref_s, ref_p], dim=1)

    def inference(
        text: str,
        ref_s: torch.Tensor,
        alpha: float,
        beta: float,
        diffusion_steps: int,
        embedding_scale: float,
    ) -> np.ndarray:
        text = text.strip()
        ps = phonemize_for_synthesis(text)
        ps = word_tokenize(ps)
        ps = " ".join(ps)
        print(f"Fonemizacja wejściowa TTS: {ps}")
        tokens = textclenaer(ps)
        tokens.insert(0, 0)
        tokens = torch.LongTensor(tokens).to(device).unsqueeze(0)

        with torch.no_grad():
            input_lengths = torch.LongTensor([tokens.shape[-1]]).to(device)
            text_mask = length_to_mask(input_lengths).to(device)

            t_en = model.text_encoder(tokens, input_lengths, text_mask)
            bert_dur = model.bert(tokens, attention_mask=(~text_mask).int())
            d_en = model.bert_encoder(bert_dur).transpose(-1, -2)

            s_pred = sampler(
                noise=torch.randn((1, 256)).unsqueeze(1).to(device),
                embedding=bert_dur,
                embedding_scale=embedding_scale,
                features=ref_s,
                num_steps=diffusion_steps,
            ).squeeze(1)

            s = s_pred[:, 128:]
            ref = s_pred[:, :128]

            ref = alpha * ref + (1 - alpha) * ref_s[:, :128]
            s = beta * s + (1 - beta) * ref_s[:, 128:]

            d = model.predictor.text_encoder(d_en, s, input_lengths, text_mask)
            x, _ = model.predictor.lstm(d)
            duration = model.predictor.duration_proj(x)

            duration = torch.sigmoid(duration).sum(axis=-1)
            pred_dur = torch.round(duration.squeeze()).clamp(min=1)

            pred_aln_trg = torch.zeros(input_lengths, int(pred_dur.sum().data))
            c_frame = 0
            for i in range(pred_aln_trg.size(0)):
                pred_aln_trg[i, c_frame : c_frame + int(pred_dur[i].data)] = 1
                c_frame += int(pred_dur[i].data)

            en = d.transpose(-1, -2) @ pred_aln_trg.unsqueeze(0).to(device)
            if model_params.decoder.type == "hifigan":
                en_new = torch.zeros_like(en)
                en_new[:, :, 0] = en[:, :, 0]
                en_new[:, :, 1:] = en[:, :, 0:-1]
                en = en_new

            f0_pred, n_pred = model.predictor.F0Ntrain(en, s)

            asr = t_en @ pred_aln_trg.unsqueeze(0).to(device)
            if model_params.decoder.type == "hifigan":
                asr_new = torch.zeros_like(asr)
                asr_new[:, :, 0] = asr[:, :, 0]
                asr_new[:, :, 1:] = asr[:, :, 0:-1]
                asr = asr_new

            out = model.decoder(asr, f0_pred, n_pred, ref.squeeze().unsqueeze(0))

        return out.squeeze().cpu().numpy()[..., :-50]

    refs_root_candidates = _unique_resolved_paths(
        [
            base_dir,
            project_root,
            project_root.parent,
            invocation_cwd,
            invocation_cwd.parent,
        ]
    )
    refs_root = resolve_refs_root(
        explicit_root=args.refs_root,
        speaker=args.speaker,
        num_refs=args.num_refs,
        candidate_roots=refs_root_candidates,
    )
    speaker_dir = refs_root / str(args.speaker)

    ref_paths = sorted(speaker_dir.rglob("*.wav"))
    if len(ref_paths) < args.num_refs:
        raise ValueError(
            f"Expected at least {args.num_refs} wav files in {speaker_dir}, found {len(ref_paths)}"
        )
    ref_paths = ref_paths[: args.num_refs]

    print(f"Using device: {device}")
    print(f"Using speaker: {args.speaker}")
    print(f"References root: {refs_root}")
    print("Reference files:")
    for p in ref_paths:
        print(f"  - {p}")

    ref_styles = [compute_style(str(p)) for p in ref_paths]
    ref_s = torch.stack(ref_styles, dim=0).mean(dim=0)

    start = time.time()
    wav = inference(
        args.text,
        ref_s,
        alpha=infer_params["alpha"],
        beta=infer_params["beta"],
        diffusion_steps=infer_params["diffusion_steps"],
        embedding_scale=infer_params["embedding_scale"],
    )
    sample_rate = 24000
    elapsed = time.time() - start
    rtf = elapsed / (len(wav) / sample_rate)

    # Edit edges before saving:
    # 1) remove start/end trim, 2) apply fade-in/fade-out.
    trim_in_ms = args.trim_in_ms
    trim_out_ms = args.trim_out_ms
    fade_ms = 200
    if trim_in_ms < 0 or trim_out_ms < 0:
        raise ValueError(
            f"Trim values must be non-negative, got trim_in_ms={trim_in_ms}, trim_out_ms={trim_out_ms}."
        )

    trim_in_samples = int(sample_rate * (trim_in_ms / 1000.0))
    trim_out_samples = int(sample_rate * (trim_out_ms / 1000.0))
    fade_samples = int(sample_rate * (fade_ms / 1000.0))

    if len(wav) <= (trim_in_samples + trim_out_samples):
        raise ValueError(
            f"Generated waveform too short for trimming: {len(wav)} samples, "
            f"need more than {trim_in_samples + trim_out_samples}."
        )

    trim_end = len(wav) - trim_out_samples if trim_out_samples > 0 else len(wav)
    wav = wav[trim_in_samples:trim_end].copy()

    fade_samples = min(fade_samples, len(wav) // 2)
    if fade_samples > 0:
        fade_in = np.linspace(0.0, 1.0, fade_samples, dtype=wav.dtype)
        fade_out = np.linspace(1.0, 0.0, fade_samples, dtype=wav.dtype)
        wav[:fade_samples] *= fade_in
        wav[-fade_samples:] *= fade_out

    output_path = args.output if args.output is not None else (project_root / f"synth_speaker_{args.speaker}.m4a")
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() not in {".m4a", ".aac"}:
        output_path = output_path.with_suffix(".m4a")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Final AAC compression (128 kbps) to m4a.
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        raise RuntimeError("ffmpeg is required for AAC compression step but was not found in PATH.")

    with tempfile.TemporaryDirectory(prefix="styletts2_aac_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        wav_pre_path = tmpdir_path / "pre_aac.wav"

        sf.write(wav_pre_path, wav, sample_rate)

        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(wav_pre_path),
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(output_path),
            ],
            check=True,
        )

    print(f"Preset: {args.preset} -> {infer_params}")
    print(f"RTF: {rtf:.5f}")
    print("Compression: AAC 128 kbps (final m4a output)")
    print(
        f"Post-process: trim_in={trim_in_ms}ms, trim_out={trim_out_ms}ms, "
        f"fade={fade_ms}ms each side"
    )
    print(f"Saved: {output_path}")
