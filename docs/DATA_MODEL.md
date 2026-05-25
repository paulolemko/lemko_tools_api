# Dane, Artefakty i Model Słownika

Ten dokument opisuje dane używane przez repozytorium: tabele PostgreSQL, pliki morfologii, artefakty transkrypcji, logi i dane modeli.

## PostgreSQL

Kod zakłada bazę słownika w PostgreSQL. Najważniejsze tabele używane przez skrypty:

- `public.terms`
- `public.term_word_associations`
- `public.sources`
- `public.users`

Repozytorium nie zawiera migracji ani pełnego schematu SQL. Poniżej opis wynika z zapytań wykonywanych przez kod.

## public.terms

Tabela haseł słownikowych.

### Pola używane przez wyszukiwanie

`lem-search.py` i `pl-lem-search.py` czytają m.in.:

```text
id
base_form
grammatical_part_of_speech
grammatical_gender
grammatical_declension
grammatical_conjugation
grammatical_aspect
grammatical_stem
grammatical_numeral_type
grammatical_pronoun_type
grammatical_adverb_type
grammatical_aspect_pair
grammatic_description
semantic_description
polish_translation
english_translation
context1_body
context1_tag
context1_source_id
context2_body
context2_tag
context2_source_id
context3_body
context3_tag
context3_source_id
order
flagged
deleted
redacted
```

`pl-lem-search.py` dodatkowo używa kolumn wariantowych:

```text
polish_translation_1
polish_translation_2
polish_translation_3
polish_translation_4
english_translation_1
english_translation_2
english_translation_3
english_translation_4
```

`term-transfer.py` eksportuje i importuje pełny rekord `SELECT * FROM public.terms`.

### Filtry widoczności

Wyszukiwarki publiczne filtrują:

```sql
deleted = FALSE
redacted = TRUE
```

To oznacza, że niewyredagowane albo usunięte hasła nie powinny pojawiać się w API wyszukiwania.

### Rzymskie Sufiksy

Kod obsługuje formy typu:

```text
бесіда I
бесіда II
```

`lem-search.py` grupuje rodzinę haseł po bazie bez końcowego rzymskiego sufiksu. `pl-lem-search.py` usuwa sufiks w polu `base_form`, ale zachowuje oryginał w `raw_base_form`.

## public.term_word_associations

Tabela form odmienionych terminu.

Pola używane przez kod:

```text
id
term_id
word
grammatical_case
grammatical_person
grammatical_number
grammatical_comparison
grammatical_mood
grammatical_tense
grammatical_gender
created_at
updated_at
```

`lem-search.py` używa tej tabeli do:

- znalezienia hasła po wpisanej formie odmienionej,
- zbudowania drzewa form dla terminu.

`fill_missing_adjective_forms.py` wstawia brakujące rekordy slotów z pustym `word`.

`term-transfer.py` przy imporcie usuwa wszystkie stare formy danego `term_id` i wstawia formy z payloadu.

## public.sources

Tabela źródeł kontekstów.

Pola używane przez kod:

```text
id
authors
```

`lem-search.py` łączy `sources` jako:

```sql
LEFT JOIN public.sources AS s1 ON s1.id = t.context1_source_id
LEFT JOIN public.sources AS s2 ON s2.id = t.context2_source_id
LEFT JOIN public.sources AS s3 ON s3.id = t.context3_source_id
```

Jeśli kontekst nie ma autora, API używa domyślnego autora:

```text
Жыва бесіда
```

## public.users

`term-transfer.py` może eksportować i importować właściciela hasła z `public.users`, jeśli `terms.owner_id` nie jest `NULL`.

Wyszukiwarki API nie używają danych użytkowników w odpowiedziach.

## Pliki Morfologii

Katalog:

```text
morphology_structure_pl_lem_eng/
```

Pliki:

```text
morphology_structure_lem01.json
morphology_structure_pl01.json
morphology_structure_eng01.json
```

Każdy plik ma top-level keys:

```json
{
  "generated_at_utc": "...",
  "source_tables": {},
  "enums": {},
  "parts_of_speech": {}
}
```

### Części Mowy

Wszystkie trzy pliki zawierają 13 części mowy o kodach `0..12`.

Wersja polska:

| Kod | Label |
| ---: | --- |
| `0` | `rzeczownik` |
| `1` | `czasownik` |
| `2` | `przymiotnik` |
| `3` | `liczebnik porządkowy` |
| `4` | `zaimek odmienny (liczba + przypadek)` |
| `5` | `zaimek (przypadek)` |
| `6` | `przysłówek` |
| `7` | `przysłówek stopniowalny` |
| `8` | `partykuła` |
| `9` | `spójnik` |
| `10` | `wykrzyknik` |
| `11` | `przyimek` |
| `12` | `liczebnik główny` |

Wersja angielska:

| Kod | Label |
| ---: | --- |
| `0` | `noun` |
| `1` | `verb` |
| `2` | `adjective` |
| `3` | `ordinal numeral` |
| `4` | `declinable pronoun (number + case)` |
| `5` | `pronoun (case)` |
| `6` | `adverb` |
| `7` | `gradable adverb` |
| `8` | `particle` |
| `9` | `conjunction` |
| `10` | `interjection` |
| `11` | `preposition` |
| `12` | `cardinal numeral` |

Wersja łemkowska ma etykiety cyryliczne, np. `назывник`, `часослово`, `придавник`.

### Enumy

Każdy plik zawiera 14 enumów:

```text
grammatical_case
grammatical_number
grammatical_person
grammatical_mood
grammatical_tense
grammatical_gender
noun_declension
verb_aspect
verb_conjugation
adjective_stem
grammatical_comparison
numeral_type
pronoun_type
adverb_type
```

`lem-search.py` używa enumów do przekształcania kodów liczbowych z bazy na etykiety w odpowiedzi API.

### Drzewo Form

Pole `forms` w odpowiedzi `/v1/lemko/search` jest zagnieżdżonym drzewem zbudowanym z wymiarów z `parts_of_speech[POS].form_dimensions`.

Przykład uproszczony:

```json
{
  "mianownik": {
    "liczba pojedyncza": {
      "_words": [
        "forma"
      ]
    }
  },
  "dopełniacz": {
    "liczba pojedyncza": {
      "_words": [
        "formy"
      ]
    }
  }
}
```

Klucz `_words` jest liściem zawierającym listę rzeczywistych form tekstowych.

## Artefakty Transkrypcji

### Archiwum Uploadów

Każdy upload audio jest zapisywany w `TRANSCRIPTED_SOURCE_DIR`.

Nazwa:

```text
<UTC YYYYMMDDTHHMMSSZ>__<job_id>__<sanitized_original_filename>
```

Przykład:

```text
20260525T131500Z__2f5b8d1a__sample.wav
```

Sanityzacja nazwy:

- używa tylko basename,
- znaki inne niż alnum, `-`, `_`, `.` zamienia na `_`,
- obcina do 80 znaków,
- usuwa wiodące/końcowe `.` i `_`,
- jeśli nazwa wyjdzie pusta, używa `upload`.

### Artefakt JSON ASR

Po zakończeniu transkrypcji `_process_job()` zapisuje artefakt do `TRANS_DIR`.

Nazwa:

```text
<stem oryginalnego pliku>__<pierwsze 8 znaków sha256>.json
```

Przykład:

```text
sample__f4b5a123.json
```

Format:

```json
{
  "text": "Pełny rozpoznany tekst",
  "words": [
    {
      "index": 1,
      "start": "00:00:00,000",
      "end": "00:00:24,800",
      "text": "Tekst segmentu"
    }
  ],
  "meta": {
    "duration_s": 24.8,
    "sha256": "pełny sha256 oryginalnego pliku",
    "model_id": "epoch6-step4571_CAPS_WER8.nemo",
    "device": "cpu",
    "timestamps_source": "srt-like-from-envelope"
  }
}
```

`words` jest nazwą odziedziczoną po wcześniejszym formacie. Aktualnie zawiera segmenty wynikające z chunkowania po obwiedni, nie pojedyncze słowa ani alignments modelu.

## Logi

### LOG_PATH

JSON Lines. Każda linia jest osobnym obiektem JSON z polem `ts` dodanym przez `append_log()`.

Event enqueue:

```json
{
  "event": "enqueue",
  "job_id": "2f5b8d1a",
  "filename": "sample.wav",
  "size": 123456,
  "archived_path": "/app/logs/transcripted_source/...",
  "ts": "2026-05-25T13:15:00.000000Z"
}
```

Event done:

```json
{
  "event": "done",
  "job_id": "2f5b8d1a",
  "artifact_path": "transkrypcje/sample__f4b5a123.json",
  "archived_path": "/app/logs/transcripted_source/...",
  "ts": "2026-05-25T13:16:00.000000Z"
}
```

Event error:

```json
{
  "event": "error",
  "job_id": "2f5b8d1a",
  "error": "opis wyjątku",
  "archived_path": "/app/logs/transcripted_source/...",
  "ts": "2026-05-25T13:16:00.000000Z"
}
```

Event tts:

```json
{
  "event": "tts",
  "speaker": 0,
  "preset": "default",
  "text_chars": 32,
  "rtf": 0.5,
  "duration_s": 2.3,
  "ts": "2026-05-25T13:16:00.000000Z"
}
```

### TRANSCRIPTIONS_CSV_PATH

Nagłówki:

```csv
filename,timestamp,size_bytes,transcript_text
```

Wiersz jest dopisywany dopiero po udanej transkrypcji.

`transcript_text` jest spłaszczany do jednej linii przez usunięcie line breaks.

### LEM_SEARCH_LOG_PATH

Nagłówki:

```csv
timestamp,endpoint,query,result
```

`result`:

- `found` - wynik zawiera trafienia,
- `failed` - brak trafień albo błąd.

### LEM_TRANSLATE_LOG_PATH

Nagłówki:

```csv
timestamp,endpoint,query,result_text
```

`result_text` to finalne tłumaczenie, spłaszczone do jednej linii. W błędach zwykle puste.

### LEM_TTS_LOG_PATH

Nagłówki:

```csv
timestamp,endpoint,speaker,text
```

Log jest dopisywany przed próbą syntezy, więc może istnieć także dla żądań zakończonych błędem TTS.

## Modele i Dane Nieśledzone Przez Git

`.gitignore` i `.dockerignore` wykluczają duże pliki oraz dane runtime.

Typowe katalogi:

```text
models/
logs/
psql_dump/
qdrant_data/
qdrant_snapshots/
vocab_json/
transkrypcje/
artifacts/
```

Typowe modele:

```text
models/epoch6-step4571_CAPS_WER8.nemo
models/cc.pl.300.bin
models/cc.en.300.bin
models/ft_words.bin
models/StyleTTS2/
```

## Payload term-transfer

`scripts/term-transfer.py export` tworzy payload:

```json
{
  "meta": {
    "exported_at": "2026-05-25T13:15:00+00:00",
    "term_id": 8581
  },
  "term": {},
  "forms": [],
  "owner": null,
  "sources": []
}
```

`export-bulk` tworzy:

```json
{
  "meta": {
    "exported_at": "2026-05-25T13:15:00+00:00",
    "mode": "bulk",
    "filters": {
      "redacted": true,
      "deleted": false
    },
    "term_count": 100,
    "forms_count": 1400
  },
  "terms": [
    {
      "meta": {},
      "term": {},
      "forms": [],
      "owner": null,
      "sources": []
    }
  ]
}
```

Import bulk jest transakcyjny na poziomie całego payloadu. Jeśli któryś element rzuci wyjątek, kod robi `rollback()`.

## Dane Referencyjne TTS

StyleTTS2 wymaga katalogu referencji speakera:

```text
<refs_root>/
  0/
    *.wav
  1/
    *.wav
```

`TTS_NUM_REFS` określa, ile pierwszych posortowanych plików `.wav` zostanie użytych. Jeśli jest mniej plików niż `TTS_NUM_REFS`, synteza kończy się błędem.

Embedding stylu jest cache'owany w pamięci po krotce ścieżek referencji.

## Słowniki Vocab dla FastText

`fasttext2lemtools.py` oczekuje JSON:

```json
{
  "vocab": [
    "word1",
    "word2"
  ],
  "frequency": {
    "word1": 1.0,
    "word2": 0.5
  }
}
```

Albo:

```json
{
  "vocab": [
    "word1",
    "word2"
  ],
  "frequency": [
    ["word1", 1.0],
    ["word2", 0.5]
  ]
}
```

Brak częstotliwości dla słowa oznacza `0.0`.
