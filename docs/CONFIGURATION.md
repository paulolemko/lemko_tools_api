# Konfiguracja

Ten dokument opisuje konfigurację runtime dla FastAPI, ASR, słownika, tłumaczenia, TTS i Docker Compose.

## Pliki Konfiguracyjne

| Plik | Rola |
| --- | --- |
| `Dockerfile` | Buduje obraz Python 3.10 slim z zależnościami CPU, ffmpeg i espeak-ng. |
| `compose.yml` | Definiuje usługi `asr`, `postgres`, `qdrant`, `caddy`. |
| `Caddyfile` | Reverse proxy do `lemko-asr:8000`, gzip, timeouty i SSE flush. |
| `requirements-prod.txt` | Produkcyjne zależności Pythona. |
| `.env.example` | Przykład lokalnej konfiguracji dla `compose.yml` i aplikacji. |
| `morphology_structure_pl_lem_eng/*.json` | Mapy morfologii w języku łemkowskim, polskim i angielskim. |

`.env` jest ignorowany przez Git i powinien zawierać sekrety oraz ścieżki specyficzne dla środowiska.

## Docker Compose

### asr

Usługa `asr` buduje obraz z `Dockerfile` i uruchamia:

```bash
uvicorn --app-dir /app/scripts app:app --host 0.0.0.0 --port 8000
```

Porty:

```yaml
ports:
  - "127.0.0.1:8000:8000"
```

API jest wystawione wyłącznie na loopback hosta. Publiczny ruch powinien iść przez Caddy albo inne reverse proxy.

Healthcheck kontenera:

```yaml
test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/readyz"]
interval: 20s
timeout: 5s
retries: 5
```

Wolumeny:

```yaml
volumes:
  - ./scripts:/app/scripts:ro
  - ./logs:/app/logs
  - ./models:/app/models:ro
  - ./vocab_json:/app/vocab_json:ro
  - ./morphology_structure_pl_lem_eng:/app/morphology_structure_pl_lem_eng:ro
```

Zmienne ustawione bezpośrednio w `compose.yml` dla `asr`:

```yaml
TRANSCRIPTIONS_CSV_PATH: /app/logs/transcriptions_log.csv
TRANSCRIPTED_SOURCE_DIR: /app/logs/transcripted_source
LEM_SEARCH_LOG_PATH: /app/logs/lemko_search_log.csv
LEM_TRANSLATE_LOG_PATH: /app/logs/lemko_translate_log.csv
LEM_TTS_LOG_PATH: /app/logs/lemko_tts_log.csv
VOCAB_JSON_DIR: /app/vocab_json
```

Uwaga: `compose.yml` nie ustawia jawnie `TRANS_DIR` ani `LOG_PATH`. Domyślnie będą to odpowiednio `/app/transkrypcje` i `/app/log.json` wewnątrz kontenera. Jeśli artefakty transkrypcji i JSONL log mają przetrwać odtworzenie kontenera, ustaw je np. na `/app/logs/transkrypcje` i `/app/logs/log.json`.

### postgres

Usługa `postgres` używa obrazu `postgres:16`.

Wymagane zmienne w `.env`:

```dotenv
POSTGRES_PASSWORD=...
LEMSLOWNIK_DB_NAME=...
```

Porty:

```yaml
ports:
  - "127.0.0.1:5432:5432"
```

Wolumeny:

```yaml
volumes:
  - postgres_data:/var/lib/postgresql/data
  - ./psql_dump:/dumps:ro
```

Kod aplikacji i skrypty słownikowe łączą się przez `DATABASE_URL`. W sieci Compose hostem powinno być zwykle `postgres`, nie `127.0.0.1`.

### qdrant

Usługa `qdrant` jest zdefiniowana w Compose, ale aktualny kod w śledzonych plikach nie odwołuje się do Qdrant bezpośrednio.

Porty:

```yaml
ports:
  - "127.0.0.1:6333:6333"
  - "127.0.0.1:6334:6334"
```

Wolumeny:

```yaml
volumes:
  - ./qdrant_data:/qdrant/storage
  - ./qdrant_snapshots:/qdrant/snapshots
```

### caddy

Caddy reverse proxy kieruje ruch dla:

```text
lemko.tools
www.lemko.tools
apiasr.spektrogram.com
```

do:

```text
lemko-asr:8000
```

Konfiguracja transportu ma długie timeouty dla dużych uploadów i wolnych transkrypcji:

```caddy
transport http {
  read_timeout  15m
  write_timeout 15m
}
flush_interval 1s
```

`flush_interval 1s` jest istotny dla Server-Sent Events w `/v1/transcriptions/{job_id}/events`.

## Zmienne Środowiskowe API

### Core

| Zmienna | Domyślnie | Użycie |
| --- | --- | --- |
| `PRODUCTION_MODE` | zależy od `ENVIRONMENT`; zwykle `false` | Włącza wymagany token, ograniczone CORS, ukrywanie szczegółów 5xx, wymagany limit audio i rate limiting. |
| `ENVIRONMENT` / `APP_ENV` | `development` | Jeśli ma wartość `prod` albo `production`, domyślnie włącza `PRODUCTION_MODE`. |
| `JWT_SECRET` | pusty string | Jeśli ustawione, endpointy `/v1/...` wymagają `Authorization: Bearer <JWT_SECRET>`. |
| `CORS_ALLOW_ORIGINS` | `*` | Lista originów oddzielona przecinkami. |
| `CORS_ALLOW_CREDENTIALS` | `true` w dev, `false` w production | Wartość `allow_credentials` dla CORS. |
| `TRUST_PROXY_HEADERS` | `PRODUCTION_MODE` | Używa `X-Forwarded-For`/`X-Real-IP` do rate limitingu za zaufanym proxy. |
| `MAX_UPLOAD_MB` | `200` | Maksymalny rozmiar pliku audio w MB. |
| `MAX_AUDIO_SECONDS` | `7200` | Maksymalny czas audio, egzekwowany gdy `torchaudio.info()` odczyta duration. W produkcji musi być dodatni. |
| `MAX_CONCURRENCY` | `1` | Limit równoległych transkrypcji ASR. |

W produkcji aplikacja odmawia startu, jeśli:

- `JWT_SECRET` jest pusty,
- `CORS_ALLOW_ORIGINS` jest puste albo zawiera `*`,
- `MAX_AUDIO_SECONDS <= 0`.

### Rate limiting

| Zmienna | Domyślnie | Użycie |
| --- | --- | --- |
| `RATE_LIMIT_ENABLED` | `false` w dev; zawsze efektywnie `true` w production | Włącza limit żądań dla `/v1/...`. |
| `RATE_LIMIT_REQUESTS` | `60` | Maksymalna liczba żądań w oknie. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Długość okna w sekundach. |

Limit jest procesowy i trzymany w pamięci. Przy wielu replikach API zastosuj dodatkowy limit na reverse proxy lub w zewnętrznym store.

### Ścieżki i logi

| Zmienna | Domyślnie | Użycie |
| --- | --- | --- |
| `TRANS_DIR` | `transkrypcje` | Katalog artefaktów JSON transkrypcji. |
| `LOG_PATH` | `log.json` | JSONL log z eventami aplikacji. |
| `TRANSCRIPTED_SOURCE_DIR` | `transcripted_source` | Archiwum oryginalnych uploadów audio. |
| `TRANSCRIPTIONS_CSV_PATH` | `transcriptions_log.csv` | CSV z transkrypcjami. |
| `LEM_SEARCH_LOG_PATH` | `lemko_search_log.csv` | CSV z wywołaniami słownika. |
| `LEM_TRANSLATE_LOG_PATH` | `lemko_translate_log.csv` | CSV z wywołaniami tłumaczenia. |
| `LEM_TTS_LOG_PATH` | `lemko_tts_log.csv` | CSV z wywołaniami TTS. |

Rekomendacja dla Compose:

```dotenv
TRANS_DIR=/app/logs/transkrypcje
LOG_PATH=/app/logs/log.json
TRANSCRIPTED_SOURCE_DIR=/app/logs/transcripted_source
TRANSCRIPTIONS_CSV_PATH=/app/logs/transcriptions_log.csv
LEM_SEARCH_LOG_PATH=/app/logs/lemko_search_log.csv
LEM_TRANSLATE_LOG_PATH=/app/logs/lemko_translate_log.csv
LEM_TTS_LOG_PATH=/app/logs/lemko_tts_log.csv
```

## Zmienne ASR

`scripts/asr_engine.py` czyta konfigurację przez dataclass `ASRConfig`.

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `MODEL_PATH` | `models/epoch6-step4571_CAPS_WER8.nemo` | Ścieżka do modelu NeMo `.nemo`. W kontenerze zwykle `/app/models/...`. |
| `TARGET_SR` | `16000` | Docelowe próbkowanie audio po resamplingu. |
| `BEAM_SIZE` | `8` | Rozmiar beam search RNNT. |
| `MAX_SYMBOLS_PER_STEP` | `32` | Parametr dekodera RNNT. |
| `MIN_CHUNK_S` | `15` | Minimalna długość chunka audio w sekundach. |
| `MAX_CHUNK_S` | `25` | Maksymalna długość chunka audio w sekundach. |
| `ENV_WIN_MS` | `50` | Okno obwiedni RMS w milisekundach. |
| `SILENCE_Q` | `0.25` | Kwantyl obwiedni do progu ciszy. |
| `SILENCE_FACTOR` | `1.0` | Mnożnik progu ciszy. |

Wymagany plik modelu:

```text
models/epoch6-step4571_CAPS_WER8.nemo
```

albo inny plik wskazany przez `MODEL_PATH`.

## Zmienne Bazy Danych

| Zmienna | Domyślnie w skryptach | Opis |
| --- | --- | --- |
| `DATABASE_URL` | `postgres://lemslownik:lemslownik@127.0.0.1:5432/lemslownik` | Główne połączenie do bazy słownika. |
| `LEM_TRANSLATE_DATABASE_URL` | brak | Fallback dla `lem_translate.py`, używany jeśli `DATABASE_URL` nie istnieje. |
| `POSTGRES_PASSWORD` | brak | Hasło usługi Postgres w Compose. |
| `LEMSLOWNIK_DB_NAME` | brak | Nazwa bazy tworzonej przez kontener Postgresa. |

Przykład dla Compose:

```dotenv
DATABASE_URL=postgres://postgres:${POSTGRES_PASSWORD}@postgres:5432/${LEMSLOWNIK_DB_NAME}
POSTGRES_PASSWORD=change-me
LEMSLOWNIK_DB_NAME=lemslownik
```

Skrypty mają logikę fallbacku hostów: jeśli nie uda się połączyć z hostem `127.0.0.1`, próbują m.in. `db`, `postgres` albo odwrotnie, zależnie od skryptu.

## Zmienne Słownika i Wyszukiwania

### `POST /v1/lemko/search`

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `LEM_SEARCH_ENABLED` | `1` | `0`, `false`, `no`, `off` wyłącza endpointy wyszukiwania. |
| `LEM_SEARCH_MORPHOLOGY_PATH` | brak | Jawna ścieżka do pliku morfologii dla `lem-search.py`. |
| `LEM_SEARCH_SIMILAR_LIMIT` | `10` | Liczba kandydatów FastText dla fallbacku. |
| `LEM_SEARCH_SIMILAR_LANG` | `lem` | Język przekazywany do `fasttext2lemtools.suggest`. |
| `LEM_SEARCH_SIMILAR_VOCAB_DIR` | brak | Katalog `vocab_{lang}.json` dla fallbacku. |
| `LEM_SEARCH_SIMILAR_DEBUG` | `false` | Diagnostyka fallbacku FastText. |
| `VOCAB_JSON_DIR` | brak | Wspólny katalog słowników JSON, jeśli nie podano bardziej specyficznej zmiennej. |

### `POST /v1/lemko/search/pl`

Prefiks środowiskowy: `LEM_PL`.

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `LEM_PL_SEARCH_SUGGEST_LIMIT` | `15` | Liczba sugestii FastText. |
| `LEM_PL_SEARCH_SUGGEST_COUNT` | `15` | Starsza alternatywa, używana jeśli `*_LIMIT` nie istnieje. |
| `LEM_PL_SEARCH_NO_SUGGEST` | `false` | Wyłącza sugestie FastText i LLM. |
| `LEM_PL_SEARCH_SUGGEST_DEBUG` | `false` | Diagnostyka FastText. |
| `LEM_PL_SEARCH_SUGGEST_VOCAB_DIR` | brak | Katalog vocab dla polskiego. |
| `LEM_PL_SEARCH_LLM_MODEL` | `gpt-5-mini` | Model OpenAI do lematyzacji fallbackowej. |
| `LEM_PL_SEARCH_LLM_DEBUG` | `false` | Diagnostyka promptu i odpowiedzi LLM. |

### `POST /v1/lemko/search/en`

Prefiks środowiskowy: `LEM_EN`.

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `LEM_EN_SEARCH_SUGGEST_LIMIT` | `15` | Liczba sugestii FastText. |
| `LEM_EN_SEARCH_SUGGEST_COUNT` | `15` | Starsza alternatywa, używana jeśli `*_LIMIT` nie istnieje. |
| `LEM_EN_SEARCH_NO_SUGGEST` | `false` | Wyłącza sugestie FastText i LLM. |
| `LEM_EN_SEARCH_SUGGEST_DEBUG` | `false` | Diagnostyka FastText. |
| `LEM_EN_SEARCH_SUGGEST_VOCAB_DIR` | brak | Katalog vocab dla angielskiego. |
| `LEM_EN_SEARCH_LLM_MODEL` | `gpt-5-mini` | Model OpenAI do lematyzacji fallbackowej. |
| `LEM_EN_SEARCH_LLM_DEBUG` | `false` | Diagnostyka promptu i odpowiedzi LLM. |

## Zmienne Tłumaczenia OpenAI

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `LEM_TRANSLATE_ENABLED` | `1` | `0`, `false`, `no`, `off` wyłącza endpoint tłumaczenia. |
| `LEM_TRANSLATE_MODEL` | `gpt-5` | Model dla `POST /v1/lemko/translate/pl`. |
| `OPENAI_API_KEY` | brak | Klucz OpenAI. |
| `OPENAI_API_KEY_FILE` | brak | Ścieżka do pliku z kluczem; jeśli istnieje, wartość trafia do `OPENAI_API_KEY`. |
| `LEM_TRANSLATE_SERVICE_TIER` | brak | Opcjonalny `service_tier` dla Responses API. |
| `OPENAI_SERVICE_TIER` | brak | Alternatywna zmienna dla `service_tier`. |

## Zmienne FastText

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `FASTTEXT_MODEL_DIR` | brak | Katalog albo konkretna ścieżka do modelu FastText. |
| `VOCAB_JSON_DIR` | brak | Katalog z `vocab_pl.json`, `vocab_en.json`, `vocab_lem.json`. |

`fasttext2lemtools.py` szuka modeli w kolejności:

1. `FASTTEXT_MODEL_DIR`, jeśli ustawione.
2. `/app/models/<filename>`.
3. katalog `scripts/`.
4. katalog nadrzędny `scripts/`.
5. aktualny katalog roboczy.

Wymagane nazwy domyślne:

```text
cc.pl.300.bin
cc.en.300.bin
ft_words.bin
```

Pliki vocab:

```text
vocab_pl.json
vocab_en.json
vocab_lem.json
```

Format vocab:

```json
{
  "vocab": ["słowo", "inne"],
  "frequency": {
    "słowo": 0.5,
    "inne": 0.1
  }
}
```

`frequency` może być też listą par `[word, value]`.

## Zmienne Morfologii

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `MORPHOLOGY_STRUCTURE_FILE` | ustawione w Dockerfile na `/app/morphology_structure_pl_lem_eng/morphology_structure_lem01.json` | Główny plik morfologii. |
| `MORPHOLOGY_STRUCTURE_DIR` | brak | Katalog z wariantami `morphology_structure_*.json`. |
| `LEM_SEARCH_MORPHOLOGY_PATH` | brak | Jawna ścieżka tylko dla API search. |

W repo są trzy warianty:

```text
morphology_structure_lem01.json
morphology_structure_pl01.json
morphology_structure_eng01.json
```

Domyślnie API w kontenerze używa wersji łemkowskiej przez `MORPHOLOGY_STRUCTURE_FILE` ustawione w `Dockerfile`.

## Zmienne TTS

### API TTS

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `MAX_TTS_CONCURRENCY` | `1` | Limit równoległych żądań TTS dopuszczonych przez semaphore. |
| `TTS_MAX_WORKERS` | wartość `MAX_TTS_CONCURRENCY` | Liczba workerów `ThreadPoolExecutor`. |
| `TTS_TEXT_MAX_CHARS` | `1000`, minimum `32` | Maksymalna długość tekstu wejściowego. |
| `TTS_NUM_REFS` | `3` | Liczba referencyjnych plików `.wav` uśrednianych dla speakera. |
| `TTS_TRIM_IN_MS` | `100` | Przycięcie początku wygenerowanej fali w ms. |
| `TTS_TRIM_OUT_MS` | `200` | Przycięcie końca wygenerowanej fali w ms. |

### StyleTTS2

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `STYLE_TTS2_DIR` | autodetekcja | Katalog kodu StyleTTS2 zawierający `models.py` i `Modules/`. |
| `STYLE_TTS2_REFS_ROOT` | autodetekcja | Katalog zawierający podkatalogi speakerów `0/` i `1/`. |
| `STYLE_TTS2_PHONEMIZER_PL` | `pl` | Kod espeak dla segmentów polskich. |
| `STYLE_TTS2_PHONEMIZER_UK` | `uk` | Kod espeak dla segmentów ukraińskich/cyrylicznych. |
| `STYLE_TTS2_PHONEMIZER_BG` | `bg` | Kod espeak dla segmentów bułgarskich używanych technicznie dla `ы`. |

`styletts2_engine.py` szuka StyleTTS2 tak:

1. `STYLE_TTS2_DIR`, jeśli ustawione.
2. katalog skryptu i jego rodzice.
3. `models/StyleTTS2` względem katalogu nadrzędnego `scripts/`.

W kontenerze Compose naturalna ścieżka to:

```dotenv
STYLE_TTS2_DIR=/app/models/StyleTTS2
STYLE_TTS2_REFS_ROOT=/app/models/StyleTTS2
```

Wymagane pliki/katalogi wewnątrz StyleTTS2:

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

## Zależności Systemowe

Dockerfile instaluje:

```text
build-essential
libsndfile1
ffmpeg
git
libgomp1
espeak-ng
espeak-ng-espeak
```

Najważniejsze powody:

- `ffmpeg` - wejście/wyjście audio i kodowanie M4A/AAC.
- `libsndfile1` - `soundfile`.
- `espeak-ng` - `phonemizer`.
- `build-essential`, `libgomp1` - kompilowane zależności i FastText.

## Zależności Python

Najważniejsze grupy z `requirements-prod.txt`:

- API: `fastapi`, `uvicorn`, `python-multipart`, `pydantic`, `starlette`.
- PyTorch CPU: `torch==2.3.1+cpu`, `torchaudio==2.3.1+cpu`.
- ASR: `nemo-toolkit`, `hydra-core`, `omegaconf`, `lightning`, `transformers`, `lhotse`.
- Audio/DSP: `soundfile`, `librosa`, `numpy`, `numba`, `phonemizer`, `nltk`.
- Database: `psycopg2-binary`.
- Search: `fasttext`, `rapidfuzz`, `python-Levenshtein`.
- OpenAI: `openai>=1.40.0`.

Dockerfile dodatkowo uruchamia:

```bash
python -m nltk.downloader punkt punkt_tab
```

## Minimalny `.env`

Minimalna konfiguracja do startu API z ASR i słownikiem:

```dotenv
JWT_SECRET=change-me
MODEL_PATH=/app/models/epoch6-step4571_CAPS_WER8.nemo
DATABASE_URL=postgres://postgres:change-me@postgres:5432/lemslownik
POSTGRES_PASSWORD=change-me
LEMSLOWNIK_DB_NAME=lemslownik
TRANS_DIR=/app/logs/transkrypcje
LOG_PATH=/app/logs/log.json
```

Dla OpenAI:

```dotenv
OPENAI_API_KEY=sk-...
LEM_TRANSLATE_MODEL=gpt-5
```

Dla TTS:

```dotenv
STYLE_TTS2_DIR=/app/models/StyleTTS2
STYLE_TTS2_REFS_ROOT=/app/models/StyleTTS2
```

## Bezpieczeństwo

- Nie commituj `.env`, modeli, dumpów bazy, snapshotów Qdrant ani logów.
- Jeśli API jest dostępne publicznie, ustaw `JWT_SECRET`.
- Jeśli frontend działa z credentials, nie używaj `CORS_ALLOW_ORIGINS=*`; ustaw konkretne domeny.
- `OPENAI_API_KEY_FILE` jest bezpieczniejszy operacyjnie niż trzymanie klucza bezpośrednio w `.env`, jeśli środowisko wspiera mount sekretów.
