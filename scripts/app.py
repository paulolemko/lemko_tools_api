# =============================
# app.py — API (odchudzone) korzystające z ASREngine
# =============================

import os, json, asyncio, datetime, uuid, importlib.util, threading, sys, csv, tempfile, shutil, functools, concurrent.futures, re, time
from typing import Dict, Any, Optional, List, Set, Sequence, Tuple, Literal
from pathlib import Path
import torchaudio
import numpy as np

from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Request, BackgroundTasks, Response
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, conint, constr


# import silnika ASR
from asr_engine import ASREngine, ASRConfig
from lem_translate import DEFAULT_MODEL as LEM_TRANSLATE_BASE_MODEL, lem_translate as run_lemko_translate
from styletts2_engine import StyleTTS2Engine, SynthesisResult

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Konfiguracja API ---
TRANS_DIR    = os.getenv("TRANS_DIR", "transkrypcje")
LOG_PATH     = os.getenv("LOG_PATH", "log.json")
TRANSCRIPTED_SOURCE_DIR = os.getenv("TRANSCRIPTED_SOURCE_DIR", "transcripted_source")
TRANSCRIPTIONS_CSV_PATH = os.getenv("TRANSCRIPTIONS_CSV_PATH", "transcriptions_log.csv")
TRANSCRIPTIONS_CSV_HEADERS = ("filename", "timestamp", "size_bytes", "transcript_text")
LEM_SEARCH_LOG_PATH = os.getenv("LEM_SEARCH_LOG_PATH", "lemko_search_log.csv")
LEM_SEARCH_LOG_HEADERS = ("timestamp", "endpoint", "query", "result")
LEM_TRANSLATE_LOG_PATH = os.getenv("LEM_TRANSLATE_LOG_PATH", "lemko_translate_log.csv")
LEM_TRANSLATE_LOG_HEADERS = ("timestamp", "endpoint", "query", "result_text")
LEM_TTS_LOG_PATH = os.getenv("LEM_TTS_LOG_PATH", "lemko_tts_log.csv")
LEM_TTS_LOG_HEADERS = ("timestamp", "endpoint", "speaker", "text")
ENVIRONMENT = os.getenv("ENVIRONMENT", os.getenv("APP_ENV", "development")).strip().lower()
PRODUCTION_MODE = _env_bool("PRODUCTION_MODE", ENVIRONMENT in {"prod", "production"})
CORS_ALLOW_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
CORS_ALLOW_CREDENTIALS = _env_bool("CORS_ALLOW_CREDENTIALS", not PRODUCTION_MODE)
JWT_SECRET  = os.getenv("JWT_SECRET", "").strip()
MAX_UPLOAD_MB = max(1, _env_int("MAX_UPLOAD_MB", 200))
MAX_AUDIO_S = max(0, _env_int("MAX_AUDIO_SECONDS", 7200))
ALLOWED_AUDIO_TYPES = {"audio/wav", "audio/x-wav", "audio/flac", "audio/mpeg", "application/octet-stream"}
RATE_LIMIT_ENABLED = PRODUCTION_MODE or _env_bool("RATE_LIMIT_ENABLED", False)
RATE_LIMIT_REQUESTS = max(1, _env_int("RATE_LIMIT_REQUESTS", 60))
RATE_LIMIT_WINDOW_SECONDS = max(1, _env_int("RATE_LIMIT_WINDOW_SECONDS", 60))
TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", PRODUCTION_MODE)


def _validate_security_config() -> None:
    if not PRODUCTION_MODE:
        return
    if not JWT_SECRET:
        raise RuntimeError("PRODUCTION_MODE requires JWT_SECRET to be set.")
    if not CORS_ALLOW_ORIGINS or "*" in CORS_ALLOW_ORIGINS:
        raise RuntimeError("PRODUCTION_MODE requires explicit CORS_ALLOW_ORIGINS without wildcard '*'.")
    if MAX_AUDIO_S <= 0:
        raise RuntimeError("PRODUCTION_MODE requires MAX_AUDIO_SECONDS to be greater than zero.")


_validate_security_config()

app = FastAPI(title="Lemko RNNT ASR – API v1 (engine-separated)", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
)

engine: ASREngine | None = None
sem = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENCY", "1")))
log_lock = asyncio.Lock()
csv_log_lock = asyncio.Lock()
lem_search_log_lock = asyncio.Lock()
lem_translate_log_lock = asyncio.Lock()
lem_tts_log_lock = asyncio.Lock()
jobs_lock = asyncio.Lock()
rate_limit_lock = asyncio.Lock()
jobs: Dict[str, Dict[str, Any]] = {}
rate_limit_buckets: Dict[str, List[float]] = {}

MAX_TTS_CONCURRENCY = max(1, int(os.getenv("MAX_TTS_CONCURRENCY", "1")))
TTS_MAX_WORKERS = max(1, int(os.getenv("TTS_MAX_WORKERS", str(MAX_TTS_CONCURRENCY))))
TTS_TEXT_MAX_CHARS = max(32, int(os.getenv("TTS_TEXT_MAX_CHARS", "1000")))
TTS_NUM_REFS = max(1, int(os.getenv("TTS_NUM_REFS", "3")))
TTS_TRIM_IN_MS = max(0, int(os.getenv("TTS_TRIM_IN_MS", "100")))
TTS_TRIM_OUT_MS = max(0, int(os.getenv("TTS_TRIM_OUT_MS", "200")))
tts_sem = asyncio.Semaphore(MAX_TTS_CONCURRENCY)
tts_engine: StyleTTS2Engine | None = None
tts_executor: concurrent.futures.ThreadPoolExecutor | None = None
tts_runtime_lock = threading.Lock()

LEM_SEARCH_ENABLED = os.getenv("LEM_SEARCH_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_lem_search_resources: Optional[Dict[str, Any]] = None
_lem_search_error: Optional[str] = None
_lem_search_lock = threading.Lock()

_pl_lem_search_resources: Optional[Dict[str, Any]] = None
_pl_lem_search_error: Optional[str] = None
_pl_lem_search_lock = threading.Lock()

LEM_TRANSLATE_ENABLED = os.getenv("LEM_TRANSLATE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_lem_translate_model_env = os.getenv("LEM_TRANSLATE_MODEL", "").strip()
LEM_TRANSLATE_DEFAULT_MODEL = _lem_translate_model_env or LEM_TRANSLATE_BASE_MODEL


def _client_rate_limit_key(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip() or "unknown"
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def _rate_limit_response(request: Request) -> Optional[JSONResponse]:
    if not RATE_LIMIT_ENABLED or not request.url.path.startswith("/v1/"):
        return None

    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    key = _client_rate_limit_key(request)
    async with rate_limit_lock:
        bucket = [ts for ts in rate_limit_buckets.get(key, []) if ts > cutoff]
        if len(bucket) >= RATE_LIMIT_REQUESTS:
            oldest = min(bucket) if bucket else now
            retry_after = max(1, int(round((oldest + RATE_LIMIT_WINDOW_SECONDS) - now)))
            rate_limit_buckets[key] = bucket
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(RATE_LIMIT_REQUESTS),
                    "X-RateLimit-Window": str(RATE_LIMIT_WINDOW_SECONDS),
                    "X-RateLimit-Remaining": "0",
                },
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "Too many requests.",
                        "details": {"retry_after_seconds": retry_after},
                    }
                },
            )
        bucket.append(now)
        rate_limit_buckets[key] = bucket

    return None


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    response = await _rate_limit_response(request)
    if response is not None:
        return response
    return await call_next(request)


def _load_lem_search_module():
    spec = importlib.util.spec_from_file_location("lem_search_module", Path(__file__).with_name("lem-search.py"))
    if spec is None or spec.loader is None:
        raise ImportError("Nie można załadować modułu lem-search.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _build_similar_args():
    limit = _env_int("LEM_SEARCH_SIMILAR_LIMIT", 10)
    lang = os.getenv("LEM_SEARCH_SIMILAR_LANG", "lem").strip().lower() or "lem"
    vocab_dir_env = os.getenv("LEM_SEARCH_SIMILAR_VOCAB_DIR") or os.getenv("VOCAB_JSON_DIR")
    vocab_dir = Path(vocab_dir_env).expanduser() if vocab_dir_env else None
    debug = _env_bool("LEM_SEARCH_SIMILAR_DEBUG", False)
    return {
        "limit": limit,
        "lang": lang,
        "vocab_dir": vocab_dir,
        "debug": debug,
    }


def _ensure_lem_search_loaded():
    global _lem_search_resources, _lem_search_error
    if _lem_search_resources or _lem_search_error is not None:
        return
    with _lem_search_lock:
        if _lem_search_resources or _lem_search_error is not None:
            return
        try:
            module = _load_lem_search_module()
            morphology_path = os.getenv("LEM_SEARCH_MORPHOLOGY_PATH")
            morphology = module.load_morphology_structure(Path(morphology_path)) if morphology_path else module.load_morphology_structure()
            similar_args = _build_similar_args()
            _lem_search_resources = {
                "module": module,
                "morphology": morphology,
                "similar_args": similar_args,
            }
        except Exception as exc:
            _lem_search_error = str(exc)


def _run_lem_search(query: str) -> Dict[str, Any]:
    _ensure_lem_search_loaded()
    if _lem_search_error:
        raise RuntimeError(_lem_search_error)
    assert _lem_search_resources is not None  # dla typu
    module = _lem_search_resources["module"]
    morphology = _lem_search_resources["morphology"]
    similar_args = _lem_search_resources["similar_args"]
    with module._get_connection() as conn:
        results = module.search_term(conn=conn, morphology=morphology, query=query, similar_args=similar_args)
    groups = module._group_results(results)
    groups_json = module._groups_to_json(groups)
    has_results = any(
        isinstance(group, dict)
        and isinstance(group.get("entries"), list)
        and len(group["entries"]) > 0
        for group in groups_json
    )
    return {
        "query": query,
        "groups": groups_json,
        "has_results": has_results,
    }


def _load_pl_lem_search_module():
    spec = importlib.util.spec_from_file_location("pl_lem_search_module", Path(__file__).with_name("pl-lem-search.py"))
    if spec is None or spec.loader is None:
        raise ImportError("Nie można załadować modułu pl-lem-search.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _ensure_pl_lem_search_loaded():
    global _pl_lem_search_resources, _pl_lem_search_error
    if _pl_lem_search_resources or _pl_lem_search_error is not None:
        return
    with _pl_lem_search_lock:
        if _pl_lem_search_resources or _pl_lem_search_error is not None:
            return
        try:
            module = _load_pl_lem_search_module()
            _pl_lem_search_resources = {"module": module}
        except Exception as exc:
            _pl_lem_search_error = str(exc)


def _run_pl_lem_translations(query: str, lang: str) -> Dict[str, Any]:
    _ensure_pl_lem_search_loaded()
    if _pl_lem_search_error:
        raise RuntimeError(_pl_lem_search_error)
    assert _pl_lem_search_resources is not None
    module = _pl_lem_search_resources["module"]

    normalized_lang = (lang or "").strip().lower()
    if not normalized_lang:
        raise ValueError("Brak ustawionego języka wyszukiwania.")
    lang_config = getattr(module, "LANG_CONFIG", {}).get(normalized_lang)
    if lang_config is None:
        raise ValueError(f"Nieobsługiwany język: {lang!r}")

    columns = tuple(str(col) for col in lang_config.get("translation_columns", ()))
    if not columns:
        raise RuntimeError(f"Brak kolumn tłumaczeń dla języka: {normalized_lang}")

    env_prefix = "LEM_PL" if normalized_lang == "pl" else "LEM_EN"
    suggest_lang = str(lang_config.get("suggest_lang", normalized_lang)) or normalized_lang
    suggest_limit = _env_int(f"{env_prefix}_SEARCH_SUGGEST_LIMIT", _env_int(f"{env_prefix}_SEARCH_SUGGEST_COUNT", 15))
    no_suggest = _env_bool(f"{env_prefix}_SEARCH_NO_SUGGEST", False)
    suggest_debug = _env_bool(f"{env_prefix}_SEARCH_SUGGEST_DEBUG", False)
    vocab_dir_env = os.getenv(f"{env_prefix}_SEARCH_SUGGEST_VOCAB_DIR") or os.getenv("VOCAB_JSON_DIR")
    vocab_dir: Optional[Path] = None
    if vocab_dir_env:
        try:
            vocab_dir = Path(vocab_dir_env).expanduser()
        except Exception:
            vocab_dir = None

    llm_model = os.getenv(f"{env_prefix}_SEARCH_LLM_MODEL", "gpt-5-mini")
    llm_debug = _env_bool(f"{env_prefix}_SEARCH_LLM_DEBUG", False)
    llm_func = getattr(module, "llm_suggest_base_form", None)
    suggest_func = getattr(module, "_fetch_suggestions", None)
    suggest_limit = max(suggest_limit, 1)

    variants = module._generate_query_variants(query, normalized_lang)
    checked_variants: List[str] = []
    checked_lower: Set[str] = set()

    def _mark_checked(value: str) -> None:
        cleaned = (value or "").strip()
        if not cleaned:
            return
        lowered = cleaned.lower()
        if lowered not in checked_lower:
            checked_lower.add(lowered)
            checked_variants.append(cleaned)

    for variant in variants:
        _mark_checked(variant)

    def _matches_to_entries(match_list: Sequence[Any]) -> List[Dict[str, Any]]:
        ordered_entries: List[Dict[str, Any]] = []
        seen_forms: Set[str] = set()
        for match in match_list:
            base_form = module._strip_roman_suffix(match.base_form)
            sanitized = base_form or match.base_form
            lowered = sanitized.lower()
            if lowered in seen_forms:
                continue
            seen_forms.add(lowered)
            ordered_entries.append(
                {
                    "term_id": match.term_id,
                    "base_form": sanitized,
                    "raw_base_form": match.base_form,
                    "matched_translations": match.matched_tokens,
                    "all_translations": match.tokens,
                }
            )
        return ordered_entries

    entries: List[Dict[str, Any]] = []
    used_variant: Optional[str] = None
    match_source: str = "none"

    fasttext_entries: List[Dict[str, Any]] = []
    fasttext_form: Optional[str] = None
    fasttext_suggestions_words: List[str] = []

    llm_entries: List[Dict[str, Any]] = []
    llm_form: Optional[str] = None
    llm_suggestions_words: List[str] = []

    with module._get_connection() as conn:
        for variant in variants:
            matches = module.query_terms_by_language(conn, variant, columns, normalized_lang)
            if not matches:
                continue
            ranked_matches = module.rank_match_results(variant, matches)
            entries = _matches_to_entries(ranked_matches)
            used_variant = variant
            match_source = "primary"
            break

        suggestion_words: List[str] = []
        if not entries and not no_suggest and callable(suggest_func):
            try:
                suggestion_words = suggest_func(  # type: ignore[misc]
                    query,
                    suggest_lang,
                    suggest_limit,
                    vocab_dir,
                    debug=suggest_debug,
                    skip_lower=checked_lower,
                ) or []
            except Exception:
                suggestion_words = []
        elif not entries:
            suggestion_words = []

        fasttext_suggestions_words = suggestion_words[:15]
        for suggestion in suggestion_words:
            lowered = suggestion.lower()
            if lowered in checked_lower:
                continue
            _mark_checked(suggestion)
            suggestion_matches = module.query_terms_by_language(conn, suggestion, columns, normalized_lang)
            if not suggestion_matches:
                continue
            ranked_matches = module.rank_match_results(suggestion, suggestion_matches)
            fasttext_entries = _matches_to_entries(ranked_matches)
            fasttext_form = suggestion
            if not entries:
                entries = fasttext_entries
                used_variant = suggestion
                match_source = "fasttext"
            break

        llm_variants_checked = set(checked_lower)

        if not entries and callable(llm_func) and not no_suggest:
            try:
                candidate_form = llm_func(query, lang=normalized_lang, model=llm_model, debug=llm_debug)  # type: ignore[arg-type]
            except Exception:
                candidate_form = None
            if candidate_form:
                llm_form = candidate_form
                lowered = candidate_form.lower()
                if lowered not in llm_variants_checked:
                    _mark_checked(candidate_form)
                    llm_variants_checked.add(lowered)
                    llm_matches = module.query_terms_by_language(conn, candidate_form, columns, normalized_lang)
                    if llm_matches:
                        ranked_matches = module.rank_match_results(candidate_form, llm_matches)
                        llm_entries = _matches_to_entries(ranked_matches)
                        if not entries:
                            entries = llm_entries
                            used_variant = candidate_form
                            match_source = "llm"

                if callable(suggest_func):
                    llm_candidates: List[str] = []
                    try:
                        llm_candidates = suggest_func(  # type: ignore[misc]
                            candidate_form,
                            suggest_lang,
                            suggest_limit,
                            vocab_dir,
                            debug=suggest_debug,
                            skip_lower=llm_variants_checked,
                        ) or []
                    except Exception:
                        llm_candidates = []
                    llm_variants_checked.update(word.lower() for word in llm_candidates)
                    for word in llm_candidates:
                        _mark_checked(word)
                    llm_suggestions_words = llm_candidates[:15]
                    if not llm_entries:
                        for suggestion in llm_candidates:
                            llm_suggestion_matches = module.query_terms_by_language(conn, suggestion, columns, normalized_lang)
                            if not llm_suggestion_matches:
                                continue
                            ranked_matches = module.rank_match_results(suggestion, llm_suggestion_matches)
                            llm_entries = _matches_to_entries(ranked_matches)
                            if not entries:
                                entries = llm_entries
                                used_variant = suggestion
                                match_source = "llm"
                                llm_form = suggestion
                            break

    seen_forms: Set[str] = set()
    lemko_forms: List[str] = []
    for entry in entries:
        form = entry["base_form"]
        key = form.lower()
        if key not in seen_forms:
            lemko_forms.append(form)
            seen_forms.add(key)

    return {
        "query": query,
        "language": normalized_lang,
        "checked_variants": checked_variants,
        "variant_used": used_variant,
        "lemko_forms": lemko_forms,
        "entries": entries,
        "suggestions": fasttext_suggestions_words,
        "llm_suggestions": llm_suggestions_words,
        "fasttext_entries": fasttext_entries,
        "fasttext_form": fasttext_form,
        "llm_entries": llm_entries,
        "llm_form": llm_form,
        "match_source": match_source,
        "has_results": bool(entries),
    }


class LemSearchRequest(BaseModel):
    text: str


class LemTranslateRequest(BaseModel):
    text: str


class TTSSynthesizeRequest(BaseModel):
    text: constr(strip_whitespace=True, min_length=1, max_length=TTS_TEXT_MAX_CHARS)
    speaker: conint(ge=0, le=1) = 0
    preset: Literal["default", "less", "more"] = "default"


class LemfmArticleTTSRequest(BaseModel):
    text: constr(strip_whitespace=True, min_length=1, max_length=LEMFM_TTS_ARTICLE_MAX_CHARS)
    article_id: Optional[str] = None
    article_url: Optional[str] = None
    title: Optional[str] = None
    speaker: conint(ge=0, le=1) = 0
    preset: Literal["default", "less", "more"] = "default"
    single_file: bool = True
    crossfade_ms: conint(ge=0, le=1000) = LEMFM_TTS_CROSSFADE_MS


# --- Auth (token == JWT_SECRET) ---
async def require_auth(authorization: str | None = Header(default=None)):
    if not JWT_SECRET:
        if PRODUCTION_MODE:
            raise HTTPException(status_code=500, detail="Authentication is not configured")
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != JWT_SECRET:
        raise HTTPException(status_code=401, detail="Invalid token")

async def append_log(entry: dict):
    entry["ts"] = datetime.datetime.utcnow().isoformat() + "Z"
    async with log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _ensure_csv_initialized(path: Path, headers: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(headers)


def _sanitize_filename(name: Optional[str]) -> str:
    raw = (Path(name).name if name else "upload").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw)
    cleaned = cleaned.strip("._")
    return cleaned[:80] or "upload"


async def archive_upload(job_id: str, original_name: str, payload: bytes) -> str:
    safe_name = _sanitize_filename(original_name)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"{stamp}__{job_id}__{safe_name}"
    archive_path = Path(TRANSCRIPTED_SOURCE_DIR) / archive_name
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    def _write():
        with open(archive_path, "wb") as f:
            f.write(payload)

    await asyncio.to_thread(_write)
    return str(archive_path)


async def append_transcription_csv(job_id: str, text: Optional[str]) -> None:
    async with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        filename = (job.get("filename") or "").replace("\n", " ").strip()
        size_bytes = int(job.get("size_bytes") or 0)

    flattened_text = " ".join((text or "").splitlines()).strip()
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    row = [filename, timestamp, size_bytes, flattened_text]

    async with csv_log_lock:
        csv_path = Path(TRANSCRIPTIONS_CSV_PATH)
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        def _write_row():
            needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with open(csv_path, "a", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                if needs_header:
                    writer.writerow(TRANSCRIPTIONS_CSV_HEADERS)
                writer.writerow(row)

        await asyncio.to_thread(_write_row)


async def append_lem_search_log(endpoint: str, query: Optional[str], result: str) -> None:
    sanitized_query = " ".join((query or "").splitlines()).strip()
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    row = [timestamp, endpoint, sanitized_query, result]

    async with lem_search_log_lock:
        csv_path = Path(LEM_SEARCH_LOG_PATH)

        def _write_row():
            needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with open(csv_path, "a", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                if needs_header:
                    writer.writerow(LEM_SEARCH_LOG_HEADERS)
                writer.writerow(row)

        await asyncio.to_thread(_write_row)


async def append_lem_translate_log(endpoint: str, query: Optional[str], result_text: Optional[str]) -> None:
    sanitized_query = " ".join((query or "").splitlines()).strip()
    sanitized_result = " ".join((result_text or "").splitlines()).strip()
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    row = [timestamp, endpoint, sanitized_query, sanitized_result]

    async with lem_translate_log_lock:
        csv_path = Path(LEM_TRANSLATE_LOG_PATH)

        def _write_row():
            needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with open(csv_path, "a", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                if needs_header:
                    writer.writerow(LEM_TRANSLATE_LOG_HEADERS)
                writer.writerow(row)

        await asyncio.to_thread(_write_row)


async def append_lem_tts_log(endpoint: str, speaker: int | str, text: Optional[str]) -> None:
    sanitized_text = " ".join((text or "").splitlines()).strip()
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    row = [timestamp, endpoint, str(speaker), sanitized_text]

    async with lem_tts_log_lock:
        csv_path = Path(LEM_TTS_LOG_PATH)

        def _write_row():
            needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with open(csv_path, "a", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                if needs_header:
                    writer.writerow(LEM_TTS_LOG_HEADERS)
                writer.writerow(row)

        await asyncio.to_thread(_write_row)


@app.head("/healthz")
async def healthz_head():
    return Response(status_code=200, headers={"Content-Length": "0"})

@app.head("/readyz")
async def readyz_head():
    return Response(status_code=200, headers={"Content-Length": "0"})

# --- Lifecycle ---
@app.on_event("startup")
def startup():
    global engine
    os.makedirs(TRANS_DIR, exist_ok=True)
    os.makedirs(TRANSCRIPTED_SOURCE_DIR, exist_ok=True)
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8"):
        pass

    for raw_path, headers in (
        (Path(TRANSCRIPTIONS_CSV_PATH), TRANSCRIPTIONS_CSV_HEADERS),
        (Path(LEM_SEARCH_LOG_PATH), LEM_SEARCH_LOG_HEADERS),
        (Path(LEM_TRANSLATE_LOG_PATH), LEM_TRANSLATE_LOG_HEADERS),
        (Path(LEM_TTS_LOG_PATH), LEM_TTS_LOG_HEADERS),
    ):
        _ensure_csv_initialized(raw_path, headers)
    engine = ASREngine.from_env().load()

@app.get("/healthz")
async def healthz():
    assert engine is not None
    return {"status":"ok","device":engine.device.type,"model_id":os.path.basename(engine.cfg.model_path)}

@app.get("/readyz")
async def readyz():
    return {"ready": engine is not None}

# --- Mapowanie etapów -> progress ---
STAGE_PROGRESS = {
    "queued": 0.0,
    "przygotowuję audio…": 0.1,
    "beam-search RNNT…": 0.6,
    "wyznaczam znaczniki czasu…": 0.85,
    "zapisuję wyniki…": 0.95,
    "gotowe": 1.0,
    "błąd": 1.0,
}

def _public_error_message(status: int) -> str:
    if status == 502:
        return "Upstream service error."
    if status == 503:
        return "Service unavailable."
    return "Internal service error."


def _error(code: str, message: str, details: dict, status: int):
    if PRODUCTION_MODE and status >= 500 and not code.endswith("_DISABLED"):
        message = _public_error_message(status)
        details = {}
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details}})


def _public_job_error(error: Optional[str]) -> Optional[str]:
    if error and PRODUCTION_MODE:
        return "Job failed."
    return error

@app.post("/v1/transcriptions")
async def create_transcription(request: Request, file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    await require_auth(authorization)
    if file.content_type and file.content_type not in ALLOWED_AUDIO_TYPES:
        return _error("UNSUPPORTED_MEDIA_TYPE", f"{file.content_type} is not allowed", {"allowed": sorted(list(ALLOWED_AUDIO_TYPES))}, 415)

    data = await file.read()
    size_bytes = len(data)
    if size_bytes > MAX_UPLOAD_MB * 1024 * 1024:
        return _error("PAYLOAD_TOO_LARGE", "Upload too large", {}, 413)

    # Zapisz upload do tymczasowego pliku; engine sam zadba o resampling
    fd, tmp_in = tempfile.mkstemp(suffix=".bin"); os.close(fd)
    with open(tmp_in, "wb") as f:
        f.write(data)

    # Wyznacz długość orientacyjnie (można pominąć i pokazać później)
    try:
        info = torchaudio.info(tmp_in)
        duration_s = float(info.num_frames) / float(info.sample_rate) if info.sample_rate else None
    except Exception:
        duration_s = None
    if duration_s is not None and MAX_AUDIO_S > 0 and duration_s > MAX_AUDIO_S:
        try:
            os.remove(tmp_in)
        except OSError:
            pass
        del data
        return _error(
            "AUDIO_TOO_LONG",
            "Audio duration exceeds the configured limit.",
            {"audio_duration_s": round(duration_s, 3), "max_audio_seconds": MAX_AUDIO_S},
            413,
        )

    job_id = uuid.uuid4().hex[:8]
    original_filename = file.filename or "upload"
    archived_path = await archive_upload(job_id, original_filename, data)
    del data

    async with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "filename": file.filename,
            "status": "queued",
            "stage": "oczekuję w kolejce",
            "audio_duration_s": duration_s,
            "artifact_path": None,
            "model_id": os.path.basename(engine.cfg.model_path),
            "error": None,
            "_tmp_input": tmp_in,  # do sprzątnięcia po jobie
            "size_bytes": size_bytes,
            "archived_path": archived_path,
        }

    asyncio.create_task(_process_job(job_id))

    await append_log({"event":"enqueue","job_id":job_id,"filename":original_filename,"size":size_bytes,"archived_path":archived_path})

    return JSONResponse(status_code=202, content={
        "job_id": job_id,
        "filename": file.filename,
        "audio_duration_s": duration_s,
        "sha256": None,
        "status": "queued",
        "stage": "oczekuję w kolejce",
    })

@app.get("/v1/transcriptions/{job_id}")
async def transcription_status(job_id: str, authorization: str | None = Header(default=None)):
    await require_auth(authorization)
    j = jobs.get(job_id)
    if not j:
        return _error("NOT_FOUND","Unknown job_id",{},404)
    progress = STAGE_PROGRESS.get(j["stage"], 0.0)
    return {
        "job_id": job_id,
        "status": j["status"],
        "stage": j["stage"],
        "progress": progress,
        "filename": j["filename"],
        "audio_duration_s": j.get("audio_duration_s"),
        "model_id": j["model_id"],
        "error": _public_job_error(j["error"]),
    }

@app.get("/v1/transcriptions/{job_id}/result")
async def transcription_result(job_id: str, authorization: str | None = Header(default=None)):
    await require_auth(authorization)
    j = jobs.get(job_id)
    if not j:
        return _error("NOT_FOUND","Unknown job_id",{},404)
    if j["status"] != "done":
        return _error("NOT_READY","Job not finished",{"status":j["status"],"stage":j["stage"]},409)
    # Wczytaj tekst z artefaktu
    try:
        with open(j["artifact_path"], "r", encoding="utf-8") as f:
            art = json.load(f)
        text = art.get("text","")
    except Exception as e:
        return _error("ARTIFACT_ERROR","Cannot read artifact",{"exc":str(e)},500)
    return {
        "job_id": job_id,
        "text": text,
        "model_id": j["model_id"],
        "audio_duration_s": art.get("meta",{}).get("duration_s") or j.get("audio_duration_s"),
        "sha256": art.get("meta",{}).get("sha256"),
        "artifact_url": f"/v1/transcriptions/{job_id}/artifact",
    }

@app.get("/v1/transcriptions/{job_id}/artifact")
async def transcription_artifact(job_id: str, authorization: str | None = Header(default=None)):
    await require_auth(authorization)
    j = jobs.get(job_id)
    if not j or not j.get("artifact_path"):
        return _error("NOT_FOUND","Artifact not available",{},404)
    return FileResponse(j["artifact_path"], media_type="application/json", filename=os.path.basename(j["artifact_path"]))

@app.get("/v1/transcriptions/{job_id}/events")
async def transcription_events(job_id: str, authorization: str | None = Header(default=None)):
    await require_auth(authorization)
    if job_id not in jobs:
        return _error("NOT_FOUND","Unknown job_id",{},404)
    async def event_stream():
        last_stage=None
        while True:
            j = jobs.get(job_id)
            if not j:
                yield f"event: error\ndata: {{\"message\": \"job removed\"}}\n\n"; break
            if j["stage"] != last_stage:
                payload = json.dumps({"stage": j["stage"], "progress": STAGE_PROGRESS.get(j["stage"],0.0)})
                yield f"data: {payload}\n\n"; last_stage = j["stage"]
            if j["status"] in ("done","error"):
                payload = json.dumps({"stage": j["stage"], "progress": STAGE_PROGRESS.get(j["stage"],1.0), "status": j["status"]})
                yield f"data: {payload}\n\n"; break
            await asyncio.sleep(0.8)
    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- Lemko dictionary search ---
async def _lemko_translation_endpoint(payload: LemSearchRequest, lang: str, authorization: str | None, endpoint_path: str):
    await require_auth(authorization)
    if not LEM_SEARCH_ENABLED:
        await append_lem_search_log(endpoint_path, payload.text, "failed")
        return _error("LEM_SEARCH_DISABLED", "Funkcja słownika Lemko jest wyłączona.", {}, 503)

    text = (payload.text or "").strip()
    if not text:
        await append_lem_search_log(endpoint_path, payload.text, "failed")
        return _error("INVALID_REQUEST", "Pole 'text' nie może być puste.", {"field": "text"}, 400)

    try:
        result = await asyncio.to_thread(_run_pl_lem_translations, text, lang)
    except ValueError as exc:
        await append_lem_search_log(endpoint_path, text, "failed")
        return _error("INVALID_REQUEST", str(exc), {"lang": lang}, 400)
    except RuntimeError as exc:
        await append_lem_search_log(endpoint_path, text, "failed")
        return _error("LEM_SEARCH_UNAVAILABLE", "Wyszukiwarka słownika jest chwilowo niedostępna.", {"reason": str(exc)}, 503)
    except Exception as exc:
        await append_lem_search_log(endpoint_path, text, "failed")
        return _error("LEM_SEARCH_ERROR", "Nie udało się wyszukać tłumaczeń.", {"reason": str(exc)}, 500)
    found = _lemko_result_has_hits(result)
    await append_lem_search_log(endpoint_path, text, "found" if found else "failed")
    return result


def _lemko_result_has_hits(result: Dict[str, Any]) -> bool:
    has_results = result.get("has_results")
    if isinstance(has_results, bool):
        return has_results

    groups = result.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("entries")
            if isinstance(entries, list) and entries:
                return True
        return False

    entries = result.get("entries")
    if isinstance(entries, list):
        return len(entries) > 0

    lemko_forms = result.get("lemko_forms")
    if isinstance(lemko_forms, list):
        return len(lemko_forms) > 0

    return False


def _split_tts_article_text(text: str, max_chars: int) -> List[str]:
    normalized = re.sub(r"\r\n?", "\n", text or "")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        return []

    chunks: List[str] = []
    current = ""
    parts = [part.strip() for part in re.split(r"(\n\n+|(?<=[.!?…:;])\s+)", normalized) if part.strip()]

    def push_piece(piece: str) -> None:
        nonlocal current
        piece = piece.strip()
        if not piece:
            return
        if len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            words = piece.split()
            word_chunk = ""
            for word in words:
                if len(word) > max_chars:
                    if word_chunk:
                        chunks.append(word_chunk)
                        word_chunk = ""
                    for start in range(0, len(word), max_chars):
                        chunks.append(word[start:start + max_chars])
                    continue
                candidate = f"{word_chunk} {word}".strip()
                if len(candidate) <= max_chars:
                    word_chunk = candidate
                else:
                    chunks.append(word_chunk)
                    word_chunk = word
            if word_chunk:
                chunks.append(word_chunk)
            return

        separator = "\n\n" if current and "\n" in piece else " "
        candidate = f"{current}{separator}{piece}".strip() if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            return
        chunks.append(current)
        current = piece

    for part in parts:
        if part.startswith("\n"):
            continue
        push_piece(part)

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk]


def _merge_tts_waves(waves: Sequence[np.ndarray], sample_rate: int, crossfade_ms: int) -> np.ndarray:
    if not waves:
        raise ValueError("No audio chunks generated")

    merged = np.asarray(waves[0], dtype=np.float32)
    fade_samples = int(sample_rate * (max(0, crossfade_ms) / 1000.0))

    for wav in waves[1:]:
        next_wav = np.asarray(wav, dtype=np.float32)
        usable_fade = min(fade_samples, len(merged), len(next_wav))
        if usable_fade <= 0:
            merged = np.concatenate([merged, next_wav])
            continue
        fade_out = np.linspace(1.0, 0.0, usable_fade, dtype=np.float32)
        fade_in = np.linspace(0.0, 1.0, usable_fade, dtype=np.float32)
        overlap = (merged[-usable_fade:] * fade_out) + (next_wav[:usable_fade] * fade_in)
        merged = np.concatenate([merged[:-usable_fade], overlap, next_wav[usable_fade:]])

    return np.clip(merged, -1.0, 1.0)


def _ensure_tts_runtime() -> tuple[StyleTTS2Engine, concurrent.futures.ThreadPoolExecutor]:
    global tts_engine, tts_executor
    base_env = os.getenv("STYLE_TTS2_DIR")
    refs_env = os.getenv("STYLE_TTS2_REFS_ROOT")
    base_path = Path(base_env).expanduser().resolve() if base_env else None
    refs_path = Path(refs_env).expanduser().resolve() if refs_env else None
    with tts_runtime_lock:
        if tts_engine is None:
            tts_engine = StyleTTS2Engine(base_dir=base_path, refs_root=refs_path)
        if tts_executor is None:
            tts_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=TTS_MAX_WORKERS,
                thread_name_prefix="styletts2",
            )
    assert tts_engine is not None
    assert tts_executor is not None
    return tts_engine, tts_executor


async def _run_tts_synthesis(payload: TTSSynthesizeRequest, output_path: Path) -> SynthesisResult:
    engine, executor = _ensure_tts_runtime()
    loop = asyncio.get_running_loop()
    func = functools.partial(
        engine.synthesize_to_file,
        text=payload.text,
        speaker=int(payload.speaker),
        preset=payload.preset,
        num_refs=TTS_NUM_REFS,
        trim_in_ms=TTS_TRIM_IN_MS,
        trim_out_ms=TTS_TRIM_OUT_MS,
        output_path=output_path,
    )
    return await loop.run_in_executor(executor, func)


def _synthesize_lemfm_article_to_file(
    engine: StyleTTS2Engine,
    payload: LemfmArticleTTSRequest,
    output_path: Path,
) -> Tuple[SynthesisResult, int]:
    chunks = _split_tts_article_text(payload.text, LEMFM_TTS_CHUNK_MAX_CHARS)
    if not chunks:
        raise ValueError("Article text is empty")
    if not payload.single_file:
        raise ValueError("LEM.fm article TTS requires single_file=true")

    sample_rate = 24000
    elapsed_total = 0.0
    refs_used = []
    waves: List[np.ndarray] = []

    with engine._synth_lock:
        for chunk in chunks:
            wav, chunk_sample_rate, elapsed, _duration, chunk_refs = engine._synthesize_waveform(
                text=chunk,
                speaker=int(payload.speaker),
                preset=payload.preset,
                num_refs=TTS_NUM_REFS,
                trim_in_ms=TTS_TRIM_IN_MS,
                trim_out_ms=TTS_TRIM_OUT_MS,
            )
            sample_rate = chunk_sample_rate
            elapsed_total += elapsed
            waves.append(wav)
            refs_used.extend(chunk_refs)

        merged = _merge_tts_waves(waves, sample_rate, int(payload.crossfade_ms))
        output_path = output_path.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        engine._write_m4a(merged, sample_rate, output_path)

    duration = len(merged) / sample_rate if sample_rate > 0 else 0.0
    rtf = elapsed_total / duration if duration > 0 else 0.0
    unique_refs = list(dict.fromkeys(refs_used))

    return (
        SynthesisResult(
            output_path=output_path,
            elapsed_s=elapsed_total,
            duration_s=duration,
            sample_rate=sample_rate,
            rtf=rtf,
            preset=payload.preset,
            speaker=int(payload.speaker),
            text=payload.text,
            refs_used=unique_refs,
        ),
        len(chunks),
    )


async def _run_lemfm_article_tts_synthesis(
    payload: LemfmArticleTTSRequest,
    output_path: Path,
) -> Tuple[SynthesisResult, int]:
    engine, executor = _ensure_tts_runtime()
    loop = asyncio.get_running_loop()
    func = functools.partial(_synthesize_lemfm_article_to_file, engine, payload, output_path)
    return await loop.run_in_executor(executor, func)


def _request_public_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


async def _update_lemfm_tts_job(job_id: str, **values: Any) -> None:
    async with lemfm_tts_jobs_lock:
        job = lemfm_tts_jobs.get(job_id, {})
        job.update(values)
        job["updated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
        lemfm_tts_jobs[job_id] = job


async def _process_lemfm_tts_job(
    job_id: str,
    payload: LemfmArticleTTSRequest,
    stored_filename: str,
    public_base_url: str,
) -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="lemfm_tts_job_"))
    output_path = tmp_dir / stored_filename

    try:
        await _update_lemfm_tts_job(job_id, status="processing")
        async with tts_sem:
            result, chunk_count = await _run_lemfm_article_tts_synthesis(payload, output_path)
        LEMFM_TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        stored_path = LEMFM_TTS_AUDIO_DIR / stored_filename
        shutil.move(str(result.output_path), stored_path)
        audio_url = f"{public_base_url}/v1/lemfm/tts/audio/{stored_filename}"
        await append_log(
            {
                "event": "lemfm_tts",
                "article_id": payload.article_id,
                "article_url": payload.article_url,
                "title": payload.title,
                "speaker": int(payload.speaker),
                "preset": payload.preset,
                "text_chars": len(payload.text),
                "chunks": chunk_count,
                "crossfade_ms": int(payload.crossfade_ms),
                "rtf": result.rtf,
                "duration_s": result.duration_s,
            }
        )
        await _update_lemfm_tts_job(
            job_id,
            status="complete",
            complete=True,
            audio_url=audio_url,
            chunks=chunk_count,
            crossfade_ms=int(payload.crossfade_ms),
            duration_s=result.duration_s,
            rtf=result.rtf,
        )
    except Exception as exc:
        await _update_lemfm_tts_job(
            job_id,
            status="failed",
            complete=False,
            message=str(exc),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/v1/lemko/search/pl")
async def lemko_search_from_polish(payload: LemSearchRequest, authorization: str | None = Header(default=None)):
    return await _lemko_translation_endpoint(payload, "pl", authorization, "/v1/lemko/search/pl")


@app.post("/v1/lemko/search/en")
async def lemko_search_from_english(payload: LemSearchRequest, authorization: str | None = Header(default=None)):
    return await _lemko_translation_endpoint(payload, "en", authorization, "/v1/lemko/search/en")


@app.post("/v1/lemko/search")
async def lemko_search(payload: LemSearchRequest, authorization: str | None = Header(default=None)):
    endpoint = "/v1/lemko/search"
    await require_auth(authorization)
    if not LEM_SEARCH_ENABLED:
        await append_lem_search_log(endpoint, payload.text, "failed")
        return _error("LEM_SEARCH_DISABLED", "Funkcja słownika Lemko jest wyłączona.", {}, 503)

    text = (payload.text or "").strip()
    if not text:
        await append_lem_search_log(endpoint, payload.text, "failed")
        return _error("INVALID_REQUEST", "Pole 'text' nie może być puste.", {"field": "text"}, 400)

    try:
        result = await asyncio.to_thread(_run_lem_search, text)
    except RuntimeError as exc:
        await append_lem_search_log(endpoint, text, "failed")
        return _error("LEM_SEARCH_UNAVAILABLE", "Wyszukiwarka słownika jest chwilowo niedostępna.", {"reason": str(exc)}, 503)
    except Exception as exc:
        await append_lem_search_log(endpoint, text, "failed")
        return _error("LEM_SEARCH_ERROR", "Nic nie znaleziono.", {"reason": str(exc)}, 500)

    await append_lem_search_log(endpoint, text, "found" if _lemko_result_has_hits(result) else "failed")
    return result


@app.post("/v1/lemko/translate/pl")
async def lem_translate_pl(payload: LemTranslateRequest, authorization: str | None = Header(default=None)):
    endpoint = "/v1/lemko/translate/pl"
    await require_auth(authorization)
    if not LEM_TRANSLATE_ENABLED:
        await append_lem_translate_log(endpoint, payload.text, "")
        return _error("LEM_TRANSLATE_DISABLED", "Funkcja tłumaczenia jest wyłączona.", {}, 503)

    text = (payload.text or "").strip()
    if not text:
        await append_lem_translate_log(endpoint, payload.text, "")
        return _error("INVALID_REQUEST", "Pole 'text' nie może być puste.", {"field": "text"}, 400)

    model = LEM_TRANSLATE_DEFAULT_MODEL

    try:
        result = await asyncio.to_thread(run_lemko_translate, text, model=model)
    except ValueError as exc:
        await append_lem_translate_log(endpoint, text, "")
        return _error("INVALID_REQUEST", str(exc), {"field": "text"}, 400)
    except RuntimeError as exc:
        await append_lem_translate_log(endpoint, text, "")
        return _error("LEM_TRANSLATE_FAILED", str(exc), {"model": model}, 502)
    except Exception as exc:
        await append_lem_translate_log(endpoint, text, "")
        return _error("LEM_TRANSLATE_ERROR", "Nie udało się wykonać tłumaczenia.", {"reason": str(exc)}, 500)

    translated_text = result.get("translated_text")
    await append_lem_translate_log(endpoint, text, translated_text)
    response_payload = {
        "translated_text": translated_text,
        "resolved_unknown_words": result.get("resolved_unknown_words", []),
        "semantic_description_pl": result.get("semantic_description_pl", []),
        "missing_words": result.get("missing_words", []),
        "model": result.get("model") or model,
        "attempts": result.get("attempts"),
    }
    return response_payload


@app.post("/v1/lemfm/tts")
async def synthesize_lemfm_article_tts(
    payload: LemfmArticleTTSRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    authorization: str | None = Header(default=None),
):
    endpoint = "/v1/lemfm/tts"
    await require_auth(authorization)
    await append_lem_tts_log(endpoint, payload.speaker, payload.text)

    if not payload.single_file:
        return _error("INVALID_REQUEST", "LEM.fm article TTS requires single_file=true", {}, 400)

    chunks = _split_tts_article_text(payload.text, LEMFM_TTS_CHUNK_MAX_CHARS)
    if not chunks:
        return _error("INVALID_REQUEST", "Article text is empty", {}, 400)

    job_id = uuid.uuid4().hex
    article_name = _sanitize_filename(payload.article_id or payload.title or "article")
    stored_filename = f"lemfm_{article_name}_{uuid.uuid4().hex[:8]}.m4a"
    public_base_url = _request_public_base_url(request)
    status_url = f"{public_base_url}/v1/lemfm/tts/jobs/{job_id}"

    await _update_lemfm_tts_job(
        job_id,
        status="queued",
        complete=False,
        audio_url="",
        message="",
        chunks=len(chunks),
        crossfade_ms=int(payload.crossfade_ms),
        created_at=datetime.datetime.utcnow().isoformat() + "Z",
    )

    background_tasks.add_task(_process_lemfm_tts_job, job_id, payload, stored_filename, public_base_url)

    return {
        "complete": False,
        "status": "queued",
        "job_id": job_id,
        "status_url": status_url,
        "chunks": len(chunks),
        "crossfade_ms": int(payload.crossfade_ms),
    }


@app.get("/v1/lemfm/tts/jobs/{job_id}")
async def get_lemfm_tts_job(job_id: str):
    async with lemfm_tts_jobs_lock:
        job = dict(lemfm_tts_jobs.get(job_id, {}))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["job_id"] = job_id
    return job


@app.get("/v1/lemfm/tts/audio/{filename}")
async def get_lemfm_tts_audio(filename: str):
    safe_name = _sanitize_filename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=404, detail="Audio not found")
    path = LEMFM_TTS_AUDIO_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(
        path,
        media_type="audio/mp4",
        filename=safe_name,
    )


@app.post("/v1/tts")
async def synthesize_tts(
    payload: TTSSynthesizeRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    endpoint = "/v1/tts"
    await require_auth(authorization)
    await append_lem_tts_log(endpoint, payload.speaker, payload.text)
    tmp_dir = Path(tempfile.mkdtemp(prefix="tts_job_"))
    output_path = tmp_dir / f"tts_{uuid.uuid4().hex[:8]}.m4a"
    try:
        async with tts_sem:
            result = await _run_tts_synthesis(payload, output_path)
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _error("INVALID_REQUEST", str(exc), {}, 400)
    except FileNotFoundError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _error("TTS_RESOURCE_NOT_FOUND", str(exc), {}, 500)
    except RuntimeError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _error("TTS_FAILED", str(exc), {}, 500)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _error("TTS_ERROR", "Nie udało się wygenerować audio.", {"reason": str(exc)}, 500)

    background_tasks.add_task(shutil.rmtree, tmp_dir, True)
    filename = f"tts_{payload.speaker}_{uuid.uuid4().hex[:8]}.m4a"
    headers = {
        "X-TTS-RTF": f"{result.rtf:.5f}",
        "X-TTS-Duration": f"{result.duration_s:.3f}",
        "X-TTS-Elapsed": f"{result.elapsed_s:.3f}",
    }
    await append_log(
        {
            "event": "tts",
            "speaker": int(payload.speaker),
            "preset": payload.preset,
            "text_chars": len(payload.text),
            "rtf": result.rtf,
            "duration_s": result.duration_s,
        }
    )
    return FileResponse(
        result.output_path,
        media_type="audio/mp4",
        filename=filename,
        headers=headers,
        background=background_tasks,
    )


# --- Praca w tle ---
async def _update_job(job_id: str, **kw):
    async with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw)

async def _process_job(job_id: str):
    assert engine is not None
    j = jobs.get(job_id)
    if not j:
        return
    tmp_in = j.get("_tmp_input")
    archived_path = j.get("archived_path")
    try:
        await _update_job(job_id, status="running", stage="przygotowuję audio…")
        async with sem:
            await _update_job(job_id, stage="beam-search RNNT…")
            # silnik zrobi: resampling -> RNNT -> timestamps (RNNT/CTC)
            out = await asyncio.to_thread(engine.transcribe, tmp_in)
        await _update_job(job_id, stage="zapisuję wyniki…")
        # zapisz artefakt (text + words + meta)
        stem = os.path.splitext(os.path.basename(j["filename"]))[0]
        art_path = os.path.join(TRANS_DIR, f"{stem}__{out['meta']['sha256'][:8]}.json")
        with open(art_path, "w", encoding="utf-8") as f:
            json.dump({
                "text": out["text"],
                "words": out["words"],
                "meta": out["meta"],
            }, f, ensure_ascii=False, indent=2)
        await _update_job(job_id, status="done", stage="gotowe", artifact_path=art_path, audio_duration_s=out["meta"]["duration_s"])
        await append_transcription_csv(job_id, out.get("text"))
        await append_log({"event":"done","job_id":job_id,"artifact_path":art_path,"archived_path":archived_path})
    except Exception as e:
        await _update_job(job_id, status="error", stage="błąd", error=str(e))
        await append_log({"event":"error","job_id":job_id,"error":str(e),"archived_path":archived_path})
    finally:
        try: os.remove(tmp_in)
        except OSError: pass
