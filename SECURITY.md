# Security Policy

## Supported Configuration

The only supported public deployment posture is production mode:

```dotenv
PRODUCTION_MODE=1
JWT_SECRET=<long random token>
CORS_ALLOW_ORIGINS=https://your-frontend.example
MAX_AUDIO_SECONDS=<positive integer>
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
```

In production mode the API:

- fails startup if `JWT_SECRET` is missing,
- fails startup if `CORS_ALLOW_ORIGINS` is empty or contains `*`,
- fails startup if `MAX_AUDIO_SECONDS <= 0`,
- hides internal details for most 5xx errors,
- enforces upload size via `MAX_UPLOAD_MB`,
- enforces audio duration via `MAX_AUDIO_SECONDS` when duration can be read,
- enables in-memory rate limiting for `/v1/...` endpoints.

Development mode may allow public endpoints when `JWT_SECRET` is empty. Do not
use development mode on an internet-facing host.

## Reporting Vulnerabilities

Report suspected vulnerabilities privately to the repository owner or
maintainer. Include:

- affected endpoint or script,
- steps to reproduce,
- impact,
- whether secrets, personal data, audio, transcripts, model files, or database
  records may be exposed,
- suggested fix if known.

Do not open public issues containing exploit details, credentials, personal
data, private audio, or database dumps.

## Secrets

Never commit:

- `.env`
- OpenAI API keys,
- database URLs with real passwords,
- model download tokens,
- private service URLs,
- production hostnames if they are sensitive,
- database dumps,
- logs,
- exported terms containing private content.

Use environment variables or secret mounts. Prefer `OPENAI_API_KEY_FILE` where
the deployment platform supports file-based secrets.

## Authentication

The current API authentication is intentionally simple:

```http
Authorization: Bearer <JWT_SECRET>
```

It is not full JWT validation. The token string is compared directly with
`JWT_SECRET`.

For multi-user public deployments, replace this with a real auth layer:

- signed JWT validation,
- key rotation,
- per-user authorization,
- audit logging,
- token revocation.

## CORS

Production deployments must set explicit origins:

```dotenv
CORS_ALLOW_ORIGINS=https://lemko.tools,https://www.lemko.tools
```

Avoid `*` in production. If browser credentials are needed, configure a narrow
origin list and set `CORS_ALLOW_CREDENTIALS=1` only when required.

## Rate Limiting

Rate limiting is in-memory and process-local. It is suitable as a basic
protection, not as the only public abuse control.

Configuration:

```dotenv
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
TRUST_PROXY_HEADERS=1
```

If the app is behind Caddy or another trusted proxy, set
`TRUST_PROXY_HEADERS=1` so `X-Forwarded-For` is used. Do not trust forwarded
headers when clients can connect directly to the app.

For scaled deployments with multiple API replicas, use proxy-level or
centralized rate limiting.

## Upload and Audio Safety

The API accepts:

- `audio/wav`
- `audio/x-wav`
- `audio/flac`
- `audio/mpeg`
- `application/octet-stream`

Controls:

- `MAX_UPLOAD_MB` limits request body size after read.
- `MAX_AUDIO_SECONDS` rejects audio if `torchaudio.info()` can determine the
  duration and it exceeds the limit.

Recommended additional hardening:

- enforce body size at reverse proxy level,
- run antivirus or media validation for untrusted uploads,
- store uploaded audio on encrypted storage,
- delete source uploads according to retention policy.

## Error Handling

Production mode suppresses internal 5xx details in API responses. Internal
details may still be written to logs for operators.

Do not return:

- filesystem paths,
- database connection strings,
- model paths revealing internal layout,
- stack traces,
- raw upstream API errors,
- private term/source content in error details.

## Dependency Security

This repo includes:

- `.github/dependabot.yml`
- `.github/workflows/pip-audit.yml`

Run locally:

```bash
python3 -m pip install pip-audit
pip-audit -r requirements-prod.txt
```

Dependency results change over time. Treat every production build as requiring
a fresh audit.

As of the user request dated 2026-05-25, known advisory concern was raised for
several pinned dependencies including `torch==2.3.1`, `python-multipart==0.0.20`,
`starlette==0.47.3`, `nemo-toolkit==2.4.0`, `transformers==4.56.0`, and
`nltk==3.9.1`. Verify current fixed versions before upgrading because PyTorch,
NeMo, and Transformers compatibility can be tightly coupled.

The local audit result from this work is summarized in
`docs/DEPENDENCY_AUDIT.md`.

## Model and Voice Abuse Risks

TTS/voice cloning can be abused for impersonation. Public deployments should:

- require explicit consent for every speaker reference set,
- restrict who may generate TTS,
- watermark or label generated speech where appropriate,
- log abuse reports,
- support deletion and withdrawal of voice consent,
- block deceptive or harmful impersonation use cases.

See `MODELS.md` and `PRIVACY.md`.

## Operational Hardening Checklist

- `PRODUCTION_MODE=1`.
- Non-empty `JWT_SECRET`.
- Explicit `CORS_ALLOW_ORIGINS`.
- Reverse proxy body size limits.
- HTTPS only at the public edge.
- No direct public access to Postgres, Qdrant, or Uvicorn.
- Encrypted backups for database and logs.
- Regular dependency audit.
- Retention/deletion job for logs, source audio, and transcripts.
- Separate secrets from image and repository.
