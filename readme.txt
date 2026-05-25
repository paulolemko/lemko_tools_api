# Legacy operational notes

This file intentionally no longer contains deployment hostnames, private
service URLs, credentials, dictionary administration links, or one-off
production commands.

Use the maintained documentation instead:

- README.md
- docs/API.md
- docs/CONFIGURATION.md
- docs/OPERATIONS.md
- SECURITY.md
- PRIVACY.md

Common local commands:

docker compose build asr
docker compose up -d --no-deps --force-recreate asr
docker compose logs --tail=100 -f asr
curl -s http://127.0.0.1:8000/healthz
