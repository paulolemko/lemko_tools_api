# Model Governance

This repository references several machine-learning model families. The model
weights are not committed to Git and are intentionally ignored by `.gitignore`
and `.dockerignore`.

## Referenced Model Assets

### ASR

Default path:

```text
models/epoch6-step4571_CAPS_WER8.nemo
```

Container path:

```text
/app/models/epoch6-step4571_CAPS_WER8.nemo
```

Runtime:

- `scripts/asr_engine.py`
- NeMo `EncDecHybridRNNTCTCBPEModel`
- configured by `MODEL_PATH`

Before production use, document:

- model owner,
- training data sources,
- consent and data protection basis for training data,
- evaluation results and known limitations,
- license or written permission to deploy,
- whether output may contain personal data,
- retention policy for input audio and generated transcripts.

### FastText

Default filenames:

```text
cc.pl.300.bin
cc.en.300.bin
ft_words.bin
```

Runtime:

- `scripts/fasttext2lemtools.py`
- dictionary fallback suggestions for Polish, English, and Lemko.

Before redistribution, verify the license for each `.bin` file. Public
FastText models and custom trained models may have different licensing and
attribution requirements.

### StyleTTS2

Expected default container root:

```text
/app/models/StyleTTS2
```

Required files and directories:

```text
models.py
Modules/
Utils/ASR/config.yml
Utils/ASR/epoch_00080.pth
Utils/JDC/bst.t7
Utils/PLBERT_multi/
Models/lemko_finetune/epoch_2nd_00044_nmndt_p2.pth
0/**/*.wav
1/**/*.wav
```

Runtime:

- `scripts/styletts2_engine.py`
- `scripts/synthesize_from_speaker.py`
- `POST /v1/tts`

Voice cloning and speaker adaptation can be sensitive. Do not deploy TTS with
speaker reference files unless all of the following are documented:

- identity or category of each speaker,
- explicit consent for synthesis or cloning,
- permitted purpose and audience,
- retention period for reference recordings,
- process for withdrawing consent,
- restrictions on generating impersonation, deception, harassment, or other
  harmful content.

## Model Files Are Not Covered by the Repository License

The root `LICENSE` covers only repository content for which the repository
owner has the right to license. Model weights, checkpoints, vocabularies, and
speaker references may be owned by third parties or governed by separate
agreements.

Do not assume that placing a model under `models/` grants permission to:

- redistribute it,
- serve it publicly,
- fine-tune it,
- use it commercially,
- use it for voice cloning,
- use it with personal data.

## Production Checklist

- `MODEL_PATH` points to the intended ASR checkpoint.
- `STYLE_TTS2_DIR` and `STYLE_TTS2_REFS_ROOT` point to approved TTS assets.
- FastText model sources are recorded.
- Model licenses and attribution are stored with deployment records.
- Training data provenance is documented.
- Bias, error modes, and language limitations are documented for users.
- Human review is available for high-impact uses.
- Input/output retention is configured according to `PRIVACY.md`.

## Known Technical Limitations

- ASR timestamps are SRT-like chunks from audio envelope segmentation, not
  word-level alignments from the model.
- ASR job state is stored in process memory and is lost on restart.
- TTS loads lazily on the first request and can be slow on cold start.
- TTS synthesis is internally serialized by a lock in `StyleTTS2Engine`.
- FastText suggestions are heuristic and should not be treated as authoritative
  dictionary decisions.
