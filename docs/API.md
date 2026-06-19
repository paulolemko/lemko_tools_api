# API HTTP

Ten dokument opisuje API wystawiane przez `scripts/app.py`. Aplikacja jest aplikacją FastAPI o tytule `Lemko RNNT ASR – API v1 (engine-separated)` i wersji `1.0.0`.

Domyślny adres lokalny w Docker Compose:

```text
http://127.0.0.1:8000
```

Adres przez proxy Caddy z repozytorium:

```text
https://apiasr.spektrogram.com
https://lemko.tools
https://www.lemko.tools
```

FastAPI publikuje również automatyczną dokumentację runtime:

```text
/docs
/openapi.json
```

## Autoryzacja

Autoryzacja zależy od zmiennej `JWT_SECRET`.

Jeśli `JWT_SECRET` jest pusty, `require_auth()` przepuszcza żądanie bez sprawdzania nagłówka. Jeśli `JWT_SECRET` jest ustawiony, wszystkie endpointy `/v1/...` wymagają:

```http
Authorization: Bearer <wartość JWT_SECRET>
```

Kod nie dekoduje ani nie weryfikuje prawdziwego JWT. Porównuje tekst po `Bearer ` z wartością `JWT_SECRET`.

W `PRODUCTION_MODE=1` aplikacja odmawia startu, jeśli `JWT_SECRET` jest pusty.

Endpointy `GET/HEAD /healthz` i `GET/HEAD /readyz` nie wymagają autoryzacji.

## Format Błędów

Większość błędów tworzonych ręcznie przez aplikację ma format:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Opis błędu",
    "details": {}
  }
}
```

Błędy walidacji Pydantic/FastAPI, np. niepoprawny typ pola albo tekst TTS dłuższy niż limit, używają standardowego formatu FastAPI `422 Unprocessable Entity`.

W `PRODUCTION_MODE=1` większość odpowiedzi 5xx ukrywa wewnętrzne szczegóły w `details` i zastępuje komunikat publicznym komunikatem ogólnym. Szczegóły nadal mogą trafić do logów operatorskich.

## Rate Limiting

Jeśli `PRODUCTION_MODE=1`, rate limiting dla endpointów `/v1/...` jest zawsze włączony. W trybie developerskim można go włączyć przez `RATE_LIMIT_ENABLED=1`.

Konfiguracja:

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `RATE_LIMIT_REQUESTS` | `60` | Maksymalna liczba żądań w oknie. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Długość okna w sekundach. |
| `TRUST_PROXY_HEADERS` | `PRODUCTION_MODE` | Jeśli prawda, używa `X-Forwarded-For`/`X-Real-IP` do identyfikacji klienta. |

Odpowiedź po przekroczeniu limitu:

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Too many requests.",
    "details": {
      "retry_after_seconds": 42
    }
  }
}
```

Status:

```http
429 Too Many Requests
Retry-After: 42
X-RateLimit-Limit: 60
X-RateLimit-Window: 60
X-RateLimit-Remaining: 0
```

## Healthcheck

### HEAD /healthz

Minimalny healthcheck bez body.

Odpowiedź:

```http
HTTP/1.1 200 OK
Content-Length: 0
```

### GET /healthz

Zwraca stan procesu i podstawowe informacje o załadowanym modelu ASR.

Przykład:

```bash
curl -s http://127.0.0.1:8000/healthz
```

Odpowiedź:

```json
{
  "status": "ok",
  "device": "cpu",
  "model_id": "epoch6-step4571_CAPS_WER8.nemo"
}
```

Pola:

- `status` - stała wartość `ok`.
- `device` - urządzenie używane przez `ASREngine`, zwykle `cpu` albo `cuda`.
- `model_id` - nazwa pliku z `MODEL_PATH`.

Uwaga: `GET /healthz` zakłada, że `engine` istnieje. Jeśli model ASR nie załaduje się na starcie, aplikacja zwykle nie wystartuje poprawnie.

### HEAD /readyz

Minimalny readiness check bez body.

Odpowiedź:

```http
HTTP/1.1 200 OK
Content-Length: 0
```

### GET /readyz

Zwraca informację, czy globalny obiekt `engine` został utworzony.

Przykład:

```bash
curl -s http://127.0.0.1:8000/readyz
```

Odpowiedź:

```json
{
  "ready": true
}
```

## ASR: Transkrypcje

Transkrypcje są asynchroniczne. `POST /v1/transcriptions` przyjmuje plik audio, zapisuje upload w archiwum, tworzy job w pamięci procesu i od razu zwraca `202 Accepted`. Sama transkrypcja działa w tle.

### Ważne Zachowanie

- Joby są przechowywane w globalnym słowniku `jobs` w pamięci procesu.
- Restart aplikacji usuwa statusy jobów.
- Artefakt JSON transkrypcji jest zapisywany do `TRANS_DIR`, ale bez joba w pamięci nie ma po restarcie automatycznego indeksu `job_id -> artifact_path`.
- Równoległość ASR ogranicza `MAX_CONCURRENCY`, domyślnie `1`.
- Maksymalny rozmiar uploadu ogranicza `MAX_UPLOAD_MB`, domyślnie `200`.
- Maksymalny czas audio ogranicza `MAX_AUDIO_SECONDS`, domyślnie `7200`. Limit jest egzekwowany, gdy `torchaudio.info()` potrafi odczytać czas trwania pliku.
- `words` w artefakcie oznacza segmenty SRT-like, nie pojedyncze słowa.

### POST /v1/transcriptions

Tworzy job transkrypcji.

Content type:

```http
multipart/form-data
```

Pole formularza:

- `file` - wymagany plik audio.

Dozwolone `Content-Type` pliku:

- `audio/wav`
- `audio/x-wav`
- `audio/flac`
- `audio/mpeg`
- `application/octet-stream`

Jeżeli klient nie poda typu pliku, kod nie odrzuca uploadu na tej podstawie.

Przykład:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/transcriptions \
  -H "Authorization: Bearer $JWT_SECRET" \
  -F "file=@sample.wav;type=audio/wav"
```

Odpowiedź `202`:

```json
{
  "job_id": "2f5b8d1a",
  "filename": "sample.wav",
  "audio_duration_s": 12.345,
  "sha256": null,
  "status": "queued",
  "stage": "oczekuję w kolejce"
}
```

Pola:

- `job_id` - 8 znaków z `uuid.uuid4().hex[:8]`.
- `filename` - oryginalna nazwa pliku.
- `audio_duration_s` - orientacyjna długość z `torchaudio.info()`, może być `null`.
- `sha256` - zawsze `null` na etapie enqueue; hash pojawia się w końcowym artefakcie.
- `status` - początkowo `queued`.
- `stage` - początkowo `oczekuję w kolejce`.

Błędy:

```json
{
  "error": {
    "code": "UNSUPPORTED_MEDIA_TYPE",
    "message": "text/plain is not allowed",
    "details": {
      "allowed": [
        "application/octet-stream",
        "audio/flac",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav"
      ]
    }
  }
}
```

Statusy błędów:

- `401 Missing Bearer token` - jeśli `JWT_SECRET` jest ustawiony i brakuje nagłówka.
- `401 Invalid token` - jeśli token nie jest równy `JWT_SECRET`.
- `413 PAYLOAD_TOO_LARGE` - plik większy niż `MAX_UPLOAD_MB`.
- `413 AUDIO_TOO_LONG` - czas audio większy niż `MAX_AUDIO_SECONDS`.
- `415 UNSUPPORTED_MEDIA_TYPE` - nieobsługiwany typ pliku.
- `422` - brak pola `file` lub niepoprawny multipart.

### GET /v1/transcriptions/{job_id}

Zwraca status joba.

Przykład:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/2f5b8d1a \
  -H "Authorization: Bearer $JWT_SECRET"
```

Odpowiedź:

```json
{
  "job_id": "2f5b8d1a",
  "status": "running",
  "stage": "beam-search RNNT…",
  "progress": 0.6,
  "filename": "sample.wav",
  "audio_duration_s": 12.345,
  "model_id": "epoch6-step4571_CAPS_WER8.nemo",
  "error": null
}
```

Możliwe `status`:

- `queued`
- `running`
- `done`
- `error`

Mapowanie `stage -> progress`:

| Stage | Progress |
| --- | ---: |
| `oczekuję w kolejce` | `0.0` przez fallback |
| `przygotowuję audio…` | `0.1` |
| `beam-search RNNT…` | `0.6` |
| `wyznaczam znaczniki czasu…` | `0.85` |
| `zapisuję wyniki…` | `0.95` |
| `gotowe` | `1.0` |
| `błąd` | `1.0` |

`wyznaczam znaczniki czasu…` jest w mapie postępu, ale aktualny `_process_job()` go nie ustawia.

Błędy:

- `404 NOT_FOUND` - nieznany `job_id`.

### GET /v1/transcriptions/{job_id}/result

Zwraca końcowy tekst transkrypcji dla gotowego joba.

Przykład:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/2f5b8d1a/result \
  -H "Authorization: Bearer $JWT_SECRET"
```

Odpowiedź:

```json
{
  "job_id": "2f5b8d1a",
  "text": "Rozpoznany tekst...",
  "model_id": "epoch6-step4571_CAPS_WER8.nemo",
  "audio_duration_s": 12.345,
  "sha256": "f4b5...",
  "artifact_url": "/v1/transcriptions/2f5b8d1a/artifact"
}
```

Błędy:

- `404 NOT_FOUND` - nieznany `job_id`.
- `409 NOT_READY` - job nie jest zakończony.
- `500 ARTIFACT_ERROR` - nie udało się odczytać pliku artefaktu.

Przykład `409`:

```json
{
  "error": {
    "code": "NOT_READY",
    "message": "Job not finished",
    "details": {
      "status": "running",
      "stage": "beam-search RNNT…"
    }
  }
}
```

### GET /v1/transcriptions/{job_id}/artifact

Zwraca pełny artefakt JSON jako plik.

Przykład:

```bash
curl -sS http://127.0.0.1:8000/v1/transcriptions/2f5b8d1a/artifact \
  -H "Authorization: Bearer $JWT_SECRET"
```

Content type:

```http
application/json
```

Format pliku:

```json
{
  "text": "Rozpoznany tekst...",
  "words": [
    {
      "index": 1,
      "start": "00:00:00,000",
      "end": "00:00:24,800",
      "text": "Pierwszy segment transkrypcji"
    }
  ],
  "meta": {
    "duration_s": 24.8,
    "sha256": "f4b5...",
    "model_id": "epoch6-step4571_CAPS_WER8.nemo",
    "device": "cpu",
    "timestamps_source": "srt-like-from-envelope"
  }
}
```

Błędy:

- `404 NOT_FOUND` - nieznany job albo brak `artifact_path`.

### GET /v1/transcriptions/{job_id}/events

Strumień Server-Sent Events z postępem joba.

Przykład:

```bash
curl -N http://127.0.0.1:8000/v1/transcriptions/2f5b8d1a/events \
  -H "Authorization: Bearer $JWT_SECRET"
```

Content type:

```http
text/event-stream
```

Przykładowe zdarzenia:

```text
data: {"stage": "oczekuję w kolejce", "progress": 0.0}

data: {"stage": "przygotowuję audio…", "progress": 0.1}

data: {"stage": "beam-search RNNT…", "progress": 0.6}

data: {"stage": "gotowe", "progress": 1.0, "status": "done"}
```

Jeśli job zniknie w trakcie streamu:

```text
event: error
data: {"message": "job removed"}
```

Błędy:

- `404 NOT_FOUND` - nieznany `job_id` przed rozpoczęciem streamu.

## Słownik: Wyszukiwanie Łemkowskie

### POST /v1/lemko/search

Wyszukuje hasło łemkowskie po formie podstawowej, rodzinie haseł z sufiksem rzymskim albo formie odmienionej. Jeśli nie ma dopasowania ścisłego, używa sugestii FastText z `fasttext2lemtools.py`.

Request:

```json
{
  "text": "бесіда"
}
```

Przykład:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"бесіда"}'
```

Odpowiedź:

```json
{
  "query": "бесіда",
  "groups": [
    {
      "headword": "бесіда",
      "part_of_speech": {
        "code": 0,
        "label": "назывник"
      },
      "grammatical_attributes": [
        {
          "label": "Рід",
          "value": "женскій рід",
          "values": [
            "женскій рід"
          ],
          "codes": [],
          "enum": "grammatical_gender"
        }
      ],
      "grammatic_description": "opis gramatyczny",
      "entries": [
        {
          "matched_word": "бесіда",
          "semantic_description": "opis znaczenia",
          "contexts": [
            {
              "body": "Przykład użycia...",
              "author": "Жыва бесіда"
            }
          ],
          "source": "new",
          "source_id": 123,
          "order": 1
        }
      ],
      "forms_id": 123,
      "forms_headword": "бесіда",
      "forms": {
        "називаючый": {
          "єднотне чысло": {
            "_words": [
              "бесіда"
            ]
          }
        }
      }
    }
  ],
  "has_results": true
}
```

Pola top-level:

- `query` - oczyszczony tekst wejściowy.
- `groups` - lista grup wyników po części mowy i rdzeniu hasła.
- `has_results` - `true`, jeśli przynajmniej jedna grupa ma wpisy.

Pola grupy:

- `headword` - nagłówek grupy.
- `part_of_speech.code` - kod części mowy z pliku morfologii.
- `part_of_speech.label` - etykieta części mowy z aktualnie załadowanego pliku morfologii.
- `grammatical_attributes` - atrybuty terminu przetłumaczone przez mapy enum.
- `grammatic_description` - opis gramatyczny z bazy.
- `entries` - znaczenia i konteksty.
- `forms_id` - `term_id`, którego odmiana została wybrana do pokazania.
- `forms_headword` - hasło, którego odmiana jest w `forms`.
- `forms` - zagnieżdżone drzewo form odmienionych albo `null`.

Pola wpisu:

- `matched_word` - dopasowane hasło.
- `semantic_description` - opis semantyczny z bazy.
- `contexts` - przykłady użycia.
- `source` - `odf`, jeśli `terms.flagged` jest prawdziwe, inaczej `new`.
- `source_id` - identyfikator terminu.
- `order` - kolejność z `terms.order`.

Brak wyników nie jest błędem HTTP:

```json
{
  "query": "nieznane",
  "groups": [],
  "has_results": false
}
```

Błędy:

- `400 INVALID_REQUEST` - puste `text`.
- `503 LEM_SEARCH_DISABLED` - `LEM_SEARCH_ENABLED=0/false/no/off`.
- `503 LEM_SEARCH_UNAVAILABLE` - nie udało się załadować modułu, morfologii lub połączyć z zależnościami.
- `500 LEM_SEARCH_ERROR` - nieoczekiwany wyjątek.

## Słownik: Polski/Angielski -> Łemkowski

### POST /v1/lemko/search/pl

Wyszukuje hasła łemkowskie po polskich kolumnach tłumaczeń:

- `polish_translation`
- `polish_translation_1`
- `polish_translation_2`
- `polish_translation_3`
- `polish_translation_4`

Request:

```json
{
  "text": "rozmowa"
}
```

Przykład:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search/pl \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"rozmowa"}'
```

### POST /v1/lemko/search/en

Wyszukuje hasła łemkowskie po angielskich kolumnach tłumaczeń:

- `english_translation`
- `english_translation_1`
- `english_translation_2`
- `english_translation_3`
- `english_translation_4`

Request:

```json
{
  "text": "conversation"
}
```

Przykład:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/search/en \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"conversation"}'
```

Odpowiedź obu endpointów:

```json
{
  "query": "rozmowa",
  "language": "pl",
  "checked_variants": [
    "rozmowa",
    "rozmow"
  ],
  "variant_used": "rozmowa",
  "lemko_forms": [
    "бесіда"
  ],
  "entries": [
    {
      "term_id": 123,
      "base_form": "бесіда",
      "raw_base_form": "бесіда I",
      "matched_translations": [
        "rozmowa"
      ],
      "all_translations": [
        "rozmowa",
        "mowa"
      ]
    }
  ],
  "suggestions": [],
  "llm_suggestions": [],
  "fasttext_entries": [],
  "fasttext_form": null,
  "llm_entries": [],
  "llm_form": null,
  "match_source": "primary",
  "has_results": true
}
```

Pola:

- `language` - `pl` albo `en`.
- `checked_variants` - warianty formy sprawdzone przez heurystyki normalizacji.
- `variant_used` - wariant, który finalnie dał wyniki.
- `lemko_forms` - unikatowe formy podstawowe bez rzymskich sufiksów.
- `entries` - finalnie zwracane dopasowania.
- `suggestions` - sugestie FastText sprawdzone po braku dopasowania podstawowego.
- `llm_suggestions` - sugestie FastText wygenerowane po sugestii LLM.
- `fasttext_entries` - wyniki znalezione przez pierwszy skuteczny wariant FastText.
- `fasttext_form` - wariant FastText, który dał wynik.
- `llm_entries` - wyniki znalezione przez wariant LLM albo jego sugestie.
- `llm_form` - forma zaproponowana przez LLM lub sugestia oparta o LLM.
- `match_source` - `primary`, `fasttext`, `llm` albo `none`.
- `has_results` - czy `entries` jest niepuste.

Błędy:

- `400 INVALID_REQUEST` - puste `text` albo nieobsługiwany język w helperze.
- `503 LEM_SEARCH_DISABLED`.
- `503 LEM_SEARCH_UNAVAILABLE`.
- `500 LEM_SEARCH_ERROR`.

## Tłumaczenie Łemkowski -> Polski

### POST /v1/lemko/translate/pl

Tłumaczy tekst łemkowski na polski przy użyciu OpenAI i, jeśli potrzeba, danych słownikowych z PostgreSQL.

Request:

```json
{
  "text": "Тест бесіды по лемківскы."
}
```

Przykład:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/lemko/translate/pl \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Тест бесіды по лемківскы."}'
```

Odpowiedź:

```json
{
  "translated_text": "Test rozmowy po łemkowsku.",
  "resolved_unknown_words": [
    {
      "source_word": "бесіды",
      "lemma": "бесіда",
      "semantic_description": "opis semantyczny z bazy",
      "context": "kontekst z bazy",
      "semantic_description_pl": "polskie tłumaczenie opisu semantycznego"
    }
  ],
  "semantic_description_pl": [
    {
      "lemma": "бесіда",
      "description_pl": "polskie tłumaczenie opisu semantycznego"
    }
  ],
  "missing_words": [],
  "model": "gpt-5",
  "attempts": 2
}
```

Pola:

- `translated_text` - finalne tłumaczenie.
- `resolved_unknown_words` - słowa, które model uznał za niepewne i które znaleziono w słowniku.
- `semantic_description_pl` - tłumaczenia opisów semantycznych haseł wykorzystanych w drugim kroku.
- `missing_words` - słowa zgłoszone przez model, których nie znaleziono w bazie.
- `model` - użyty model OpenAI.
- `attempts` - `1`, jeśli tłumaczenie powstało bez słownika; `2`, jeśli użyto drugiego kroku ze słownikiem.

Wymagania runtime:

- pakiet `openai`,
- `OPENAI_API_KEY` albo `OPENAI_API_KEY_FILE`,
- dla kroku słownikowego: działające `DATABASE_URL` lub `LEM_TRANSLATE_DATABASE_URL`.

Błędy:

- `400 INVALID_REQUEST` - puste `text` albo błąd walidacji tekstu.
- `502 LEM_TRANSLATE_FAILED` - błąd OpenAI albo błąd kontrolowany z modułu tłumaczenia.
- `503 LEM_TRANSLATE_DISABLED` - `LEM_TRANSLATE_ENABLED=0/false/no/off`.
- `500 LEM_TRANSLATE_ERROR` - nieoczekiwany wyjątek.

## Tłumaczenie Polski -> Łemkowski

### POST /v1/polish/translate/lemko

Tłumaczy tekst polski na łemkowski przy użyciu lokalnego modułu
`scripts/pl_to_lemko_translate.py`, reguł z `docs/structured_rules` oraz
istniejących endpointów słownikowych `/v1/lemko/search/pl` i
`/v1/lemko/search`.

Request:

```json
{
  "text": "Polski tekst do tłumaczenia.",
  "max_chars": 1600,
  "max_terms": 30,
  "max_memory_examples": 3,
  "memory_min_score": 0.08,
  "memory_profile_scoring": false,
  "memory_risk_policy": "include",
  "codex_timeout": 900
}
```

Wymagane jest tylko `text`. Pozostałe pola nadpisują limity z konfiguracji
środowiskowej dla pojedynczego żądania.

Przykład:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/polish/translate/lemko \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Test tłumaczenia z polskiego na łemkowski."}'
```

Odpowiedź:

```json
{
  "translated_text": "...",
  "source_lang": "PL",
  "target_lang": "LEM",
  "model": "codex-cli-default",
  "attempts": 1,
  "duration_s": 12.345,
  "resolved_polish_terms": ["test", "tłumaczenia"],
  "dictionary_candidate_count": 2,
  "used_dictionary_entries": [],
  "missing_terms": [],
  "uncertain_terms": [],
  "warnings": [],
  "used_memory_examples": [],
  "memory_risk_policy": "include",
  "limits": {
    "max_chars": 1600,
    "max_terms": 30,
    "max_memory_examples": 3,
    "memory_min_score": 0.08,
    "memory_profile_scoring": false,
    "codex_timeout": 900
  }
}
```

Wymagania runtime:

- `scripts/pl_to_lemko_translate.py` dostępny w kontenerze;
- `docs/structured_rules` zamontowane jako `/app/docs/structured_rules`;
- działające lokalne endpointy słownikowe;
- działający Codex CLI, domyślnie `/usr/local/bin/codex`;
- pliki auth Codex zamontowane analogicznie jak dla istniejącego translatora.

Konfiguracja:

| Zmienna | Domyślnie | Użycie |
| --- | --- | --- |
| `PL_LEM_TRANSLATE_ENABLED` | `1` | Włącza endpoint. |
| `PL_LEM_TRANSLATE_API_BASE` | `http://127.0.0.1:8000` | Baza lokalnych wywołań słownikowych. |
| `PL_LEM_TRANSLATE_API_TOKEN` | puste | Opcjonalny token dla API słownika; przy lokalnej bazie i braku tej zmiennej używa `JWT_SECRET`. |
| `PL_LEM_TRANSLATE_RULES_DIR` | `docs/structured_rules` | Katalog reguł gramatycznych. |
| `PL_LEM_TRANSLATE_CODEX_BIN` | `CODEX_CLI_PATH`/`CODEX_BIN`/`codex` | Ścieżka do Codex CLI. |
| `PL_LEM_TRANSLATE_CODEX_TIMEOUT_SECONDS` | `CODEX_CLI_TIMEOUT_SECONDS` albo `600` | Timeout pojedynczego chunka. |
| `PL_LEM_TRANSLATE_MAX_CHARS` | `1600` | Domyślny limit znaków na chunk. |
| `PL_LEM_TRANSLATE_MAX_TERMS` | `30` | Domyślny limit zapytań słownikowych na chunk. |
| `PL_LEM_TRANSLATE_MAX_MEMORY_EXAMPLES` | `3` | Liczba przykładów pamięci tłumaczeniowej. |
| `PL_LEM_TRANSLATE_MEMORY_MIN_SCORE` | `0.08` | Minimalny score pamięci tłumaczeniowej. |
| `PL_LEM_TRANSLATE_MEMORY_PROFILE_SCORING` | `0` | Włącza scoring profilowy pamięci. |
| `PL_LEM_TRANSLATE_MEMORY_RISK_POLICY` | `include` | `include`, `demote` albo `exclude`. |

Błędy:

- `400 INVALID_REQUEST` - puste `text` albo błąd walidacji pól.
- `502 PL_LEM_TRANSLATE_FAILED` - błąd kontrolowany z tłumacza, API słownika albo Codex CLI.
- `503 PL_LEM_TRANSLATE_DISABLED` - `PL_LEM_TRANSLATE_ENABLED=0/false/no/off`.
- `500 PL_LEM_TRANSLATE_ERROR` - nieoczekiwany wyjątek.

## TTS

### POST /v1/tts

Generuje mowę z tekstu i zwraca plik M4A.

Request:

```json
{
  "text": "Тест бесіды по лемківскы.",
  "speaker": 0,
  "preset": "default"
}
```

Pola:

- `text` - wymagany tekst, po strip musi mieć 1 znak lub więcej i nie może przekroczyć `TTS_TEXT_MAX_CHARS`.
- `speaker` - `0` albo `1`, domyślnie `0`.
- `preset` - `default`, `less` albo `more`, domyślnie `default`.

Presety StyleTTS2:

| Preset | alpha | beta | diffusion_steps | embedding_scale |
| --- | ---: | ---: | ---: | ---: |
| `default` | `0.3` | `0.7` | `10` | `1.0` |
| `less` | `0.1` | `0.3` | `10` | `1.0` |
| `more` | `0.5` | `0.95` | `10` | `1.0` |

Przykład:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/tts \
  -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Тест бесіды по лемківскы.", "speaker":0, "preset":"default"}' \
  -D tts.headers \
  --output tts.m4a
```

Odpowiedź:

```http
HTTP/1.1 200 OK
Content-Type: audio/mp4
Content-Disposition: attachment; filename="tts_0_ab12cd34.m4a"
X-TTS-RTF: 0.54321
X-TTS-Duration: 2.340
X-TTS-Elapsed: 1.271
```

Body odpowiedzi to binarny plik M4A.

Nagłówki:

- `X-TTS-RTF` - real-time factor, `elapsed_s / duration_s`.
- `X-TTS-Duration` - długość wygenerowanego audio w sekundach.
- `X-TTS-Elapsed` - czas generowania w sekundach.

Wymagania runtime:

- katalog StyleTTS2 wykryty automatycznie albo wskazany przez `STYLE_TTS2_DIR`,
- katalog referencji speakerów wykryty automatycznie albo wskazany przez `STYLE_TTS2_REFS_ROOT`,
- podkatalogi referencji `0/` i `1/` z plikami `.wav`,
- `ffmpeg` w `PATH`,
- `espeak` albo `espeak-ng`,
- modele StyleTTS2, ASR helper, F0 i PLBERT wewnątrz katalogu StyleTTS2.

Błędy:

- `400 INVALID_REQUEST` - zły speaker, preset, liczba referencji albo niemożliwe przycięcie fali.
- `422` - walidacja Pydantic, np. za długi tekst.
- `500 TTS_RESOURCE_NOT_FOUND` - brak katalogu/modelu/referencji.
- `500 TTS_FAILED` - błąd kontrolowany silnika TTS.
- `500 TTS_ERROR` - nieoczekiwany wyjątek.

## Logowanie Wywołań API

Na starcie aplikacja inicjalizuje pliki CSV i JSONL:

- `LOG_PATH` - JSONL z eventami `enqueue`, `done`, `error`, `tts`.
- `TRANSCRIPTIONS_CSV_PATH` - `filename,timestamp,size_bytes,transcript_text`.
- `LEM_SEARCH_LOG_PATH` - `timestamp,endpoint,query,result`.
- `LEM_TRANSLATE_LOG_PATH` - `timestamp,endpoint,query,result_text`.
- `LEM_TTS_LOG_PATH` - `timestamp,endpoint,speaker,text`.

Szczegóły formatów są w [DATA_MODEL.md](DATA_MODEL.md).

## CORS

FastAPI dodaje `CORSMiddleware`:

- `allow_origins` z `CORS_ALLOW_ORIGINS`, domyślnie `*`;
- `allow_credentials=True`;
- metody `GET`, `POST`, `OPTIONS`;
- nagłówki `*`.

Caddyfile dodatkowo ustawia:

```caddy
header Access-Control-Allow-Origin "*"
```

Jeśli frontend ma używać cookies albo credentialed requests, unikaj wildcard `*` i ustaw konkretną listę originów w `CORS_ALLOW_ORIGINS`.

W `PRODUCTION_MODE=1` wildcard `*` jest niedozwolony i aplikacja nie wystartuje z taką konfiguracją.
