# asr_engine.py — RNNT beam (bez timestampów modeli) + chunking obwiednią + SRT-like w "words"

import os, hashlib, tempfile, argparse, json
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple

import math
import torch, torchaudio, soundfile as sf
import numpy as np
import nemo.collections.asr as nemo_asr

@dataclass
class ASRConfig:
    model_path: str = os.getenv("MODEL_PATH", "models/epoch6-step4571_CAPS_WER8.nemo")
    target_sr: int = int(os.getenv("TARGET_SR", "16000"))
    beam_size: int = int(os.getenv("BEAM_SIZE", "8"))
    max_symbols_per_step: int = int(os.getenv("MAX_SYMBOLS_PER_STEP", "32"))
    enable_gpu: bool = torch.cuda.is_available()
    # parametry chunkowania
    min_chunk_s: float = float(os.getenv("MIN_CHUNK_S", "15"))
    max_chunk_s: float = float(os.getenv("MAX_CHUNK_S", "25"))
    env_win_ms: float = float(os.getenv("ENV_WIN_MS", "50"))     # okno do obwiedni (RMS/MA)
    silence_q: float = float(os.getenv("SILENCE_Q", "0.25"))     # próg ciszy = quantile(env)*factor
    silence_factor: float = float(os.getenv("SILENCE_FACTOR", "1.0"))  # mnożnik progu

class ASREngine:
    """
    Jednolity silnik transkrypcji z chunkowaniem po obwiedni i SRT-like 'words'.
    """
    def __init__(self, cfg: ASRConfig):
        self.cfg = cfg
        self.device = torch.device("cuda" if (cfg.enable_gpu and torch.cuda.is_available()) else "cpu")
        self.model = None
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        torch.set_num_threads(max(1, (os.cpu_count() or 4)//2))

    @classmethod
    def from_env(cls) -> "ASREngine":
        return cls(ASRConfig())

    def load(self) -> "ASREngine":
        if not os.path.exists(self.cfg.model_path):
            raise FileNotFoundError(f"Brak modelu: {self.cfg.model_path}")
        self.model = nemo_asr.models.EncDecHybridRNNTCTCBPEModel.restore_from(self.cfg.model_path)
        self.model.eval()
        if self.device.type == "cuda":
            self.model = self.model.to(self.device)
        self._set_rnnt_beam_decoding()

        # warmup: 0.2 s szumu zerowego
        dummy = torch.zeros(1, int(self.cfg.target_sr * 0.2))
        fd, p = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        sf.write(p, dummy.squeeze(0).numpy(), self.cfg.target_sr)
        try:
            _ = self._rnnt_transcribe(p)
        finally:
            try: os.remove(p)
            except OSError: pass
        return self

    # ========= API GŁÓWNE =========
    def transcribe(self, audio_path: str) -> Dict[str, Any]:
        wav_path, duration_s, sha256 = self._ensure_mono_16k(audio_path)
        try:
            # 1) Wczytaj pełne audio (16k mono) do pamięci
            wav, sr = torchaudio.load(wav_path)  # [1, N]
            wav = wav.squeeze(0)
            assert sr == self.cfg.target_sr

            # 2) Wyznacz granice chunków po obwiedni
            chunks = self._chunk_by_envelope(wav, sr)

            # 3) Transkrypcja chunków osobno
            full_text_parts: List[str] = []
            srt_like: List[Dict[str, Any]] = []
            tmp_files: List[str] = []
            try:
                for idx, (s0, s1) in enumerate(chunks, start=1):
                    seg = wav[s0:s1].unsqueeze(0).cpu().numpy()
                    fd, cpath = tempfile.mkstemp(suffix=".wav"); os.close(fd)
                    sf.write(cpath, seg[0], sr)
                    tmp_files.append(cpath)

                    text_i = self._rnnt_transcribe(cpath).strip()
                    if not text_i:
                        # brak rozpoznanego tekstu – nie generujemy pustego wpisu SRT
                        continue

                    full_text_parts.append(text_i)
                    start_s = s0 / sr
                    end_s = s1 / sr
                    srt_like.append({
                        "index": idx,
                        "start": self._format_srt_time(start_s),
                        "end": self._format_srt_time(end_s),
                        "text": text_i,
                    })
            finally:
                for p in tmp_files:
                    try: os.remove(p)
                    except OSError: pass

            return {
                "text": " ".join(t for t in full_text_parts if t),
                "words": srt_like,  # SRT-like segmenty
                "meta": {
                    "duration_s": round(duration_s, 6),
                    "sha256": sha256,
                    "model_id": os.path.basename(self.cfg.model_path),
                    "device": self.device.type,
                    "timestamps_source": "srt-like-from-envelope",
                },
            }
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    # ========= POMOCNICZE =========
    def _ensure_mono_16k(self, in_path: str) -> Tuple[str, float, str]:
        with open(in_path, "rb") as f:
            data = f.read()
        sha256 = hashlib.sha256(data).hexdigest()

        wav, sr = torchaudio.load(in_path)
        if wav.dim() == 2 and wav.size(0) > 1:
            wav = wav.mean(dim=0, keepdim=True)
        if sr != self.cfg.target_sr:
            wav = torchaudio.transforms.Resample(sr, self.cfg.target_sr)(wav)
        wav = wav.squeeze(0)

        duration_s = float(wav.shape[0]) / self.cfg.target_sr
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        sf.write(tmp_wav, wav.cpu().numpy(), self.cfg.target_sr)
        return tmp_wav, duration_s, sha256

    def _set_rnnt_beam_decoding(self):
        base = dict(
            strategy="beam",
            beam_size=self.cfg.beam_size,
            max_symbols_per_step=self.cfg.max_symbols_per_step,
            preserve_alignments=False,  # nadal bez alignments z modelu
        )
        try:
            from nemo.collections.asr.parts.submodules.rnnt_decoding import RNNTDecodingConfig
            valid = RNNTDecodingConfig.__dataclass_fields__.keys()
            dec_cfg = RNNTDecodingConfig(**{k: v for k, v in base.items() if k in valid})
        except Exception:
            dec_cfg = base
        self.model.change_decoding_strategy(decoding_cfg=dec_cfg)

    def _rnnt_transcribe(self, wav_path: str) -> str:
        """
        Zwraca wyłącznie czysty tekst (bez żadnych repr obiektów).
        """
        try:
            # prefer: list[str]
            hyps = self.model.transcribe([wav_path], verbose=False, return_hypotheses=False)
            if isinstance(hyps, (list, tuple)) and len(hyps) > 0 and isinstance(hyps[0], str):
                return hyps[0].strip()
        except TypeError:
            # starsze warianty NeMo mogą nie mieć parametru return_hypotheses
            pass

        # bezpieczny fallback: hypotheses, ale bez użycia str(Hypothesis)
        hyps = self.model.transcribe([wav_path], verbose=False, return_hypotheses=True)
        hyp = hyps[0]
        for name in ("text", "transcript", "decoded_text"):
            val = getattr(hyp, name, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # jeśli nic sensownego – zwróć pusty string (NIGDY repr obiektu)
        return ""

    # --- Chunkowanie po obwiedni (RMS/MA) z cięciem w największej "ciszy" w oknie 15–25 s ---
    def _chunk_by_envelope(self, wav: torch.Tensor, sr: int) -> List[Tuple[int, int]]:
        N = wav.numel()
        if N == 0:
            return [(0,0)]
        # obwiednia (tu: RMS przez okno przesuwne)
        win = max(1, int(sr * (self.cfg.env_win_ms / 1000.0)))
        # szybkie RMS: konw z ones + sqrt(mean(x^2))
        x2 = wav.float().pow(2).numpy()
        ker = np.ones(win, dtype=np.float32) / float(win)
        rms = np.sqrt(np.convolve(x2, ker, mode="same") + 1e-12)

        # próg ciszy adaptacyjny
        base_thr = float(np.quantile(rms, self.cfg.silence_q))
        thr = base_thr * self.cfg.silence_factor

        # maska ciszy
        silence = rms <= thr

        # iteracyjne dzielenie 15–25 s, cięcie w najdłuższej lokalnej ciszy blisko celu
        chunks: List[Tuple[int, int]] = []
        min_len = int(self.cfg.min_chunk_s * sr)
        max_len = int(self.cfg.max_chunk_s * sr)
        pos = 0
        while pos < N:
            remaining = N - pos
            if remaining <= max_len:
                chunks.append((pos, N))
                break

            # przedział kandydacki: [pos+min_len, pos+max_len]
            a = pos + min_len
            b = min(pos + max_len, N-1)
            if a >= b:
                chunks.append((pos, min(pos + max_len, N)))
                pos = chunks[-1][1]
                continue

            # znajdź najdłuższy „ciąg ciszy” w [a, b] (preferuj blisko środka)
            slab = silence[a:b]
            if not slab.any():
                # brak ciszy: tnij twardo na b
                cut = b
            else:
                # segmenty ciszy (run-length)
                best_len, best_mid, best_lo, best_hi = -1, -1, -1, -1
                i = 0
                mid_target = (a + b) // 2
                while i < slab.size:
                    if slab[i]:
                        j = i
                        while j < slab.size and slab[j]:
                            j += 1
                        lo = a + i
                        hi = a + j - 1
                        seg_len = hi - lo + 1
                        # heurystyka: maksymalna cisza, a przy remisie bliżej środka
                        mid = (lo + hi) // 2
                        score = (seg_len, -abs(mid - mid_target))
                        if seg_len > best_len or (seg_len == best_len and abs(mid - mid_target) < abs(best_mid - mid_target)):
                            best_len, best_mid, best_lo, best_hi = seg_len, mid, lo, hi
                        i = j
                    else:
                        i += 1
                cut = best_mid if best_mid >= 0 else b

            chunks.append((pos, cut))
            pos = cut

        # sanity: rosnące, bez nakładania
        fixed: List[Tuple[int, int]] = []
        last_end = 0
        for s0, s1 in chunks:
            s0 = max(last_end, min(s0, N))
            s1 = max(s0+1, min(s1, N))
            fixed.append((s0, s1))
            last_end = s1
        return fixed

    @staticmethod
    def _format_srt_time(t: float) -> str:
        # HH:MM:SS,mmm
        if t < 0: t = 0.0
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t - math.floor(t)) * 1000.0))
        if ms == 1000:
            s += 1; ms = 0
        if s == 60:
            m += 1; s = 0
        if m == 60:
            h += 1; m = 0
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

# CLI
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ASR Engine CLI")
    parser.add_argument("--audio", required=True, help="ścieżka do pliku audio")
    parser.add_argument("--txt", help="opcjonalnie: zapisz tekst do pliku")
    parser.add_argument("--json", help="opcjonalnie: zapisz text+words+meta do JSON")
    args = parser.parse_args()

    eng = ASREngine.from_env().load()
    out = eng.transcribe(args.audio)
    print(out["text"])
    if args.txt:
        with open(args.txt, "w", encoding="utf-8") as f:
            f.write(out["text"] + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)