#!/usr/bin/env python3
"""In-memory StyleTTS2 synthesizer shared by CLI and API."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import numpy as np
import torch
import torchaudio
from munch import Munch

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

# Seeds for reproducibility similar to notebook environment.
torch.manual_seed(0)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True
random.seed(0)
np.random.seed(0)

ASR_MODEL_CONFIG = Path("Utils/ASR/config.yml")
ASR_MODEL_PATH = Path("Utils/ASR/epoch_00080.pth")
F0_MODEL_PATH = Path("Utils/JDC/bst.t7")
PLBERT_DIR = Path("Utils/PLBERT_multi/")
PRETRAINED_MODEL = Path("Models/lemko_finetune/epoch_2nd_00044_nmndt_p2.pth")

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

PRESETS = {
    "default": {"alpha": 0.3, "beta": 0.7, "diffusion_steps": 10, "embedding_scale": 1.0},
    "less": {"alpha": 0.1, "beta": 0.3, "diffusion_steps": 10, "embedding_scale": 1.0},
    "more": {"alpha": 0.5, "beta": 0.95, "diffusion_steps": 10, "embedding_scale": 1.0},
}


def recursive_munch(data):
    if isinstance(data, dict):
        return Munch((k, recursive_munch(v)) for k, v in data.items())
    if isinstance(data, list):
        return [recursive_munch(v) for v in data]
    return data


def resolve_styletts2_dirs(explicit_dir: Path | None = None) -> Tuple[Path, Path]:
    """Return (styletts2_dir, project_root_dir)."""

    def _validate(candidate: Path) -> bool:
        return (candidate / "models.py").is_file() and (candidate / "Modules").is_dir()

    env_dir = os.getenv("STYLE_TTS2_DIR") if explicit_dir is None else None
    if explicit_dir:
        base = explicit_dir.expanduser().resolve()
        if not _validate(base):
            raise FileNotFoundError(f"Provided StyleTTS2 dir {base} is invalid")
        project = base if (base / "0").is_dir() or (base / "1").is_dir() else base.parent
        return base, project
    if env_dir:
        base = Path(env_dir).expanduser().resolve()
        if not _validate(base):
            raise FileNotFoundError(f"STYLE_TTS2_DIR={base} is invalid")
        project = base if (base / "0").is_dir() or (base / "1").is_dir() else base.parent
        return base, project

    start = Path(__file__).resolve().parent
    candidates = [start] + list(start.parents)
    extra = [start.parent / "models" / "StyleTTS2"]

    for candidate in list(dict.fromkeys(candidates + extra)):
        direct = candidate / "StyleTTS2" if (candidate / "StyleTTS2").is_dir() else candidate
        if _validate(direct):
            project = direct if (direct / "0").is_dir() or (direct / "1").is_dir() else direct.parent
            return direct, project

    searched = "\n".join(str(p) for p in candidates + extra)
    raise FileNotFoundError("Cannot find StyleTTS2 directory; searched:\n" + searched)


@dataclass
class SynthesisResult:
    output_path: Path
    elapsed_s: float
    duration_s: float
    sample_rate: int
    rtf: float
    preset: str
    speaker: int
    text: str
    refs_used: List[Path]


class StyleTTS2Engine:
    def __init__(self, base_dir: Path | None = None, refs_root: Path | None = None, device: str | None = None):
        self.base_dir, self.project_root = resolve_styletts2_dirs(base_dir)
        default_refs = self.project_root if (self.project_root / "0").is_dir() else self.base_dir
        self.refs_root = Path(refs_root).expanduser().resolve() if refs_root else default_refs
        if not self.refs_root.exists():
            raise FileNotFoundError(f"Reference directory not found: {self.refs_root}")
        self.device = device or self._detect_device()
        self._ffmpeg = shutil.which("ffmpeg")
        if self._ffmpeg is None:
            raise RuntimeError("ffmpeg binary is required but was not found in PATH")
        self._prepare_sys_path()
        self._import_modules()
        self._load_models()
        self._style_cache: dict[tuple[str, ...], torch.Tensor] = {}
        self._synth_lock = threading.Lock()

    def _prepare_sys_path(self) -> None:
        if str(self.base_dir) not in sys.path:
            sys.path.insert(0, str(self.base_dir))

    def _import_modules(self) -> None:
        import librosa  # pylint: disable=import-outside-toplevel
        import soundfile as sf  # pylint: disable=import-outside-toplevel
        from nltk.tokenize import word_tokenize  # pylint: disable=import-outside-toplevel
        from models import build_model, load_ASR_models, load_F0_models  # pylint: disable=import-outside-toplevel
        from text_utils import TextCleaner  # pylint: disable=import-outside-toplevel
        from Utils.PLBERT.util import load_plbert  # pylint: disable=import-outside-toplevel
        from Modules.diffusion.sampler import (  # pylint: disable=import-outside-toplevel
            DiffusionSampler,
            ADPM2Sampler,
            KarrasSchedule,
        )

        self.librosa = librosa
        self.sf = sf
        self.word_tokenize = word_tokenize
        self.build_model = build_model
        self.load_ASR_models = load_ASR_models
        self.load_F0_models = load_F0_models
        self.TextCleaner = TextCleaner
        self.load_plbert = load_plbert
        self.DiffusionSampler = DiffusionSampler
        self.ADPM2Sampler = ADPM2Sampler
        self.KarrasSchedule = KarrasSchedule

    def _load_models(self) -> None:
        self.textcleaner = self.TextCleaner()
        asr_model_path = str((self.base_dir / ASR_MODEL_PATH).resolve())
        asr_config_path = str((self.base_dir / ASR_MODEL_CONFIG).resolve())
        f0_model_path = str((self.base_dir / F0_MODEL_PATH).resolve())
        plbert_dir = str((self.base_dir / PLBERT_DIR).resolve())
        pretrained_model_path = (self.base_dir / PRETRAINED_MODEL).resolve()

        self.text_aligner = self.load_ASR_models(asr_model_path, asr_config_path)
        self.pitch_extractor = self.load_F0_models(f0_model_path)
        self.plbert = self.load_plbert(plbert_dir)

        model_params = recursive_munch(MODEL_PARAMS)
        self.model = self.build_model(model_params, self.text_aligner, self.pitch_extractor, self.plbert)
        for key in self.model:
            self.model[key].eval()
            self.model[key].to(self.device)

        params_whole = torch.load(pretrained_model_path, map_location="cpu")
        params = params_whole["net"]
        for key in self.model:
            if key in params:
                try:
                    self.model[key].load_state_dict(params[key])
                except Exception:
                    from collections import OrderedDict  # pylint: disable=import-outside-toplevel

                    state_dict = params[key]
                    new_state_dict = OrderedDict()
                    for k, v in state_dict.items():
                        name = k[7:]
                        new_state_dict[name] = v
                    self.model[key].load_state_dict(new_state_dict, strict=False)
            self.model[key].eval()

        self.sampler = self.DiffusionSampler(
            self.model.diffusion.diffusion,
            sampler=self.ADPM2Sampler(),
            sigma_schedule=self.KarrasSchedule(sigma_min=0.0001, sigma_max=3.0, rho=9.0),
            clamp=False,
        )
        self.to_mel = torchaudio.transforms.MelSpectrogram(
            n_mels=80, n_fft=2048, win_length=1200, hop_length=300
        )
        self.mean = -4
        self.std = 4
        self.phoneme_rules = build_rules(RAW_RULES)
        self.phoneme_rule_index = build_rule_index(self.phoneme_rules)
        phoneme_langs = {LANG_PL, LANG_UK, LANG_BG}
        pl_code = os.getenv("STYLE_TTS2_PHONEMIZER_PL", "pl")
        uk_code = os.getenv("STYLE_TTS2_PHONEMIZER_UK", "uk")
        bg_code = os.getenv("STYLE_TTS2_PHONEMIZER_BG", "bg")
        self.phoneme_backends = build_backends(
            phoneme_langs,
            pl_code=pl_code,
            uk_code=uk_code,
            bg_code=bg_code,
        )

    def _detect_device(self) -> str:
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def synthesize_to_file(
        self,
        *,
        text: str,
        speaker: int,
        preset: str = "default",
        num_refs: int = 3,
        trim_in_ms: int = 0,
        trim_out_ms: int = 200,
        output_path: Path,
    ) -> SynthesisResult:
        with self._synth_lock:
            wav, sample_rate, elapsed, duration, refs_used = self._synthesize_waveform(
                text=text,
                speaker=speaker,
                preset=preset,
                num_refs=num_refs,
                trim_in_ms=trim_in_ms,
                trim_out_ms=trim_out_ms,
            )
            output_path = output_path.expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_m4a(wav, sample_rate, output_path)
        rtf = elapsed / duration if duration > 0 else 0.0
        return SynthesisResult(
            output_path=output_path,
            elapsed_s=elapsed,
            duration_s=duration,
            sample_rate=sample_rate,
            rtf=rtf,
            preset=preset,
            speaker=speaker,
            text=text,
            refs_used=refs_used,
        )

    def _synthesize_waveform(
        self,
        *,
        text: str,
        speaker: int,
        preset: str,
        num_refs: int,
        trim_in_ms: int,
        trim_out_ms: int,
    ) -> Tuple[np.ndarray, int, float, float, List[Path]]:
        preset_params = PRESETS.get(preset)
        if not preset_params:
            raise ValueError(f"Unknown preset: {preset}")
        if speaker not in (0, 1):
            raise ValueError("Speaker must be 0 or 1")
        if num_refs < 1:
            raise ValueError("num_refs must be >= 1")
        refs = self._select_references(speaker, num_refs)
        ref_style = self._build_style_embedding(tuple(refs))
        start = time.time()
        wav = self._inference(text, ref_style, preset_params)
        elapsed = time.time() - start
        wav = self._post_process(wav, trim_in_ms=trim_in_ms, trim_out_ms=trim_out_ms)
        sample_rate = 24000
        duration = len(wav) / sample_rate
        return wav, sample_rate, elapsed, duration, refs

    def _select_references(self, speaker: int, num_refs: int) -> List[Path]:
        speaker_dir = self.refs_root / str(speaker)
        if not speaker_dir.is_dir():
            raise FileNotFoundError(f"Speaker directory not found: {speaker_dir}")
        ref_paths = sorted(p for p in speaker_dir.rglob("*.wav"))
        if len(ref_paths) < num_refs:
            raise ValueError(
                f"Expected at least {num_refs} reference wav files in {speaker_dir}, found {len(ref_paths)}"
            )
        return ref_paths[:num_refs]

    def _build_style_embedding(self, ref_paths: Sequence[Path]) -> torch.Tensor:
        cache_key = tuple(str(p) for p in ref_paths)
        cached = self._style_cache.get(cache_key)
        if cached is not None:
            return cached
        tensors = [self._compute_style(str(path)) for path in ref_paths]
        ref_tensor = torch.stack(tensors, dim=0).mean(dim=0)
        self._style_cache[cache_key] = ref_tensor
        return ref_tensor

    def _compute_style(self, path: str) -> torch.Tensor:
        wave, sr = self.librosa.load(path, sr=24000)
        audio, _ = self.librosa.effects.trim(wave, top_db=30)
        if sr != 24000:
            audio = self.librosa.resample(audio, sr, 24000)
        mel_tensor = self._preprocess(audio).to(self.device)
        with torch.no_grad():
            ref_s = self.model.style_encoder(mel_tensor.unsqueeze(1))
            ref_p = self.model.predictor_encoder(mel_tensor.unsqueeze(1))
        return torch.cat([ref_s, ref_p], dim=1)

    def _preprocess(self, wave: np.ndarray) -> torch.Tensor:
        wave_tensor = torch.from_numpy(wave).float()
        mel_tensor = self.to_mel(wave_tensor)
        mel_tensor = (torch.log(1e-5 + mel_tensor.unsqueeze(0)) - self.mean) / self.std
        return mel_tensor

    def _phonemize_for_synthesis(self, text: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            return ""
        chunks = split_with_rules(normalized, self.phoneme_rule_index)
        ipa = phonemize_chunks(chunks, self.phoneme_backends)
        return ipa.replace("ɨɯ", "ɯ").replace("iɡrɛkɯ", "ɯ")

    def _post_process(self, wav: np.ndarray, *, trim_in_ms: int, trim_out_ms: int) -> np.ndarray:
        sample_rate = 24000
        fade_ms = 100
        if trim_in_ms < 0 or trim_out_ms < 0:
            raise ValueError("Trim values must be non-negative")
        trim_in_samples = int(sample_rate * (trim_in_ms / 1000.0))
        trim_out_samples = int(sample_rate * (trim_out_ms / 1000.0))
        if len(wav) <= (trim_in_samples + trim_out_samples):
            raise ValueError("Generated waveform too short for requested trims")
        trim_end = len(wav) - trim_out_samples if trim_out_samples > 0 else len(wav)
        wav = wav[trim_in_samples:trim_end].copy()
        fade_samples = min(int(sample_rate * (fade_ms / 1000.0)), len(wav) // 2)
        if fade_samples > 0:
            fade_in = np.linspace(0.0, 1.0, fade_samples, dtype=wav.dtype)
            fade_out = np.linspace(1.0, 0.0, fade_samples, dtype=wav.dtype)
            wav[:fade_samples] *= fade_in
            wav[-fade_samples:] *= fade_out
        return wav

    def _inference(self, text: str, ref_s: torch.Tensor, preset_params: dict) -> np.ndarray:
        ipa = self._phonemize_for_synthesis(text)
        token_list = self.word_tokenize(ipa)
        token_string = " ".join(token_list)
        tokens = self.textcleaner(token_string)
        tokens.insert(0, 0)
        tokens = torch.LongTensor(tokens).to(self.device).unsqueeze(0)
        with torch.no_grad():
            input_lengths = torch.LongTensor([tokens.shape[-1]]).to(self.device)
            text_mask = self._length_to_mask(input_lengths).to(self.device)

            t_en = self.model.text_encoder(tokens, input_lengths, text_mask)
            bert_dur = self.model.bert(tokens, attention_mask=(~text_mask).int())
            d_en = self.model.bert_encoder(bert_dur).transpose(-1, -2)

            s_pred = self.sampler(
                noise=torch.randn((1, 256)).unsqueeze(1).to(self.device),
                embedding=bert_dur,
                embedding_scale=preset_params["embedding_scale"],
                features=ref_s,
                num_steps=preset_params["diffusion_steps"],
            ).squeeze(1)

            s = s_pred[:, 128:]
            ref = s_pred[:, :128]
            ref = preset_params["alpha"] * ref + (1 - preset_params["alpha"]) * ref_s[:, :128]
            s = preset_params["beta"] * s + (1 - preset_params["beta"]) * ref_s[:, 128:]

            d = self.model.predictor.text_encoder(d_en, s, input_lengths, text_mask)
            x, _ = self.model.predictor.lstm(d)
            duration = self.model.predictor.duration_proj(x)
            duration = torch.sigmoid(duration).sum(axis=-1)
            pred_dur = torch.round(duration.squeeze()).clamp(min=1)

            pred_aln_trg = torch.zeros(input_lengths, int(pred_dur.sum().data), device=self.device)
            c_frame = 0
            for i in range(pred_aln_trg.size(0)):
                pred_aln_trg[i, c_frame : c_frame + int(pred_dur[i].data)] = 1
                c_frame += int(pred_dur[i].data)

            en = d.transpose(-1, -2) @ pred_aln_trg.unsqueeze(0)
            if MODEL_PARAMS["decoder"]["type"] == "hifigan":
                en_new = torch.zeros_like(en)
                en_new[:, :, 0] = en[:, :, 0]
                en_new[:, :, 1:] = en[:, :, 0:-1]
                en = en_new

            f0_pred, n_pred = self.model.predictor.F0Ntrain(en, s)
            asr = t_en @ pred_aln_trg.unsqueeze(0)
            if MODEL_PARAMS["decoder"]["type"] == "hifigan":
                asr_new = torch.zeros_like(asr)
                asr_new[:, :, 0] = asr[:, :, 0]
                asr_new[:, :, 1:] = asr[:, :, 0:-1]
                asr = asr_new

            out = self.model.decoder(asr, f0_pred, n_pred, ref.squeeze().unsqueeze(0))
        return out.squeeze().cpu().numpy()[..., :-50]

    def _length_to_mask(self, lengths: torch.Tensor) -> torch.Tensor:
        mask = (
            torch.arange(lengths.max(), device=lengths.device)
            .unsqueeze(0)
            .expand(lengths.shape[0], -1)
            .type_as(lengths)
        )
        mask = torch.gt(mask + 1, lengths.unsqueeze(1))
        return mask

    def _write_m4a(self, wav: np.ndarray, sample_rate: int, output_path: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="styletts2_aac_") as tmpdir:
            wav_pre_path = Path(tmpdir) / "pre_aac.wav"
            self.sf.write(wav_pre_path, wav, sample_rate)
            subprocess.run(
                [
                    self._ffmpeg,
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


@lru_cache(maxsize=1)
def get_cached_engine() -> StyleTTS2Engine:
    return StyleTTS2Engine()
