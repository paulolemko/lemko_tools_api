#!/usr/bin/env python3
"""Ensure all expected inflection slots exist for a term based on its POS code.

The script reads `grammatical_part_of_speech` from `public.terms` and then
builds expected slot combinations for `public.term_word_associations`
according to this matrix:

0  rzeczownik: przypadek(7) x liczba(2) = 14
1  czasownik: tryb(3) x czas(3) x osoba(8) = 72
2  przymiotnik: stopień(3) x rodzaj(3) x liczba(2) x przypadek(7) = 126
3  liczebnik porządkowy: liczba(2) x przypadek(7) = 14
4  zaimek odmienny: liczba(2) x przypadek(7) = 14
5  zaimek (przypadek): przypadek(7) = 7
6  przysłówek: 1
7  przysłówek stopniowalny: stopień(3) = 3
8  partykuła: 1
9  spójnik: 1
10 wykrzyknik: 1
11 przyimek: 1
12 liczebnik główny: przypadek(7) = 7
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import psycopg2
import psycopg2.extras


CASE_VALUES: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
NUMBER_VALUES: Tuple[int, ...] = (0, 1)
MOOD_VALUES: Tuple[int, ...] = (0, 1, 2)
TENSE_VALUES: Tuple[int, ...] = (0, 1, 2)
PERSON_VALUES: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7)
COMPARISON_VALUES: Tuple[int, ...] = (0, 1, 2)
ADJECTIVE_GENDER_VALUES: Tuple[int, ...] = (0, 1, 2)

DIMENSION_VALUES: Dict[str, Tuple[int, ...]] = {
    "case": CASE_VALUES,
    "number": NUMBER_VALUES,
    "mood": MOOD_VALUES,
    "tense": TENSE_VALUES,
    "person": PERSON_VALUES,
    "comparison": COMPARISON_VALUES,
    "gender": ADJECTIVE_GENDER_VALUES,
}


@dataclass(frozen=True)
class PosRule:
    label: str
    dimensions: Tuple[str, ...]


POS_RULES: Dict[int, PosRule] = {
    0: PosRule("rzeczownik", ("case", "number")),
    1: PosRule("czasownik", ("mood", "tense", "person")),
    2: PosRule("przymiotnik", ("comparison", "gender", "number", "case")),
    3: PosRule("liczebnik porządkowy", ("number", "case")),
    4: PosRule("zaimek odmienny (liczba + przypadek)", ("number", "case")),
    5: PosRule("zaimek (przypadek)", ("case",)),
    6: PosRule("przysłówek", ()),
    7: PosRule("przysłówek stopniowalny", ("comparison",)),
    8: PosRule("partykuła", ()),
    9: PosRule("spójnik", ()),
    10: PosRule("wykrzyknik", ()),
    11: PosRule("przyimek", ()),
    12: PosRule("liczebnik główny", ("case",)),
}


@dataclass(frozen=True)
class FormSignature:
    comparison: Optional[int] = None
    gender: Optional[int] = None
    number: Optional[int] = None
    case: Optional[int] = None
    mood: Optional[int] = None
    tense: Optional[int] = None
    person: Optional[int] = None

    @classmethod
    def from_row(cls, row: Dict[str, Optional[int]]) -> "FormSignature":
        return cls(
            comparison=_coerce(row.get("grammatical_comparison")),
            gender=_coerce(row.get("grammatical_gender")),
            number=_coerce(row.get("grammatical_number")),
            case=_coerce(row.get("grammatical_case")),
            mood=_coerce(row.get("grammatical_mood")),
            tense=_coerce(row.get("grammatical_tense")),
            person=_coerce(row.get("grammatical_person")),
        )

    def to_sql_params(self) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int], Optional[int], Optional[int], Optional[int]]:
        return (
            self.comparison,
            self.gender,
            self.number,
            self.case,
            self.mood,
            self.tense,
            self.person,
        )


def _coerce(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_dotenv(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = val


def fetch_term(conn: psycopg2.extensions.connection, term_id: int) -> Dict[str, object]:
    sql = "SELECT id, base_form, grammatical_part_of_speech FROM public.terms WHERE id = %s"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (term_id,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"Term {term_id} not found")
    return dict(row)


def fetch_existing_forms(conn: psycopg2.extensions.connection, term_id: int) -> List[FormSignature]:
    sql = """
        SELECT
            grammatical_comparison,
            grammatical_gender,
            grammatical_number,
            grammatical_case,
            grammatical_mood,
            grammatical_tense,
            grammatical_person
        FROM public.term_word_associations
        WHERE term_id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (term_id,))
        rows = cur.fetchall() or []
    return [FormSignature.from_row(dict(row)) for row in rows]


def fetch_all_term_ids(conn: psycopg2.extensions.connection) -> List[int]:
    sql = "SELECT id FROM public.terms ORDER BY id"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    return [row[0] for row in rows]


def expected_signatures_for_pos(pos_code: int) -> Tuple[PosRule, List[FormSignature]]:
    rule = POS_RULES.get(pos_code)
    if rule is None:
        known = ", ".join(str(code) for code in sorted(POS_RULES))
        raise ValueError(f"Unsupported POS code: {pos_code}. Supported: {known}")

    if not rule.dimensions:
        return rule, [FormSignature()]

    pools: List[Tuple[int, ...]] = [DIMENSION_VALUES[dim] for dim in rule.dimensions]
    signatures: List[FormSignature] = []
    for values in product(*pools):
        payload = dict(zip(rule.dimensions, values))
        signatures.append(
            FormSignature(
                comparison=payload.get("comparison"),
                gender=payload.get("gender"),
                number=payload.get("number"),
                case=payload.get("case"),
                mood=payload.get("mood"),
                tense=payload.get("tense"),
                person=payload.get("person"),
            )
        )
    return rule, signatures


def ensure_forms(
    conn: psycopg2.extensions.connection,
    term_id: int,
    signatures: Iterable[FormSignature],
) -> int:
    existing = set(fetch_existing_forms(conn, term_id))
    missing = [sig for sig in signatures if sig not in existing]
    if not missing:
        return 0

    insert_sql = """
        INSERT INTO public.term_word_associations (
            term_id,
            word,
            grammatical_comparison,
            grammatical_gender,
            grammatical_number,
            grammatical_case,
            grammatical_mood,
            grammatical_tense,
            grammatical_person,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
    """
    with conn.cursor() as cur:
        for sig in missing:
            cur.execute(insert_sql, (term_id, "", *sig.to_sql_params()))
    conn.commit()
    return len(missing)


def _load_connection_settings(raw_url: str) -> Dict[str, object]:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"Unsupported database scheme: {parsed.scheme!r}")
    query = parse_qs(parsed.query)
    params: Dict[str, object] = {
        "dbname": (parsed.path or "/").lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
    }
    for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout"):
        values = query.get(key)
        if values and values[0]:
            params[key] = values[0]
    return params


def _build_host_candidates(primary_host: Optional[str]) -> List[str]:
    candidate_order = [
        primary_host.strip() if isinstance(primary_host, str) else primary_host,
        "127.0.0.1",
        "localhost",
        "db",
        "postgres",
    ]
    seen: set[str] = set()
    hosts: List[str] = []
    for entry in candidate_order:
        if entry is None:
            continue
        entry = entry.strip()
        if not entry or entry in seen:
            continue
        seen.add(entry)
        hosts.append(entry)
    return hosts


def _get_connection(
    database_url: str,
    retries: int = 3,
    retry_delay: float = 1.0,
) -> psycopg2.extensions.connection:
    params = _load_connection_settings(database_url)
    attempts_errors: List[str] = []
    for host_option in _build_host_candidates(params.get("host")):
        current = params.copy()
        current["host"] = host_option
        for attempt in range(retries):
            try:
                return psycopg2.connect(**current)
            except psycopg2.OperationalError as exc:
                attempts_errors.append(
                    f"host={host_option}, try={attempt + 1}/{retries}: {str(exc).splitlines()[0]}"
                )
                if attempt == retries - 1:
                    break
                time.sleep(retry_delay * (attempt + 1))
    details = "\n".join(attempts_errors[-12:])
    raise psycopg2.OperationalError(
        "Failed to establish database connection.\n"
        "Recent attempts:\n"
        f"{details}"
    )


def _describe_rule(rule: PosRule) -> str:
    if not rule.dimensions:
        return "—"
    parts = [f"{name}({len(DIMENSION_VALUES[name])})" for name in rule.dimensions]
    return " x ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--term-id",
        type=int,
        default=None,
        help="Jeśli podane – obrabia tylko wskazany term_id; domyślnie przetwarza wszystkie hasła.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="np. postgres://user:pass@host:5432/db (domyślnie: DATABASE_URL z env/.env)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Plik .env z DATABASE_URL (domyślnie: .env)",
    )
    args = parser.parse_args()

    _load_dotenv(args.env_file, override=False)
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Brak DATABASE_URL. Ustaw w env/.env lub podaj --database-url.")

    conn = _get_connection(database_url)
    try:
        if args.term_id is not None:
            term_ids: Sequence[int] = [args.term_id]
        else:
            term_ids = fetch_all_term_ids(conn)
            if not term_ids:
                print("Brak rekordów w public.terms – nie ma czego przetwarzać.")
                return

        for term_id in term_ids:
            try:
                term = fetch_term(conn, term_id)
            except ValueError as exc:
                print(f"Term {term_id}: pominięty ({exc})")
                continue

            pos_code = _coerce(term.get("grammatical_part_of_speech"))
            if pos_code is None:
                print(f"Term {term_id} ({term.get('base_form')}): brak części mowy, pominięto.")
                continue

            try:
                rule, signatures = expected_signatures_for_pos(pos_code)
            except ValueError as exc:
                print(f"Term {term_id} ({term.get('base_form')}): {exc}, pominięto.")
                continue

            if not rule.dimensions:
                added = 0
            else:
                added = ensure_forms(conn, term_id, signatures)
            print(
                f"Term {term_id} ({term.get('base_form')}), POS {pos_code} [{rule.label}]: "
                f"{_describe_rule(rule)} => {len(signatures)} oczekiwanych form, dodano {added} brakujących rekordów."
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
