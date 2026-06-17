"""Translate Lemko text into Polish with GPT assistance and database hints."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, cast

import psycopg2
import psycopg2.extras
import psycopg2.extensions

try:  # pragma: no cover - import guard for optional dependency
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled at runtime
    OpenAI = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency shared with older scripts
    from lem_forms_common import _ensure_openai_key, _get_connection  # type: ignore
except Exception:  # pragma: no cover - API deployments use fallback implementations
    _ensure_openai_key = None  # type: ignore[assignment]
    _get_connection = None  # type: ignore[assignment]

DEFAULT_MODEL = "gpt-5"
CODEX_DEFAULT_SENTINEL_MODELS = {"gpt-5", "gpt-5-mini"}
CODEX_MODEL_ENV_VARS: Tuple[str, ...] = (
    "LEM_TRANSLATE_CODEX_MODEL",
    "CODEX_TRANSLATE_MODEL",
    "CODEX_MODEL",
)
_CODEX_EXEC_LOCK = threading.Lock()

SYSTEM_PROMPT = (
    "Jesteś tłumaczem języka łemkowskiego (nie ukraińskiego). "
    "Odpowiadasz wyłącznie w formie JSON zawierającym pola: translated_text (string lub pusty), "
    "unknown_words (lista stringów) oraz needs_dictionary (wartość bool). "
    "Jeżeli choć jedno słowo nie ma dla Ciebie 100% pewności znaczenia, ustaw needs_dictionary=true "
    "i wypisz wszystkie takie słowa w unknown_words. "
    "Jeżeli zwracasz translated_text, musisz mieć całkowitą pewność poprawności i ustawić needs_dictionary=false."
)

USER_PROMPT_TEMPLATE = (
    "Przetłumacz poniższy tekst na język polski. "
    "Najpierw zdecyduj, czy masz 100% pewności znaczenia KAŻDEGO terminu. "
    "Jeśli nie masz absolutnej pewności dla któregokolwiek słowa, ustaw needs_dictionary=true oraz "
    "wypisz je w unknown_words (co najmniej jedno słowo) i nie podawaj translated_text. "
    "Jeśli masz pełną pewność, ustaw needs_dictionary=false, unknown_words=[] oraz zwróć translated_text.\n{tekst}"
)

FORCED_TRANSLATION_PROMPT = (
    "Masz już wszystkie definicje słownikowe powyżej. Teraz MUSISZ zwrócić pełny JSON z wypełnionym "
    "pole translated_text (tłumaczenie na polski), listą semantic_description_pl (z tłumaczeniami opisów) "
    "i ustawić needs_dictionary=false. unknown_words możesz pozostawić pustą listę lub wypisać problemy, "
    "ale translated_text jest obowiązkowe."
)

ROMAN_SUFFIX_RE = re.compile(r"\s+([IVXLCDMІVXLCДМ]+)$", re.IGNORECASE)

TRANSLATION_KEYS: Tuple[str, ...] = (
    "translated_text",
    "translatedText",
    "translation",
    "translated",
    "text",
    "tekst",
    "result",
    "answer",
)

UNKNOWN_WORD_KEYS: Tuple[str, ...] = (
    "unknown_words",
    "unknownWords",
    "unknown_terms",
    "unknownTerms",
    "unknown",
    "missing_words",
    "missingWords",
)

NEEDS_DICTIONARY_KEYS: Tuple[str, ...] = (
    "needs_dictionary",
    "needsDictionary",
    "need_dictionary",
    "needDictionary",
    "needs_dict",
    "needsDict",
)

SEMANTIC_DESCRIPTION_PL_KEYS: Tuple[str, ...] = (
    "semantic_description_pl",
    "semanticDescriptionPl",
    "semantic_desc_pl",
    "semanticDescPl",
)

SERVICE_TIER_ENV_VARS: Tuple[str, ...] = ("LEM_TRANSLATE_SERVICE_TIER", "OPENAI_SERVICE_TIER")

INITIAL_JSON_SCHEMA = {
    "name": "lemko_translation_decision",
    "schema": {
        "type": "object",
        "properties": {
            "translated_text": {
                "type": "string",
                "description": "Pełne tłumaczenie na język polski, gdy wszystkie terminy są jasne.",
            },
            "unknown_words": {
                "type": "array",
                "items": {
                    "type": "string",
                    "description": "Pojedyncze słowo lub fraza łemkowska wymagająca wyjaśnienia słownikowego.",
                },
                "description": "Lista niezrozumiałych słów, jeśli tłumaczenie nie jest możliwe bez słownika.",
                "minItems": 0,
                "uniqueItems": True,
            },
            "needs_dictionary": {
                "type": "boolean",
                "description": "Ustaw true, jeśli potrzebujesz słownika; false tylko przy pełnej pewności tłumaczenia.",
            },
        },
        "required": ["needs_dictionary"],
        "additionalProperties": False,
    },
}

FINAL_JSON_SCHEMA = {
    "name": "lemko_translation_final",
    "schema": {
        "type": "object",
        "properties": {
            "translated_text": {
                "type": "string",
                "description": "Ostateczne tłumaczenie na język polski wykorzystujące dane słownikowe.",
            },
            "unknown_words": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pole informacyjne używane w pierwszym kroku. W odpowiedzi końcowej może pozostać puste.",
                "minItems": 0,
                "uniqueItems": True,
            },
            "semantic_description_pl": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "lemma": {"type": "string"},
                        "description_pl": {"type": "string"},
                    },
                    "required": ["lemma", "description_pl"],
                    "additionalProperties": False,
                },
                "description": "Lista tłumaczeń opisów semantycznych dla użytych haseł.",
                "minItems": 0,
            },
        },
        "required": ["translated_text"],
        "additionalProperties": False,
    },
}

_FASTTEXT_MODULE: Optional[object] = None
_FASTTEXT_SUGGEST: Optional[Callable[..., Any]] = None
_FASTTEXT_LOAD_ERROR: Optional[str] = None
_FASTTEXT_PATH = Path(__file__).resolve().parent / "pl-en-fasttext.py"


if _ensure_openai_key is None:  # pragma: no cover - runtime-specific helper

    def _ensure_openai_key() -> str:
        """
        Ensure OPENAI_API_KEY is available either directly or via OPENAI_API_KEY_FILE.
        Mirrors helper functions used in other services so deployments stay consistent.
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

        raise RuntimeError("Brak klucza OpenAI. Ustaw OPENAI_API_KEY lub OPENAI_API_KEY_FILE.")


if _get_connection is None:  # pragma: no cover - runtime-specific helper

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

        raw_url = (
            os.environ.get("DATABASE_URL")
            or os.environ.get("LEM_TRANSLATE_DATABASE_URL")
            or _default_database_url()
        )
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


@dataclass
class DictionaryEntry:
    source_word: str
    lemma: str
    semantic_description: str
    context: str
    found: bool = True

    def prompt_line(self) -> str:
        """Return concise string used in the follow-up prompt for GPT."""
        parts = [
            f"Lemat: {self.lemma}",
            f"Opis semantyczny: {self.semantic_description}",
        ]
        if self.context and self.context != "(brak kontekstu)":
            parts.append(f"Kontekst: {self.context}")
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, str]:
        """Expose dictionary entry in plain dict form for JSON responses."""
        return {
            "source_word": self.source_word,
            "lemma": self.lemma,
            "semantic_description": self.semantic_description,
            "context": self.context,
        }


def _get_fasttext_suggest() -> Optional[Callable[..., Any]]:
    """Load FastText helper module lazily and return its suggest() function."""
    global _FASTTEXT_MODULE, _FASTTEXT_SUGGEST, _FASTTEXT_LOAD_ERROR
    if _FASTTEXT_SUGGEST is not None or _FASTTEXT_LOAD_ERROR is not None:
        return _FASTTEXT_SUGGEST
    try:
        spec = importlib.util.spec_from_file_location("_pl_en_fasttext", str(_FASTTEXT_PATH))
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location failed")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suggest_fn = getattr(module, "suggest", None)
        if not callable(suggest_fn):
            raise AttributeError("Module pl-en-fasttext.py does not expose callable suggest()")
        _FASTTEXT_MODULE = module
        _FASTTEXT_SUGGEST = cast(Callable[..., Any], suggest_fn)
        return _FASTTEXT_SUGGEST
    except Exception as exc:
        _FASTTEXT_LOAD_ERROR = str(exc)
        print(f"[WARN] Nie udało się załadować modułu pl-en-fasttext.py: {exc}", file=sys.stderr)
        return None


def _fasttext_suggest_words(word: str, topn: int = 1) -> List[str]:
    """Return additional lookup candidates using FastText-based suggest()."""
    suggest_fn = _get_fasttext_suggest()
    if suggest_fn is None:
        return []
    try:
        candidates = suggest_fn(word, lang="lem", topn=topn, debug=False)  # type: ignore[misc]
    except Exception as exc:
        print(f"[WARN] FastText suggest() nie powiodło się dla {word!r}: {exc}", file=sys.stderr)
        return []
    normalized: List[str] = []
    seen: set[str] = set()
    for item in candidates or []:
        text = _clean_optional_text(item)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(text)
    return normalized


def _clean_optional_text(value: Optional[Any]) -> Optional[str]:
    """Normalize optional text-like values by stripping whitespace and empty strings."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_service_tier(value: Optional[str]) -> Optional[str]:
    """Lowercase and strip the provided service tier value."""
    cleaned = _clean_optional_text(value)
    if not cleaned:
        return None
    return cleaned.lower()


def _default_service_tier() -> Optional[str]:
    """Read preferred service_tier value from environment variables."""
    for env_key in SERVICE_TIER_ENV_VARS:
        candidate = _normalize_service_tier(os.getenv(env_key))
        if candidate:
            return candidate
    return None


def _split_roman_suffix(text: str) -> Tuple[str, Optional[str]]:
    """Remove trailing roman numeral markers (np. 'I', 'II') from headwords for lookup variants."""
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


def _lemma_key_variants(lemma: Optional[str]) -> List[str]:
    """Return normalized lemma variants (with and without trailing roman numerals)."""
    cleaned = _clean_optional_text(lemma)
    if not cleaned:
        return []
    variants: List[str] = []
    lowered = cleaned.lower()
    variants.append(lowered)
    base, _ = _split_roman_suffix(cleaned)
    if base:
        lowered_base = base.lower()
        if lowered_base not in variants:
            variants.append(lowered_base)
    return variants


def _build_description_pl_map(items: Sequence[Dict[str, str]]) -> Dict[str, str]:
    """Build lookup map lemma->description_pl for quick enrichment of resolved entries."""
    mapping: Dict[str, str] = {}
    if not items:
        return mapping
    for entry in items:
        lemma = _clean_optional_text(entry.get("lemma"))
        description = _clean_optional_text(entry.get("description_pl"))
        if not description:
            continue
        keys = _lemma_key_variants(lemma)
        if not keys:
            continue
        for key in keys:
            mapping.setdefault(key, description)
    return mapping


def _resolved_entries_with_descriptions(
    entries: Sequence["DictionaryEntry"], semantics: Sequence[Dict[str, str]]
) -> List[Dict[str, Any]]:
    """Return resolved dictionary entries enriched with translated semantic descriptions."""
    description_map = _build_description_pl_map(semantics)
    resolved_entries: List[Dict[str, Any]] = []
    for entry in entries:
        if not entry.found:
            continue
        payload = entry.to_dict()
        for key in _lemma_key_variants(entry.lemma):
            description = description_map.get(key)
            if description:
                payload["semantic_description_pl"] = description
                break
        resolved_entries.append(payload)
    return resolved_entries


def _llm_provider() -> str:
    provider = os.getenv("LEM_TRANSLATE_PROVIDER", "openai").strip().lower()
    return provider or "openai"


def _codex_executable() -> str:
    configured = os.getenv("CODEX_CLI_PATH", "codex").strip() or "codex"
    if os.path.isabs(configured):
        if os.access(configured, os.X_OK):
            return configured
        raise RuntimeError(f"Codex CLI nie jest wykonywalny: {configured}")
    resolved = shutil.which(configured)
    if resolved:
        return resolved
    raise RuntimeError(f"Nie znaleziono Codex CLI w PATH: {configured}")


def _codex_timeout_seconds() -> int:
    raw = os.getenv("CODEX_CLI_TIMEOUT_SECONDS", "900").strip()
    try:
        return max(30, int(raw))
    except ValueError:
        return 900


def _configured_codex_model() -> Optional[str]:
    for name in CODEX_MODEL_ENV_VARS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _codex_model_arg(model: Optional[str]) -> Optional[str]:
    configured = _configured_codex_model()
    if configured:
        return configured
    requested = (model or "").strip()
    if not requested:
        return None
    if requested in CODEX_DEFAULT_SENTINEL_MODELS:
        return None
    return requested


def _effective_model_label(model: str) -> str:
    if _llm_provider() != "codex":
        return model
    return _codex_model_arg(model) or "codex-cli-default"


def _copy_codex_auth(codex_home: Path) -> None:
    auth_dir = Path(os.getenv("CODEX_AUTH_DIR", "/run/codex-auth")).expanduser()
    auth_file = Path(os.getenv("CODEX_AUTH_FILE", str(auth_dir / "auth.json"))).expanduser()
    if not auth_file.is_file():
        fallback = Path.home() / ".codex" / "auth.json"
        if fallback.is_file():
            auth_file = fallback
        else:
            raise RuntimeError(
                "Brak pliku auth Codex. Ustaw CODEX_AUTH_DIR albo CODEX_AUTH_FILE."
            )

    codex_home.mkdir(parents=True, exist_ok=True)
    target_auth = codex_home / "auth.json"
    shutil.copy2(auth_file, target_auth)
    target_auth.chmod(0o600)

    config_file = Path(os.getenv("CODEX_CONFIG_FILE", str(auth_dir / "config.toml"))).expanduser()
    if config_file.is_file():
        target_config = codex_home / "config.toml"
        shutil.copy2(config_file, target_config)
        target_config.chmod(0o600)


def _schema_from_response_format(response_format: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(response_format, dict):
        return None
    json_schema = response_format.get("json_schema")
    if isinstance(json_schema, dict) and isinstance(json_schema.get("schema"), dict):
        return _schema_for_codex(cast(Dict[str, Any], json_schema["schema"]))
    return None


def _schema_for_codex(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {
            key: _schema_for_codex(item)
            for key, item in value.items()
            if key not in {"uniqueItems"}
        }
        properties = sanitized.get("properties")
        if isinstance(properties, dict):
            sanitized["required"] = list(properties.keys())
        return sanitized
    if isinstance(value, list):
        return [_schema_for_codex(item) for item in value]
    return value


def _messages_to_codex_prompt(messages: Sequence[Dict[str, str]], schema: Optional[Dict[str, Any]]) -> str:
    lines: List[str] = [
        "Pracujesz jako backendowy model językowy dla API tłumacza.",
        "Nie uruchamiaj komend, nie czytaj plików i nie modyfikuj środowiska.",
        "Odpowiedz wyłącznie treścią końcową wymaganą przez rozmowę poniżej.",
    ]
    if schema:
        lines.append("Odpowiedź musi być poprawnym JSON zgodnym z dołączonym schema; bez Markdown i komentarzy.")
    else:
        lines.append("Odpowiedź ma być bez Markdown i bez dodatkowych komentarzy.")

    for message in messages:
        role = (message.get("role") or "user").upper()
        content = message.get("content") or ""
        lines.append(f"{role}:\n{content}")
    return "\n\n".join(lines)


def _codex_error_tail(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= 1200:
        return cleaned
    return cleaned[-1200:]


def codex_complete_text(
    prompt: str,
    *,
    model: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> str:
    """Run Codex CLI once and return the final assistant message."""
    executable = _codex_executable()
    timeout = _codex_timeout_seconds()

    with tempfile.TemporaryDirectory(prefix="lemko-codex-") as tmp:
        base = Path(tmp)
        workdir = base / "work"
        codex_home = base / "codex-home"
        workdir.mkdir(parents=True, exist_ok=True)
        _copy_codex_auth(codex_home)

        output_path = base / "last-message.txt"
        cmd = [
            executable,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--cd",
            str(workdir),
            "--color",
            "never",
            "-o",
            str(output_path),
        ]
        codex_model = _codex_model_arg(model)
        if codex_model:
            cmd.extend(["-m", codex_model])

        if schema:
            schema_path = base / "output-schema.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            cmd.extend(["--output-schema", str(schema_path)])

        cmd.append("-")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        env["HOME"] = str(base / "home")
        env.setdefault("NO_COLOR", "1")
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

        try:
            with _CODEX_EXEC_LOCK:
                completed = subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    env=env,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex CLI przekroczył limit czasu {timeout}s.") from exc

        if completed.returncode != 0:
            details = _codex_error_tail(completed.stderr or completed.stdout)
            if details:
                raise RuntimeError(f"Codex CLI zakończył się błędem: {details}")
            raise RuntimeError(f"Codex CLI zakończył się kodem {completed.returncode}.")

        if output_path.is_file():
            output = output_path.read_text(encoding="utf-8").strip()
        else:
            output = (completed.stdout or "").strip()
        if not output:
            raise RuntimeError("Codex CLI zwrócił pustą odpowiedź.")
        return output


def _create_codex_response(payload: Dict[str, Any]) -> SimpleNamespace:
    messages = payload.get("input")
    if not isinstance(messages, list):
        raise RuntimeError("Niepoprawny payload LLM: brak listy input.")
    schema = _schema_from_response_format(payload.get("response_format"))
    prompt = _messages_to_codex_prompt(cast(List[Dict[str, str]], messages), schema)
    output = codex_complete_text(prompt, model=str(payload.get("model") or ""), schema=schema)
    return SimpleNamespace(output_text=output)


def _call_llm(
    client: Optional["OpenAI"],
    messages: List[Dict[str, str]],
    *,
    schema: Dict[str, Any],
    model: str,
    service_tier: Optional[str] = None,
) -> Tuple[Any, str]:
    """Send a request to the Responses API, returning parsed JSON payload plus raw text."""
    request_payload: Dict[str, Any] = {
        "model": model,
        "input": messages,
    }
    if schema:
        request_payload["response_format"] = {"type": "json_schema", "json_schema": schema}
    if service_tier:
        request_payload["service_tier"] = service_tier

    response = _create_response(client, request_payload)
    text = getattr(response, "output_text", None)
    if not text:
        raise RuntimeError("Pusta odpowiedź modelu.")
    if isinstance(text, list):
        text = "\n".join(part for part in text if part)
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return text, text


def _normalize_unknown_words(raw: Optional[Iterable[Any]]) -> List[str]:
    """Turn any iterable of strings into a de-duplicated, trimmed list of unknown words."""
    normalized: List[str] = []
    seen: set[str] = set()
    if not raw:
        return normalized
    for item in raw:
        word = _clean_optional_text(item)
        if not word:
            continue
        lowered = word.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(word)
    return normalized


def _coerce_unknown_input(value: Any) -> List[str]:
    """Accept multiple container types (list/string/etc.) and normalize into list of unknown terms."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return _normalize_unknown_words(value)
    if isinstance(value, str):
        tokens = re.split(r"[,;\n]+", value)
        return _normalize_unknown_words(tokens)
    return []


def _normalize_semantic_description_pl(value: Any) -> List[Dict[str, str]]:
    """Normalize semantic description translations into a consistent [{lemma, description_pl}] list."""
    normalized: List[Dict[str, str]] = []

    def _append(lemma: Optional[str], description: Optional[str]) -> None:
        clean_desc = _clean_optional_text(description)
        if not clean_desc:
            return
        clean_lemma = _clean_optional_text(lemma) or ""
        normalized.append({"lemma": clean_lemma, "description_pl": clean_desc})

    if value is None:
        return normalized
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                _append(item.get("lemma"), item.get("description_pl") or item.get("semantic_description_pl") or item.get("description"))
            elif isinstance(item, str):
                _append(None, item)
    elif isinstance(value, dict):
        for key, entry in value.items():
            if isinstance(entry, dict):
                _append(entry.get("lemma") or key, entry.get("description") or entry.get("description_pl") or entry.get("semantic_description_pl"))
            else:
                _append(key, entry)
    elif isinstance(value, str):
        _append(None, value)
    return normalized


def _normalize_llm_payload(payload: Any, *, raw_text: Optional[str] = None) -> Dict[str, Any]:
    """Extract best-effort translation + unknown words from loose LLM responses."""
    translation = None
    unknown_words: List[str] = []
    needs_dictionary: Optional[bool] = None
    semantic_description_pl: List[Dict[str, str]] = []

    if isinstance(payload, dict):
        for key in TRANSLATION_KEYS:
            if key in payload:
                translation = _clean_optional_text(payload.get(key))
                if translation:
                    break
        for key in UNKNOWN_WORD_KEYS:
            if key in payload:
                unknown_words = _coerce_unknown_input(payload.get(key))
                if unknown_words:
                    break
        for key in NEEDS_DICTIONARY_KEYS:
            if key in payload:
                needs_dictionary = bool(payload.get(key))
                break
        for key in SEMANTIC_DESCRIPTION_PL_KEYS:
            if key in payload:
                semantic_description_pl = _normalize_semantic_description_pl(payload.get(key))
                break
    elif isinstance(payload, str):
        translation = _clean_optional_text(payload)
    elif isinstance(payload, (list, tuple, set)):
        parts = [_clean_optional_text(item) for item in payload]
        joined = " ".join(part for part in parts if part)
        translation = _clean_optional_text(joined)

    if translation is None and raw_text:
        trimmed = raw_text.strip()
        if trimmed and not trimmed.startswith("{"):
            translation = _clean_optional_text(trimmed)

    if needs_dictionary is None:
        needs_dictionary = False

    return {
        "translated_text": translation,
        "unknown_words": unknown_words,
        "needs_dictionary": needs_dictionary,
        "semantic_description_pl": semantic_description_pl,
    }


def _debug_dump_response(step: str, raw_text: str, payload: Any) -> None:
    """Print diagnostic information whenever LLM payload misses required keys."""
    header = f"[WARN] LLM odpowiedź ({step}) w nieoczekiwanym formacie."
    print(header, file=sys.stderr)
    if raw_text:
        print(f"[WARN] Treść: {raw_text}", file=sys.stderr)
    else:
        print("[WARN] Treść: <pusta>", file=sys.stderr)
    print(f"[WARN] Typ danych: {type(payload).__name__}", file=sys.stderr)
    try:
        print(f"[WARN] Repr: {payload!r}", file=sys.stderr)
    except Exception:  # pragma: no cover - defensive
        pass


def _collect_context(row: Dict[str, Any]) -> str:
    """Compose a compact textual context summary for a term row."""
    preferred: List[str] = []
    fallback: List[str] = []
    for idx in (1, 2, 3):
        body = _clean_optional_text(row.get(f"context{idx}_body"))
        if not body:
            continue
        tag = _clean_optional_text(row.get(f"context{idx}_tag"))
        authors = _clean_optional_text(row.get(f"context{idx}_authors"))
        normalized_tag = (tag or "").strip().lower()
        pieces: List[str] = []
        if authors and normalized_tag not in {"true", "false"}:
            pieces.append(authors)
        pieces.append(body)
        composed = ": ".join(pieces) if len(pieces) > 1 else body
        if normalized_tag == "true":
            preferred.append(composed)
        else:
            fallback.append(composed)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return "(brak kontekstu)"


def _derive_semantic_description(row: Dict[str, Any]) -> str:
    """Pick the best available semantic description for a term (semantic/polish/grammatic)."""
    for key in ("semantic_description", "polish_translation", "grammatic_description"):
        value = _clean_optional_text(row.get(key))
        if value:
            return value
    return "(brak opisu)"


def _lookup_terms_by_base(
    conn: psycopg2.extensions.connection,
    word: str,
) -> List[Dict[str, Any]]:
    """Fetch all dictionary terms whose base form equals the queried word (case-insensitive)."""
    sql = """
        SELECT
            t.id,
            t.base_form,
            t.semantic_description,
            t.polish_translation,
            t.grammatic_description,
            t.context1_body,
            t.context1_tag,
            t.context2_body,
            t.context2_tag,
            t.context3_body,
            t.context3_tag,
            s1.authors AS context1_authors,
            s2.authors AS context2_authors,
            s3.authors AS context3_authors,
            t."order" AS order_index
        FROM public.terms AS t
        LEFT JOIN public.sources AS s1 ON s1.id = t.context1_source_id
        LEFT JOIN public.sources AS s2 ON s2.id = t.context2_source_id
        LEFT JOIN public.sources AS s3 ON s3.id = t.context3_source_id
        WHERE t.deleted = FALSE
          AND t.redacted = TRUE
          AND lower(t.base_form) = lower(%s)
        ORDER BY ("order" IS NULL), "order", t.id
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (word,))
        return cur.fetchall() or []


def _lookup_terms_by_form(
    conn: psycopg2.extensions.connection,
    word: str,
) -> List[Dict[str, Any]]:
    """Fetch dictionary terms that have the queried inflected form assigned."""
    sql = """
        SELECT DISTINCT ON (t.id)
            t.id,
            t.base_form,
            t.semantic_description,
            t.polish_translation,
            t.grammatic_description,
            t.context1_body,
            t.context1_tag,
            t.context2_body,
            t.context2_tag,
            t.context3_body,
            t.context3_tag,
            s1.authors AS context1_authors,
            s2.authors AS context2_authors,
            s3.authors AS context3_authors,
            t."order" AS order_index
        FROM public.term_word_associations AS twa
        JOIN public.terms AS t ON t.id = twa.term_id
        LEFT JOIN public.sources AS s1 ON s1.id = t.context1_source_id
        LEFT JOIN public.sources AS s2 ON s2.id = t.context2_source_id
        LEFT JOIN public.sources AS s3 ON s3.id = t.context3_source_id
        WHERE t.deleted = FALSE
          AND t.redacted = TRUE
          AND lower(twa.word) = lower(%s)
        ORDER BY t.id, ("order" IS NULL), "order"
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (word,))
        return cur.fetchall() or []


def _build_lookup_variants(word: str) -> List[str]:
    """Create lookup variants for a word (raw + version bez numeru rzymskiego)."""
    variants: List[str] = []
    cleaned = _clean_optional_text(word)
    if not cleaned:
        return variants
    variants.append(cleaned)
    base, _ = _split_roman_suffix(cleaned)
    if base and base.lower() != cleaned.lower():
        variants.append(base)
    return variants


def _augment_variants_with_suggestions(word: str, base_variants: Sequence[str]) -> List[str]:
    """Merge explicit variants with FastText-based suggestions."""
    ordered: List[str] = []
    seen: set[str] = set()

    def _append(candidate: Optional[str]) -> None:
        if not candidate:
            return
        normalized = _clean_optional_text(candidate)
        if not normalized:
            return
        lowered = normalized.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        ordered.append(normalized)

    for item in base_variants:
        _append(item)
    for suggestion in _fasttext_suggest_words(word, topn=1):
        _append(suggestion)
    return ordered


def _resolve_unknown_words(words: Sequence[str]) -> List[DictionaryEntry]:
    """Query Postgres for each unknown word, gathering lemma info and contexts."""
    if not words:
        return []
    with closing(_get_connection()) as conn:
        entries: List[DictionaryEntry] = []
        for word in words:
            variants = _augment_variants_with_suggestions(word, _build_lookup_variants(word))
            seen_ids: set[int] = set()
            matches: List[Dict[str, Any]] = []
            for variant in variants:
                for row in _lookup_terms_by_base(conn, variant):
                    term_id = row.get("id")
                    if isinstance(term_id, int) and term_id in seen_ids:
                        continue
                    if isinstance(term_id, int):
                        seen_ids.add(term_id)
                    matches.append(row)
            for variant in variants:
                for row in _lookup_terms_by_form(conn, variant):
                    term_id = row.get("id")
                    if isinstance(term_id, int) and term_id in seen_ids:
                        continue
                    if isinstance(term_id, int):
                        seen_ids.add(term_id)
                    matches.append(row)
            if not matches:
                entries.append(
                    DictionaryEntry(
                        source_word=word,
                        lemma=word,
                        semantic_description="(brak wpisu w bazie)",
                        context="(brak kontekstu)",
                        found=False,
                    )
                )
                continue
            matches.sort(key=_matches_sort_key)
            for row in matches:
                entries.append(
                    DictionaryEntry(
                        source_word=word,
                        lemma=_clean_optional_text(row.get("base_form")) or word,
                        semantic_description=_derive_semantic_description(row),
                        context=_collect_context(row),
                        found=True,
                    )
                )
        return entries


def _build_dictionary_prompt(entries: Sequence[DictionaryEntry]) -> str:
    """Format dictionary entries into lines passed back to the LLM."""
    if not entries:
        return "Brak wpisów słownikowych."
    lines = [entry.prompt_line() for entry in entries]
    base = "\n".join(lines)
    instructions = (
        "\n\nNa podstawie powyższych haseł przetłumacz tekst i przygotuj finalny JSON zawierający: "
        "'translated_text', 'unknown_words', 'semantic_description_pl' (lista obiektów {\"lemma\": \"...\", "
        "\"description_pl\": \"tłumaczenie opisu semantycznego\"}). "
        "Przetłumacz WYŁĄCZNIE treść z sekcji 'Opis semantyczny' (bez fragmentów z pola 'Kontekst') na język polski "
        "i umieść ją w semantic_description_pl razem z odpowiadającym lematem."
    )
    return base + instructions


def _matches_sort_key(row: Dict[str, Any]) -> Tuple[int, int, int]:
    """Ensure deterministic ordering of term lookup results (explicit order first)."""
    order_value = row.get("order_index")
    has_no_order = 1 if order_value is None else 0
    order_rank = int(order_value) if isinstance(order_value, int) else 0
    term_id = row.get("id")
    term_rank = int(term_id) if isinstance(term_id, int) else 0
    return has_no_order, order_rank, term_rank


def lem_translate(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
    service_tier: Optional[str] = None,
) -> Dict[str, Any]:
    """Main orchestration: ask LLM for translation, add dictionary hints if needed, return summary."""
    cleaned = _clean_optional_text(text)
    if not cleaned:
        raise ValueError("Tekst do tłumaczenia nie może być pusty.")

    provider = _llm_provider()
    if provider == "openai":
        if OpenAI is None:  # pragma: no cover - runtime guard
            raise RuntimeError("Pakiet openai nie jest zainstalowany.")
        _ensure_openai_key()
        client: Optional["OpenAI"] = OpenAI()  # pragma: no cover - network object
    elif provider == "codex":
        client = None
    else:
        raise RuntimeError(f"Nieobsługiwany provider LLM: {provider!r}")

    effective_model = _effective_model_label(model)
    resolved_service_tier = _normalize_service_tier(service_tier) or _default_service_tier()

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(tekst=cleaned)},
    ]

    first_raw_payload, first_raw_text = _call_llm(
        client,
        messages,
        schema=INITIAL_JSON_SCHEMA,
        model=model,
        service_tier=resolved_service_tier,
    )
    normalized_first = _normalize_llm_payload(first_raw_payload, raw_text=first_raw_text)
    translation = normalized_first.get("translated_text")
    unknown_words = normalized_first.get("unknown_words", [])
    needs_dictionary_flag = bool(normalized_first.get("needs_dictionary"))

    if needs_dictionary_flag and not unknown_words:
        _debug_dump_response("krok 1", first_raw_text, first_raw_payload)
        raise RuntimeError("Model zgłosił potrzebę słownika, ale nie podał żadnych słów.")

    if translation and not needs_dictionary_flag:
        return {
            "translated_text": translation,
            "resolved_unknown_words": [],
            "missing_words": [],
            "attempts": 1,
            "model": effective_model,
            "semantic_description_pl": [],
        }

    if not unknown_words:
        _debug_dump_response("krok 1", first_raw_text, first_raw_payload)
        raise RuntimeError("Model nie zwrócił tłumaczenia ani listy niezrozumiałych słów.")

    dictionary_entries = _resolve_unknown_words(unknown_words)
    dictionary_prompt = _build_dictionary_prompt(dictionary_entries)

    messages.append({"role": "assistant", "content": first_raw_text})
    messages.append({"role": "user", "content": dictionary_prompt})

    final_translation: Optional[str] = None
    final_payload: Any = None
    final_text: str = ""
    semantic_description_pl: List[Dict[str, str]] = []
    MAX_FINAL_ATTEMPTS = 2
    attempts_left = MAX_FINAL_ATTEMPTS

    while attempts_left > 0 and not final_translation:
        final_payload, final_text = _call_llm(
            client,
            messages,
            schema=FINAL_JSON_SCHEMA,
            model=model,
            service_tier=resolved_service_tier,
        )
        normalized_second = _normalize_llm_payload(final_payload, raw_text=final_text)
        final_translation = normalized_second.get("translated_text")
        semantic_description_pl = normalized_second.get("semantic_description_pl", []) or semantic_description_pl
        if final_translation:
            break
        attempts_left -= 1
        _debug_dump_response("krok 2", final_text, final_payload)
        if attempts_left <= 0:
            break
        messages.append({"role": "assistant", "content": final_text})
        messages.append({"role": "user", "content": FORCED_TRANSLATION_PROMPT})

    if not final_translation:
        raise RuntimeError("Model nie dostarczył tłumaczenia w odpowiedzi końcowej mimo ponowienia prośby.")

    resolved = _resolved_entries_with_descriptions(dictionary_entries, semantic_description_pl)
    missing = [entry.source_word for entry in dictionary_entries if not entry.found]

    return {
        "translated_text": final_translation,
        "resolved_unknown_words": resolved,
        "missing_words": missing,
        "attempts": 2,
        "model": effective_model,
        "semantic_description_pl": semantic_description_pl,
    }


def _create_response(client: Optional["OpenAI"], payload: Dict[str, Any]) -> Any:
    """Call OpenAI Responses API with graceful fallbacks for unsupported kwargs."""
    provider = _llm_provider()
    if provider == "codex":
        return _create_codex_response(payload)
    if provider != "openai":
        raise RuntimeError(f"Nieobsługiwany provider LLM: {provider!r}")
    if client is None:
        raise RuntimeError("Brak klienta OpenAI.")

    attempt_payload = dict(payload)
    fallback_keys = ("response_format", "service_tier")
    while True:
        try:
            return client.responses.create(**attempt_payload)  # type: ignore[attr-defined]  # pragma: no cover - network
        except TypeError as exc:  # pragma: no cover - compatibility shim
            message = str(exc)
            removed = False
            for key in fallback_keys:
                if key in attempt_payload and key in message:
                    attempt_payload.pop(key, None)
                    removed = True
                    break
            if not removed:
                raise


def _parse_cli_args() -> argparse.Namespace:
    """Configure CLI arguments for standalone usage."""
    parser = argparse.ArgumentParser(description="Tłumaczenie tekstu łemkowskiego na polski z pomocą GPT.")
    parser.add_argument(
        "text",
        help="Tekst łemkowski do przetłumaczenia lub ścieżka do pliku .txt z treścią.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Nazwa modelu OpenAI (domyślnie {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--service-tier",
        help='Opcjonalne service_tier (np. "priority") dla pojedynczego uruchomienia.',
    )
    return parser.parse_args()


def _resolve_input_text(argument: str) -> str:
    """Take CLI input (literal or path) and return the text content."""
    path = Path(argument)
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - zależy od środowiska
            raise RuntimeError(f"Nie udało się odczytać pliku {path}: {exc}") from exc
    return argument


def main() -> None:
    """Entry point for CLI execution."""
    args = _parse_cli_args()
    input_text = _resolve_input_text(args.text)
    result = lem_translate(input_text, model=args.model, service_tier=args.service_tier)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
