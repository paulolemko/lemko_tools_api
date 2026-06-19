# Skrypty CLI i Moduły

Ten dokument opisuje zawartość katalogu `scripts/`. Część plików jest uruchamialna bezpośrednio, część pełni rolę bibliotek dla API.

## scripts/app.py

Główna aplikacja FastAPI.

Uruchomienie lokalne:

```bash
uvicorn --app-dir scripts app:app --host 0.0.0.0 --port 8000
```

Uruchomienie w kontenerze:

```bash
uvicorn --app-dir /app/scripts app:app --host 0.0.0.0 --port 8000
```

Odpowiada za:

- inicjalizację logów i katalogów runtime,
- ładowanie `ASREngine` na starcie,
- lazy-load modułów słownikowych,
- lazy-load silnika StyleTTS2,
- autoryzację bearer tokenem,
- endpointy `/healthz`, `/readyz`, `/v1/transcriptions`, `/v1/lemko/search`, `/v1/lemko/translate/pl`, `/v1/tts`.

Nie jest projektowany jako typowe CLI poza uruchomieniem przez Uvicorn.

## scripts/asr_engine.py

Silnik transkrypcji RNNT.

### Rola w API

`app.py` importuje:

```python
from asr_engine import ASREngine, ASRConfig
```

Na starcie:

```python
engine = ASREngine.from_env().load()
```

### Zachowanie

1. Sprawdza istnienie pliku `MODEL_PATH`.
2. Ładuje model `nemo_asr.models.EncDecHybridRNNTCTCBPEModel.restore_from()`.
3. Ustawia strategię dekodowania RNNT beam.
4. Wykonuje warmup na krótkim pliku WAV z zerami.
5. Dla każdego audio:
   - liczy SHA256 oryginału,
   - konwertuje do mono,
   - resampluje do `TARGET_SR`,
   - dzieli na chunki na podstawie obwiedni RMS,
   - transkrybuje chunki,
   - zwraca tekst i segmenty SRT-like.

### CLI

```bash
python3 scripts/asr_engine.py --audio sample.wav --txt out.txt --json out.json
```

Argumenty:

| Argument | Wymagany | Opis |
| --- | --- | --- |
| `--audio` | tak | Ścieżka do audio. |
| `--txt` | nie | Zapis samego tekstu do pliku. |
| `--json` | nie | Zapis pełnego obiektu `text`, `words`, `meta`. |

Wymaga poprawnego `MODEL_PATH` i zależności NeMo/PyTorch.

## scripts/lem-search.py

Wyszukiwanie łemkowskiego hasła po formie podstawowej lub formie odmienionej.

### Rola w API

`app.py` ładuje ten plik dynamicznie, bo nazwa zawiera myślnik:

```python
spec_from_file_location("lem_search_module", Path(__file__).with_name("lem-search.py"))
```

Endpoint:

```text
POST /v1/lemko/search
```

### Algorytm

1. Ładuje strukturę morfologii.
2. Łączy się z PostgreSQL przez `DATABASE_URL`.
3. Próbuje znaleźć hasła po `base_form`.
4. Jeśli nie znajdzie, szuka w `term_word_associations.word`.
5. Jeśli nadal nie znajdzie, pobiera sugestie FastText przez `fasttext2lemtools.suggest`.
6. Dla wyników pobiera formy odmienione, buduje drzewo form i grupuje wyniki po części mowy oraz rdzeniu hasła.

SQL filtruje tylko:

```sql
deleted = FALSE
redacted = TRUE
```

### CLI

```bash
python3 scripts/lem-search.py "бесіда"
python3 scripts/lem-search.py "бесіда" --json
```

Argumenty:

| Argument | Opis |
| --- | --- |
| `word` | Szukane hasło lub forma. |
| `--json` | Zwraca wynik jako JSON. |
| `--morphology-file PATH` | Jawny plik morfologii. |
| `--similar-limit N` | Limit sugestii FastText, domyślnie `10`. |
| `--similar-lang LANG` | Język dla FastText, domyślnie `lem`. |
| `--similar-vocab-dir PATH` | Katalog `vocab_{lang}.json`. |
| `--similar-debug` | Diagnostyka sugestii. |

Przykład JSON:

```bash
DATABASE_URL=postgres://postgres:pass@127.0.0.1:5432/lemslownik \
python3 scripts/lem-search.py "бесіда" --json
```

## scripts/pl-lem-search.py

Wyszukiwanie haseł łemkowskich po tłumaczeniu polskim albo angielskim.

### Rola w API

Endpointy:

```text
POST /v1/lemko/search/pl
POST /v1/lemko/search/en
```

### Języki

`--lang pl` używa kolumn:

```text
polish_translation
polish_translation_1
polish_translation_2
polish_translation_3
polish_translation_4
```

`--lang en` używa kolumn:

```text
english_translation
english_translation_1
english_translation_2
english_translation_3
english_translation_4
```

### Algorytm

1. Generuje warianty zapytania przez proste normalizacje fleksyjne.
2. Wyszukuje tłumaczenie przez `ILIKE`.
3. Tokenizuje wartości tłumaczeń po przecinkach, średnikach, slashach i whitespace.
4. Dopasowuje znormalizowane tokeny.
5. Jeśli brak wyników, używa sugestii FastText.
6. Jeśli nadal brak wyników, może użyć OpenAI do zasugerowania formy podstawowej.
7. Usuwa rzymskie sufiksy z form bazowych w widoku `base_form`.

### CLI

```bash
python3 scripts/pl-lem-search.py "rozmowa" --pl
python3 scripts/pl-lem-search.py "conversation" --en
```

Argumenty:

| Argument | Opis |
| --- | --- |
| `word` | Słowo albo fraza. |
| `--lang pl|en` | Język zapytania, domyślnie `pl`. |
| `--pl` | Wymusza polski. |
| `--en` | Wymusza angielski. |
| `--suggest-limit N` | Limit sugestii FastText, domyślnie `15`. |
| `--no-suggest` | Wyłącza fallback FastText i LLM. |
| `--llm-model MODEL` | Model OpenAI dla formy podstawowej, domyślnie `gpt-5-mini`. |
| `--llm-debug` | Diagnostyka OpenAI. |
| `--suggest-vocab-dir PATH` | Katalog vocab. |
| `--suggest-debug` | Diagnostyka FastText. |

## scripts/lem_translate.py

Tłumaczenie tekstu łemkowskiego na polski z pomocą OpenAI i słownika.

### Rola w API

Endpoint:

```text
POST /v1/lemko/translate/pl
```

`app.py` importuje:

```python
from lem_translate import DEFAULT_MODEL, lem_translate
```

### Przepływ

1. Weryfikuje niepusty tekst.
2. Tworzy klienta OpenAI.
3. Wysyła pierwszy prompt z wymuszonym JSON:
   - `translated_text`,
   - `unknown_words`,
   - `needs_dictionary`.
4. Jeśli model jest pewny, zwraca wynik po pierwszym kroku.
5. Jeśli model potrzebuje słownika, pobiera hasła z PostgreSQL:
   - najpierw po `terms.base_form`,
   - potem po `term_word_associations.word`,
   - dodatkowo może używać sugestii FastText.
6. Wysyła drugi prompt z definicjami i kontekstami.
7. Zwraca finalny JSON.

### CLI

```bash
python3 scripts/lem_translate.py "Тест бесіды по лемківскы."
python3 scripts/lem_translate.py input.txt --model gpt-5 --service-tier priority
```

Argumenty:

| Argument | Opis |
| --- | --- |
| `text` | Literalny tekst albo ścieżka do pliku `.txt`. |
| `--model MODEL` | Model OpenAI, domyślnie `gpt-5`. |
| `--service-tier TIER` | Opcjonalny `service_tier`. |

Wymaga:

- `OPENAI_API_KEY` albo `OPENAI_API_KEY_FILE`,
- działającego połączenia do PostgreSQL, jeśli model zgłosi nieznane słowa.

Uwaga: moduł próbuje opcjonalnie załadować `scripts/pl-en-fasttext.py`, którego nie ma w śledzonych plikach repo. Jeśli go nie znajdzie, wypisuje ostrzeżenie i działa dalej bez tego dodatkowego fallbacku.

## scripts/pl_to_lemko_translate.py

Tłumaczenie tekstu polskiego na łemkowski z pomocą Codex CLI, istniejących
endpointów słownikowych i lokalnych reguł z `docs/structured_rules`.

### Rola w API

Endpoint:

```text
POST /v1/polish/translate/lemko
```

`app.py` importuje:

```python
from pl_to_lemko_translate import translate_text
```

### Przepływ

1. Dzieli tekst polski na chunki.
2. Wyciąga polskie terminy i odpytuje `/v1/lemko/search/pl`.
3. Dla znalezionych form łemkowskich odpytuje `/v1/lemko/search`.
4. Ładuje reguły z `docs/structured_rules`.
5. Opcjonalnie dobiera przykłady z raportów pamięci tłumaczeniowej.
6. Uruchamia `codex exec` z wymuszonym schematem JSON.
7. Zwraca tłumaczenie oraz metadane słownikowe i ostrzeżenia.

### CLI

```bash
python3 scripts/pl_to_lemko_translate.py --text "Test tłumaczenia z polskiego na łemkowski." --json
python3 scripts/pl_to_lemko_translate.py --input input.txt --output out.json
```

Wymaga:

- działającego Codex CLI,
- dostępu do endpointów słownikowych API,
- katalogu `docs/structured_rules`.

## scripts/fasttext2lemtools.py

Sugestie podobnych słów na bazie FastText, RapidFuzz i odległości Levenshteina.

### Rola w API

Używany przez:

- `lem-search.py`,
- `pl-lem-search.py`,
- częściowo przez `lem_translate.py` tylko jeśli dostępny jest oczekiwany helper.

### Modele

Domyślne pliki:

```text
cc.pl.300.bin
cc.en.300.bin
ft_words.bin
```

Języki:

- `pl`
- `en`
- `lem`

### CLI

```bash
python3 scripts/fasttext2lemtools.py "rozmowaa" --lang pl --topn 10
python3 scripts/fasttext2lemtools.py "бесідаа" --lang lem --topn 10 --debug
```

Argumenty:

| Argument | Opis |
| --- | --- |
| `word` | Słowo wejściowe. |
| `--lang LANG` | `pl`, `en`, `lem`; domyślnie `pl`. |
| `--topn N` | Liczba sugestii, domyślnie `10`. |
| `--debug` | Wypisuje filtrowanie i scoring. |
| `--vocab-dir PATH` | Katalog `vocab_{lang}.json`. |

Wynik to JSON array:

```json
[
  "rozmowa",
  "rozmowy"
]
```

## scripts/styletts2_engine.py

Biblioteczny silnik TTS używany przez API i `synthesize_from_speaker.py`.

### Rola w API

`app.py` ładuje `StyleTTS2Engine` leniwie przy pierwszym wywołaniu `/v1/tts`.

### Zachowanie

1. Wykrywa katalog StyleTTS2.
2. Weryfikuje `models.py` i `Modules/`.
3. Wczytuje modele:
   - text aligner ASR,
   - F0,
   - PLBERT,
   - checkpoint StyleTTS2.
4. Buduje phonemizer na bazie `lem_phonemizer2.py`.
5. Dla syntezy wybiera pierwsze `num_refs` plików `.wav` z katalogu speakera.
6. Buduje i cache'uje embedding stylu dla zestawu referencji.
7. Generuje waveform 24 kHz.
8. Przycina początek/koniec i nakłada fade.
9. Koduje do M4A/AAC przez `ffmpeg`.

### Publiczne API Pythona

```python
from pathlib import Path
from styletts2_engine import StyleTTS2Engine

engine = StyleTTS2Engine()
result = engine.synthesize_to_file(
    text="Тест бесіды по лемківскы.",
    speaker=0,
    preset="default",
    num_refs=3,
    trim_in_ms=100,
    trim_out_ms=200,
    output_path=Path("tts.m4a"),
)
print(result.output_path, result.rtf)
```

## scripts/synthesize_from_speaker.py

CLI wrapper wokół `styletts2_engine.py`. To preferowany skrypt CLI do TTS.

```bash
python3 scripts/synthesize_from_speaker.py "Тест бесіды по лемківскы." 0 \
  --preset default \
  --num-refs 3 \
  --model-dir /path/to/StyleTTS2 \
  --refs-root /path/to/refs \
  --output out.m4a
```

Argumenty:

| Argument | Opis |
| --- | --- |
| `text` | Tekst do syntezy. |
| `speaker` | `0` albo `1`. |
| `--preset default|less|more` | Preset inferencji. |
| `--num-refs N` | Liczba referencji `.wav`. |
| `--refs-root PATH` | Katalog z podkatalogami `0/` i `1/`. |
| `--model-dir PATH` | Katalog StyleTTS2. |
| `--output PATH` | Plik wyjściowy M4A. |
| `--trim-in-ms N` | Przycięcie początku. |
| `--trim-out-ms N` | Przycięcie końca. |

Skrypt wypisuje urządzenie, wybrane pliki referencyjne, preset, RTF i ścieżkę wyniku.

## scripts/synthesize_from_speaker2.py

Starszy, samodzielny skrypt TTS zawierający dużą część logiki ładowania StyleTTS2 lokalnie w pliku. Jest użyteczny jako fallback diagnostyczny, ale dla API i nowych zastosowań preferowany jest `styletts2_engine.py` oraz `synthesize_from_speaker.py`.

CLI:

```bash
python3 scripts/synthesize_from_speaker2.py "Тест бесіды по лемківскы." 0 \
  --preset default \
  --num-refs 3 \
  --refs-root /path/to/refs \
  --output out.m4a
```

Różnice względem nowszego wrappera:

- ma własną autodetekcję assetów,
- domyślnie `--trim-in-ms 600`,
- nie używa klasy `StyleTTS2Engine`.

## scripts/lem_phonemizer2.py

Segmentowy fonemizator dla tekstu łemkowskiego.

### Zasada działania

1. Przekształca tekst przez reguły `RAW_RULES`.
2. Dzieli wynik na chunki przypisane do backendów:
   - `pl`,
   - `uk`,
   - `bg`,
   - `raw`.
3. Fonemizuje segmenty przez `phonemizer.backend.EspeakBackend`.
4. Skleja IPA.

Najdłuższy wzorzec wygrywa, więc reguły wieloznakowe mają pierwszeństwo przed krótszymi.

### CLI

```bash
python3 scripts/lem_phonemizer2.py "Тест бесіды по лемківскы."
echo "Тест бесіды" | python3 scripts/lem_phonemizer2.py
python3 scripts/lem_phonemizer2.py "Тест" --debug
```

Argumenty:

| Argument | Opis |
| --- | --- |
| `text` | Tekst wejściowy; jeśli brak, czyta stdin. |
| `--pl-code CODE` | Kod espeak dla polskiego, domyślnie `pl`. |
| `--uk-code CODE` | Kod espeak dla ukraińskiego, domyślnie `uk`. |
| `--bg-code CODE` | Kod espeak dla bułgarskiego, domyślnie `bg`. |
| `--debug` | Wypisuje segmenty i ostrzeżenia na stderr. |

Wymaga `espeak` albo `espeak-ng` w systemie.

## scripts/kyr2lat.py

Prosta transliteracja cyrylicy łemkowskiej na zapis łaciński.

### API Pythona

```python
from kyr2lat import lem_transliterate

print(lem_transliterate("Тест бесіды по лемківскы."))
```

### CLI

```bash
python3 scripts/kyr2lat.py input.txt
```

Jeśli podasz plik `.txt`, wynik jest zapisywany obok jako:

```text
<nazwa>_kyr.txt
```

Jeśli nie podasz argumentu, skrypt wypisuje przykładową transliterację zakodowanego tekstu testowego.

## scripts/term-transfer.py

Eksport i import haseł słownikowych z formami między bazami PostgreSQL.

### Obsługiwane tabele

- `public.terms`
- `public.term_word_associations`
- `public.users`
- `public.sources`

### Eksport jednego terminu

```bash
python3 scripts/term-transfer.py export \
  --term-id 8581 \
  --output term_8581.json \
  --database-url "$DATABASE_URL"
```

### Eksport wielu terminów

```bash
python3 scripts/term-transfer.py export-bulk \
  --redacted true \
  --deleted false \
  --limit 1000 \
  --output terms.json
```

### Import

```bash
python3 scripts/term-transfer.py import --input term_8581.json
python3 scripts/term-transfer.py import --input terms.json
```

Argument globalny:

| Argument | Opis |
| --- | --- |
| `--env-file PATH` | Plik `.env`, domyślnie `.env`. |

Subcommand `export`:

| Argument | Opis |
| --- | --- |
| `--term-id ID` | ID terminu. |
| `--output PATH` | Plik JSON. |
| `--database-url URL` | Źródłowa baza. |

Subcommand `export-bulk`:

| Argument | Opis |
| --- | --- |
| `--output PATH` | Plik JSON. |
| `--redacted true|false` | Filtr `terms.redacted`. |
| `--deleted true|false` | Filtr `terms.deleted`. |
| `--limit N` | Opcjonalny limit. |
| `--database-url URL` | Źródłowa baza. |

Subcommand `import`:

| Argument | Opis |
| --- | --- |
| `--input PATH` | Plik JSON z pojedynczym terminem albo payloadem bulk. |
| `--database-url URL` | Docelowa baza. |

Import robi upsert właściciela, źródeł i terminu, usuwa stare formy dla `term_id`, a następnie wstawia formy z payloadu.

## scripts/fill_missing_adjective_forms.py

Uzupełnianie pustych rekordów form odmienionych w `public.term_word_associations` na podstawie części mowy.

Nazwa pliku mówi o przymiotnikach, ale kod obsługuje macierz POS `0..12`.

### Macierz POS

| Kod | Część mowy | Liczba slotów |
| ---: | --- | ---: |
| `0` | rzeczownik | `7 przypadków x 2 liczby = 14` |
| `1` | czasownik | `3 tryby x 3 czasy x 8 osób = 72` |
| `2` | przymiotnik | `3 stopnie x 3 rodzaje x 2 liczby x 7 przypadków = 126` |
| `3` | liczebnik porządkowy | `2 liczby x 7 przypadków = 14` |
| `4` | zaimek odmienny | `2 liczby x 7 przypadków = 14` |
| `5` | zaimek przypadkowy | `7 przypadków = 7` |
| `6` | przysłówek | `1` |
| `7` | przysłówek stopniowalny | `3` |
| `8` | partykuła | `1` |
| `9` | spójnik | `1` |
| `10` | wykrzyknik | `1` |
| `11` | przyimek | `1` |
| `12` | liczebnik główny | `7 przypadków = 7` |

Skrypt wstawia brakujące rekordy z pustym `word`.

### CLI

```bash
python3 scripts/fill_missing_adjective_forms.py --term-id 12886
python3 scripts/fill_missing_adjective_forms.py
```

Argumenty:

| Argument | Opis |
| --- | --- |
| `--term-id ID` | Jeśli podane, przetwarza tylko jeden termin. Bez tego przetwarza wszystkie. |
| `--database-url URL` | Połączenie do bazy. |
| `--env-file PATH` | Plik `.env`, domyślnie `.env`. |

Przykład w kontenerze:

```bash
docker exec -it lemko-asr \
  python3 /app/scripts/fill_missing_adjective_forms.py --term-id 12886
```

## Zależności Między Skryptami

```mermaid
graph TD
  app["app.py FastAPI"]
  asr["asr_engine.py"]
  lemsearch["lem-search.py"]
  plsearch["pl-lem-search.py"]
  translate["lem_translate.py"]
  tts["styletts2_engine.py"]
  ft["fasttext2lemtools.py"]
  phon["lem_phonemizer2.py"]
  synth["synthesize_from_speaker.py"]
  term["term-transfer.py"]
  fill["fill_missing_adjective_forms.py"]

  app --> asr
  app --> lemsearch
  app --> plsearch
  app --> translate
  app --> tts
  lemsearch --> ft
  plsearch --> ft
  translate -. optional .-> ft
  tts --> phon
  synth --> tts
  term --> db["PostgreSQL"]
  fill --> db
  lemsearch --> db
  plsearch --> db
  translate --> db
```

## Szybkie Sprawdzenie Składni

```bash
python3 -m py_compile scripts/*.py
```

To sprawdza składnię, ale nie uruchamia modeli, FastAPI, połączeń DB ani OpenAI.
