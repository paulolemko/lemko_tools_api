def _build_rules_with_case(base_rules):
    """Z podstawowych reguł (lowercase) tworzy także warianty z wielką literą i ALL CAPS."""
    rules = []
    seen_src = set()

    for src, dst in base_rules:
        # 1) oryginalne (małe litery)
        variants = [(src, dst)]

        # 2) Tytułowe (pierwszy znak wielki, reszta jak była)
        src_cap = src[0].upper() + src[1:] if src else src
        dst_cap = dst[0].upper() + dst[1:] if dst else dst
        variants.append((src_cap, dst_cap))

        # 3) Wszystko WIELKIMI literami
        variants.append((src.upper(), dst.upper()))

        for s, d in variants:
            if s not in seen_src:
                rules.append((s, d))
                seen_src.add(s)

    return rules


_base_rules = [
    ("ля", "la"),
    ("щ", "szcz"),
    ("лю", "lu"),
    ("зъ", "z"),
    ("бю", "biu"),
    ("вю", "viu"),
    ("гю", "hiu"),
    ("ґю", "giu"),
    ("зю", "ziu"),
    ("кю", "kiu"),
    ("мю", "miu"),
    ("ню", "niu"),
    ("пю", "piu"),
    ("рю", "riu"),
    ("сю", "siu"),
    ("тю", "tiu"),
    ("фю", "fiu"),
    ("хю", "chiu"),
    ("цю", "ciu"),
    ("шю", "sziu"),

    ("бя", "bja"),
    ("вя", "vja"),
    ("гя", "hja"),
    ("ґя", "gja"),
    ("зя", "zja"),
    ("кя", "kja"),
    ("мя", "mja"),
    ("ня", "nja"),
    ("пя", "pja"),
    ("ря", "rja"),
    ("ся", "sia"),
    ("тя", "tja"),
    ("фя", "fja"),
    ("хя", "chia"),
    ("ця", "cia"),

    ("бє", "bje"),
    ("вє", "vje"),
    ("гє", "hie"),
    ("ґє", "gje"),
    ("дє", "dje"),
    ("дю", "dju"),
    ("дя", "dja"),
    ("зє", "zie"),
    ("кє", "kje"),   
    ("лє", "le"),
    ("мє", "mje"),
    ("нє", "nie"),
    ("пє", "pje"),
    ("рє", "rje"),
    ("сє", "sie"),
    ("тє", "tje"),
    ("фє", "fje"),
    ("хє", "chje"),
    ("цє", "cie"),
    ("чє", "czje"),
    ("шє", "szczje"),

    ("сь", "ś"),
    ("зь", "ź"),
    ("ць", "ć"),
    ("нь", "ń"),
    ("ли", "ly"),
    ("лe", "lе"),
    ("ль", "l"),
    
    ("ж", "ż"),
    ("а", "a"),
    ("б", "b"),
    ("в", "v"),
    ("г", "h"),
    ("ґ", "g"),
    ("д", "d"),
    ("е", "e"),
    ("є", "je"),
    ("з", "z"),
    ("и", "y"),
    ("ы", "y"),
    ("і", "i"),
    ("й", "j"),
    ("к", "k"),
    ("л", "l"),
    ("м", "m"),
    ("н", "n"),
    ("о", "o"),
    ("п", "p"),
    ("р", "r"),
    ("с", "s"),
    ("т", "t"),
    ("у", "u"),
    ("ф", "f"),
    ("х", "ch"),
    ("ц", "c"),
    ("ч", "cz"),
    ("ш", "sz"),
    ("щ", "szcz"),
    ("ю", "ju"),
    ("я", "ja"),

]

RULES = _build_rules_with_case(_base_rules)


def lem_transliterate(text: str) -> str:
    """Prosta transliteracja Lemkowskiego z cyrylicy na zapis łaciński, z obsługą wielkich liter."""
    for src, dst in RULES:
        text = text.replace(src, dst)
    return text


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Transliteracja Lemkowskiego z cyrylicy na zapis łaciński."
    )
    parser.add_argument(
        "txt_path",
        nargs="?",
        help="Ścieżka do pliku .txt do transliteracji. Wynik zapisywany jest w pliku *_kyr.txt obok oryginału.",
    )
    args = parser.parse_args()

    if args.txt_path:
        input_path = Path(args.txt_path).expanduser()
        if input_path.suffix.lower() != ".txt":
            parser.error("Oczekiwano pliku z rozszerzeniem .txt")
        if not input_path.exists():
            parser.error(f"Nie znaleziono pliku: {input_path}")

        text = input_path.read_text(encoding="utf-8")
        output_path = input_path.with_name(input_path.stem + "_kyr.txt")
        output_path.write_text(lem_transliterate(text), encoding="utf-8")
        print(f"Zapisano transliterację do: {output_path}")
    else:
        s = "Тест бесіды по лемківскы. Я того не бесідувал николи. Здає ми ся же дост байка тото єст. Старынскій то найвекшый лемківскій діяч"
        print(lem_transliterate(s))
        # przykładowy wynik: "Mia, lubyty, Gie, Z', SZCZO, luks"
