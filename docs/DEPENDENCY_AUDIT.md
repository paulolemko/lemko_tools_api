# Dependency Audit

Last local audit date: 2026-05-25.

Tooling:

- `pip-audit`
- PyPI vulnerability service
- OSV vulnerability service

## Commands Run

Direct audit of `requirements-prod.txt` was attempted first:

```bash
pip-audit -r requirements-prod.txt
```

On this macOS workstation it could not resolve `torch==2.3.1+cpu` because the
local Python/platform resolver did not find the CPU wheel variant.

Then an exact-pinned temporary requirements file was generated from
`requirements-prod.txt`, excluding unpinned entries such as `openai>=1.40.0`
and `IPython`, and audited without dependency resolution:

```bash
pip-audit -r /tmp/lemko-audit-req.XXXXXX.txt --no-deps --disable-pip
pip-audit -r /tmp/lemko-audit-req.XXXXXX.txt --no-deps --disable-pip -s osv
```

Limitations:

- transitive dependencies were not audited in the `--no-deps` run,
- unpinned direct dependencies were not audited,
- the exact PyTorch CPU local-version package was not auditable through the
  PyPI service, but was reported by the OSV service,
- results are time-sensitive and must be regenerated for each production build.

## OSV Service Result

The OSV-backed run found 42 known vulnerabilities in 7 packages:

| Package | Version | Advisory IDs reported | Fix versions reported |
| --- | --- | --- | --- |
| `torch` | `2.3.1+cpu` | `PYSEC-2025-191`, `PYSEC-2025-41`, `PYSEC-2024-259`, `PYSEC-2025-198`, `PYSEC-2025-203`, `PYSEC-2025-204`, `PYSEC-2025-205`, `PYSEC-2025-206`, `PYSEC-2025-207`, `PYSEC-2025-208`, `PYSEC-2025-209`, `PYSEC-2026-139`, `CVE-2025-3730` | mixed: `2.5.0`, `2.6.0`, `2.7.0`, `2.7.1`, `2.7.1rc1`, `2.8.0`, `2.9.0`; some advisories had no fix in output |
| `python-multipart` | `0.0.20` | `CVE-2026-24486`, `CVE-2026-40347`, `CVE-2026-42561` | `0.0.22`, `0.0.26`, `0.0.27` |
| `starlette` | `0.47.3` | `PYSEC-2026-161`, `CVE-2025-62727` | `1.0.1`, `0.49.1` |
| `nemo-toolkit` | `2.4.0` | `CVE-2025-33245`, `CVE-2025-33253`, `CVE-2026-24157`, `CVE-2026-24159` | `2.6.1`, `2.6.2` |
| `pytorch-lightning` | `2.4.0` | `CVE-2026-31221` | none reported |
| `transformers` | `4.56.0` | `PYSEC-2025-214`, `PYSEC-2025-215`, `PYSEC-2025-216`, `PYSEC-2025-217`, `PYSEC-2025-218`, `CVE-2026-1839` | `5.0.0rc3` for `CVE-2026-1839`; none reported for several PYSEC IDs |
| `nltk` | `3.9.1` | `PYSEC-2026-96`, `PYSEC-2026-97`, `PYSEC-2026-98`, `PYSEC-2026-99`, `CVE-2026-33230`, `CVE-2026-33231`, `CVE-2026-33236`, `GHSA-rf74-v2fm-23pw` | `3.9.3`, `3.9.4`; some advisories had no fix in output |

The PyPI-backed run found 23 known vulnerabilities in 6 packages and skipped
`torch`/`torchaudio` because the local-version CPU packages were not found on
PyPI by that audit mode.

## Recommended Remediation Plan

1. Upgrade the multipart/API stack together, not package by package:
   - `python-multipart >= 0.0.27`,
   - `starlette >= 0.49.1` or a FastAPI release that supports a fixed
     Starlette version,
   - latest compatible `fastapi`.

2. Treat the ML stack as a compatibility migration:
   - test PyTorch/torchaudio upgrade against NeMo,
   - test NeMo upgrade to at least `2.6.2`,
   - test `transformers` upgrade path; `5.0.0rc3` may be a breaking release,
   - test `nltk >= 3.9.4`.

3. Replace unpinned requirements with pinned versions and hashes:
   - pin `openai`,
   - pin `IPython` or remove it from production requirements,
   - generate a locked requirements file with `pip-compile --generate-hashes`
     or an equivalent tool.

4. Re-run:

```bash
pip-audit -r requirements-prod.txt
pip-audit -r requirements-prod.txt -s osv
python3 -m py_compile scripts/*.py
docker compose build asr
```

5. Run ASR, dictionary, translation, and TTS smoke tests after upgrades.

## CI

This repo now includes:

- `.github/dependabot.yml`
- `.github/workflows/pip-audit.yml`

Dependabot opens dependency PRs only after the repository is pushed to GitHub.
The GitHub Actions workflow runs `pip-audit -r requirements-prod.txt` on PRs,
pushes to `main`/`master`, weekly schedule, and manual dispatch.
