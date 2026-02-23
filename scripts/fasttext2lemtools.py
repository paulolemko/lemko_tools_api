import argparse
import json
import os
import unicodedata
from pathlib import Path

import fasttext
from rapidfuzz import process, fuzz
from Levenshtein import distance as _lev


def _fasttext_model_path(filename: str) -> str:
    """Resolve ścieżkę do modelu FastText biorąc pod uwagę wolumeny i środowisko."""
    candidates: list[Path] = []

    env_dir = os.environ.get("FASTTEXT_MODEL_DIR")
    if env_dir:
        env_path = Path(env_dir).expanduser()
        if env_path.is_file():
            candidates.append(env_path)
        else:
            candidates.append(env_path / filename)

    candidates.append(Path("/app/models") / filename)

    script_dir = Path(__file__).resolve().parent
    candidates.append(script_dir / filename)
    candidates.append(script_dir.parent / filename)

    # ostatnia próba – aktualny katalog roboczy
    candidates.append(Path(filename).expanduser())

    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists():
            return str(resolved)
    return filename

def make_eq_map(pairs):
    m = {}
    for src, tgt in pairs:
        m[ord(src)] = tgt
        if src.isalpha() and tgt.isalpha():
            m[ord(src.upper())] = tgt.upper()
    return m

PL_EQ_PAIRS = [
    ("ą","a"),("ć","c"),("ę","e"),("ł","l"),("ń","n"),
    ("ó","o"),("ś","s"),("ż","z"),("ź","z"),
]

LEM_EQ_PAIRS = [
    *PL_EQ_PAIRS,
    ("ґ","г"),("ы","и"),("ї","і"),("є","е"),
]
LEM_DROP = {ord("’"): None, ord("'"): None, ord("ь"): None, ord("ъ"): None}

PL_EQ_MAP   = make_eq_map(PL_EQ_PAIRS)
LEMK_EQ_MAP = {**make_eq_map(LEM_EQ_PAIRS), **LEM_DROP}
IDENT_EQ    = str.maketrans({})

LANG_EQ = {
    "pl": str.maketrans(PL_EQ_MAP),
    "en": IDENT_EQ,
    "lem": str.maketrans(LEMK_EQ_MAP),
}

def _canon(s: str, lang: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    trans = LANG_EQ.get(lang)
    return s.translate(trans) if trans else s
FT = {
    "pl": fasttext.load_model(_fasttext_model_path("cc.pl.300.bin")),
    "en": fasttext.load_model(_fasttext_model_path("cc.en.300.bin")),
    "lem": fasttext.load_model(_fasttext_model_path("ft_words.bin")),
}

STOP = {
    "pl": {"i","w","z","do","na","o","u","a","że","to","od","po","za"},
    "en": {"the","a","an","to","of","in","on","at","for","and","or"},
    "lem": {"і","же","то","на","у","до","з"},
}

LANG_PARAMS = {
    "pl": {
        "alpha": 2.4,
        "beta": 1.0,
        "gamma": 0.45,
        "filters": {
            "max_len_diff": 3,
            "max_norm_dl": 0.5,
            "min_dice": 0.35,
            "min_cosine": 0.35,
            "stopwords": STOP["pl"],
        },
    },
    "en": {
        "alpha": 2.4,
        "beta": 1.0,
        "gamma": 0.45,
        "filters": {
            "max_len_diff": 3,
            "max_norm_dl": 0.5,
            "min_dice": 0.35,
            "min_cosine": 0.35,
            "stopwords": STOP["en"],
        },
    },
    "lem": {
        "alpha": 2.4,
        "beta": 1.0,
        "gamma": 0.45,
        "filters": {
            "max_len_diff": 3,
            "max_norm_dl": 0.5,
            "min_dice": 0.35,
            "min_cosine": 0.35,
            "stopwords": STOP["lem"],
        },
    },
}

_VOCAB_CACHE: dict[tuple[str, str | None], tuple[list[str], dict[str, float]]] = {}


def _normalize_lang(lang: str) -> str:
    return str(lang).strip().lower()


def _get_lang_params(lang: str) -> dict:
    normalized = _normalize_lang(lang)
    if normalized in LANG_PARAMS:
        return LANG_PARAMS[normalized]
    return LANG_PARAMS["pl"]


def _normalize_base_dir(base_dir: str | Path | None) -> Path | None:
    if base_dir is None:
        return None
    return Path(base_dir).expanduser().resolve()


def _resolve_vocab_path(lang: str, base_dir: Path | None) -> Path:
    filename = f"vocab_{lang}.json"
    candidates: list[Path | None] = []
    if base_dir is not None:
        candidates.append(base_dir / filename)
    env_dir = os.environ.get("VOCAB_JSON_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser() / filename)
    script_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            script_dir / filename,
            script_dir / "vocab" / filename,
            script_dir / "vocab_json" / filename,
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = candidate.expanduser()
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.exists():
            return candidate

    raise FileNotFoundError(f"Nie znaleziono pliku słownika dla języka {lang!r} ({filename}).")


def _load_vocab_file(path: Path) -> tuple[list[str], dict[str, float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    vocab_raw = data.get("vocab") or []
    freq_raw = data.get("frequency") or {}

    vocab: list[str] = []
    for entry in vocab_raw:
        word = str(entry).strip()
        if word:
            vocab.append(word)

    freq_map: dict[str, float] = {}
    items: list[tuple[str, float]] = []
    if isinstance(freq_raw, dict):
        items = list(freq_raw.items())
    elif isinstance(freq_raw, list):
        for item in freq_raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                items.append((item[0], item[1]))
    for key, value in items:
        word = str(key).strip()
        if not word:
            continue
        try:
            freq_value = float(value)
        except (TypeError, ValueError):
            continue
        freq_map[word] = freq_value

    for word in vocab:
        freq_map.setdefault(word, 0.0)

    return vocab, freq_map


def get_vocab(lang: str, base_dir: str | Path | None = None) -> tuple[list[str], dict[str, float]]:
    normalized_lang = _normalize_lang(lang)
    normalized_dir = _normalize_base_dir(base_dir)
    cache_key = (normalized_lang, str(normalized_dir) if normalized_dir is not None else None)
    if cache_key in _VOCAB_CACHE:
        return _VOCAB_CACHE[cache_key]

    vocab_path = _resolve_vocab_path(normalized_lang, normalized_dir)
    vocab, freq_map = _load_vocab_file(vocab_path)
    _VOCAB_CACHE[cache_key] = (vocab, freq_map)
    return vocab, freq_map

def _first_char_norm(text, lang):
    if not text:
        return ""
    return _canon(text, lang)[:1]

def ch_bigrams(s):
    s = f"#{s}#"
    return {s[i:i+2] for i in range(len(s)-1)}

def dice(a, b, lang):
    def _d(x, y):
        X, Y = ch_bigrams(x), ch_bigrams(y)
        return 2*len(X & Y)/(len(X)+len(Y) or 1)
    return max(_d(a, b), _d(_canon(a, lang), _canon(b, lang)))

def norm_dl(a, b, lang):
    a1, b1 = a.casefold(), b.casefold()
    a2, b2 = _canon(a, lang), _canon(b, lang)
    L = max(len(a), len(b), 1)
    return min(_lev(a1, b1), _lev(a2, b2)) / L

def cosine(ft, x, w, lang):
    import math
    def _cos(vx, vw):
        dot = sum(a*b for a,b in zip(vx, vw))
        nx = math.sqrt(sum(a*a for a in vx)); nw = math.sqrt(sum(a*a for a in vw))
        return dot/(nx*nw + 1e-12)
    v1x, v1w = ft.get_word_vector(x), ft.get_word_vector(w)
    v2x, v2w = ft.get_word_vector(_canon(x, lang)), ft.get_word_vector(_canon(w, lang))
    return max(_cos(v1x, v1w), _cos(v2x, v2w))

def _passes_filters_generic(x, w, lang, cfg, debug=False):
    len_diff = abs(len(w) - len(x))
    first_ok = _first_char_norm(x, lang) == _first_char_norm(w, lang)
    nd = norm_dl(x, w, lang)
    dice_score = dice(x, w, lang)
    cosine_score = cosine(FT[lang], x, w, lang)

    max_len_diff = cfg.get("max_len_diff", 3)
    max_norm_dl = cfg.get("max_norm_dl", 0.5)
    min_dice = cfg.get("min_dice", 0.35)
    min_cosine = cfg.get("min_cosine", 0.35)
    stopwords = cfg.get("stopwords", STOP.get(lang, set()))

    reason = None
    if len(x) >= 4 and stopwords and w in stopwords:
        reason = "stopword"
    elif len_diff > max_len_diff:
        reason = f"len_diff>{len_diff}"
    elif not first_ok:
        reason = "initial_mismatch"
    elif nd > max_norm_dl:
        reason = f"norm_dl>{nd:.3f}"
    elif dice_score < min_dice:
        reason = f"dice<{dice_score:.3f}"
    elif cosine_score < min_cosine:
        reason = f"cos<{cosine_score:.3f}"
    if debug:
        status = "PASS" if reason is None else "DROP"
        print(
            f"[FILTER {lang}] {w!r}: status={status}, reason={reason or 'ok'}, "
            f"len_diff={len_diff}, norm_dl={nd:.3f}, dice={dice_score:.3f}, cos={cosine_score:.3f}"
        )
    return reason is None


def passes_filters_pl(x, w, debug=False):
    cfg = _get_lang_params("pl").get("filters", {})
    return _passes_filters_generic(x, w, "pl", cfg, debug=debug)


def passes_filters_en(x, w, debug=False):
    cfg = _get_lang_params("en").get("filters", {})
    return _passes_filters_generic(x, w, "en", cfg, debug=debug)


def passes_filters_lem(x, w, debug=False):
    cfg = _get_lang_params("lem").get("filters", {})
    return _passes_filters_generic(x, w, "lem", cfg, debug=debug)


def passes_filters_default(x, w, lang, debug=False):
    cfg = _get_lang_params(lang).get("filters", {})
    return _passes_filters_generic(x, w, lang, cfg, debug=debug)


PASSES_FILTERS = {
    "pl": passes_filters_pl,
    "en": passes_filters_en,
    "lem": passes_filters_lem,
}

def score(x, w, lang, freq_map):
    params = _get_lang_params(lang)
    alpha = params.get("alpha", 2.4)
    beta = params.get("beta", 1.0)
    gamma = params.get("gamma", 0.45)
    return -alpha * norm_dl(x, w, lang) + beta * cosine(FT[lang], x, w, lang) + gamma * freq_map.get(w, 0.0)


def suggest(word, lang="pl", topn=10, debug=False, vocab_dir: str | Path | None = None):
    lang_key = _normalize_lang(lang)
    if lang_key not in FT:
        raise ValueError(f"Unsupported language: {lang}")
    vocab, freq_map = get_vocab(lang_key, vocab_dir)
    word = unicodedata.normalize("NFKC", word.strip()).replace("’","'")
    base = [w for w, _, _ in process.extract(word, vocab, scorer=fuzz.QRatio, limit=100)]
    if debug:
        print(f"[DEBUG {lang_key}] input={word!r}, base_candidates={len(base)}")
        if base:
            preview = base[: min(15, len(base))]
            print(f"[DEBUG {lang_key}] base preview: {preview}{' …' if len(base) > len(preview) else ''}")
    filter_func = PASSES_FILTERS.get(lang_key)
    filtered: list[str] = []
    for candidate in base:
        if filter_func:
            is_ok = filter_func(word, candidate, debug=debug)
        else:
            is_ok = passes_filters_default(word, candidate, lang_key, debug=debug)
        if is_ok:
            filtered.append(candidate)
    if debug:
        print(f"[DEBUG {lang_key}] after filters={len(filtered)}")
        if not filtered:
            print("[DEBUG] no candidates survived filters")
    unique_candidates = list(dict.fromkeys(filtered))
    scored: list[tuple[str, float]] = []
    if unique_candidates:
        scored = [(candidate, score(word, candidate, lang_key, freq_map)) for candidate in unique_candidates]
    else:
        base_unique = list(dict.fromkeys(base))
        if base_unique:
            fallback_scored = [(candidate, score(word, candidate, lang_key, freq_map)) for candidate in base_unique]
            fallback_scored.sort(key=lambda item: item[1], reverse=True)
            best_candidate = fallback_scored[0]
            scored = [best_candidate]
            if debug:
                print(
                    f"[DEBUG {lang_key}] fallback candidate selected: {best_candidate[0]!r} "
                    f"(score={best_candidate[1]:.3f})"
                )
        else:
            scored = []
    scored.sort(key=lambda item: item[1], reverse=True)
    if debug and scored:
        print(f"[DEBUG {lang_key}] top scored candidates:")
        for w, sc in scored[: topn * 2]:
            print(f"  {w:<20} score={sc:.3f}")
    return [w for w, _ in scored[:topn]]

# Przykłady (oczekiwane):
# PL: suggest("domekl","pl")   -> ['domek', 'domeczek', ...]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sugestie słów w oparciu o modele FastText i słowniki JSON.")
    parser.add_argument("word", nargs="?", help="Słowo do zasugerowania.")
    parser.add_argument("--lang", default="pl", help="Kod języka (domyślnie pl).")
    parser.add_argument("--topn", type=int, default=10, help="Maksymalna liczba zwracanych propozycji.")
    parser.add_argument("--debug", action="store_true", help="Włącz logowanie diagnostyczne filtrów i scoringu.")
    parser.add_argument(
        "--vocab-dir",
        help="Katalog z plikami vocab_{lang}.json. "
        "Gdy nie podasz, skrypt poszuka słowników w VOCAB_JSON_DIR lub obok skryptu.",
    )
    args = parser.parse_args()

    if not args.word:
        parser.print_help()
        raise SystemExit(1)

    suggestions = suggest(
        args.word,
        lang=args.lang,
        topn=args.topn,
        debug=args.debug,
        vocab_dir=args.vocab_dir,
    )
    print(json.dumps(suggestions, ensure_ascii=False, indent=2))
