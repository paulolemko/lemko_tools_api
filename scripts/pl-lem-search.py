#!/usr/bin/env python3
"""Wyszukaj hasła łemkowskie na podstawie polskiego tłumaczenia, z fallbackiem fastText (fasttext2lemtools)."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import psycopg2
import psycopg2.extras

try:  # pragma: no cover - optional dependency
    # Korzystamy z tej samej logiki sugestii co lem-search, żeby utrzymać spójność w fallbackach.
    from fasttext2lemtools import suggest as ft_suggest  # type: ignore
except ImportError:  # pragma: no cover
    ft_suggest = None  # type: ignore

try:  # pragma: no cover - optional dependency
    from openai import OpenAI  # type: ignore
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

try:  # pragma: no cover - optional shared Codex CLI wrapper
    from lem_translate import codex_complete_text  # type: ignore
except Exception:  # pragma: no cover
    codex_complete_text = None  # type: ignore

try:
    from importlib import import_module

    _lem_translations = import_module("lem-translations")
    _ensure_openai_key = getattr(_lem_translations, "_ensure_openai_key", None)
except Exception:  # pragma: no cover
    _ensure_openai_key = None

if _ensure_openai_key is None:  # pragma: no cover - fallback for API deployments
    def _ensure_openai_key() -> str:
        """
        Minimal fallback that mirrors the helper from `lem-translations` by
        ensuring an OpenAI API key is available via environment variables.
        """
        direct_key = os.getenv("OPENAI_API_KEY", "").strip()
        if direct_key:
            return direct_key

        key_file = os.getenv("OPENAI_API_KEY_FILE")
        if key_file:
            try:
                path = Path(key_file).expanduser()
            except Exception as exc:
                raise RuntimeError(f"Niepoprawna ścieżka OPENAI_API_KEY_FILE: {key_file!r}") from exc
            if path.is_file():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    os.environ["OPENAI_API_KEY"] = content
                    return content

        raise RuntimeError(
            "Brak klucza OpenAI. Ustaw OPENAI_API_KEY lub OPENAI_API_KEY_FILE."
        )


DEFAULT_DATABASE_URL = "postgres://lemslownik:lemslownik@127.0.0.1:5432/lemslownik"

TOKEN_SPLIT_RE = re.compile(r"[;,/]|\\s+")  # rozdzielaj po przecinkach, średnikach i białych znakach
POLISH_SUFFIXES: Tuple[str, ...] = (
    "ami",
    "ach",
    "owi",
    "owa",
    "owe",
    "ego",
    "emu",
    "ami",
    "om",
    "ów",
    "iu",
    "ie",
    "em",
    "om",
    "ą",
    "ę",
    "a",
    "u",
    "y",
    "i",
    "e",
)

ROMAN_TRANSLIT_MAP = str.maketrans(
    {
        "І": "I",
        "Ї": "I",
        "V": "V",
        "Ł": "L",  # rarely used but safe
    }
)
ROMAN_NUMERAL_SET = {"I", "II", "III", "IV", "V"}


def _strip_roman_suffix(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    parts = stripped.rsplit(" ", 1)
    if len(parts) != 2:
        return stripped
    suffix = parts[1].translate(ROMAN_TRANSLIT_MAP).upper()
    if suffix in ROMAN_NUMERAL_SET:
        return parts[0]
    return stripped


def _normalize_polish(word: str) -> Set[str]:
    base = word.strip().lower()
    forms: Set[str] = {base}
    for suffix in POLISH_SUFFIXES:
        if len(base) > len(suffix) + 1 and base.endswith(suffix):
            forms.add(base[: -len(suffix)])
    return forms


def _normalize_english(word: str) -> Set[str]:
    base = word.strip().lower()
    forms: Set[str] = {base}
    if base.endswith("'s") and len(base) > 2:
        forms.add(base[:-2])
    if base.endswith("ies") and len(base) > 3:
        forms.add(base[:-3] + "y")
    if base.endswith("es") and len(base) > 3 and not base.endswith("ses"):
        forms.add(base[:-2])
    if base.endswith("s") and len(base) > 2 and not base.endswith("ss"):
        forms.add(base[:-1])
    return forms


def _normalize_word(word: str, lang: str) -> Set[str]:
    if lang == "en":
        return _normalize_english(word)
    return _normalize_polish(word)


LANG_CONFIG: Dict[str, Dict[str, object]] = {
    "pl": {
        "translation_columns": (
            "polish_translation",
            "polish_translation_1",
        "polish_translation_2",
        "polish_translation_3",
        "polish_translation_4",
    ),
    "suggest_lang": "pl",
    "llm_prompt": "Podaj najbardziej prawdopodobną podstawową formę polskiego słowa lub wyrażenia \"{word}\". Odpowiedz tylko jednym słowem.",
},
"en": {
        "translation_columns": (
            "english_translation",
            "english_translation_1",
        "english_translation_2",
        "english_translation_3",
        "english_translation_4",
    ),
    "suggest_lang": "en",
    "llm_prompt": "Provide the most probable base (dictionary) form of the English word or expression \"{word}\". Respond with a single lemma.",
},
}


@dataclass
class MatchResult:
    term_id: int
    base_form: str
    tokens: List[str]
    matched_tokens: List[str]


def _load_connection_settings(raw_url: str) -> Dict[str, Optional[str]]:
    from urllib.parse import urlparse

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"Unsupported database scheme: {parsed.scheme!r}")
    return {
        "dbname": (parsed.path or "/").lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
    }


def _get_connection(retries: int = 3, retry_delay: float = 1.5) -> psycopg2.extensions.connection:
    import time

    raw_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    params = _load_connection_settings(raw_url)

    hosts: List[str] = []
    host = params.get("host")
    if host:
        hosts.append(host)
        if host == "127.0.0.1":
            hosts.append("db")
        elif host in {"db", "localhost"}:
            hosts.append("127.0.0.1")

    for host_option in hosts or [None]:
        current = params.copy()
        if host_option is not None:
            current["host"] = host_option
        for attempt in range(retries):
            try:
                return psycopg2.connect(**current)
            except psycopg2.OperationalError:
                if attempt == retries - 1:
                    break
                time.sleep(retry_delay * (attempt + 1))
    raise psycopg2.OperationalError("Failed to establish database connection")


def _tokenize(text: Optional[str]) -> List[str]:
    if not text:
        return []
    cleaned = text.replace("\r", " ").replace("\n", " ").strip()
    if not cleaned:
        return []
    tokens = [token.strip() for token in TOKEN_SPLIT_RE.split(cleaned) if token.strip()]
    # zachowaj kolejność, usuń duplikaty
    seen: Set[str] = set()
    unique_tokens: List[str] = []
    for token in tokens:
        lower = token.lower()
        if lower not in seen:
            unique_tokens.append(token)
            seen.add(lower)
    return unique_tokens


def _match_tokens(tokens: Iterable[str], needle: str, lang: str) -> List[str]:
    target_forms = _normalize_word(needle, lang)
    matched: List[str] = []
    seen: Set[str] = set()
    for token in tokens:
        stripped = token.strip()
        if not stripped:
            continue
        token_forms = _normalize_word(stripped, lang)
        if target_forms & token_forms:
            key = stripped.lower()
            if key not in seen:
                matched.append(stripped)
                seen.add(key)
    return matched


def _generate_query_variants(phrase: str, lang: str) -> List[str]:
    variants: List[str] = []
    seen: Set[str] = set()
    primary = phrase.strip()
    if primary:
        variants.append(primary)
        seen.add(primary.lower())
    normalized = sorted(_normalize_word(phrase, lang), key=len)
    for form in normalized:
        if form and form not in seen:
            variants.append(form)
            seen.add(form)
    return variants


def _collect_tokens(row: Dict[str, Optional[str]], columns: Sequence[str]) -> List[str]:
    tokens: List[str] = []
    for column in columns:
        tokens.extend(_tokenize(row.get(column)))
    seen: Set[str] = set()
    unique: List[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered not in seen:
            unique.append(token)
            seen.add(lowered)
    return unique


def query_terms_by_language(
    conn: psycopg2.extensions.connection,
    phrase: str,
    columns: Sequence[str],
    lang: str,
) -> List[MatchResult]:
    pattern = f"%{phrase.strip()}%"
    select_columns = ", ".join(columns)
    where_clause = " OR ".join(f"{col} ILIKE %s" for col in columns)
    sql = f"""
        SELECT
            id,
            base_form,
            {select_columns}
        FROM public.terms
        WHERE deleted = FALSE
          AND redacted = TRUE
          AND ({where_clause})
        ORDER BY base_form, id
    """
    params: List[str] = [pattern] * len(columns)

    results: List[MatchResult] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        for row in cur:
            tokens = _collect_tokens(row, columns)
            matched = _match_tokens(tokens, phrase, lang)
            if not matched:
                continue
            results.append(
                MatchResult(
                    term_id=row["id"],
                    base_form=row.get("base_form") or "",
                    tokens=tokens,
                    matched_tokens=matched,
                )
            )
    # scal wyniki o tym samym term_id
    merged: Dict[int, MatchResult] = {}
    for item in results:
        existing = merged.get(item.term_id)
        if not existing:
            merged[item.term_id] = item
            continue
        combined_matches = {token.lower(): token for token in existing.matched_tokens}
        for token in item.matched_tokens:
            combined_matches.setdefault(token.lower(), token)
        existing.matched_tokens = list(combined_matches.values())
    return sorted(merged.values(), key=lambda m: m.base_form.lower())


def _fetch_suggestions(
    word: str,
    lang: str,
    limit: int,
    vocab_dir: Optional[Path],
    debug: bool = False,
    skip_lower: Optional[Set[str]] = None,
) -> List[str]:
    """Pobierz warianty z fasttext2lemtools.suggest, filtrując duplikaty i puste wpisy."""
    if ft_suggest is None:  # pragma: no cover
        return []
    cleaned = word.strip()
    if not cleaned:
        return []
    topn = max(limit, 1)
    kwargs: Dict[str, object] = {
        "word": cleaned,
        "lang": lang,
        "topn": topn,
        "debug": debug,
    }
    if vocab_dir is not None:
        kwargs["vocab_dir"] = str(vocab_dir)
    try:
        raw_candidates = ft_suggest(**kwargs)
    except Exception:
        return []
    results: List[str] = []
    seen: Set[str] = set()
    skip = skip_lower or set()
    for candidate in raw_candidates or []:
        word_candidate = str(candidate).strip()
        if not word_candidate:
            continue
        lower = word_candidate.lower()
        if lower == cleaned.lower() or lower in seen or lower in skip:
            continue
        results.append(word_candidate)
        seen.add(lower)
        if len(results) >= topn:
            break
    return results


def rank_match_results(query: str, matches: Sequence[MatchResult]) -> List[MatchResult]:
    """Sort match results using the same criteria as CLI output."""
    if not matches:
        return []
    ranked: Dict[str, Tuple[Tuple[int, int, int, int, int, int, int, str], MatchResult]] = {}
    query_lower = query.strip().lower()
    for rank, item in enumerate(matches):
        tokens_lower = [token.lower() for token in item.tokens]
        matched_lower = {token.lower() for token in item.matched_tokens}
        exact_translation_value_flag = 1
        for original_token in item.matched_tokens:
            if original_token.strip().lower() == query_lower:
                exact_translation_value_flag = 0
                break
        min_index = len(tokens_lower)
        for idx, token in enumerate(tokens_lower):
            if token in matched_lower:
                min_index = idx
                break
        display_base = _strip_roman_suffix(item.base_form)
        key = display_base.lower()
        exact_base_flag = 0 if key == query_lower else 1
        word_count_flag = display_base.count(" ")
        exact_translation_flag = 0 if query_lower in matched_lower else 1
        position = (
            exact_base_flag,
            word_count_flag,
            exact_translation_value_flag,
            exact_translation_flag,
            min_index,
            rank,
            -len(item.matched_tokens),
            key,
        )
        current = ranked.get(key)
        if current is None or position < current[0]:
            ranked[key] = (position, item)
    ordered_matches = [entry[1] for entry in sorted(ranked.values(), key=lambda entry: entry[0])]
    return ordered_matches


def print_matches(
    query: str,
    matches: Sequence[MatchResult],
    *,
    original_query: Optional[str] = None,
) -> None:
    if not matches:
        print("")
        return
    ordered_matches = rank_match_results(original_query or query, matches)
    lemko_words = ", ".join(_strip_roman_suffix(item.base_form) for item in ordered_matches)
    print(lemko_words)


def llm_suggest_base_form(
    word: str,
    *,
    lang: str,
    model: str = "gpt-5-mini",
    debug: bool = False,
) -> Optional[str]:
    cleaned = word.strip()
    if not cleaned:
        return None

    lang_config = LANG_CONFIG.get(lang, LANG_CONFIG["pl"])
    prompt_template = str(lang_config.get("llm_prompt", LANG_CONFIG["pl"]["llm_prompt"]))
    prompt = prompt_template.format(word=cleaned)
    if debug:
        print(f"[LLM prompt] {prompt}")

    provider = os.getenv("LEM_SEARCH_LLM_PROVIDER", os.getenv("LEM_TRANSLATE_PROVIDER", "openai")).strip().lower()
    if provider == "codex":
        if codex_complete_text is None:
            if debug:
                print("[LLM error] Wrapper Codex CLI jest niedostępny.")
            return None
        try:
            candidate = codex_complete_text(prompt, model=model)
        except Exception as exc:  # pragma: no cover
            if debug:
                print(f"[LLM error] Zapytanie Codex CLI nie powiodło się: {exc}")
            return None
    else:
        if OpenAI is None or _ensure_openai_key is None:  # pragma: no cover
            return None
        try:  # pragma: no cover
            _ensure_openai_key()
        except Exception as exc:
            if debug:
                print(f"[LLM error] Nie ustawiono klucza: {exc}")
            return None
        try:
            client = OpenAI()
        except Exception as exc:  # pragma: no cover
            if debug:
                print(f"[LLM error] Nie udało się utworzyć klienta: {exc}")
            return None

        try:
            response = client.responses.create(  # type: ignore[attr-defined]
                model=model,
                input=prompt,
            )
        except Exception as exc:  # pragma: no cover
            if debug:
                print(f"[LLM error] Zapytanie nie powiodło się: {exc}")
            return None

        if debug:
            try:
                print(f"[LLM output object] {response.output}")
            except Exception:
                pass

        text = getattr(response, "output_text", None)
        if isinstance(text, list):
            text = "\n".join(part for part in text if part)
        if not text:
            if debug:
                print("[LLM info] Pusta odpowiedź")
            return None
        candidate = str(text).strip()

    if debug:
        print(f"[LLM raw response] {candidate}")
    candidate = candidate.splitlines()[0].strip()
    parts = re.split(r"[^a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ-]+", candidate)
    parts = [p for p in parts if p]
    if not parts:
        return None
    return parts[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("word", help="Słowo lub wyrażenie do wyszukania.")
    parser.add_argument(
        "--lang",
        choices=tuple(LANG_CONFIG.keys()),
        default="pl",
        help="Język zapytania: 'pl' (tłumaczenia polskie) lub 'en' (tłumaczenia angielskie).",
    )
    parser.add_argument(
        "--pl",
        action="store_true",
        help="Wymuś wyszukiwanie w tłumaczeniach polskich.",
    )
    parser.add_argument(
        "--en",
        action="store_true",
        help="Wymuś wyszukiwanie w tłumaczeniach angielskich.",
    )
    parser.add_argument(
        "--suggest-limit",
        type=int,
        default=15,
        help="Maksymalna liczba wariantów sugerowanych przez fasttext2lemtools.",
    )
    parser.add_argument(
        "--no-suggest",
        action="store_true",
        help="Wyłącz podpowiedzi fastText w razie braku wyników.",
    )
    parser.add_argument(
        "--llm-model",
        default="gpt-5-mini",
        help="Model OpenAI używany do sprowadzania podstawowej formy (domyślnie gpt-5-mini).",
    )
    parser.add_argument(
        "--llm-debug",
        action="store_true",
        help="Wypisz prompt i surową odpowiedź LLM (tryb diagnostyczny).",
    )
    parser.add_argument(
        "--suggest-vocab-dir",
        type=Path,
        help="Katalog z plikami vocab_{lang}.json używanymi przez fasttext2lemtools.",
    )
    parser.add_argument(
        "--suggest-debug",
        action="store_true",
        help="Włącz logowanie diagnostyczne fasttext2lemtools.suggest.",
    )
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    if args.pl and args.en:
        raise SystemExit("Nie można jednocześnie używać opcji --pl i --en.")
    if args.pl:
        lang = "pl"
    elif args.en:
        lang = "en"
    else:
        lang = args.lang
    lang_config = LANG_CONFIG.get(lang, LANG_CONFIG["pl"])
    translation_columns: Tuple[str, ...] = tuple(lang_config.get("translation_columns", ()))  # type: ignore[arg-type]
    if not translation_columns:
        raise RuntimeError(f"Brak konfiguracji kolumn tłumaczeń dla języka {lang!r}.")

    # Kolejno próbujemy: dopasowania ścisłe, sugestie fasttext2lemtools, a na końcu LLM.
    suggest_lang = str(lang_config.get("suggest_lang", lang)) or lang
    vocab_dir: Optional[Path] = args.suggest_vocab_dir
    if vocab_dir is None:
        vocab_env = os.getenv("VOCAB_JSON_DIR")
        if vocab_env:
            try:
                vocab_dir = Path(vocab_env).expanduser()
            except Exception:
                if args.suggest_debug:
                    print(f"[SUGGEST info] Nie udało się zinterpretować VOCAB_JSON_DIR={vocab_env!r}")
                vocab_dir = None

    phrase = args.word.strip()
    if not phrase:
        raise SystemExit("Podaj niepuste słowo lub frazę.")

    with _get_connection() as conn:
        variants = _generate_query_variants(phrase, lang)
        for idx, variant in enumerate(variants):
            matches = query_terms_by_language(conn, variant, translation_columns, lang)
            if not matches:
                continue
            if idx == 0:
                print_matches(variant, matches)
            else:
                print_matches(variant, matches, original_query=phrase)
            return

        if args.no_suggest:
            print("")
            return

        suggestions: List[str] = []
        if ft_suggest is None:
            if args.suggest_debug:
                print("[SUGGEST info] fasttext2lemtools.suggest jest niedostępne (brak modułu).")
        else:
            # Wspólny fallback: używamy fasttext2lemtools, żeby mieć te same słowniki
            # i zasady filtrowania co narzędzie lem-search.
            suggestions = _fetch_suggestions(
                phrase,
                suggest_lang,
                max(args.suggest_limit, 1),
                vocab_dir,
                debug=args.suggest_debug,
            )

        display_suggestions = suggestions[: min(len(suggestions), 15)]
        if display_suggestions:
            print("Sugestie:", ", ".join(display_suggestions))
        else:
            print("Sugestie: (brak)")

        best_suggest_form: Optional[str] = None
        best_suggest_matches: Optional[List[MatchResult]] = None
        if suggestions:
            for suggestion in suggestions:
                suggestion_matches = query_terms_by_language(conn, suggestion, translation_columns, lang)
                if suggestion_matches:
                    best_suggest_form = suggestion
                    best_suggest_matches = suggestion_matches
                    break

        outputs: List[Tuple[str, str, List[MatchResult]]] = []
        if best_suggest_matches:
            outputs.append(("Sugestie", best_suggest_form or phrase, best_suggest_matches))
        else:
            llm_variants_checked = {v.lower() for v in variants}
            llm_variants_checked.update(word.lower() for word in suggestions)

            llm_matches: Optional[List[MatchResult]] = None
            llm_result_form: Optional[str] = None
            llm_form: Optional[str] = llm_suggest_base_form(
                phrase, lang=lang, model=args.llm_model, debug=args.llm_debug
            )
            if llm_form:
                llm_lower = llm_form.lower()
                print(f"Sugestia LLM: {llm_form}")
                if llm_lower not in llm_variants_checked:
                    llm_variants_checked.add(llm_lower)
                    temp_matches = query_terms_by_language(conn, llm_form, translation_columns, lang)
                    if temp_matches:
                        llm_matches = temp_matches
                        llm_result_form = llm_form
                elif args.llm_debug:
                    print(f"[LLM info] Forma '{llm_form}' była już wcześniej sprawdzana.")

                llm_suggestions_words: List[str] = []
                if ft_suggest is None:
                    print("Sugestie LLM: (brak)")
                else:
                    llm_suggestions = _fetch_suggestions(
                        llm_form,
                        suggest_lang,
                        max(args.suggest_limit, 1),
                        vocab_dir,
                        debug=args.suggest_debug,
                        skip_lower=llm_variants_checked,
                    )
                    llm_variants_checked.update(word.lower() for word in llm_suggestions)
                    if llm_suggestions:
                        llm_suggestions_words = llm_suggestions[: min(len(llm_suggestions), 15)]
                        print("Sugestie LLM:", ", ".join(llm_suggestions_words))
                    else:
                        print("Sugestie LLM: (brak)")

                    if llm_matches is None:
                        for suggestion in llm_suggestions:
                            suggestion_matches = query_terms_by_language(conn, suggestion, translation_columns, lang)
                            if suggestion_matches:
                                llm_result_form = suggestion
                                llm_matches = suggestion_matches
                                break

            if llm_matches:
                outputs.append(("LLM", llm_result_form or llm_form or phrase, llm_matches))

        if outputs:
            for label, form, matches in outputs:
                sanitized_form = _strip_roman_suffix(form)
                if len(outputs) > 1 or label != "Sugestie":
                    print(f"{label}: {sanitized_form}")
                print_matches(form, matches, original_query=phrase)
            return

    print("")


if __name__ == "__main__":
    main()
