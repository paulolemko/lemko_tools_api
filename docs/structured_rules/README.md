# Structured Lemko Rule Tables

Ten katalog zawiera maszynowo wydobyte tabele z `docs/rule_sources/*.md`.

## Pliki

- `tables.json` - pelny indeks tabel z metadanymi i sparsowanymi wierszami.
- `summary.json` - podsumowanie liczby tabel i wierszy wedlug pliku zrodlowego.
- `tables/table-*.md` - osobne tabele markdown z informacja o pliku zrodlowym, naglowku i linii startowej.

## Zakres

Wydobyto 127 tabel:

- alfabet/grafemy: 1
- rzeczownik: 48
- przymiotnik: 17
- liczebnik: 16
- zaimek: 18
- czasownik: 23
- imieslow przymiotnikowy: 1
- ortografia: 3

## Jak tego uzywac

Dla algorytmu lub skillu LLM:

1. Uzyj `summary.json`, aby znalezc kategorie.
2. Uzyj `tables.json`, aby dobrac tabele po `source_file`, `heading` albo `source_start_line`.
3. Uzyj `markdown_file`, gdy potrzebny jest czytelny kontekst dla modelu.
4. Traktuj `table` jako surowy material paradygmatyczny. Naglowki nie zawsze sa unikalne, dlatego identyfikuj tabele przez `id` i `source_start_line`.

## Ograniczenia

Ekstraktor zachowuje tabele, ale nie interpretuje jeszcze semantyki kolumn. Kolejny etap powinien zamienic najwazniejsze paradygmaty na jawne reguly typu:

```json
{
  "part_of_speech": "noun",
  "declension": "I",
  "stem_type": "hard",
  "forms": {
    "singular": {
      "nominative": "-а"
    }
  }
}
```
