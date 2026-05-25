# Privacy Notice Template

This file documents privacy-relevant behavior in the current codebase. It is a
technical template for operators and is not legal advice. A public deployment
should publish a jurisdiction-appropriate privacy notice before accepting user
data.

## Data Processed by the API

### ASR Transcription

Endpoint:

```text
POST /v1/transcriptions
```

Processed data:

- uploaded audio file,
- original filename,
- file size,
- approximate audio duration,
- generated transcript,
- generated segment timestamps,
- SHA256 hash of original audio.

Storage:

- source upload archived in `TRANSCRIPTED_SOURCE_DIR`,
- transcript artifact JSON written to `TRANS_DIR`,
- `TRANSCRIPTIONS_CSV_PATH` stores filename, timestamp, size and transcript
  text,
- `LOG_PATH` stores enqueue/done/error metadata.

### Dictionary Search

Endpoints:

```text
POST /v1/lemko/search
POST /v1/lemko/search/pl
POST /v1/lemko/search/en
```

Processed data:

- query text,
- language/endpoint,
- dictionary matches from PostgreSQL.

Storage:

- `LEM_SEARCH_LOG_PATH` stores timestamp, endpoint, query, and `found`/`failed`.

### Translation

Endpoint:

```text
POST /v1/lemko/translate/pl
```

Processed data:

- user-provided Lemko text,
- dictionary entries and contexts for unknown words,
- final Polish translation,
- model metadata.

External processing:

- text may be sent to OpenAI through the OpenAI Responses API,
- dictionary hints may be sent to OpenAI if the model requests dictionary
  assistance.

Storage:

- `LEM_TRANSLATE_LOG_PATH` stores timestamp, endpoint, query and result text.

### TTS and Voice Cloning

Endpoint:

```text
POST /v1/tts
```

Processed data:

- text to synthesize,
- selected speaker id,
- selected preset,
- speaker reference `.wav` files loaded from `STYLE_TTS2_REFS_ROOT`.

Storage:

- `LEM_TTS_LOG_PATH` stores timestamp, endpoint, speaker and text,
- `LOG_PATH` stores TTS event metadata such as speaker, preset, text length,
  RTF and generated duration,
- generated M4A files are written to a temporary directory and scheduled for
  deletion after response.

Voice cloning requirements:

- obtain explicit consent from every speaker represented by reference files,
- document permitted use,
- provide withdrawal and deletion procedures,
- prevent deceptive impersonation and harmful use.

## Log Fields

The current application logs the following fields.

| Destination | Fields |
| --- | --- |
| `LOG_PATH` | `event`, `job_id`, `filename`, `size`, `artifact_path`, `archived_path`, `error`, `speaker`, `preset`, `text_chars`, `rtf`, `duration_s`, `ts` |
| `TRANSCRIPTIONS_CSV_PATH` | `filename`, `timestamp`, `size_bytes`, `transcript_text` |
| `LEM_SEARCH_LOG_PATH` | `timestamp`, `endpoint`, `query`, `result` |
| `LEM_TRANSLATE_LOG_PATH` | `timestamp`, `endpoint`, `query`, `result_text` |
| `LEM_TTS_LOG_PATH` | `timestamp`, `endpoint`, `speaker`, `text` |

These logs can contain personal data, sensitive linguistic content, filenames,
voice content, and generated transcripts. Protect them accordingly.

## Retention

The current code initializes and writes logs, source audio archives and
transcript artifacts, but it does not automatically delete old data.

Operators must define and implement retention. Suggested defaults:

| Data | Suggested retention |
| --- | --- |
| Uploaded source audio | 7-30 days. |
| Transcript artifacts | 30-90 days. |
| CSV/JSONL logs | 30-90 days. |
| Translation and search logs | 30-90 days or less if queries may be sensitive. |
| TTS text logs | 30-90 days or less if requests may identify a person. |
| Speaker reference files | Only while consent and purpose remain valid. |
| Database backups | As required by operational policy, encrypted and access-controlled. |

## User Rights and Deletion

The codebase does not implement an automated user self-service portal. A public
operator should provide a contact channel and internal procedure for:

- access to stored audio/transcripts/logs,
- correction of inaccurate data,
- deletion of uploaded audio, transcripts, TTS text logs and search/translation
  logs where legally required,
- withdrawal of consent for speaker reference files,
- objections to external processing,
- explanation of OpenAI processing and model usage.

Because ASR job state is in memory, deletion by `job_id` after a restart may
require locating files by timestamp, filename, hash, or operator logs.

## OpenAI Processing

The translation flow can send user text and dictionary context to OpenAI. The
operator should disclose:

- which endpoint uses OpenAI,
- categories of data sent,
- purpose of processing,
- whether OpenAI retention/training settings apply to the account,
- how users can opt out if required,
- alternative path if external processing is not acceptable.

Do not send secrets, private database dumps, unnecessary personal data, or
speaker reference audio to OpenAI through this codebase.

## Consent for TTS and Speaker References

Before using any speaker reference directory:

1. Record who the speaker is or why the speaker is anonymous.
2. Record the source of the `.wav` files.
3. Obtain explicit consent for TTS or voice cloning.
4. State allowed uses and prohibited uses.
5. Define retention and deletion.
6. Provide withdrawal process.
7. Restrict access to raw voice recordings.

If consent is withdrawn, remove the speaker reference files, clear derived
caches by restarting the API process, and delete related logs where required.

## Minimization Recommendations

- Avoid logging full transcript text if not operationally necessary.
- Avoid logging full TTS input text in public deployments.
- Prefer opaque upload IDs over original filenames.
- Store logs on encrypted volumes.
- Use short retention for source audio.
- Keep OpenAI calls limited to text needed for translation.
- Do not expose raw exception details in production.
