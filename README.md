# Lemko Tools API

Backend API i zestaw narzędzi dla przetwarzania języka łemkowskiego. Repozytorium łączy trzy główne obszary:

- ASR: transkrypcja plików audio modelem NeMo RNNT.
- Słownik i wyszukiwanie: wyszukiwanie haseł łemkowskich, form odmienionych oraz tłumaczeń polskich/angielskich w bazie PostgreSQL.
- TTS i fonemizacja: synteza mowy StyleTTS2, transliteracja i fonemizacja tekstu łemkowskiego.

Główna aplikacja FastAPI znajduje się w `scripts/app.py`. Uruchamianie produkcyjne jest opisane przez `Dockerfile`, `compose.yml` i `Caddyfile`.

## Spis Dokumentacji

- [docs/API.md](docs/API.md) - kompletna referencja HTTP API, formaty żądań/odpowiedzi, błędy i przykłady `curl`.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) - zmienne środowiskowe, wymagane modele, wolumeny, Docker Compose i konfiguracja proxy.
- [docs/SCRIPTS.md](docs/SCRIPTS.md) - opis wszystkich skryptów CLI w `scripts/` z przykładami użycia.
- [docs/DATA_MODEL.md](docs/DATA_MODEL.md) - struktura bazy, formaty artefaktów, logi CSV/JSONL i pliki morfologii.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) - operacje administracyjne: start, rebuild, logi, healthcheck, backup i typowe problemy.
- [docs/DEPENDENCY_AUDIT.md](docs/DEPENDENCY_AUDIT.md) - wynik lokalnego `pip-audit` i plan aktualizacji podatnych zależności.
- [SECURITY.md](SECURITY.md) - tryb produkcyjny, raportowanie podatności, sekrety, auth, CORS i dependency audit.
- [PRIVACY.md](PRIVACY.md) - logowane pola, retencja, OpenAI, TTS/voice cloning i prawa do usunięcia danych.
- [MODELS.md](MODELS.md) - zasady użycia modeli ASR, FastText i StyleTTS2.
- [DATA_LICENSE.md](DATA_LICENSE.md) - licencja i governance danych, dumpów, logów i artefaktów.
- [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) - zależności i zewnętrzne licencje do weryfikacji.
- [.env.example](.env.example) - przykładowa konfiguracja środowiska.

## Struktura Repozytorium

```text
.
├── Caddyfile
├── Dockerfile
├── compose.yml
├── requirements-prod.txt
├── readme.txt
├── morphology_structure_pl_lem_eng/
│   ├── morphology_structure_eng01.json
│   ├── morphology_structure_lem01.json
│   └── morphology_structure_pl01.json
└── scripts/
    ├── app.py
    ├── asr_engine.py
    ├── fasttext2lemtools.py
    ├── fill_missing_adjective_forms.py
    ├── kyr2lat.py
    ├── lem-search.py
    ├── lem_phonemizer2.py
    ├── lem_translate.py
    ├── pl-lem-search.py
    ├── styletts2_engine.py
    ├── synthesize_from_speaker.py
    ├── synthesize_from_speaker2.py
    └── term-transfer.py
```

Katalogi takie jak `models/`, `logs/`, `vocab_json/`, `psql_dump/`, `qdrant_data/`, `qdrant_snapshots/` i `transkrypcje/` są ignorowane przez Git, bo zawierają dane lokalne, modele, dumpy, logi albo artefakty runtime.

## Główne Komponenty

### FastAPI

`scripts/app.py` wystawia endpointy:

- `GET /healthz`, `HEAD /healthz` - status procesu API i załadowanego modelu ASR.
- `GET /readyz`, `HEAD /readyz` - gotowość procesu.
- `POST /v1/transcriptions` - przyjęcie pliku audio do transkrypcji asynchronicznej.
- `GET /v1/transcriptions/{job_id}` - status joba.
- `GET /v1/transcriptions/{job_id}/result` - wynik tekstowy gotowej transkrypcji.
- `GET /v1/transcriptions/{job_id}/artifact` - pełny artefakt JSON transkrypcji.
- `GET /v1/transcriptions/{job_id}/events` - Server-Sent Events z postępem.
- `POST /v1/lemko/search` - wyszukiwanie hasła/formy łemkowskiej.
- `POST /v1/lemko/search/pl` - wyszukiwanie haseł łemkowskich po polskim tłumaczeniu.
- `POST /v1/lemko/search/en` - wyszukiwanie haseł łemkowskich po angielskim tłumaczeniu.
- `POST /v1/lemko/translate/pl` - tłumaczenie tekstu łemkowskiego na polski z użyciem OpenAI i słownika.
- `POST /v1/tts` - synteza mowy do pliku M4A.

Pełny opis znajduje się w [docs/API.md](docs/API.md).

### ASR

`scripts/asr_engine.py` ładuje model NeMo `EncDecHybridRNNTCTCBPEModel` z `MODEL_PATH`. Audio jest konwertowane do mono 16 kHz, dzielone na chunki według obwiedni RMS, a każdy chunk jest transkrybowany beam searchem RNNT. Wynik ma tekst ciągły oraz segmenty SRT-like w polu `words`.

Model ASR jest ładowany na starcie aplikacji. Jeżeli plik modelu nie istnieje, proces FastAPI nie wystartuje poprawnie.

### Słownik

Wyszukiwanie słownikowe opiera się o PostgreSQL i tabele `public.terms`, `public.term_word_associations`, `public.sources` oraz częściowo `public.users`.

`scripts/lem-search.py` wyszukuje hasła łemkowskie po formie podstawowej lub odmianie. `scripts/pl-lem-search.py` wyszukuje haseł łemkowskich po polskim lub angielskim tłumaczeniu. Oba skrypty mają fallback do sugestii FastText przez `scripts/fasttext2lemtools.py`; wyszukiwanie po polskim/angielskim może dodatkowo użyć OpenAI do sprowadzenia słowa do formy podstawowej.

### Tłumaczenie

`scripts/lem_translate.py` tłumaczy tekst łemkowski na polski przez OpenAI Responses API. Przepływ jest dwuetapowy:

1. Model próbuje przetłumaczyć tekst bez słownika i zwraca JSON z informacją, czy potrzebuje haseł słownikowych.
2. Jeśli model zgłosi nieznane słowa, kod pobiera definicje i konteksty z PostgreSQL, po czym prosi model o finalne tłumaczenie.

Endpoint API dla tego modułu to `POST /v1/lemko/translate/pl`.

### TTS

`scripts/styletts2_engine.py` ładuje StyleTTS2, fonemizator i referencje speakerów. Endpoint `POST /v1/tts` zwraca plik M4A (`audio/mp4`) oraz metryki w nagłówkach `X-TTS-RTF`, `X-TTS-Duration` i `X-TTS-Elapsed`.

Silnik TTS jest ładowany leniwie przy pierwszym żądaniu TTS, więc pierwsza synteza jest zauważalnie wolniejsza.

## Szybki Start Przez Docker Compose

1. Przygotuj `.env` na podstawie [.env.example](.env.example).

2. Upewnij się, że lokalnie istnieją wymagane katalogi i pliki:

```text
models/
  epoch6-step4571_CAPS_WER8.nemo
  cc.pl.300.bin
  cc.en.300.bin
  ft_words.bin
  StyleTTS2/
vocab_json/
  vocab_pl.json
  vocab_en.json
  vocab_lem.json
logs/
psql_dump/
```

Dokładne wymagania są w [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

3. Zbuduj i uruchom backend:

```bash
docker compose build asr
docker compose up -d asr postgres qdrant caddy
```

4. Sprawdź gotowość:

```bash
curl -s http://127.0.0.1:8000/readyz
curl -s http://127.0.0.1:8000/healthz
```

5. Otwórz dokumentację FastAPI generowaną runtime:

```text
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/openapi.json
```

## Przykłady API

### Transkrypcja Audio

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/transcriptions \
  -H "Authorization: Bearer $JWT_SECRET" \
  -F "file=@sample.wav;type=audio/wav"
```

Odpowiedź:

```json
{
  "job_id": "2f5b8d1a",
  "filename": "sample.wav",
  "audio_duration_s": 12.34,
  "sha256": null,
  "status": "queued",
  "stage": "oczekuję w kolejce"
}
```

Status:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/2f5b8d1a \
  -H "Authorization: Bearer $JWT_SECRET"
```

Wynik:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/2f5b8d1a/result \
  -H "Authorization: Bearer $JWT_SECRET"
```

### Wyszukiwanie Hasła Łemkowskiego

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"бесіда"}'
```

### Wyszukiwanie Po Polskim Tłumaczeniu

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search/pl \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"rozmowa"}'
```

### Tłumaczenie Łemkowski -> Polski

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/translate/pl \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Тест бесіды по лемківскы."}'
```

### TTS

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/tts \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Тест бесіды по лемківскы.", "speaker":0, "preset":"default"}' \
  --output tts.m4a
```

## Lokalny Start Bez Dockera

Instalacja zależności jest ciężka, bo obejmuje PyTorch CPU, NeMo, FastText, phonemizer i biblioteki audio.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-prod.txt
python -m nltk.downloader punkt punkt_tab
uvicorn --app-dir scripts app:app --host 0.0.0.0 --port 8000
```

Minimalnie potrzebujesz poprawnych wartości `MODEL_PATH` i `DATABASE_URL`; dla tłumaczenia wymagane jest `OPENAI_API_KEY` albo `OPENAI_API_KEY_FILE`; dla TTS wymagany jest katalog StyleTTS2 z modelami i referencjami.

## Tryb Produkcyjny

Publiczne wdrożenie powinno działać z:

```dotenv
PRODUCTION_MODE=1
JWT_SECRET=<long-random-token>
CORS_ALLOW_ORIGINS=https://twoja-domena.example
MAX_AUDIO_SECONDS=7200
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
```

W trybie produkcyjnym aplikacja wymaga tokenu, odrzuca wildcard CORS, ukrywa wewnętrzne szczegóły większości błędów 5xx, egzekwuje limit czasu audio i włącza rate limiting dla `/v1/...`.

## Autoryzacja

Autoryzacja jest opcjonalna i zależy od `JWT_SECRET`:

- jeśli `JWT_SECRET` jest pusty, endpointy `/v1/...` są publiczne;
- jeśli `JWT_SECRET` ma wartość, każdy endpoint `/v1/...` wymaga nagłówka `Authorization: Bearer <JWT_SECRET>`.

To nie jest pełna walidacja JWT. Kod porównuje token tekstowo z wartością `JWT_SECRET`.

## Istotne Uwagi Operacyjne

- Joby transkrypcji są trzymane w pamięci procesu. Po restarcie kontenera statusy jobów znikają, nawet jeśli pliki artefaktów nadal istnieją.
- `MAX_AUDIO_SECONDS` jest zdefiniowane w konfiguracji, ale w aktualnym kodzie nie jest egzekwowane przy uploadzie.
- `TRANS_DIR` i `LOG_PATH` mają domyślne ścieżki względne. W obecnym `compose.yml` logi CSV są montowane do `/app/logs`, ale `TRANS_DIR` i `LOG_PATH` powinny być jawnie ustawione, jeśli mają przetrwać rekreację kontenera.
- `qdrant` jest uruchamiany przez `compose.yml`, ale aktualny kod w tym repo nie używa klienta Qdrant bezpośrednio.
- `scripts/lem_translate.py` próbuje opcjonalnie załadować `scripts/pl-en-fasttext.py`; tego pliku nie ma w śledzonych plikach repo. Brak pliku wyłącza tylko dodatkowe sugestie FastText w tym module.
- TTS ma blokadę wewnątrz silnika, więc w praktyce jedna instancja procesu wykonuje syntezę sekwencyjnie.

## Najczęstsze Komendy

```bash
# Rebuild i restart backendu
docker compose build asr
docker compose up -d --no-deps --force-recreate asr

# Logi backendu
docker compose logs --tail=100 -f asr

# Healthcheck lokalny
curl -s http://127.0.0.1:8000/healthz

# Healthcheck przez proxy
curl -I https://apiasr.spektrogram.com/healthz

# Import terminu do bazy w kontenerze
docker exec -it lemko-asr python3 /app/scripts/term-transfer.py import --input /app/models/term_8581.json

# Uzupełnienie brakujących slotów odmiany dla terminu
docker exec -it lemko-asr python3 /app/scripts/fill_missing_adjective_forms.py --term-id 12886
```

## Status Testów

Repozytorium nie zawiera katalogu testów ani konfiguracji CI w śledzonych plikach. Najbardziej praktyczne sprawdzenia po zmianach to:

```bash
python3 -m py_compile scripts/*.py
docker compose config
docker compose build asr
curl -s http://127.0.0.1:8000/readyz
```

`docker compose config` może wymagać lokalnego `.env`, bo `compose.yml` odwołuje się do zmiennych Postgresa.
