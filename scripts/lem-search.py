#!/usr/bin/env python3
"""Search Lemko dictionary entries by base form or inflected form with FastText suggestions."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg2
import psycopg2.extras

try:  # pragma: no cover - optional dependency
    from fasttext2lemtools import suggest as ft_suggest  # type: ignore
except ImportError:  # pragma: no cover
    ft_suggest = None  # type: ignore

MORPHOLOGY_STRUCTURE_DIR = Path("/home/webserver/lemko-asr/morphology_structure_pl_lem_eng")
MORPHOLOGY_STRUCTURE_VARIANTS: Tuple[str, ...] = (
    "morphology_structure_lem01.json",
    "morphology_structure_pl01.json",
    "morphology_structure_eng01.json",
)
MORPHOLOGY_STRUCTURE_DEFAULT = MORPHOLOGY_STRUCTURE_DIR / MORPHOLOGY_STRUCTURE_VARIANTS[0]
DEFAULT_CONTEXT_AUTHOR = "Жыва бесіда"


@dataclass
class ContextEntry:
    body: str
    author: Optional[str]


@dataclass
class TermRecord:
    term_id: int
    base_form: str
    part_of_speech: Optional[int]
    grammatical_gender: Optional[int]
    grammatical_declension: Optional[str]
    grammatical_conjugation: Optional[str]
    grammatical_aspect: Optional[int]
    grammatical_stem: Optional[int]
    grammatical_numeral_type: Optional[int]
    grammatical_pronoun_type: Optional[int]
    grammatical_adverb_type: Optional[int]
    grammatical_aspect_pair: Optional[str]
    grammatic_description: Optional[str]
    semantic_description: Optional[str]
    polish_translation: Optional[str]
    english_translation: Optional[str]
    contexts: List[ContextEntry] = field(default_factory=list)
    order_index: Optional[int] = None
    flagged: bool = False

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "TermRecord":
        return cls(
            term_id=int(row["id"]),
            base_form=_clean_optional_text(row.get("base_form")) or "",
            part_of_speech=_coerce_optional_int(row.get("grammatical_part_of_speech")),
            grammatical_gender=_coerce_optional_int(row.get("grammatical_gender")),
            grammatical_declension=_clean_optional_text(row.get("grammatical_declension")),
            grammatical_conjugation=_clean_optional_text(row.get("grammatical_conjugation")),
            grammatical_aspect=_coerce_optional_int(row.get("grammatical_aspect")),
            grammatical_stem=_coerce_optional_int(row.get("grammatical_stem")),
            grammatical_numeral_type=_coerce_optional_int(row.get("grammatical_numeral_type")),
            grammatical_pronoun_type=_coerce_optional_int(row.get("grammatical_pronoun_type")),
            grammatical_adverb_type=_coerce_optional_int(row.get("grammatical_adverb_type")),
            grammatical_aspect_pair=_clean_optional_text(row.get("grammatical_aspect_pair")),
            grammatic_description=_clean_optional_text(row.get("grammatic_description")),
            semantic_description=_clean_optional_text(row.get("semantic_description")),
            polish_translation=_clean_optional_text(row.get("polish_translation")),
            english_translation=_clean_optional_text(row.get("english_translation")),
            contexts=_collect_contexts(row),
            order_index=_coerce_optional_int(row.get("order_index")),
            flagged=_coerce_bool(row.get("flagged")),
        )

    def to_payload(self, morphology: Dict[str, Any]) -> Dict[str, Any]:
        pos_key = str(self.part_of_speech) if self.part_of_speech is not None else ""
        part_meta: Dict[str, Any] = morphology.get("parts_of_speech", {}).get(pos_key, {})
        enums = morphology.get("enums", {})

        def resolve_enum(enum_name: Optional[str], value: Optional[int | str]) -> Optional[str]:
            if enum_name is None or value is None:
                return None
            mapping = enums.get(enum_name, {})
            return mapping.get(str(value))

        attributes: List[Dict[str, Any]] = []
        for attr in part_meta.get("term_attributes", []):
            column = attr.get("column")
            if not column:
                continue
            label = attr.get("label", column)
            raw_value = getattr(self, column, None)
            if raw_value is None:
                continue
            if isinstance(raw_value, str):
                raw_value = raw_value.strip()
                if not raw_value:
                    continue

            enum_name = attr.get("enum")
            value_type = attr.get("type", "enum" if enum_name else "string")
            resolved = resolve_enum(enum_name, raw_value) if enum_name else None

            entry = {
                "label": label,
                "code": raw_value,
                "value": resolved if resolved is not None else (str(raw_value) if value_type == "string" else raw_value),
                "enum": enum_name,
            }
            attributes.append(entry)

        contexts_payload = [
            {"body": entry.body, "author": entry.author} for entry in self.contexts
        ]

        return {
            "term_id": self.term_id,
            "base_form": self.base_form,
            "part_of_speech": {
                "code": self.part_of_speech,
                "label": part_meta.get("label"),
            },
            "order": self.order_index,
            "grammatical": {
                "attributes": attributes,
            },
            "descriptions": {
                "semantic": self.semantic_description,
                "grammatical": self.grammatic_description,
            },
            "translations": {
                "polish": self.polish_translation,
                "english": self.english_translation,
            },
            "contexts": contexts_payload,
        }


@dataclass
class FormMatch:
    word: str
    attributes: Dict[str, Optional[int]]

    def to_payload(self, morphology: Dict[str, Any]) -> Dict[str, Any]:
        enums = morphology.get("enums", {})
        formatted_attrs = {}
        for key, value in self.attributes.items():
            enum_values = enums.get(key, {})
            formatted_attrs[key] = _enum_payload(value, enum_values)
        return {"word": self.word, "attributes": formatted_attrs}


ROMAN_SUFFIX_RE = re.compile(r"\s+([IVXLCDMІVXLCДМ]+)$", re.IGNORECASE)
ROMAN_TRANSLATION_TABLE = str.maketrans({
    "І": "I",
    "I": "I",
    "V": "V",
    "В": "V",
    "X": "X",
    "Х": "X",
    "L": "L",
    "Л": "L",
    "C": "C",
    "Ц": "C",
    "С": "C",
    "D": "D",
    "Д": "D",
    "M": "M",
    "М": "M",
})


def _normalize_roman_numeral(text: str) -> str:
    normalized = (text or "").upper().translate(ROMAN_TRANSLATION_TABLE)
    return normalized.replace(" ", "")


def _roman_to_int(roman: Optional[str]) -> Optional[int]:
    if not roman:
        return None
    roman_map = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    s = _normalize_roman_numeral(roman)
    if not s or any(ch not in roman_map for ch in s):
        return None
    total = 0
    prev = 0
    for ch in reversed(s):
        value = roman_map[ch]
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total


def _term_sort_key(term: TermRecord) -> Tuple[int, str, int, int, int]:
    base, roman = split_roman_suffix(term.base_form)
    first_char = term.base_form[:1]
    if first_char.isalpha():
        lowercase_priority = 0 if first_char.islower() else 1
    else:
        lowercase_priority = 0
    roman_value = _roman_to_int(roman)
    if roman_value is None:
        roman_priority = -1
    else:
        roman_priority = roman_value
    order_priority = term.order_index if term.order_index is not None else 1_000_000
    return (
        base.lower(),
        lowercase_priority,
        roman_priority,
        order_priority,
        term.term_id,
    )


def split_roman_suffix(text: str) -> Tuple[str, Optional[str]]:
    clean = (text or "").strip()
    if not clean:
        return "", None
    match = ROMAN_SUFFIX_RE.search(clean)
    if not match:
        return clean, None
    base = clean[: match.start()].strip()
    roman = match.group(1).strip()
    if not base:
        return clean, None
    return base, roman or None


def _enum_payload(value: Optional[int], mapping: Dict[str, str]) -> Dict[str, Optional[Any]]:
    label = None
    if value is not None:
        label = mapping.get(str(int(value)))
    return {"code": value, "label": label}


def _clean_optional_text(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", ""}:
            return False
    return False


def _collect_contexts(row: Dict[str, Any]) -> List[ContextEntry]:
    contexts: List[ContextEntry] = []
    for idx in (1, 2, 3):
        body = _clean_optional_text(row.get(f"context{idx}_body"))
        selected = row.get(f"context{idx}_tag")
        if not body or not selected:
            continue
        author = _clean_optional_text(row.get(f"context{idx}_authors"))
        contexts.append(ContextEntry(body=body, author=author))
    return contexts


def _default_database_url() -> str:
    return "postgres://lemslownik:lemslownik@127.0.0.1:5432/lemslownik"


def _load_connection_settings(raw_url: str) -> Dict[str, Optional[str]]:
    from urllib.parse import urlparse

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"Unsupported database scheme: {parsed.scheme!r}")

    host = parsed.hostname or "127.0.0.1"
    return {
        "dbname": (parsed.path or "/").lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
        "host": host,
        "port": parsed.port or 5432,
    }


def _get_connection(retries: int = 3, retry_delay: float = 1.5) -> psycopg2.extensions.connection:
    import time

    raw_url = os.environ.get("DATABASE_URL", _default_database_url())
    params = _load_connection_settings(raw_url)

    last_exc: Optional[Exception] = None
    host = params.get("host")
    host_candidates: List[str] = []
    if host:
        host_candidates.append(host)
    if host in {"db", "localhost"}:
        host_candidates.append("127.0.0.1")
    elif host == "127.0.0.1":
        host_candidates.append("db")

    for host_option in host_candidates or [None]:
        current = params.copy()
        if host_option is not None:
            current["host"] = host_option
        for attempt in range(retries):
            try:
                return psycopg2.connect(**current)
            except psycopg2.OperationalError as exc:
                last_exc = exc
                if attempt == retries - 1:
                    break
                time.sleep(retry_delay * (attempt + 1))
    if last_exc:
        raise last_exc
    raise psycopg2.OperationalError("Failed to establish database connection")


def _morphology_structure_candidates(explicit: Optional[Path]) -> List[Path]:
    candidates: List[Path] = []
    if explicit is not None:
        candidates.append(explicit)

    env_file = os.environ.get("MORPHOLOGY_STRUCTURE_FILE")
    if env_file:
        candidates.append(Path(env_file))

    env_dir = os.environ.get("MORPHOLOGY_STRUCTURE_DIR")
    if env_dir:
        base_dir = Path(env_dir)
        for filename in MORPHOLOGY_STRUCTURE_VARIANTS:
            candidates.append(base_dir / filename)

    for filename in MORPHOLOGY_STRUCTURE_VARIANTS:
        candidates.append(MORPHOLOGY_STRUCTURE_DIR / filename)

    unique: List[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = Path(candidate).expanduser()
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return unique


def load_morphology_structure(path: Optional[Path] = None) -> Dict[str, Any]:
    candidates = _morphology_structure_candidates(path)
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))

    target = path or os.environ.get("MORPHOLOGY_STRUCTURE_FILE") or MORPHOLOGY_STRUCTURE_DEFAULT
    raise FileNotFoundError(f"Morphology structure file not found. Checked: {', '.join(str(c) for c in candidates)}. Expected default: {target}")


def fetch_terms_by_base_form(
    conn: psycopg2.extensions.connection,
    word: str,
) -> List[TermRecord]:
    base_candidate, _ = split_roman_suffix(word)
    base_lookup = base_candidate or (word or "").strip()
    if not base_lookup:
        return []

    sql = """
        SELECT
            t.id,
            t.base_form,
            t.grammatical_part_of_speech,
            t.grammatical_gender,
            t.grammatical_declension,
            t.grammatical_conjugation,
            t.grammatical_aspect,
            t.grammatical_stem,
            t.grammatical_numeral_type,
            t.grammatical_pronoun_type,
            t.grammatical_adverb_type,
            t.grammatical_aspect_pair,
            t.grammatic_description,
            t.semantic_description,
            t.polish_translation,
            t.english_translation,
            t.context1_body,
            t.context1_tag,
            t.context2_body,
            t.context2_tag,
            t.context3_body,
            t.context3_tag,
            s1.authors AS context1_authors,
            s2.authors AS context2_authors,
            s3.authors AS context3_authors,
            t."order" AS order_index,
            t.flagged
        FROM public.terms AS t
        LEFT JOIN public.sources AS s1 ON s1.id = t.context1_source_id
        LEFT JOIN public.sources AS s2 ON s2.id = t.context2_source_id
        LEFT JOIN public.sources AS s3 ON s3.id = t.context3_source_id
        WHERE deleted = FALSE
          AND redacted = TRUE
          AND (
                lower(base_form) = lower(%s)
             OR lower(base_form) LIKE lower(%s)
          )
        ORDER BY ("order" IS NULL), "order", id
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (base_lookup, f"{base_lookup} %"))
        rows = cur.fetchall() or []

    terms: List[TermRecord] = []
    base_lookup_lower = base_lookup.lower()
    for row in rows:
        base_form = _clean_optional_text(row.get("base_form")) or ""
        family_base, roman = split_roman_suffix(base_form)
        if family_base.lower() != base_lookup_lower:
            continue
        terms.append(TermRecord.from_row(row))
    terms.sort(key=_term_sort_key)
    return terms


def fetch_term_by_form(
    conn: psycopg2.extensions.connection,
    word: str,
) -> Optional[Tuple[TermRecord, FormMatch]]:
    sql = """
        SELECT
            t.id,
            t.base_form,
            t.grammatical_part_of_speech,
            t.grammatical_gender,
            t.grammatical_declension,
            t.grammatical_conjugation,
            t.grammatical_aspect,
            t.grammatical_stem,
            t.grammatical_numeral_type,
            t.grammatical_pronoun_type,
            t.grammatical_adverb_type,
            t.grammatical_aspect_pair,
            t.grammatic_description,
            t.semantic_description,
            t.polish_translation,
            t.english_translation,
            t.context1_body,
            t.context1_tag,
            t.context2_body,
            t.context2_tag,
            t.context3_body,
            t.context3_tag,
            s1.authors AS context1_authors,
            s2.authors AS context2_authors,
            s3.authors AS context3_authors,
            t."order" AS order_index,
            t.flagged,
            twa.word AS form_word,
            twa.grammatical_case,
            twa.grammatical_number,
            twa.grammatical_gender AS form_gender,
            twa.grammatical_person,
            twa.grammatical_mood,
            twa.grammatical_tense,
            twa.grammatical_comparison
        FROM public.term_word_associations AS twa
        JOIN public.terms AS t ON t.id = twa.term_id
        LEFT JOIN public.sources AS s1 ON s1.id = t.context1_source_id
        LEFT JOIN public.sources AS s2 ON s2.id = t.context2_source_id
        LEFT JOIN public.sources AS s3 ON s3.id = t.context3_source_id
        WHERE t.deleted = FALSE
          AND t.redacted = TRUE
          AND lower(twa.word) = lower(%s)
    """
    sql += ' ORDER BY ("order" IS NULL), "order", t.id LIMIT 1'

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (word,))
        row = cur.fetchone()
    if not row:
        return None

    term = TermRecord.from_row(row)
    attrs = {
        "grammatical_case": _coerce_optional_int(row.get("grammatical_case")),
        "grammatical_number": _coerce_optional_int(row.get("grammatical_number")),
        "grammatical_gender": _coerce_optional_int(row.get("form_gender")),
        "grammatical_person": _coerce_optional_int(row.get("grammatical_person")),
        "grammatical_mood": _coerce_optional_int(row.get("grammatical_mood")),
        "grammatical_tense": _coerce_optional_int(row.get("grammatical_tense")),
        "grammatical_comparison": _coerce_optional_int(row.get("grammatical_comparison")),
    }
    form_match = FormMatch(word=_clean_optional_text(row.get("form_word")) or word, attributes=attrs)
    return term, form_match


def fetch_forms_for_term(
    conn: psycopg2.extensions.connection,
    term_id: int,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT word,
               grammatical_case,
               grammatical_number,
               grammatical_gender,
               grammatical_person,
               grammatical_mood,
               grammatical_tense,
               grammatical_comparison
        FROM public.term_word_associations
        WHERE term_id = %s
        ORDER BY word
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (term_id,))
        rows = cur.fetchall() or []
    return rows


def build_forms_structure(
    term: TermRecord,
    forms_rows: Iterable[Dict[str, Any]],
    morphology: Dict[str, Any],
) -> Dict[str, Any]:
    rows = list(forms_rows or [])
    pos_code = term.part_of_speech
    pos_key = str(pos_code) if pos_code is not None else ""
    pos_meta = morphology.get("parts_of_speech", {}).get(pos_key)
    dims = pos_meta.get("form_dimensions", []) if isinstance(pos_meta, dict) else []
    enums = morphology.get("enums", {})

    enum_order_cache: Dict[str, Dict[Any, int]] = {}

    def enum_order(enum_name: str) -> Dict[Any, int]:
        if enum_name in enum_order_cache:
            return enum_order_cache[enum_name]
        mapping = enums.get(enum_name, {})
        order_map: Dict[Any, int] = {}
        for idx, key in enumerate(mapping.keys()):
            order_map[key] = idx
            try:
                order_map[int(key)] = idx
            except (TypeError, ValueError):
                pass
        order_map[None] = len(order_map)
        enum_order_cache[enum_name] = order_map
        return order_map

    def resolve_label(enum_name: str, value: Optional[int]) -> str:
        enum_map = enums.get(enum_name, {})
        if value is None:
            return "(brak danych)"
        return enum_map.get(str(value), f"kod_{value}")

    def row_sort_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
        parts: List[Any] = []
        for dim in dims:
            attr = dim.get("attribute")
            enum_name = dim.get("enum")
            if not attr or not enum_name:
                continue
            value = _coerce_optional_int(row.get(attr))
            order_map = enum_order(enum_name)
            idx = order_map.get(value)
            if idx is None:
                raw = row.get(attr)
                raw_key = str(raw).strip() if raw is not None else None
                idx = order_map.get(raw_key, len(order_map))
            parts.append(idx)
        word = _clean_optional_text(row.get("word")) or ""
        parts.append(word)
        return tuple(parts)

    if rows:
        rows.sort(key=row_sort_key)

    forms_tree: Dict[str, Any] = {}
    for row in rows:
        word = _clean_optional_text(row.get("word"))
        if not word:
            continue
        node = forms_tree
        for dim in dims:
            attr = dim.get("attribute")
            enum_name = dim.get("enum")
            if attr is None or enum_name is None:
                continue
            value = _coerce_optional_int(row.get(attr))
            label = resolve_label(enum_name, value)
            node = node.setdefault(label, {})
        leaf_words = node.setdefault("_words", [])
        if word not in leaf_words:
            leaf_words.append(word)
    return forms_tree


def _forms_tree_to_rows(tree: Dict[str, Any], prefix: Optional[List[str]] = None) -> List[str]:
    if prefix is None:
        prefix = []
    rows: List[str] = []
    for key, value in tree.items():
        if key == "_words":
            words = ", ".join(value) if value else "(brak form)"
            rows.append(" -> ".join(prefix or ["formy"]) + ": " + words)
            continue
        rows.extend(_forms_tree_to_rows(value, prefix + [key]))
    return rows


def render_forms(tree: Dict[str, Any]) -> str:
    if not tree:
        return "Brak zarejestrowanych form w bazie."
    rows = _forms_tree_to_rows(tree)
    return "\n".join(rows)


def _count_forms_in_tree(tree: Dict[str, Any]) -> int:
    count = 0

    def _traverse(node: Dict[str, Any]) -> None:
        nonlocal count
        for key, value in node.items():
            if key == "_words":
                count += len(value)
            elif isinstance(value, dict):
                _traverse(value)

    _traverse(tree)
    return count


def _grammar_priority_key(payload: Dict[str, Any]) -> Tuple[int, int, int, str, int]:
    term_info = payload.get("term", {})
    order = term_info.get("order")
    if isinstance(order, int):
        order_priority = 0 if order == 1 else 1
    else:
        order_priority = 2

    base_form = term_info.get("base_form") or payload.get("matched_word") or ""
    first_char = base_form[:1]
    lowercase_priority = 0 if first_char and first_char.islower() else 1

    _, roman = split_roman_suffix(base_form)
    roman_value = _roman_to_int(roman)
    roman_priority = 0 if roman_value == 1 else 1

    tie_breaker = base_form.lower()
    term_id = term_info.get("term_id")
    term_id_priority = term_id if isinstance(term_id, int) else 0
    return (order_priority, lowercase_priority, roman_priority, tie_breaker, term_id_priority)

def find_similar_candidates(
    query: str,
    limit: int,
    lang: str = "lem",
    vocab_dir: Optional[Path] = None,
    debug: bool = False,
) -> List[str]:
    if ft_suggest is None:  # pragma: no cover
        return []
    topn = max(limit, 1)
    suggest_kwargs: Dict[str, Any] = {
        "word": query,
        "lang": lang,
        "topn": topn,
        "debug": debug,
    }
    if vocab_dir is not None:
        suggest_kwargs["vocab_dir"] = str(vocab_dir)

    try:
        candidates = ft_suggest(**suggest_kwargs)
    except Exception:  # pragma: no cover - defensive fallback
        return []

    normalized: List[str] = []
    seen: set[str] = set()
    for item in candidates or []:
        word = _clean_optional_text(item)
        if not word:
            continue
        if word in seen:
            continue
        seen.add(word)
        normalized.append(word)
        if len(normalized) >= topn:
            break
    return normalized


def build_result_payload(
    query: str,
    term: TermRecord,
    morphology: Dict[str, Any],
    forms_tree: Dict[str, Any],
    match_type: str,
    display_word: str,
    origin_word: str,
    form_match: Optional[FormMatch],
    forms_word_count: int,
) -> Dict[str, Any]:
    payload = {
        "query": query,
        "match_type": match_type,
        "matched_word": display_word,
        "match_origin": origin_word,
        "term": term.to_payload(morphology),
        "forms": forms_tree,
        "forms_word_count": forms_word_count,
        "forms_display": False,
        "source": "odf" if term.flagged else "new",
        "form_id": term.term_id,
    }
    if form_match:
        payload["matched_form"] = form_match.to_payload(morphology)
    return payload


def _finalize_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    forms_by_pos: Dict[Optional[int], Dict[str, Any]] = {}
    for payload in results:
        term_info = payload.get("term", {})
        pos_code = term_info.get("part_of_speech", {}).get("code")
        if pos_code is None:
            continue
        best = forms_by_pos.get(pos_code)
        current_count = payload.get("forms_word_count") or 0
        best_count = best.get("forms_word_count") if best else -1
        if best is None or current_count > (best_count or 0):
            forms_by_pos[pos_code] = payload

    for payload in results:
        term_info = payload.get("term", {})
        pos_code = term_info.get("part_of_speech", {}).get("code")
        is_best = forms_by_pos.get(pos_code) is payload if pos_code is not None else False
        payload["forms_display"] = bool(is_best and payload.get("forms"))
        if not payload["forms_display"]:
            payload["forms"] = None
    return results


def _group_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    group_map: Dict[Any, Dict[str, Any]] = {}

    for payload in results:
        term_info = payload.get("term", {})
        pos_info = term_info.get("part_of_speech") or {}
        pos_code = pos_info.get("code")
        attributes: List[Dict[str, Any]] = term_info.get("grammatical", {}).get("attributes") or []
        base_root = split_roman_suffix(payload.get("matched_word", ""))[0] or payload.get("matched_word", "")
        base_key = (base_root or "").lower()
        signature = (pos_code, base_key)

        group = group_map.get(signature)
        if group is None:
            headword = base_root or payload.get("matched_word") or "(brak hasła)"
            group = {
                "signature": signature,
                "headword": headword,
                "pos_code": pos_code,
                "pos_label": pos_info.get("label") or "nieznana część mowy",
                "entries": [],
                "forms": None,
                "forms_word": None,
                "forms_word_count": -1,
                "forms_term_id": None,
                "grammar_source": None,
            }
            group_map[signature] = group
            groups.append(group)

        group["entries"].append(payload)

        current_source = group.get("grammar_source")
        if current_source is None or _grammar_priority_key(payload) < _grammar_priority_key(current_source):
            group["grammar_source"] = payload

        if payload.get("forms_display") and payload.get("forms"):
            current_best = group.get("forms_word_count", -1)
            candidate_count = payload.get("forms_word_count", 0)
            if candidate_count >= current_best:
                group["forms"] = payload["forms"]
                group["forms_word"] = payload.get("matched_word")
                group["forms_word_count"] = candidate_count
                group["forms_term_id"] = payload.get("form_id")

    for group in groups:
        grammar_source = group.pop("grammar_source", None)
        formatted_attributes: List[Dict[str, Any]] = []
        grammatic_description: Optional[str] = None
        if grammar_source:
            term_info = grammar_source.get("term", {})
            attr_entries = term_info.get("grammatical", {}).get("attributes") or []
            for attr in attr_entries:
                label = attr.get("label") or "Atrybut"
                value = attr.get("value")
                code = attr.get("code")
                enum_name = attr.get("enum")
                display = None
                if value is not None:
                    display = str(value)
                elif code is not None:
                    display = f"kod {code}"
                if display is None:
                    display = "(brak)"
                formatted_attributes.append(
                    {
                        "label": label,
                        "value": display,
                        "values": [str(value)] if value is not None else [],
                        "codes": [str(code)] if (value is None and code is not None) else [],
                        "enum": enum_name,
                    }
                )
            descriptions = term_info.get("descriptions", {})
            grammatic_description = descriptions.get("grammatical")
        group["attributes"] = formatted_attributes
        group["grammatic_description"] = grammatic_description
        group.pop("forms_word_count", None)
        if "forms_term_id" not in group:
            group["forms_term_id"] = None

    return groups


def search_term(
    conn: psycopg2.extensions.connection,
    morphology: Dict[str, Any],
    query: str,
    similar_args: Dict[str, Any],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen_term_ids: set[int] = set()
    terms = fetch_terms_by_base_form(conn, query)

    def append_term_results(
        matched_terms: Iterable[TermRecord],
        match_type: str,
        origin_word: str,
        form_match: Optional[FormMatch],
        form_match_term_id: Optional[int],
    ) -> None:
        for term in matched_terms:
            if term.term_id in seen_term_ids:
                continue
            forms_rows = fetch_forms_for_term(conn, term.term_id)
            forms_tree = build_forms_structure(term, forms_rows, morphology)
            forms_word_count = _count_forms_in_tree(forms_tree)
            results.append(
                build_result_payload(
                    query=query,
                    term=term,
                    morphology=morphology,
                    forms_tree=forms_tree,
                    match_type=match_type,
                    display_word=term.base_form,
                    origin_word=origin_word,
                    form_match=form_match if (form_match and form_match_term_id == term.term_id) else None,
                    forms_word_count=forms_word_count,
                )
            )
            seen_term_ids.add(term.term_id)

    if terms:
        append_term_results(
            terms,
            match_type="exact_term",
            origin_word=query,
            form_match=None,
            form_match_term_id=None,
        )
        return _finalize_results(results)

    form_result = fetch_term_by_form(conn, query)
    if form_result:
        term, form_match = form_result
        family = fetch_terms_by_base_form(conn, term.base_form)
        append_term_results(
            family,
            match_type="exact_form",
            origin_word=query,
            form_match=form_match,
            form_match_term_id=term.term_id,
        )
        return _finalize_results(results)

    similar_candidates = find_similar_candidates(query=query, **similar_args)
    for candidate in similar_candidates:
        candidate_terms = fetch_terms_by_base_form(conn, candidate)
        if candidate_terms:
            append_term_results(
                candidate_terms,
                match_type="similar_term",
                origin_word=candidate,
                form_match=None,
                form_match_term_id=None,
            )
            break
        form_result = fetch_term_by_form(conn, candidate)
        if form_result:
            term, form_match = form_result
            family = fetch_terms_by_base_form(conn, term.base_form)
            append_term_results(
                family,
                match_type="similar_form",
                origin_word=candidate,
                form_match=form_match,
                form_match_term_id=term.term_id,
            )
            break

    return _finalize_results(results)


def render_group_text(group: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    lines: List[str] = []
    header = f"{group['headword']} — {group['pos_label']}"
    lines.append(header)

    attributes = group.get("attributes") or []
    if attributes:
        lines.append("Informacje gramatyczne:")
        for attr in attributes:
            label = attr.get("label", "Atrybut")
            value = attr.get("value")
            code = attr.get("code")
            if value is not None:
                display = str(value)
            elif code is not None:
                display = f"kod {code}"
            else:
                display = "(brak)"
            lines.append(f"- {label}: {display}")

    entries = group.get("entries") or []
    if entries:
        lines.append("")
        lines.append("Znaczenia:")
        for idx, payload in enumerate(entries, start=1):
            term_info = payload.get("term", {})
            descriptions = term_info.get("descriptions", {})
            semantic = descriptions.get("semantic")
            entry_word = payload.get("matched_word") or group["headword"]

            entry_line = f"{idx}. {entry_word}"
            if semantic:
                entry_line += f" — {semantic}"
            lines.append(entry_line)

            contexts = term_info.get("contexts") or []
            if contexts:
                for context in contexts:
                    if isinstance(context, dict):
                        body = _clean_optional_text(context.get("body"))
                        author = _clean_optional_text(context.get("author"))
                    else:
                        body = _clean_optional_text(context)
                        author = None
                    if body:
                        body = " ".join(body.replace("\r", " ").replace("\n", " ").split())
                    if not body:
                        continue
                    author_display = author or DEFAULT_CONTEXT_AUTHOR
                    lines.append(f"   • {body} — {author_display}")
            else:
                lines.append("   • (brak przykładów)")

    forms_text: Optional[str] = None
    if group.get("forms"):
        forms_heading = f"Formy — {group.get('forms_word') or group['headword']}"
        forms_body = render_forms(group["forms"])
        forms_text = f"{forms_heading}\n{forms_body}"

    return "\n".join(lines), forms_text


def _groups_to_json(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    json_groups: List[Dict[str, Any]] = []
    for group in groups:
        entries_json: List[Dict[str, Any]] = []
        for payload in group.get("entries", []):
            term_info = payload.get("term", {})
            descriptions = term_info.get("descriptions", {})
            contexts_raw = term_info.get("contexts") or []
            contexts_json: List[Dict[str, Optional[str]]] = []
            for context in contexts_raw:
                if isinstance(context, dict):
                    body = context.get("body")
                    author = context.get("author")
                else:
                    body = context
                    author = None
                if body is not None:
                    body = " ".join(str(body).replace("\r", " ").replace("\n", " ").split())
                    if not body:
                        body = None
                if author is not None:
                    author = str(author).strip() or None
                if body is None:
                    continue
                author = author or DEFAULT_CONTEXT_AUTHOR
                contexts_json.append({"body": body, "author": author})

            entries_json.append({
                "matched_word": payload.get("matched_word"),
                "semantic_description": descriptions.get("semantic"),
                "contexts": contexts_json,
                "source": payload.get("source"),
                "source_id": term_info.get("term_id") or payload.get("form_id"),
                "order": term_info.get("order"),
            })

        json_groups.append({
            "headword": group.get("headword"),
            "part_of_speech": {
                "code": group.get("pos_code"),
                "label": group.get("pos_label"),
            },
            "grammatical_attributes": group.get("attributes"),
            "grammatic_description": group.get("grammatic_description"),
            "entries": entries_json,
            "forms_id": group.get("forms_term_id"),
            "forms_headword": group.get("forms_word"),
            "forms": group.get("forms"),
        })
    return json_groups


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("word", help="Szukane hasło (forma podstawowa lub odmieniona).")
    parser.add_argument("--json", action="store_true", help="Zwróć wynik w formacie JSON.")
    parser.add_argument(
        "--morphology-file",
        type=Path,
        help=(
            "Ścieżka do pliku struktury morfologicznej. "
            "Domyślnie morphology_structure_lem01.json z katalogu morphology_structure_pl_lem_eng."
        ),
    )

    parser.add_argument(
        "--similar-limit",
        type=int,
        default=10,
        help="Maksymalna liczba propozycji alternatywnych z FastText.",
    )
    parser.add_argument(
        "--similar-lang",
        default="lem",
        help="Kod języka przekazywany do modułu fasttext2lemtools (domyślnie lem).",
    )
    parser.add_argument(
        "--similar-vocab-dir",
        type=Path,
        help="Ścieżka do katalogu z plikami vocab_{lang}.json dla fasttext2lemtools.",
    )
    parser.add_argument(
        "--similar-debug",
        action="store_true",
        help="Włącz diagnostykę modułu fasttext2lemtools.suggest.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    morphology = load_morphology_structure(path=args.morphology_file)
    similar_args = {
        "limit": args.similar_limit,
        "lang": args.similar_lang,
        "vocab_dir": args.similar_vocab_dir,
        "debug": args.similar_debug,
    }

    with _get_connection() as conn:
        results = search_term(
            conn=conn,
            morphology=morphology,
            query=args.word,
            similar_args=similar_args,
        )

    if not results:
        print(f"Nie znaleziono hasła ani form dla '{args.word}'.")
        return

    groups = _group_results(results)

    if args.json:
        print(
            json.dumps(
                {
                    "query": args.word,
                    "groups": _groups_to_json(groups),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        forms_sections: List[str] = []
        for idx, group in enumerate(groups, start=1):
            if idx > 1:
                print("\n" + ("-" * 40) + f"\nWynik {idx}\n" + ("-" * 40) + "\n")
            main_text, forms_section = render_group_text(group)
            print(main_text)
            if forms_section:
                forms_sections.append(forms_section)

        if forms_sections:
            print("\n" + ("=" * 40) + "\nFormy\n" + ("=" * 40))
            for idx, section in enumerate(forms_sections, start=1):
                if idx > 1:
                    print("")
                print(section)


if __name__ == "__main__":
    main()
