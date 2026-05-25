# Operacje i Utrzymanie

Ten dokument zbiera komendy administracyjne i typowe scenariusze pracy z repozytorium.

## Start Produkcyjny

Przygotuj `.env`:

```bash
cp .env.example .env
```

Uzupełnij co najmniej:

```dotenv
JWT_SECRET=...
POSTGRES_PASSWORD=...
LEMSLOWNIK_DB_NAME=lemslownik
DATABASE_URL=postgres://postgres:<hasło>@postgres:5432/lemslownik
MODEL_PATH=/app/models/epoch6-step4571_CAPS_WER8.nemo
```

Upewnij się, że istnieją katalogi montowane przez Compose:

```bash
mkdir -p logs models vocab_json psql_dump qdrant_data qdrant_snapshots
```

Zbuduj obraz backendu:

```bash
docker compose build asr
```

Uruchom usługi:

```bash
docker compose up -d postgres qdrant asr caddy
```

Sprawdź status:

```bash
docker compose ps
docker compose logs --tail=100 asr
curl -s http://127.0.0.1:8000/readyz
curl -s http://127.0.0.1:8000/healthz
```

## Rebuild Samego Backendu

Po zmianie kodu Pythona, zależności albo plików kopiowanych do obrazu:

```bash
docker compose build asr
docker compose up -d --no-deps --force-recreate asr
```

Skrócona wersja:

```bash
docker compose build asr && docker compose up -d --no-deps --force-recreate asr
```

Jeśli zmiany dotyczą tylko plików w `scripts/`, Compose montuje `./scripts:/app/scripts:ro`, więc sam restart kontenera zwykle wystarczy:

```bash
docker compose up -d --no-deps --force-recreate asr
```

## Logi

Backend:

```bash
docker compose logs --tail=100 -f asr
```

Postgres:

```bash
docker compose logs --tail=100 -f postgres
```

Caddy:

```bash
docker compose logs --tail=100 -f caddy
```

Pliki logów aplikacji, jeśli ustawione zgodnie z rekomendacją:

```bash
tail -f logs/log.json
tail -f logs/transcriptions_log.csv
tail -f logs/lemko_search_log.csv
tail -f logs/lemko_translate_log.csv
tail -f logs/lemko_tts_log.csv
```

## Healthchecki

Lokalnie na hoście:

```bash
curl -s http://127.0.0.1:8000/readyz
curl -s http://127.0.0.1:8000/healthz
```

Przez proxy:

```bash
curl -I https://apiasr.spektrogram.com/healthz
curl -s https://apiasr.spektrogram.com/readyz
```

Interpretacja:

- `/readyz` mówi tylko, czy `engine is not None`.
- `/healthz` zwraca też `device` i `model_id`.
- Jeśli ASR modelu nie da się załadować, proces może nie przejść startupu i healthcheck kontenera będzie failował.

## Test Transkrypcji

Upload:

```bash
JOB_ID="$(
  curl -sS -X POST http://127.0.0.1:8000/v1/transcriptions \
    -H "Authorization: Bearer $JWT_SECRET" \
    -F "file=@sample.wav;type=audio/wav" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])'
)"
echo "$JOB_ID"
```

Status:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/$JOB_ID \
  -H "Authorization: Bearer $JWT_SECRET"
```

SSE:

```bash
curl -N http://127.0.0.1:8000/v1/transcriptions/$JOB_ID/events \
  -H "Authorization: Bearer $JWT_SECRET"
```

Wynik:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/$JOB_ID/result \
  -H "Authorization: Bearer $JWT_SECRET"
```

Artefakt:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/$JOB_ID/artifact \
  -H "Authorization: Bearer $JWT_SECRET" \
  -o artifact.json
```

## Test Słownika

Łemkowski -> wpis słownikowy:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"бесіда"}'
```

Polski -> łemkowski:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search/pl \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"rozmowa"}'
```

Angielski -> łemkowski:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search/en \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"conversation"}'
```

## Test Tłumaczenia

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/translate/pl \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Тест бесіды по лемківскы."}'
```

Jeśli dostajesz błąd braku klucza:

```bash
docker compose exec asr env | grep OPENAI
```

Jeśli model zgłasza słowa nieznane, a potem błąd bazy:

```bash
docker compose exec asr python3 - <<'PY'
import os, psycopg2
print(os.environ.get("DATABASE_URL"))
conn = psycopg2.connect(os.environ["DATABASE_URL"])
print("ok")
conn.close()
PY
```

## Test TTS

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/tts \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Тест бесіды по лемківскы.", "speaker":0, "preset":"default"}' \
  -D tts.headers \
  --output tts.m4a
```

Sprawdź nagłówki:

```bash
cat tts.headers
```

Pierwsze wywołanie może być wolne, bo ładuje StyleTTS2, PLBERT, ASR helper, F0 i buduje embedding referencji.

## Praca z Bazą

Wejście do psql:

```bash
docker compose exec postgres psql -U postgres -d "$LEMSLOWNIK_DB_NAME"
```

Import dumpa z katalogu `psql_dump`:

```bash
docker compose exec -T postgres psql -U postgres -d "$LEMSLOWNIK_DB_NAME" < psql_dump/dump.sql
```

Backup:

```bash
docker compose exec -T postgres pg_dump -U postgres "$LEMSLOWNIK_DB_NAME" > "psql_dump/backup_$(date +%Y%m%d_%H%M%S).sql"
```

## Transfer Termów

Eksport:

```bash
docker exec -it lemko-asr \
  python3 /app/scripts/term-transfer.py export \
  --term-id 8581 \
  --output /app/logs/term_8581.json
```

Import:

```bash
docker exec -it lemko-asr \
  python3 /app/scripts/term-transfer.py import \
  --input /app/logs/term_8581.json
```

Eksport bulk:

```bash
docker exec -it lemko-asr \
  python3 /app/scripts/term-transfer.py export-bulk \
  --redacted true \
  --deleted false \
  --output /app/logs/terms_redacted.json
```

## Uzupełnianie Slotów Odmiany

Dla jednego terminu:

```bash
docker exec -it lemko-asr \
  python3 /app/scripts/fill_missing_adjective_forms.py --term-id 12886
```

Dla wszystkich terminów:

```bash
docker exec -it lemko-asr \
  python3 /app/scripts/fill_missing_adjective_forms.py
```

Uwaga: skrypt wstawia brakujące rekordy z pustym `word`, więc przed masowym uruchomieniem zrób backup bazy.

## Czyszczenie Dockera

Ostrożnie:

```bash
docker system prune -a --volumes
```

Ta komenda usuwa nieużywane obrazy, kontenery, sieci i wolumeny. Może usunąć dane, jeśli wolumeny nie są aktualnie używane. Nie uruchamiaj jej bez backupu bazy.

Bezpieczniejsze czyszczenie obrazów build cache:

```bash
docker builder prune
docker image prune
```

## Typowe Problemy

### Kontener ASR się restartuje

Sprawdź logi:

```bash
docker compose logs --tail=200 asr
```

Najczęstsze przyczyny:

- brak pliku `MODEL_PATH`,
- brak zależności modelu NeMo,
- błąd importu w `scripts/app.py`,
- niekompatybilny checkpoint,
- brak uprawnień do katalogów logów.

### `/readyz` nie odpowiada

Sprawdź:

```bash
docker compose ps
docker compose logs --tail=100 asr
```

Jeśli proces dopiero startuje, może ładować model ASR i wykonywać warmup. Healthcheck Compose ma retry, ale bardzo wolny storage/model może wymagać cierpliwości.

### `POST /v1/transcriptions` zwraca 415

Podaj prawidłowy content type:

```bash
curl -F "file=@sample.wav;type=audio/wav" ...
```

Dozwolone typy są opisane w [API.md](API.md).

### `GET /v1/transcriptions/{job_id}/result` zwraca 409

Job jeszcze działa. Sprawdź status:

```bash
curl -s http://127.0.0.1:8000/v1/transcriptions/$JOB_ID
```

albo SSE:

```bash
curl -N http://127.0.0.1:8000/v1/transcriptions/$JOB_ID/events
```

### Po restarcie job_id nie działa

To oczekiwane w obecnej architekturze. Joby są w pamięci procesu. Artefakty JSON mogą nadal istnieć na dysku, ale API nie ma po restarcie mapowania `job_id -> artifact_path`.

### Słownik zwraca `LEM_SEARCH_UNAVAILABLE`

Sprawdź:

```bash
docker compose exec asr env | grep DATABASE_URL
docker compose exec asr ls -la /app/morphology_structure_pl_lem_eng
docker compose exec asr ls -la /app/vocab_json
```

Weryfikacja połączenia DB:

```bash
docker compose exec asr python3 - <<'PY'
import os, psycopg2
print(os.environ.get("DATABASE_URL"))
conn = psycopg2.connect(os.environ["DATABASE_URL"])
print("connected")
conn.close()
PY
```

### FastText nie działa

Sprawdź modele:

```bash
docker compose exec asr ls -lh /app/models/cc.pl.300.bin /app/models/cc.en.300.bin /app/models/ft_words.bin
```

Sprawdź vocab:

```bash
docker compose exec asr ls -lh /app/vocab_json/vocab_pl.json /app/vocab_json/vocab_en.json /app/vocab_json/vocab_lem.json
```

Test:

```bash
docker compose exec asr python3 /app/scripts/fasttext2lemtools.py rozmowaa --lang pl --topn 5
```

### TTS zwraca `TTS_RESOURCE_NOT_FOUND`

Sprawdź:

```bash
docker compose exec asr env | grep STYLE_TTS2
docker compose exec asr ls -la /app/models/StyleTTS2
docker compose exec asr find /app/models/StyleTTS2 -maxdepth 3 -type f | head
docker compose exec asr find /app/models/StyleTTS2/0 -name '*.wav' | head
docker compose exec asr find /app/models/StyleTTS2/1 -name '*.wav' | head
```

Wymagane są m.in.:

```text
models.py
Modules/
Utils/ASR/config.yml
Utils/ASR/epoch_00080.pth
Utils/JDC/bst.t7
Utils/PLBERT_multi/
Models/lemko_finetune/epoch_2nd_00044_nmndt_p2.pth
```

### TTS zwraca błąd phonemizera

Sprawdź espeak:

```bash
docker compose exec asr which espeak-ng
docker compose exec asr python3 /app/scripts/lem_phonemizer2.py "Тест"
```

### OpenAI zwraca błąd

Sprawdź klucz:

```bash
docker compose exec asr env | grep OPENAI_API_KEY
```

Jeśli używasz pliku:

```bash
docker compose exec asr sh -lc 'test -f "$OPENAI_API_KEY_FILE" && echo ok'
```

Sprawdź model:

```bash
docker compose exec asr env | grep LEM_TRANSLATE_MODEL
```

## Weryfikacja Po Zmianach

Szybka składnia:

```bash
python3 -m py_compile scripts/*.py
```

Konfiguracja Compose:

```bash
docker compose config
```

Build backendu:

```bash
docker compose build asr
```

Smoke test API:

```bash
curl -s http://127.0.0.1:8000/readyz
curl -s http://127.0.0.1:8000/healthz
```

## Checklist Przed Publicznym Wystawieniem

- `JWT_SECRET` jest ustawiony i nie jest wartością testową.
- `CORS_ALLOW_ORIGINS` zawiera konkretne domeny frontendu, jeśli używane są credentials.
- `TRANS_DIR`, `LOG_PATH` i CSV logi są na montowanym wolumenie.
- Backup Postgresa jest skonfigurowany i sprawdzony.
- Modele w `models/` są tylko do odczytu w kontenerze.
- `OPENAI_API_KEY` nie jest zapisany w repo.
- Caddy ma prawidłowe domeny i certyfikaty.
- Healthcheck `/readyz` przechodzi po restarcie hosta.
