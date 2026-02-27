#!/usr/bin/env python3
"""
lem_phonemizer2.py

Nowa wersja fonemizacji lemkowskiej cyrylicy:
1) Zamienia tekst na segmenty wg reguł.
2) Każdy segment ma przypisany backend: pl / uk / bg.
3) Fonemizuje segmenty osobno i skleja wynik IPA.

Klucz: matcher jest "najdłuższy wzorzec wygrywa", więc krótsze reguły
nie rozbijają dłuższych połączeń (np. "льы" przed "ль" + "ы").
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass

from phonemizer.backend import EspeakBackend


# Dopuszczalne backendy językowe (kody espeak).
LANG_PL = "pl"
LANG_UK = "uk"
LANG_BG = "bg"
LANG_RAW = "raw"  # brak fonemizacji, tekst przechodzi bez zmian


@dataclass(frozen=True)
class RawRule:
    src: str
    dst: str
    lang: str | None = None  # None = auto (cyrylica -> uk, łacina -> pl)


@dataclass(frozen=True)
class Rule:
    src: str
    dst: str
    lang: str
    order: int


@dataclass
class Chunk:
    text: str
    lang: str


def is_cyrillic_char(ch: str) -> bool:
    code = ord(ch)
    return 0x0400 <= code <= 0x052F or 0x2DE0 <= code <= 0x2DFF or 0xA640 <= code <= 0xA69F


def is_latin_char(ch: str) -> bool:
    code = ord(ch)
    return (0x0041 <= code <= 0x005A) or (0x0061 <= code <= 0x007A) or ch in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"


def has_cyrillic(text: str) -> bool:
    return any(is_cyrillic_char(ch) for ch in text)


def has_latin(text: str) -> bool:
    return any(is_latin_char(ch) for ch in text)


def infer_lang_from_dst(dst: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if has_cyrillic(dst):
        return LANG_UK
    if has_latin(dst):
        return LANG_PL
    return LANG_RAW


def infer_lang_from_unmatched_char(ch: str) -> str:
    if is_cyrillic_char(ch):
        return LANG_UK
    if is_latin_char(ch):
        return LANG_PL
    return LANG_RAW


def case_variants(src: str, dst: str) -> list[tuple[str, str]]:
    variants = [(src, dst)]
    if src:
        src_cap = src[0].upper() + src[1:]
        dst_cap = dst[0].upper() + dst[1:] if dst else dst
        variants.append((src_cap, dst_cap))
    variants.append((src.upper(), dst.upper()))
    return variants


RAW_RULES: list[RawRule] = [
    # ===== Specjalne reguły "ы" przez bułgarski backend =====
    RawRule("кы", "къъ", LANG_BG),
    RawRule("вы", "въъ", LANG_BG),
    RawRule("ґы", "гъъ", LANG_BG),
    RawRule("ды", "дъъ", LANG_BG),
    RawRule("жы", "жъъ", LANG_BG),
    RawRule("зы", "зъъ", LANG_BG),
    RawRule("йы", "йъъ", LANG_BG),
    RawRule("лы", "лъъ", LANG_BG),
    RawRule("льы", "лъъ", LANG_BG),
    RawRule("мы", "мъъ", LANG_BG),
    RawRule("ны", "нъъ", LANG_BG),
    RawRule("пы", "пъъ", LANG_BG),
    RawRule("ры", "ръъ", LANG_BG),
    RawRule("сы", "съъ", LANG_BG),
    RawRule("ты", "тъъ", LANG_BG),
    RawRule("фы", "фъъ", LANG_BG),
    RawRule("хы", "хъъ", LANG_BG),
    RawRule("цы", "цъъ", LANG_BG),
    RawRule("чы", "чъъ", LANG_BG),
    RawRule("щы", "щъъ", LANG_BG),
    RawRule("шы", "шъъ", LANG_BG),
    RawRule("ы", "ъъ", LANG_BG),
    # ===== Reguły mieszane (auto: cyrylica->uk, łacina->pl) =====
    RawRule("сіль", "сіль"),
    RawRule("льо", "lo"),
    RawRule("ьо", "io"),
    RawRule("іль", "l"),
    RawRule("аль", "al"),
    RawRule("ель", "el"),
    RawRule("уль", "ul"),
    RawRule("иль", "yl"),
    RawRule("іль", "il"),
    RawRule("ли", "ly"),
    RawRule("лe", "ле"),
    RawRule("лі", "лі"),
    RawRule("ля", "la"),
    RawRule("лє", "ле"),
    RawRule("лю", "lu"),
    RawRule("ль", "l"),
    RawRule("л", "l"),
    RawRule("г", "г"),
    RawRule("х", "х"),
    RawRule("щ", "szcz"),
    RawRule("зъ", "z"),
    RawRule("бю", "biu"),
    RawRule("вю", "viu"),
    RawRule("гю", "гю"),
    RawRule("ґю", "giu"),
    RawRule("зю", "ziu"),
    RawRule("кю", "kiu"),
    RawRule("мю", "miu"),
    RawRule("ню", "niu"),
    RawRule("пю", "piu"),
    RawRule("рю", "riu"),
    RawRule("сю", "siu"),
    RawRule("тю", "tiu"),
    RawRule("фю", "fiu"),
    RawRule("хю", "chiu"),
    RawRule("цю", "ciu"),
    RawRule("шю", "sziu"),
    RawRule("бя", "bja"),
    RawRule("вя", "vja"),
    RawRule("гя", "гя"),
    RawRule("ґя", "gja"),
    RawRule("зя", "zja"),
    RawRule("кя", "kja"),
    RawRule("мя", "mja"),
    RawRule("ня", "nja"),
    RawRule("пя", "pja"),
    RawRule("ря", "rja"),
    RawRule("ся", "sia"),
    RawRule("тя", "tja"),
    RawRule("фя", "fja"),
    RawRule("хя", "chia"),
    RawRule("ця", "cia"),
    RawRule("бє", "bje"),
    RawRule("вє", "vje"),
    RawRule("гє", "гє"),
    RawRule("ґє", "gje"),
    RawRule("дє", "dje"),
    RawRule("дю", "dju"),
    RawRule("дя", "dja"),
    RawRule("зє", "zie"),
    RawRule("кє", "kje"),
    RawRule("мє", "mje"),
    RawRule("нє", "nie"),
    RawRule("пє", "pje"),
    RawRule("рє", "rje"),
    RawRule("сє", "sie"),
    RawRule("тє", "tje"),
    RawRule("фє", "fje"),
    RawRule("хє", "chje"),
    RawRule("цє", "cie"),
    RawRule("чє", "czje"),
    RawRule("шє", "szczje"),
    RawRule("сь", "ś"),
    RawRule("зь", "ź"),
    RawRule("ць", "ć"),
    RawRule("нь", "ń"),
    RawRule("ж", "ż"),
    RawRule("а", "a"),
    RawRule("б", "b"),
    RawRule("в", "v"),
    RawRule("ґ", "g"),
    RawRule("д", "d"),
    RawRule("е", "e"),
    RawRule("є", "je"),
    RawRule("з", "z"),
    RawRule("и", "y"),
    RawRule("і", "i"),
    RawRule("й", "j"),
    RawRule("к", "k"),
    RawRule("м", "m"),
    RawRule("н", "n"),
    RawRule("о", "o"),
    RawRule("п", "p"),
    RawRule("р", "r"),
    RawRule("с", "s"),
    RawRule("т", "t"),
    RawRule("у", "u"),
    RawRule("ф", "f"),
    RawRule("ц", "c"),
    RawRule("ч", "cz"),
    RawRule("ш", "sz"),
    RawRule("щ", "szcz"),
    RawRule("ю", "ju"),
    RawRule("я", "ja"),
]


def build_rules(raw_rules: list[RawRule]) -> list[Rule]:
    expanded: list[Rule] = []
    # Jeden src ma jedną regułę (pierwsza wygrywa), żeby nie było niejawnych kolizji.
    seen_src: set[str] = set()

    for order, raw in enumerate(raw_rules):
        lang = infer_lang_from_dst(raw.dst, raw.lang)
        for src_var, dst_var in case_variants(raw.src, raw.dst):
            if src_var in seen_src:
                continue
            expanded.append(Rule(src=src_var, dst=dst_var, lang=lang, order=order))
            seen_src.add(src_var)

    # Krytyczne: najpierw dłuższe wzorce, potem krótsze.
    expanded.sort(key=lambda r: (-len(r.src), r.order))
    return expanded


def build_rule_index(rules: list[Rule]) -> dict[str, list[Rule]]:
    index: dict[str, list[Rule]] = {}
    for rule in rules:
        index.setdefault(rule.src[0], []).append(rule)
    return index


def split_with_rules(text: str, rule_index: dict[str, list[Rule]]) -> list[Chunk]:
    chunks: list[Chunk] = []
    i = 0
    n = len(text)

    while i < n:
        candidates = rule_index.get(text[i], [])
        matched: Rule | None = None

        for rule in candidates:
            if text.startswith(rule.src, i):
                matched = rule
                break

        if matched is None:
            ch = text[i]
            chunks.append(Chunk(ch, infer_lang_from_unmatched_char(ch)))
            i += 1
            continue

        chunks.append(Chunk(matched.dst, matched.lang))
        i += len(matched.src)

    return merge_adjacent_chunks(chunks)


def merge_adjacent_chunks(chunks: list[Chunk]) -> list[Chunk]:
    if not chunks:
        return chunks

    merged: list[Chunk] = [Chunk(chunks[0].text, chunks[0].lang)]
    for ch in chunks[1:]:
        prev = merged[-1]
        if ch.lang == prev.lang:
            prev.text += ch.text
        else:
            merged.append(Chunk(ch.text, ch.lang))
    return merged


def build_backend(language: str) -> EspeakBackend:
    return EspeakBackend(
        language=language,
        preserve_punctuation=True,
        with_stress=True,
        words_mismatch="ignore",
    )


def build_backends(langs: set[str], pl_code: str, uk_code: str, bg_code: str) -> dict[str, EspeakBackend]:
    if shutil.which("espeak") is None and shutil.which("espeak-ng") is None:
        sys.exit("Brak espeak/espeak-ng. Zainstaluj np. `brew install espeak-ng`.")

    lang_code_map = {
        LANG_PL: pl_code,
        LANG_UK: uk_code,
        LANG_BG: bg_code,
    }

    backends: dict[str, EspeakBackend] = {}
    for lang in sorted(langs):
        if lang not in lang_code_map:
            continue
        code = lang_code_map[lang]
        try:
            backends[lang] = build_backend(code)
        except RuntimeError as exc:
            sys.exit(f"Nie mozna uruchomic backendu '{lang}' (kod '{code}'): {exc}")
    return backends


def phonemize_chunks(chunks: list[Chunk], backends: dict[str, EspeakBackend]) -> str:
    out: list[str] = []

    for ch in chunks:
        # Izolowane pojedyncze "г"/"х" w uk backendzie bywają czytane jako nazwy liter.
        # Tu wymuszamy docelowy fonem, żeby uniknąć artefaktów typu "hˈɛ".
        if ch.lang == LANG_UK and ch.text.lower() == "г":
            out.append("h")
            continue
        if ch.lang == LANG_UK and ch.text.lower() == "х":
            out.append("x")
            continue

        backend = backends.get(ch.lang)
        if backend is None:
            out.append(ch.text)
            continue
        ipa = backend.phonemize([ch.text], strip=True, njobs=1)[0]

        # Marker "ъъ" (używany technicznie dla lemkowskiego "ы") mapujemy na "ɤɤ".
        # Przykład: "kˈəə" -> "kɤɤ".
        if ch.lang == LANG_BG:
            ipa = ipa.replace("ˈəə", "ɤɤ").replace("ˌəə", "ɤɤ").replace("əə", "ɤɤ")

        out.append(ipa)

    return "".join(out)


def read_input_text(text_arg: str | None) -> str:
    if text_arg:
        return text_arg
    stdin_text = sys.stdin.read().strip()
    if stdin_text:
        return stdin_text
    sys.exit("Podaj tekst jako argument albo przez stdin.")


def debug_print(enabled: bool, message: str) -> None:
    if enabled:
        print(message, file=sys.stderr)


def summarize_duplicate_sources(raw_rules: list[RawRule]) -> list[str]:
    first: dict[str, tuple[str, str]] = {}
    notes: list[str] = []
    for rr in raw_rules:
        lang = rr.lang or "auto"
        current = (rr.dst, lang)
        if rr.src not in first:
            first[rr.src] = current
            continue
        if first[rr.src] != current:
            notes.append(
                f"Kolizja src='{rr.src}': pierwsza={first[rr.src]}, kolejna={current} (kolejna pominięta)."
            )
    return notes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lemko phonemizer v2: segmentowa fonemizacja pl/uk/bg."
    )
    parser.add_argument("text", nargs="?", help="Tekst wejściowy (cyrylica).")
    parser.add_argument("--pl-code", default="pl", help="Kod espeak dla polskiego (domyślnie: pl).")
    parser.add_argument("--uk-code", default="uk", help="Kod espeak dla ukraińskiego (domyślnie: uk).")
    parser.add_argument("--bg-code", default="bg", help="Kod espeak dla bułgarskiego (domyślnie: bg).")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Wypisuje segmenty i ich backendy na stderr.",
    )
    args = parser.parse_args()

    text = read_input_text(args.text)

    dup_notes = summarize_duplicate_sources(RAW_RULES)
    for note in dup_notes:
        debug_print(args.debug, f"[warn] {note}")

    rules = build_rules(RAW_RULES)
    rule_index = build_rule_index(rules)
    chunks = split_with_rules(text, rule_index)

    langs_in_use = {c.lang for c in chunks if c.lang in {LANG_PL, LANG_UK, LANG_BG}}
    backends = build_backends(langs_in_use, args.pl_code, args.uk_code, args.bg_code)

    if args.debug:
        debug_print(args.debug, "[debug] Segmenty po regułach:")
        for c in chunks:
            debug_print(args.debug, f"  - {c.lang}: {c.text}")

    ipa = phonemize_chunks(chunks, backends)

    # Wyjście właściwe: tylko finalna fonemizacja.
    print(ipa)


if __name__ == "__main__":
    main()
