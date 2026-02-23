#!/usr/bin/env python3
"""Export/import dictionary terms with forms between PostgreSQL databases."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple 
from urllib.parse import parse_qs, urlparse

import psycopg2
import psycopg2.extras
from psycopg2 import sql


DEFAULT_DATABASE_URL = "postgres://lemslownik:lemslownik@127.0.0.1:5432/lemslownik"
TERM_FORMS_COLUMNS: Tuple[str, ...] = (
    "term_id",
    "grammatical_case",
    "grammatical_person",
    "grammatical_number",
    "grammatical_comparison",
    "grammatical_mood",
    "grammatical_tense",
    "created_at",
    "updated_at",
    "word",
    "grammatical_gender",
)


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
    result: List[str] = []
    for host in candidate_order:
        if host is None:
            continue
        host = host.strip()
        if not host or host in seen:
            continue
        seen.add(host)
        result.append(host)
    return result


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


def _json_default(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _parse_bool_arg(value: str) -> bool:
    text = value.strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("Expected boolean value (true/false)")


def _fetch_one_dict(
    cur: psycopg2.extensions.cursor,
    query: str,
    params: Sequence[Any],
) -> Optional[Dict[str, Any]]:
    cur.execute(query, params)
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_all_dicts(
    cur: psycopg2.extensions.cursor,
    query: str,
    params: Sequence[Any],
) -> List[Dict[str, Any]]:
    cur.execute(query, params)
    rows = cur.fetchall() or []
    return [dict(row) for row in rows]


def export_term_payload(conn: psycopg2.extensions.connection, term_id: int) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        term = _fetch_one_dict(cur, "SELECT * FROM public.terms WHERE id = %s", (term_id,))
        if term is None:
            raise ValueError(f"Term {term_id} not found")

        forms = _fetch_all_dicts(
            cur,
            """
            SELECT *
            FROM public.term_word_associations
            WHERE term_id = %s
            ORDER BY grammatical_number, grammatical_case, grammatical_gender, grammatical_comparison, id
            """,
            (term_id,),
        )
        for form in forms:
            form.pop("id", None)

        owner = None
        owner_id = term.get("owner_id")
        if owner_id is not None:
            owner = _fetch_one_dict(cur, "SELECT * FROM public.users WHERE id = %s", (owner_id,))

        source_ids = []
        for key in ("context1_source_id", "context2_source_id", "context3_source_id"):
            sid = term.get(key)
            if isinstance(sid, int):
                source_ids.append(sid)
        source_ids = sorted(set(source_ids))
        sources: List[Dict[str, Any]] = []
        if source_ids:
            sources = _fetch_all_dicts(
                cur,
                "SELECT * FROM public.sources WHERE id = ANY(%s) ORDER BY id",
                (source_ids,),
            )

    return {
        "meta": {
            "exported_at": dt.datetime.now(dt.UTC).isoformat(),
            "term_id": term_id,
        },
        "term": term,
        "forms": forms,
        "owner": owner,
        "sources": sources,
    }


def export_terms_payload_by_flags(
    conn: psycopg2.extensions.connection,
    *,
    redacted: bool,
    deleted: bool,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if limit is None:
            cur.execute(
                """
                SELECT id
                FROM public.terms
                WHERE redacted = %s
                  AND deleted = %s
                ORDER BY id
                """,
                (redacted, deleted),
            )
        else:
            cur.execute(
                """
                SELECT id
                FROM public.terms
                WHERE redacted = %s
                  AND deleted = %s
                ORDER BY id
                LIMIT %s
                """,
                (redacted, deleted, limit),
            )
        term_ids = [int(row["id"]) for row in (cur.fetchall() or [])]

    terms: List[Dict[str, Any]] = []
    total_forms = 0
    total = len(term_ids)
    for idx, term_id in enumerate(term_ids, start=1):
        item = export_term_payload(conn, term_id)
        terms.append(item)
        total_forms += len(item.get("forms") or [])
        if idx % 200 == 0 or idx == total:
            print(f"Collected {idx}/{total} terms")

    return {
        "meta": {
            "exported_at": dt.datetime.now(dt.UTC).isoformat(),
            "mode": "bulk",
            "filters": {
                "redacted": redacted,
                "deleted": deleted,
            },
            "term_count": len(terms),
            "forms_count": total_forms,
        },
        "terms": terms,
    }


def _upsert_row(
    cur: psycopg2.extensions.cursor,
    schema: str,
    table: str,
    row: Dict[str, Any],
    pk_column: str = "id",
) -> None:
    if not row:
        return
    columns = list(row.keys())
    values = [row[col] for col in columns]
    update_columns = [col for col in columns if col != pk_column]
    query = sql.SQL(
        "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO {}"
    ).format(
        sql.Identifier(schema, table),
        sql.SQL(", ").join(sql.Identifier(col) for col in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        sql.Identifier(pk_column),
        sql.SQL("UPDATE SET {}").format(
            sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(col), sql.Identifier(col))
                for col in update_columns
            )
        )
        if update_columns
        else sql.SQL("NOTHING"),
    )
    cur.execute(query, values)


def import_term_payload(
    conn: psycopg2.extensions.connection,
    payload: Dict[str, Any],
    *,
    commit: bool = True,
) -> Tuple[int, int]:
    term = payload.get("term")
    if not isinstance(term, dict):
        raise ValueError("Payload does not contain valid 'term' object")
    term_id = term.get("id")
    if not isinstance(term_id, int):
        raise ValueError("Payload term has no integer 'id'")

    forms_raw = payload.get("forms") or []
    if not isinstance(forms_raw, list):
        raise ValueError("Payload 'forms' must be a list")

    owner = payload.get("owner")
    sources = payload.get("sources") or []
    if owner is not None and not isinstance(owner, dict):
        raise ValueError("Payload 'owner' must be an object or null")
    if not isinstance(sources, list):
        raise ValueError("Payload 'sources' must be a list")

    with conn.cursor() as cur:
        if owner:
            _upsert_row(cur, "public", "users", owner, pk_column="id")
        for source in sources:
            if isinstance(source, dict):
                _upsert_row(cur, "public", "sources", source, pk_column="id")

        _upsert_row(cur, "public", "terms", term, pk_column="id")

        cur.execute("DELETE FROM public.term_word_associations WHERE term_id = %s", (term_id,))

        inserted = 0
        insert_sql = sql.SQL(
            """
            INSERT INTO public.term_word_associations ({})
            VALUES ({})
            """
        ).format(
            sql.SQL(", ").join(sql.Identifier(col) for col in TERM_FORMS_COLUMNS),
            sql.SQL(", ").join(sql.Placeholder() for _ in TERM_FORMS_COLUMNS),
        )

        for raw in forms_raw:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["term_id"] = term_id
            values = [row.get(col) for col in TERM_FORMS_COLUMNS]
            cur.execute(insert_sql, values)
            inserted += 1

    if commit:
        conn.commit()
    return term_id, inserted


def import_terms_payload(
    conn: psycopg2.extensions.connection,
    payload: Dict[str, Any],
) -> Tuple[int, int]:
    items = payload.get("terms")
    if not isinstance(items, list):
        raise ValueError("Bulk payload must contain list field 'terms'")

    imported_terms = 0
    imported_forms = 0
    total = len(items)

    try:
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Invalid item at index {idx - 1}: expected object")
            _term_id, inserted_forms = import_term_payload(conn, item, commit=False)
            imported_terms += 1
            imported_forms += inserted_forms
            if idx % 200 == 0 or idx == total:
                print(f"Imported {idx}/{total} terms")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return imported_terms, imported_forms


def cmd_export(args: argparse.Namespace) -> None:
    payload_path = Path(args.output)
    with _get_connection(args.database_url) as conn:
        payload = export_term_payload(conn, args.term_id)
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(f"Exported term {args.term_id} to {payload_path}")
    print(f"Forms exported: {len(payload.get('forms') or [])}")


def cmd_export_bulk(args: argparse.Namespace) -> None:
    payload_path = Path(args.output)
    with _get_connection(args.database_url) as conn:
        payload = export_terms_payload_by_flags(
            conn,
            redacted=args.redacted,
            deleted=args.deleted,
            limit=args.limit,
        )
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    meta = payload.get("meta") or {}
    print(f"Exported {meta.get('term_count', 0)} terms to {payload_path}")
    print(f"Forms exported: {meta.get('forms_count', 0)}")
    print(
        "Filters used: "
        f"redacted={meta.get('filters', {}).get('redacted')}, "
        f"deleted={meta.get('filters', {}).get('deleted')}"
    )


def cmd_import(args: argparse.Namespace) -> None:
    payload_path = Path(args.input)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    with _get_connection(args.database_url) as conn:
        if isinstance(payload, dict) and isinstance(payload.get("terms"), list):
            imported_terms, imported_forms = import_terms_payload(conn, payload)
            print(f"Imported {imported_terms} terms from {payload_path}")
            print(f"Forms inserted: {imported_forms}")
            return

        term_id, inserted_forms = import_term_payload(conn, payload, commit=True)
    print(f"Imported term {term_id} from {payload_path}")
    print(f"Forms inserted: {inserted_forms}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env (default: .env).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Export one term with forms")
    exp.add_argument("--term-id", type=int, required=True, help="Term ID to export")
    exp.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output JSON file path",
    )
    exp.add_argument(
        "--database-url",
        default=None,
        help="Source DB URL (default: DATABASE_URL from env/.env)",
    )
    exp.set_defaults(func=cmd_export)

    bulk = sub.add_parser("export-bulk", help="Export many terms with filters")
    bulk.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output JSON file path",
    )
    bulk.add_argument(
        "--redacted",
        type=_parse_bool_arg,
        default=False,
        help="Filter by redacted flag (default: false)",
    )
    bulk.add_argument(
        "--deleted",
        type=_parse_bool_arg,
        default=False,
        help="Filter by deleted flag (default: false)",
    )
    bulk.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for number of exported terms",
    )
    bulk.add_argument(
        "--database-url",
        default=None,
        help="Source DB URL (default: DATABASE_URL from env/.env)",
    )
    bulk.set_defaults(func=cmd_export_bulk)

    imp = sub.add_parser("import", help="Import one term or bulk payload with forms")
    imp.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Input JSON payload path",
    )
    imp.add_argument(
        "--database-url",
        default=None,
        help="Target DB URL (default: DATABASE_URL from env/.env)",
    )
    imp.set_defaults(func=cmd_import)

    args = parser.parse_args()
    _load_dotenv(args.env_file, override=False)
    database_url = getattr(args, "database_url", None) or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    args.database_url = database_url
    args.func(args)


if __name__ == "__main__":
    main()