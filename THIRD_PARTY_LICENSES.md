# Third-Party Licenses

This project depends on third-party software and services. This file is a
human-maintained inventory, not a substitute for the license metadata shipped
with each package, model, operating-system package, or external API.

Before redistribution or public hosting, verify the exact licenses of installed
artifacts in the target build environment.

## Python Dependencies

The production Python dependency list is maintained in
`requirements-prod.txt`.

Key packages and ecosystems:

| Component | Purpose | License/terms source to verify |
| --- | --- | --- |
| `torch`, `torchaudio` | PyTorch runtime and audio I/O | PyTorch package metadata and PyTorch license. |
| `fastapi`, `starlette`, `uvicorn`, `pydantic` | HTTP API stack | Package metadata on PyPI/source repositories. |
| `python-multipart` | Multipart upload parsing | Package metadata on PyPI/source repository. |
| `pandas`, `numpy`, `scikit-learn`, `numba`, `llvmlite` | Data/scientific runtime | Package metadata on PyPI/source repositories. |
| `nemo-toolkit`, `lhotse`, `hydra-core`, `omegaconf`, `lightning`, `pytorch-lightning` | ASR/model runtime | Package metadata and upstream project licenses. |
| `transformers`, `datasets`, `sentencepiece` | Model utilities | Package metadata and Hugging Face/project licenses. |
| `soundfile`, `librosa`, `phonemizer`, `nltk` | Audio processing and phonemization | Package metadata and upstream project licenses. |
| `psycopg2-binary` | PostgreSQL client | Package metadata and upstream project license. |
| `qdrant-client` | Qdrant client library | Package metadata and upstream project license. |
| `fasttext`, `rapidfuzz`, `python-Levenshtein` | Similarity and suggestion tooling | Package metadata and upstream project licenses. |
| `openai` | OpenAI API client | OpenAI package license and API terms. |
| `munch`, `editdistance`, `webdataset`, `einops`, `einops-exts`, `jiwer`, `IPython` | Utility/runtime dependencies | Package metadata and upstream project licenses. |

Recommended license inventory command in an isolated environment:

```bash
python -m pip install pip-licenses
pip-licenses --from=mixed --with-license-file --format=markdown
```

## System Packages

The Docker image installs Debian packages including:

- `build-essential`
- `libsndfile1`
- `ffmpeg`
- `git`
- `libgomp1`
- `espeak-ng`
- `espeak-ng-espeak`

Their license notices are provided by the Debian packages installed into the
container image. For distribution of a built image, preserve the package
notices under `/usr/share/doc` and verify any codec-related restrictions for
`ffmpeg`.

## External Services

The translation and fallback lemmatization flows can call the OpenAI API.
OpenAI API usage is governed by OpenAI's applicable service terms and data
processing terms. Do not send personal data or protected linguistic/community
data to external services unless you have a lawful basis and user notice.

## Models and Data

This repository references model files and datasets that are not committed to
Git:

- NeMo ASR `.nemo` checkpoints.
- FastText `.bin` files.
- StyleTTS2 code, checkpoints, PLBERT, F0 and ASR helper weights.
- Speaker reference `.wav` files.
- PostgreSQL dumps and dictionary content.
- Vocab JSON files.

These assets are not licensed by `LICENSE` unless explicitly stated in a
separate written license. See `MODELS.md` and `DATA_LICENSE.md`.

## Security Audit

Dependency vulnerabilities are tracked separately from licensing. Use:

```bash
pip-audit -r requirements-prod.txt
```

and review Dependabot alerts after pushing this repository to GitHub.
