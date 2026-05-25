# Data License and Data Governance

Unless a separate written license says otherwise, data committed to this
repository is proprietary and all rights are reserved.

This includes:

- documentation authored for this repository,
- morphology structure JSON files under `morphology_structure_pl_lem_eng/`,
- scripts and configuration files,
- examples authored specifically for this repository.

The root `LICENSE` does not grant rights to private runtime data, model files,
voice recordings, database dumps, logs, exports, or generated artifacts.

## Data Not Included in Git

The following data categories are intentionally ignored and should not be
committed:

- `logs/`
- `transcripted_source/`
- `transkrypcje/`
- `exports/`
- `psql_dump/`
- `qdrant_data/`
- `qdrant_snapshots/`
- `vocab_json/`
- `models/`
- `term_*.json`
- audio files such as `*.wav`, `*.mp3`, `*.m4a`
- CSV/JSONL logs such as `*.csv`, `*.jsonl`
- binary model/vector files such as `*.bin`, `*.nemo`, `*.pt`, `*.onnx`

## Dictionary Data

The API and CLI tools query dictionary content from PostgreSQL tables such as:

- `public.terms`
- `public.term_word_associations`
- `public.sources`
- `public.users`

Database contents may include linguistic examples, source references,
translations, semantic descriptions, and user/admin metadata. They are not
licensed by this repository unless a separate data license explicitly says so.

Before sharing exports or dumps, verify:

- source rights for dictionary entries and examples,
- whether context examples contain personal data,
- whether user records or owner fields are included,
- whether `redacted` and `deleted` flags are respected,
- whether the target recipient has permission to receive the data.

## Generated Data

Generated artifacts include:

- uploaded audio archives,
- ASR transcript JSON files,
- CSV logs,
- JSONL operational logs,
- TTS output audio,
- term export JSON files.

Generated data may contain personal data or protected voice/linguistic content.
Do not redistribute generated artifacts without a lawful basis, appropriate
notice, and consent where required.

## Retention

The application currently writes data to disk but does not enforce automatic
retention or deletion. Operators must implement retention externally, for
example with scheduled deletion or archival jobs.

Recommended default retention for public deployments:

| Data category | Suggested retention |
| --- | --- |
| Uploaded source audio | 7-30 days, or shorter if not needed. |
| ASR transcripts | 30-90 days, unless users request deletion earlier. |
| API CSV/JSONL logs | 30-90 days. |
| TTS request logs | 30-90 days. |
| Generated TTS audio temp files | Deleted immediately after response by API. |
| Database backups | According to legal/business requirements, encrypted. |

Document the actual deployed retention in `PRIVACY.md` or an operator-specific
policy before public use.

## User Rights

If deployed for users, provide a channel for:

- access requests,
- correction requests,
- deletion requests,
- withdrawal of consent for TTS/voice cloning,
- objections to external processing,
- questions about data sources.

The current codebase does not implement these workflows automatically.
