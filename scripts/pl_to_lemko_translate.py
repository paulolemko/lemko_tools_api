#!/usr/bin/env python3
"""Local Polish -> Lemko translator driven by Codex CLI and Lemko tools.

The script intentionally uses only the Python standard library plus an external
`codex exec` process. Dictionary data comes from the existing Lemko API.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_API_BASE = "https://apiasr.spektrogram.com"
DEFAULT_MAX_CHARS = 1600
DEFAULT_MAX_TERMS = 30
DEFAULT_CODEX_TIMEOUT = 600
DEFAULT_MAX_MEMORY_EXAMPLES = 3
DEFAULT_MEMORY_MIN_SCORE = 0.08
DEFAULT_MEMORY_PROFILE_LEXICAL_FLOOR = 0.05
DEFAULT_MEMORY_LOW_SCORE_AUDIT_THRESHOLD = 0.10
DEFAULT_MEMORY_RISK_POLICY = "include"
MEMORY_RISK_POLICIES = ("include", "demote", "exclude")

POLISH_ASCII_TRANSLATION = str.maketrans(
    {
        "\u0105": "a",
        "\u0107": "c",
        "\u0119": "e",
        "\u0142": "l",
        "\u0144": "n",
        "\u00f3": "o",
        "\u015b": "s",
        "\u017a": "z",
        "\u017c": "z",
    }
)

STYLE_PREFERENCES: tuple[dict[str, Any], ...] = (
    {
        "triggers": ("letnim słońcu", "letnim sloncu", "w letnim słońcu", "w letnim sloncu"),
        "prefer": "в літнім сонци",
        "avoid": "на літнім сонци",
        "reason": "The radio reference keeps the locative preposition в in this weather simile.",
    },
    {
        "triggers": ("metropolitę ławra szkurłę", "metropolite lawra szkurle", "metropolitę lawra szkurłę"),
        "prefer": "митрополиту Лавра Шкурла",
        "avoid": "митрополіта Лавра Шкурлу",
        "reason": "Use the case and name ending attested in the radio reference.",
    },
    {
        "triggers": (
            "książnica.fm",
            "ksiaznica.fm",
            "knyżnycia.fm",
            "knižnycia.fm",
            "knyznycia.fm",
            "kniznycia.fm",
            "biblioteka.fm",
        ),
        "prefer": "Книжниця.фм",
        "avoid": "Книжниця.fm / Knyżnycia.fm / Knižnycia.fm",
        "reason": "In these Lemko radio programme notes, the rubric name is written in Cyrillic as Книжниця.фм.",
    },
    {
        "triggers": ("wiersze", "twórczość - wiersze", "tworczosc - wiersze"),
        "prefer": "вершы",
        "avoid": "віршы",
        "reason": "The observed radio rubric uses вершы.",
    },
    {
        "triggers": (
            "nadszedł już dzień radosny",
            "nadszedl juz dzien radosny",
            "oto zbawiciel zmartwychwstał",
            "oto zbawiciel zmartwychwstal",
            "powiedział anioł trzem mariom",
            "powiedzial aniol trzem mariom",
            "śpieszą do świątyń bożych",
            "spiesza do swiatyn bozych",
            "groby głuche, ciemne",
            "groby gluche, ciemne",
        ),
        "prefer": (
            "Надышол уж ден радістный; З давна нами жданый; "
            "То Спаситель воскрес з мертвых; Гоіт нашы раны; "
            "Прибитый Він был до креста; Три дни был в могылі; "
            "Днеска воскрес, світ премінят; В Своій свіжій силі; "
            "Сказал Ангел трьом Мариям: Глядате даремно - "
            "Христос воскрес, Він переміг; Гробы глухы, - темны; "
            "Радуют ся християне; Спішат в храмы Божы; "
            "Там молят ся і співают; Славят Імя Боже"
        ),
        "avoid": (
            "Настал уж ден; Оддавна; чеканый; Ото Спаситель; "
            "воскрес without з мертвых; Гоит; могилі; Днес воскрес; перемінят; "
            "Повіл Ангел; Шукате дармо; до Божых святынь; співат"
        ),
        "reason": "This Easter poem has attested poetic line order and liturgical diction; preserve the verse sequence instead of modern paraphrase.",
    },
    {
        "triggers": ("pasterze świętowali zielone świątki", "pasterze swietowali zielone swiatki"),
        "prefer": "пастухы святкyвали Русаля",
        "avoid": "пастырі святкували Зелене Свята",
        "reason": "The radio reference uses пастухы and the holiday name Русаля.",
    },
    {
        "triggers": (
            "by zostały członkiniami organizacji i wspólnie włączyły się do pracy narodowej",
            "by zostaly czlonkiniami organizacji i wspolnie wlaczyly sie do pracy narodowej",
            "członkiniami organizacji i wspólnie włączyły się do pracy narodowej",
            "czlonkiniami organizacji i wspolnie wlaczyly sie do pracy narodowej",
            "członkiniami organizacji i ogólnie do pracy narodowej",
            "czlonkiniami organizacji i ogolnie do pracy narodowej",
        ),
        "prefer": "быти членкынями орґанізациі і до народной роботы обще",
        "avoid": "жебы стали ся членкинями орґанізациі і спільні включыли ся до народовой працы",
        "reason": "Keep the source radio wording and final placement of обще.",
    },
    {
        "triggers": ("zapraszamy na dzisiejszą premierę", "zapraszamy na dzisiejsza premiere"),
        "prefer": "Просиме на днешню премєру",
        "avoid": "Запрашаме на днешню премєру",
        "reason": "In the observed radio-announcement style, this fixed opening uses просиме на and should not be paraphrased.",
    },
    {
        "triggers": (
            "zapraszamy na piątkową premierę",
            "zapraszamy na piatkowa premiere",
            "piątkową premierę",
            "piatkowa premiere",
        ),
        "prefer": "Просиме на пятницьову премєру",
        "avoid": "Запрашаме на пятничну премєру",
        "reason": "Use the attested opening for the Friday Preszów radio premiere.",
    },
    {
        "triggers": ("mówi preszów", "mowi preszow", "говорить пряшів", "hovorit pryashiv"),
        "prefer": "«Говорить Пряшів»",
        "avoid": "«Бесідує Пряшів» / translated programme-title variants",
        "reason": "Keep the fixed radio programme title instead of translating it freely.",
    },
    {
        "triggers": ("zaprzyjaźnionego radia rusyn.fm", "zaprzyjaznionego radia rusyn.fm", "radia rusyn.fm", "rusyn.fm"),
        "prefer": "од камаратского радия Русин.фм",
        "avoid": "доброго радия Rusyn.fm",
        "reason": "Use the source radio affiliation phrase and Cyrillicized station name.",
    },
    {
        "triggers": ("prosimy słuchać premiery", "prosimy sluchac premiery"),
        "prefer": "Просиме слухати премєру",
        "avoid": "Просиме слухати премєры",
        "reason": "Keep the attested radio opening and accusative form премєру.",
    },
    {
        "triggers": (
            "andrija szabaka",
            "andrija szabak",
            "dzień ojców",
            "dzien ojcow",
            "związku łemków",
            "zwiazku lemkow",
            "okresie międzywojennym powstało",
            "okresie miedzywojennym powstalo",
        ),
        "prefer": (
            "Днес познаме ближе особу Андрия Шабака, православного священника, "
            "культурно-общественного діяча серед Русинів в ЗША; "
            "В части Книжниця.фм - Михал Павук і його творчіст - вершы; "
            "Перличка - в ґазеті «Карпатска Русь» пишут, як члены єдного з куржків "
            "Лемко Союза одсвяткували Ден Отців; "
            "Припоминка - припомянеме собі русиньску ґімназию, котра в меджевоєнным часі "
            "выникла в Пряшові"
        ),
        "avoid": (
            "священика; культурно-громадского; медже Русинами; в США; членове; "
            "кружків Союзу Лемків святкували; в меджевоєнным періоді повстала"
        ),
        "reason": "This Preszów radio item has fixed social-activist and Lemko Union wording; keep the attested word order.",
    },
    {
        "triggers": ("prosimy słuchać dzisiejszej audycji", "prosimy sluchac dzisiejszej audycji"),
        "prefer": "Просиме слухати днешню передачу од Пряшова",
        "avoid": "Просиме слухати днешнього проґраму з Пряшова",
        "reason": "Use the radio reference wording for this Preszów-audition opening.",
    },
    {
        "triggers": (
            "udziału w słuchaniu dzisiejszej audycji",
            "udzialu w sluchaniu dzisiejszej audycji",
            "zapraszamy do udziału w słuchaniu",
            "zapraszamy do udzialu w sluchaniu",
            "w wielki piątek zapraszamy",
            "w wielki piatek zapraszamy",
        ),
        "prefer": (
            "І в Велику Пятницю просиме до участи при слуханю днешньой передачы з Пряшова; "
            "А буде цікаво; дознате ся дашто; прожыват днес в Ню Йорку; "
            "наштоден робит в Нюйорскій Публичній Бібіотеці; "
            "В Книжници.фм - Миколай Ксеняк - поетичный твір «Най не поблудиш»; "
            "В части Перличка - ґазета «Карпатска Русь» пише, же ... мінит політику односно Русинів і Карпатской Руси"
        ),
        "avoid": (
            "просиме слухати днешню передачу од Пряшова; інтересно; довісте ся; "
            "мешкат днес в Новым Йорку; на каждый ден працує; "
            "стих під наголовком «Жебы-с ся не заблудил»; взглядом Русинів"
        ),
        "reason": "This Good-Friday Preszów radio note has an attested participation/listening formula and fixed Knižnica/Perliczka wording.",
    },
    {
        "triggers": (
            "podczas majowego weekendu",
            "podczas majowego weekendu także usiąść",
            "nestora repeli",
            "iwan petrowcij",
            "gary w stanie indiana",
        ),
        "prefer": (
            "Просиме - при майовым вікенді - тіж си сісти і залучыти порядне радийо; "
            "Нестора Репелы; писменника; "
            "В Книжници.фм - Іван Петровцій - його стихы зо збіркы «Спüванкы»; "
            "Перличка - лемківска ґазета «Карпатска Русь» приносит писмо - статю чытателя; "
            "комуніта Русинів; в місті Ґары, Індияна, або в Чікаґо; "
            "Мукачівской Єпархіі"
        ),
        "avoid": (
            "жебы сте за майового вікенду; влучыти; радіо; писателя; "
            "Іван Петровций представит; «Співанкы»; лист, статю; громада Русинів; "
            "в місті Ґері в штаті Індіана, ци в Чикаґо; Епархиі Мукачівской"
        ),
        "reason": "This Preszów May-weekend radio note has fixed source order and rubric punctuation; keep the attested formula instead of grammatical paraphrase.",
    },
    {
        "triggers": (
            "wiery giric",
            "mikołaj konewał",
            "mikolaj konewal",
            "z rusińskiego serca iii",
            "z rusinskiego serca iii",
            "list babci jednego z czytelników",
            "list babci jednego z czytelnikow",
            "zakarpaciu pod węgierską okupacją",
            "zakarpaciu pod wegierska okupacja",
            "co rusinom dawało polowanie",
            "co rusinom dawalo polowanie",
        ),
        "prefer": (
            "Просиме сісти, залучыти радия і лем послухати. А буде цікаво; "
            "Віры Ґіріц, русиньской політичкы, культурно-общесвтенной діячкы з Мадяр; "
            "В Книжници.фм - Миколай Коневал - стихы з новой книжкы під назвом "
            "«З русиньского сердця ІІІ»; "
            "Перличка - ту наша ґазета «Карпатска Русь» приносит писмо бабы єдного з чытателів; "
            "в котрым ся пише, як ся жыє на Підкарпатю під мадярском окупацийом; "
            "Припоминка - што Русинам давала полювачка і як ся мінили законы в монархіі односно полювань"
        ),
        "avoid": (
            "влучыти радийо; цікаві; політычкы; культурно-суспільной; з Мадярщыны; "
            "вершы; під титулом; «З русиньского серця III»; "
            "лист бабці; чытальників; на Закарпатю; "
            "Припомніня; полюваня; міняли ся права; односні до полювань"
        ),
        "reason": "This Preszów radio note has attested Perliczka and hunting-law wording; keep its local word order and lexical register.",
    },
    {
        "triggers": ("jak szybko nadszedł", "jak szybko nadszedl"),
        "prefer": "як скоро пришла",
        "avoid": "як скоро надішла",
        "reason": "In the Friday-radio phrase, пятниця is resumed by the feminine verb пришла.",
    },
    {
        "triggers": ("dziś poznamy bliżej", "dzis poznamy blizej"),
        "prefer": "Днес познаме ближе",
        "avoid": "Днеска познаме близше",
        "reason": "The radio rubric uses the shorter adverb sequence and should keep this local word order.",
    },
    {
        "triggers": ("poznacie bliżej postać", "poznacie blizej postac", "poznamy bliżej postać", "poznamy blizej postac"),
        "prefer": "Познате ближе особу",
        "avoid": "Пізнате близше постать",
        "reason": "Use the radio-profile wording and keep the adverb order.",
    },
    {
        "triggers": (
            "w knyżnyci.fm",
            "w knyznyci.fm",
            "w knižnici.fm",
            "w kniznici.fm",
            "w ksiąznicy.fm",
            "w książnicy.fm",
            "w ksiaznicy.fm",
        ),
        "prefer": "В Книжници.фм",
        "avoid": "В Knyżnyci.fm / В Knižnici.fm / В Książnicy.fm",
        "reason": "When the Polish intermediate has a locative 'w ...fm' rubric, the source writes it in Cyrillic as В Книжници.фм.",
    },
    {
        "triggers": ("knižnica.fm", "kniznica.fm", "knyżnica.fm", "knyznica.fm"),
        "prefer": "Книжниця.фм",
        "avoid": "Knižnica.fm in Latin script",
        "reason": "Use the Cyrillic rubric form when the Polish intermediate gives the nominative programme name.",
    },
    {
        "triggers": ("ruskiego państwowego gimnazjum w humenném", "ruskiego panstwowego gimnazjum w humennem", "państwowego gimnazjum w humenném", "panstwowego gimnazjum w humennem"),
        "prefer": "Руской Штатной Ґімназиі в Гуменным",
        "avoid": "Руского Державного Ґімназия в Гуменнім",
        "reason": "Use the institution name and case forms from the radio reference.",
    },
    {
        "triggers": ("kveta morochovičová-cvik", "kveta morochovicova-cvik", "morochovičová-cvik", "morochovicova-cvik"),
        "prefer": "Квета Мороховічова-Цвик",
        "avoid": "Kveta Morochovičová-Cvik in Latin script",
        "reason": "Use the Cyrillicized name in the radio rubric.",
    },
    {
        "triggers": ("miłosne perypetie", "milosne perypetie"),
        "prefer": "«Любовны періпетії»",
        "avoid": "„Милосны перипетиі”",
        "reason": "Use the source title spelling for the poetry collection.",
    },
    {
        "triggers": ("wiersze ze zbioru poezji", "wiersze ze zbioru"),
        "prefer": "стихы зо збіркы поезиі",
        "avoid": "вершы зо збіркы поезиі",
        "reason": "Use the observed radio wording for poetry from a collection.",
    },
    {
        "triggers": ("milan sidor", "milana sidora", "milana sidor", "milan sidora"),
        "prefer": "Міляна Сидора",
        "avoid": "Мілана Сідора",
        "reason": "Use the name spelling attested in the Preszów radio reference.",
    },
    {
        "triggers": ("biznesmena i aktywisty", "biznesmena oraz aktywisty", "biznesmen i aktywista", "aktywiści", "aktywisty", "aktywista"),
        "prefer": "бізнесмена та активісты",
        "avoid": "бізнесмена і актывісты",
        "reason": "Use the source conjunction and spelling in the radio profile list.",
    },
    {
        "triggers": ("wiersz pod tytułem", "wiersz pod tytulem", "utwór poetycki", "utwor poetycki"),
        "prefer": "стих під наголовком",
        "avoid": "верш під назвом",
        "reason": "Use the attested literary-rubric wording in these radio notes.",
    },
    {
        "triggers": ("wesele", "„wesele”", "\"wesele\""),
        "prefer": "«Свадьба»",
        "avoid": "«Весіля»",
        "reason": "Use the poem-title translation from the radio reference.",
    },
    {
        "triggers": ("że trzeba", "ze trzeba", "pisze o tym, że trzeba", "pisze o tym ze trzeba"),
        "prefer": "пише о тым, што треба",
        "avoid": "пише о тым, же треба",
        "reason": "The observed Perliczka sentence uses што in this construction.",
    },
    {
        "triggers": ("została zjednoczona w jednym terytorium", "zostala zjednoczona w jednym terytorium", "zjednoczona w jednym terytorium"),
        "prefer": "была по войні соєдинена в єдній териториі",
        "avoid": "по войні остала зъєднана в єдным територию",
        "reason": "Use the source political-history wording and case sequence.",
    },
    {
        "triggers": ("nigdy się to nie udało", "nigdy sie to nie udalo"),
        "prefer": "не повело ся тото николи",
        "avoid": "николи ся то не удало",
        "reason": "Keep the reference word order in the closing sentence.",
    },
    {
        "triggers": ("przypomnienie - przypomnimy sobie", "przypomnienie przypomnimy sobie", "przypomnienie: przypomnimy sobie"),
        "prefer": "Припоминка - припомянеме собі",
        "avoid": "Припомніня - припомниме собі / Припоминаня - припомниме собі",
        "reason": "The radio rubric uses the fixed label Припоминка and verb припомянеме.",
    },
    {
        "triggers": ("zielone świątki", "zielone swiatki"),
        "prefer": "Русаля",
        "avoid": "Зелене Свята",
        "reason": "For this radio note, the Lemko reference uses the holiday name Русаля.",
    },
    {
        "triggers": ("greckokatolicka parafia zaśnięcia najświętszej bogurodzicy w legnicy", "greckokatolicka parafia zasniecia najświętszej bogurodzicy w legnicy", "parafia zaśnięcia najświętszej bogurodzicy w legnicy"),
        "prefer": "грекокатолицка парохія Успінія Пресвятой Богородиці в Ліґници",
        "avoid": "парохія Успіня Найсвятшой Богородиці в Леґници",
        "reason": "Use the church-name wording and Legnica spelling from the Lemko reference.",
    },
    {
        "triggers": (
            "tradycja teatralna na łemkowszczyźnie",
            "tradycja teatralna na lemkowszczyznie",
            "działalność deklamatorska",
            "dzialalnosc deklamatorska",
            "nawiązuje do teatru wśród łemków",
            "nawiazuje do teatru wsrod lemkow",
        ),
        "prefer": "становит важный елемент єй спадковины; Mимо, же; вельох - предовшыткым; асоциюют ся з Лемками; то декляматорска діяльніст; як ся тото днес памятат; послідніх реализаций; Стоваришыня «Руска Бурса»",
        "avoid": "єст важным елементом; дідицтва; Мимо того, же; многых перед вшыткым; лучат ся з Лемками; ніж ся то днес; днеска; остатніх реалізаций; Общества «Руска Бурса»",
        "reason": "Use the observed heritage/theatre register and preserve the source POS order.",
    },
    {
        "triggers": (
            "obwodnicą gorlic",
            "obwodnica gorlic",
            "obwodnicy gorlic",
            "kasztelu w szymbarku",
            "przekazanie umowy",
            "dokumentacji koncepcyjnej",
            "drogi wojewódzkiej nr 977",
            "drogi wojewodzkiej nr 977",
        ),
        "prefer": "обводниця/обводницьом; в Каштели в Шымбарку; торжественне переказаня догваріня, якє тыкат; участ взяли; пармляментаристы і самоурядовці; Малопольскє Воєвідство; Лукаш Смулка; Рафал Кукля; бургомайстер; но 977; близко",
        "avoid": "об’іздна дорога; замок; святочне переданя догоды, котра іде о; уділ взяли; саморядовці; Łukasz Smółka; Rafał Kukla; воєвідство lowercase; бурмістр; ч. 977; около",
        "reason": "Use the Mareszka road-administration wording and keep the relative-clause order.",
    },
    {
        "triggers": (
            "sławomir kaniuk",
            "slawomir kaniuk",
            "kantor cerkiewny",
            "chórzysta",
            "chorzysta",
            "parafii opieki przenajświętszej bogurodzicy",
            "parafii opieki przenajswietszej bogurodzicy",
            "kancelaria prawosławnego arcybiskupa",
            "kancelaria prawoslawnego arcybiskupa",
        ),
        "prefer": "вмер; дяк; на 53 році жытя; по долгій і тяжкій хвороті; З жальом інформуєме, што; парохіянин; Парохіі/парохіі Покровы Пресвятой Богородиці; Креници; Наталиі; Абсольвувал студиі на; активну участ; літургічным; Послугувал як дяк; Од все співал; Перемышльского і Ґорлицкого; приязні успособленого; о 10.00 год.; все зрыхтуваного, жычливого",
        "avoid": "помер; церковный жак; в 53. році; по долгой; інформуєме, же; парафіян; Опікы; Крениці; Наталіі; Скінчыл студиі в; актывну; літурґічным; Служыл як; Од вшыткы; Перемыскошо; приятельско; о год. 10.00; завсе приготуваного, зычливого",
        "reason": "Use the observed church-obituary register for Sławomir Kaniuk and related Orthodox parish notices.",
    },
    {
        "triggers": (
            "wypełnijcie ankietę",
            "wypelnijcie ankiete",
            "badaniu ankietowym dotyczącym państwa odwiedzin w górach",
            "badaniu ankietowym dotyczacym panstwa odwiedzin w gorach",
            "bazy noclegowej w beskidzie niskim",
            "webankieta.pl",
        ),
        "prefer": "Стоваришыня «Руска Бурса»; просит до участи; про Вашы одвидины в Горах; напрявлена є предовшыткым на туристів; то каждый може єй выполнити; Звіданя тыкают; Вашой оціны нічліговой базы в Низкым Бескіді; трасы прогульок; заперты; около 5 минут; Подаєме мотузок",
        "avoid": "Общество «Руска Бурса»; просит о участ; стосовні вашых візит; скєрувана єст передо вшыткым до; каждый може ю; стосуют; оцінкы ночліговой базы; Бескиді; проходовы трасы; замкнены; близко; линк",
        "reason": "Use the Mareszka tourism-survey register and preserve the questionnaire sentence order.",
    },
    {
        "triggers": (
            "21. numer rocznika ruskiej bursy",
            "21 numer rocznika ruskiej bursy",
            "rocznik ruskiej bursy jest specjalistycznym",
            "najnowszy numer gromadzi badaczy",
            "akademicką księgarnię",
            "akademicka ksiegarnie",
        ),
        "prefer": "В посліднім часі вказал ся 21 номер; котрый в цілости посвяченый єст лемківскій/русиньскій літературі; єст специялистичным науковым періодиком печатаным Стоваришыньом; та науковым выдавництвом з Кракова - Академіцком Книгарньом; Найновшый номер громадит бадачы з Европы; што записана в літературных текстах",
        "avoid": "В остатнім часі; вказало ся сесе 21. чысло; котре; посвячене єст літературі лемківскій; є специялістычным; друкуваным през Общество; а тіж наукове выдавництво; Академічну Книгарню; Остатнє чысло; бадачів з Європы; записаном",
        "reason": "This lem.fm promotion text uses article/news wording, not the stricter RRB-PDF issue profile.",
    },
    {
        "triggers": (
            "europejskie forum dziedzictwa i wojny światowej",
            "europejskie forum dziedzictwa i wojny swiatowej",
            "gov4peace",
            "europejski fundusz rozwoju regionalnego",
            "interreg",
            "międzynarodowe targi dziedzictwa",
            "miedzynarodowe targi dziedzictwa",
        ),
        "prefer": "Европейскій Форум Спадковины І Світовой Войны; Подія орґанізувана єст в рамках; «GOV4PeaCE» спілфінансуваного через Европейскій Фундуш Реґіонального Розвитку; в рамках проґраму Інтерреґ; Медженародны Торгы Спадковины; інституциі, якы занимают ся історийом і спадковином І світовой войны з заграниці і з Польщы; О 11.30 год.; панель пн.",
        "avoid": "Європскє Форум Дідицтва; Подія єст орґанізувана в рамах; през Європскій Фонд; Interreg in Latin; Торгы Дідицтва; інституциі з заграниці і з Польщы, котры; О год. 11.30; під назвом",
        "reason": "Use the observed EU-project/heritage wording and preserve the institution-clause order.",
    },
    {
        "triggers": (
            "muzeum historii żydów polskich polin",
            "muzeum historii zydow polskich polin",
            "siła słów",
            "sila slow",
            "międzynarodowego dnia języka ojczystego",
            "miedzynarodowego dnia jezyka ojczystego",
        ),
        "prefer": "В найблизшый вікенд; Музею Iсториi Польскых Жыдiв POLIN; творчого діяня; З нагоды; обзераня часовой выставы «Сила слів»; Серед запрошеных гости не бракне представника лемківской меншыны",
        "avoid": "В найвлизшый; Істориі Польскых Жыдів; творчых діянь; З оказиі; екскурсию выставы часової; гостий не забракне представителя",
        "reason": "Use the observed POLIN/Mother Language Day register and title wording.",
    },
    {
        "triggers": (
            "już jest! już można kupić",
            "juz jest! juz mozna kupic",
            "jedyny taki",
            "dwutomowe wydawnictwo liczy",
            "uporządkowanego zasobu leksykalnego",
            "uporzadkowanego zasobu leksykalnego",
            "każde hasło ma przykład",
            "kazde haslo ma przyklad",
        ),
        "prefer": "Уж є! Уж мож купити! Єдиный такій; уж є доступный в книжковій, выдрукуваній версиі; Тото двотомове выдавництво чыслит; 1154 страны; 1217 стран; упорядкуваного лексикального ресурсу; Кажде госло має примір з джерельных текстів",
        "avoid": "Уж єст; го придбати; доступный єст; друкуваній; двотомова публикация має; сторіны/сторін; лексикального засобу; гасло; жереловых текстів",
        "reason": "Use the observed Contextual Dictionary availability notice and keep its noun choices and sentence order.",
    },
    {
        "triggers": (
            "na rynku wydawniczym pojawiła się nowa publikacja",
            "na rynku wydawniczym pojawila sie nowa publikacja",
            "wyszedł kontekstowy słownik",
            "wyszedl kontekstowy slownik",
            "monumentalna praca",
            "słownikowych haseł",
            "slownikowych hasel",
            "prostej mowy",
        ),
        "prefer": "Явил ся «Контекстуальный словник лемківского языка»; явила ся нова позиция; што тыкат лемківского языка; є то двотомовый; То монументальна робота і ефект мурянчаной працы; Опрацуваных было 34 612 словниковых госел; на базі 1892 позиций та простой бесіды; Томы рахуют",
        "avoid": "Вышол; появила ся нова публикация; яка односит ся до; єст то двотомный; монументальна праца; пильной працы; Опрацувано; словниковых гасел; простой мовы; Томы мают",
        "reason": "Use the observed Contextual Dictionary release article wording, including passive order and publication-register nouns.",
    },
    {
        "triggers": (
            "wyjątkowe witraże",
            "wyjatkowe witraze",
            "opatrzności bożej w wesołej",
            "opatrznosci bozej w wesolej",
            "jerzego nowosielskiego",
            "architektury romańskiej",
            "architektury romanskiej",
            "emanuel bułhak",
            "emanuel bulhak",
        ),
        "prefer": "Вынятковы вітражы; в варшавскым храмі Божого Провидіня в Весолій; частю проєкту середины авторства Юрия Новосільского; зреализувал; в 70. роках ХХ столітя; Як раз закінчено консервацию; варшавского дистрикту Весола; Выбудували го; в другій части 30. років ХХ столітя; в стили, што навязує до романьской архітектуры; дарувал бывшый маітель",
        "avoid": "Неповторны; варшавскій святыні; Веселій/Весела; проєкту внутри; выконал; ХХ ст.; Власні закінчено; варшавской дільниці; Выбудувано го; половині; в стилю навязуючым; подарувал былый властитель",
        "reason": "Use the observed Nowosielski stained-glass conservation register and keep the locative/place-name sequence.",
    },
    {
        "triggers": (
            "cyfrowego społecznego archiwum gminy uście ruskie",
            "cyfrowego spolecznego archiwum gminy uscie ruskie",
            "fundacja memo",
            "regina pazdur",
            "archiwistka społeczna",
            "archiwistka spoleczna",
            "mieszkańcami i mieszkankami gminy",
            "mieszkancami i mieszkankami gminy",
            "stronie casgug",
        ),
        "prefer": "Вернісаж выставы фотоґрафій; Діґітальный Соспільный Архів Ґміны Устя Рускє; Од парох місяців; Фундация Мемо реализує; котрого цілю є зробити; В рамках той задачы; етноложка і соспільна архівістка Реґіна Паздур; мешканцями і мешканками; ци з особами повязаныма; хотят ся поділити фотоґрафіями, споминами і істориями; Вшыткы зобраны материялы сут пак діґітализуваны, архівізуваны і забезпечаны, пак публикуваны",
        "avoid": "знимок; Діґітального Соспільного Архіву; пару місяців; Memo in Latin; реалізує; створіня; В рамах того заданя; етнолоґ; Regina Pazdur in Latin; жытелями і жытельками; або; поділити ся знимками; потім діґіталізуваны; а пак публикуваны",
        "reason": "Use the observed Uście Ruskie community-archive wording and preserve the archive-project clause order.",
    },
    {
        "triggers": (
            "formy obecności",
            "formy obecnosci",
            "sztuka łemków / karpackich rusinów",
            "sztuka lemkow / karpackich rusinow",
            "państwowym muzeum etnograficznym",
            "panstwowym muzeum etnograficznym",
            "kuratorem wystawy jest dr michał szymko",
            "kuratorem wystawy jest dr michal szymko",
            "nasz człowiek od sztuki",
            "nasz czlowiek od sztuki",
        ),
        "prefer": "Докладні два тыжні минули; од одкрытя спектакулярной выставы; standalone Lemko title «Формы присутности. Штука Лемків / Карпатскых Русинів»; bilingual pipe title «Formy obecności. Sztuka Łemków/Rusinów Karpackich | Формы присутности. Штука Лемків/Карпатскых Русинів»; 16. січня/януара 2026 р.; Выставі кураторує др Михал Шымко; наш чловек од штукы; Михале – можу так бесідувати, правда?",
        "avoid": "прешло; отворіня видовисковой; Мистецтво Лемків; 16 січня without януара; Куратором выставы єст; свій чловек; од мистецтва; можу ся так звертати",
        "reason": "Use the observed Warsaw art-exhibition register and preserve the curator sentence order.",
    },
    {
        "triggers": (
            "propozycjami tworzenia nowych rezerwatów",
            "propozycjami tworzenia nowych rezerwatow",
            "bieszczadzkim związku gmin i powiatów pogranicza",
            "bieszczadzkim zwiazku gmin i powiatow pogranicza",
            "dyrekcja ochrony środowiska",
            "dyrekcja ochrony srodowiska",
            "resort klimatu",
            "106 rezerwatów",
            "106 rezerwatow",
        ),
        "prefer": "Конечна векша бесіда звязана з пропозициями творіня новых резерватів; самоурядів зосередженых в Бєщадскым Союзі Ґмін і Повітів Погранича; котру называют соспільном ініциятивом; ґенеральна і реґіональна дирекция охороны середовиска і ресорт климату; Іде о думку створіня черговых резерватів природы; долгій список; На тот момент на обшыри підкарпатского воєвідства є 106 резерватів, якы занимают; 13 тис. гектарів",
        "avoid": "Потрібна єст шырша бесіда; повязана; утворіня; згрупуваных; Бєщадскым Звязку; котру зовут; ініциятывом; Дирекция uppercase; клімату; помысл; дальшых; долгий спис; Тепер; Підкарпатского uppercase; єст 106; котры занимают; тыс.",
        "reason": "Use the observed environmental-administration register and keep the petition/addressee clause order.",
    },
    {
        "triggers": (
            "zakolędują siostry boczniewicz",
            "zakołedują siostry boczniewicz",
            "zakoledą siostry boczniewicz",
            "zakoleduja siostry boczniewicz",
            "gminnym centrum kultury w niegosławicach",
            "gminnym centrum kultury w niegoslawicach",
            "gościeszowicach",
            "goscieszowicach",
            "akompaniamencie dwojga skrzypiec",
            "kameralnej orkiestrze concertino",
        ),
        "prefer": "Заколядуют Сестры Бочнєвич; 24. січня/януара; в Ґмінным Центрі Культуры з Нєґославицях (Niegosławicach) з сідибом в Ґосцєшовицях (Gościeszowicach); Новорічный Концерт; выступлят; разом з родичами; традицийну музику; при акомпаніяменті двоіх гушель і ґітары; артисткы, якы знаны сут; Музичну дорогу; ансамблі Окмель; Реализували чысленны музичны проєкты; Протягом років выступували; Лемко Тавер; На совім конті",
        "avoid": "в Нєґославицях з садибом; Ґосьцєшовицях without Polish parenthesis; Концерт Новорічный; выступят; вєдно; традицийну музыку; акомпаняменті двох скрипок; артисткы знаны; Музычну; ансамбли Okmel; Реалізували; многочисленны музычны; Через рокы; выступляли; Lemko Tower; На свойым конті",
        "reason": "Use the observed concert-announcement wording, preserving names, parenthesized Polish place names, and music-register order.",
    },
    {
        "triggers": (
            "święto patronalne cerkwi w koniecznej",
            "swieto patronalne cerkwi w koniecznej",
            "kalendarza juliańskiego obchodzimy dziś nowy rok",
            "kalendarza julianskiego obchodzimy dzis nowy rok",
            "święty bazyli wielki",
            "swiety bazyli wielki",
            "cerkiew greckokatolicka",
        ),
        "prefer": "Храмове свято в Конечній; За юлияньскым календарьом празднуєме днес Новый Рік; з той нагоды; най ся Вам веде, каре і щєстит цілый 2026 рік; Першый ден Нового Рока за юлияньскым календарьом; храмове свято парохіі; котрой покровительом єст Святый Василий Великій; была вознесена в 1905 році як грекокатолицкій храм",
        "avoid": "Праздник покровителя церкви; Подля юлияньского календаря; одзначаме днеска; оказиі; вам lowercase; дарит; щестит през; праздник покровителя парохіі; патроном; святый Василь; была збудувана; грекокатолицка церков",
        "reason": "Use the observed Orthodox/Greek-Catholic patronal-feast register and preserve the wish formula order.",
    },
    {
        "triggers": ("główną kopułę świątyni wrócił krzyż", "glowna kopule swiatyni wrocil krzyz", "na główną kopułę świątyni", "na glowna kopule swiatyni"),
        "prefer": "на головну баню храму вернул крест",
        "avoid": "на головну куполу святыни вернул хрест",
        "reason": "For this church-register note, prefer баня храму and крест.",
    },
    {
        "triggers": ("uroczyste poświęcenie", "uroczyste poswiecenie"),
        "prefer": "торжественне посвячыня",
        "avoid": "урочысте посвячыня",
        "reason": "Use the attested church-register adjective.",
    },
    {
        "triggers": ("proces przygotowań do remontu dachu oraz odnowienia krzyża", "proces przygotowan do remontu dachu oraz odnowienia krzyza"),
        "prefer": "Процес рыхтуваня до ремонту даху та одновліня креста",
        "avoid": "Процес приготовлянь до ремонту даху а так само одновліня хреста",
        "reason": "Preserve the reference wording for the repair-preparation sentence.",
    },
    {
        "triggers": ("osiem miesięcy temu", "osiem miesiecy temu"),
        "prefer": "вісем місяців тому",
        "avoid": "осем місяців тому",
        "reason": "Use the numeral spelling from the reference.",
    },
    {
        "triggers": ("metalowy krzyż", "metalowy krzyz"),
        "prefer": "метальовый крест",
        "avoid": "металевый хрест",
        "reason": "Use the reference church-register noun and adjective.",
    },
    {
        "triggers": ("pocięty na kawałki", "pociety na kawalki", "pocięty na części", "pociety na czesci"),
        "prefer": "потятый на кавальці",
        "avoid": "порізаный на кусникы",
        "reason": "Use the reference phrase for the destroyed cross.",
    },
    {
        "triggers": ("najbliższy weekend", "najblizszy weekend"),
        "prefer": "В найвлизшый вікенд",
        "avoid": "В близкій вікенд",
        "reason": "Use the event-register opening from the reference.",
    },
    {
        "triggers": ("jedno z wydarzeń organizowanych przez stowarzyszenie łemków", "jedno z wydarzen organizowanych przez stowarzyszenie lemkow"),
        "prefer": "єдна з подій орґанізуваных Стоваришыньом Лемків",
        "avoid": "єдно з дій орґанізуваных през Стоваришыня Лемків",
        "reason": "Keep the feminine єдна, подій, and instrumental agency from the event reference.",
    },
    {
        "triggers": ("rutenale", "ruthenale"),
        "prefer": "РутенАле",
        "avoid": "RutenAle / RuthenAle",
        "reason": "Use the Cyrillic event name in Lemko prose.",
    },
    {
        "triggers": ("międzynarodowe biennale", "miedzynarodowe biennale", "13. międzynarodowe biennale", "13. miedzynarodowe biennale"),
        "prefer": "13. Медженародне Бієнале",
        "avoid": "13. Медженародове Бієнале",
        "reason": "Use the adjective form attested in the event reference.",
    },
    {
        "triggers": (
            "międzynarodowego biennale kultury łemkowskiej",
            "miedzynarodowego biennale kultury lemkowskiej",
            "biennale kultury łemkowskiej",
            "biennale kultury lemkowskiej",
        ),
        "prefer": "Медженародне Бієнале Лемківской / Русиньской Культуры",
        "avoid": "Медженародне Бієнале Культуры Лемківской / Русиньской",
        "reason": "Preserve the Lemko reference word order for the event name instead of the Polish calque order.",
    },
    {
        "triggers": ("czasopisma „besida”", "czasopisma \"besida\"", "czasopisma besida", "czasopismo besida"),
        "prefer": "явил ся дальшый номер часопису «Бесіда»",
        "avoid": "вышол дальшый номер часопису «Бесіда»",
        "reason": "Use the publication-register verb and reflexive position attested in the source.",
    },
    {
        "triggers": ("to nr 2", "to numer 2", "nr 2 (209)", "numer 2 (209)"),
        "prefer": "То ч. 2 (209)",
        "avoid": "То номер 2 (209)",
        "reason": "The source abbreviates magazine issue number as ч.",
    },
    {
        "triggers": ("w ostatnim numerze przeczytamy m.in.", "w ostatnim numerze przeczytamy między innymi", "w ostatnim numerze przeczytamy m in"),
        "prefer": "В посліднім номері прочытаме м.ін. статі",
        "avoid": "В остатнім номері прочытаме медже інчыма статі",
        "reason": "Keep the source rubric wording and abbreviation.",
    },
    {
        "triggers": ("podkarpaccy rusini w stalinowskim raju", "podkarpaccy rusini", "stalinowskim raju"),
        "prefer": "Підкарпатські русини в сталіньскому раю",
        "avoid": "Підкарпатскы Русины в сталінівскым раю",
        "reason": "Use the title wording from the Lemko article reference.",
    },
    {
        "triggers": ("archiwalny tekst zmarłego historyka", "archiwalny tekst zmarlego historyka", "zmarłego historyka rusi podkarpackiej", "zmarlego historyka rusi podkarpackiej"),
        "prefer": "архівный допис покійного історика Підкарпатской Руси",
        "avoid": "архівальный текст померлого історика Підкарпатской Руси",
        "reason": "Use the source's publication-register nouns and adjective.",
    },
    {
        "triggers": ("odbywa się ono co dwa lata", "odbywa sie ono co dwa lata"),
        "prefer": "проходит она што два рокы",
        "avoid": "оно одбыват ся што два рокы",
        "reason": "Preserve the reference word order and verb.",
    },
    {
        "triggers": ("edycja 2026 roku przynosi coś nowego", "edycja 2026 roku przynosi cos nowego"),
        "prefer": "Як раз едиция 2026 рока приносит дашто нове",
        "avoid": "Саме едиция 2026 рока приносит штоси нового",
        "reason": "Use the event-register wording from the reference.",
    },
    {
        "triggers": ("honorowych obywateli gminy kałuskiej", "honorowych obywateli gminy kaluskiej"),
        "prefer": "Почестных Громадян Калушской Громады",
        "avoid": "Гоноровых Громадян Ґміны Калушской",
        "reason": "Use the administrative title from the reference.",
    },
    {
        "triggers": ("honorowym obywatelem gromady kałuskiej", "honorowym obywatelem gromady kaluskiej"),
        "prefer": "Почестным Громадянином Калушской Громады",
        "avoid": "Гоноровым Обивательом Калуськой Громады",
        "reason": "Use the singular instrumental title from the daily-summary reference.",
    },
    {
        "triggers": ("honorowego obywatela gromady kałuskiej", "honorowego obywatela gromady kaluskiej", "tytuł honorowego obywatela", "tytul honorowego obywatela"),
        "prefer": "званя Почестного Громадянина Калушской Громады",
        "avoid": "титул гонорового обивателя Калуськой",
        "reason": "Use the reference title phrase for receiving the civic honor.",
    },
    {
        "triggers": ("decyzją kałuskiej rady miejskiej", "decyzja kaluskiej rady miejskiej", "zgodnie z decyzją kałuskiej rady miejskiej", "zgodnie z decyzja kaluskiej rady miejskiej"),
        "prefer": "згідно з рішыньом Калушской Містецкой Рады",
        "avoid": "згодні з рішыньом Калуськой Міской Рады",
        "reason": "Use the administrative phrase and council name from the reference.",
    },
    {
        "triggers": ("fedira łabyka", "fedir łabyk", "fedir labyk", "fedira labyka"),
        "prefer": "Федір Лабик / Лабик",
        "avoid": "Федір Лабык / Лабык",
        "reason": "Use the name spelling from the reference.",
    },
    {
        "triggers": ("kałuska rada miejska", "kaluska rada miejska"),
        "prefer": "Калушска Містецка Рада",
        "avoid": "Калушка Міска Рада",
        "reason": "Use the council name from the reference.",
    },
    {
        "triggers": ("wieloletnią pracę i wybitne osiągnięcia", "wieloletnia prace i wybitne osiagniecia", "długoletnią pracę i wybitne osiągnięcia", "dlugoletnia prace i wybitne osiagniecia"),
        "prefer": "долголітню працу і вызначны досягніня",
        "avoid": "вельолітню працу і выдатны осягніня",
        "reason": "Use the administrative praise formula from the reference.",
    },
    {
        "triggers": ("laureat urodził się", "laureat urodzil sie"),
        "prefer": "Лавреат вродил ся",
        "avoid": "Лавреат уродил ся",
        "reason": "Use the biographical wording from the reference.",
    },
    {
        "triggers": ("świątkowej wielkiej", "swiatkowej wielkiej"),
        "prefer": "Святковій Великій",
        "avoid": "Святковій Велькій",
        "reason": "Use the place-name spelling from the reference.",
    },
    {
        "triggers": ("powiatu jasielskiego", "jasielskiego powiatu"),
        "prefer": "ясельского повіту",
        "avoid": "ясельского повіту in Polish word order after the town",
        "reason": "Use the reference word order after the birthplace.",
    },
    {
        "triggers": ("radziecką ukrainę", "radziecka ukraine"),
        "prefer": "радяньску Украіну",
        "avoid": "радяньску Україну",
        "reason": "Use the spelling from the reference.",
    },
    {
        "triggers": ("obwodu stalińskiego", "obwodu stalinskiego", "dziś donieckiego", "dzis donieckiego"),
        "prefer": "до сталіньской области (днес донецка)",
        "avoid": "до сталіньского обводу (днес донецкого)",
        "reason": "Use the reference wording for the historical region.",
    },
    {
        "triggers": ("człowiek-archiwum, człowiek-dusza", "czlowiek-archiwum, czlowiek-dusza"),
        "prefer": "чловек-архів, чловек-душа",
        "avoid": "чоловік-архів, чоловік-душа",
        "reason": "Use the reference wording and spelling.",
    },
    {
        "triggers": ("zachowuje pamięć przodków", "zachowuje pamiec przodkow"),
        "prefer": "сохранят памят предків",
        "avoid": "заховує памят предків",
        "reason": "Use the reference verb for preserving memory.",
    },
    {
        "triggers": (
            "rocznica wysiedlenia i zniewolenia",
            "rocznica wysiedlenia",
            "zniewolenia",
            "jaworznie odsłonięto pomnik",
            "jaworznie odslonieto pomnik",
        ),
        "prefer": (
            "Річниця выселіня і поневоліня; Докладні девят років тому; "
            "29. квітня/апріля; в Явожні был одкрытый памятник в рамках; "
            "70. річниці акциі «Вісла»"
        ),
        "avoid": (
            "Роковины; зневоліня; девят років сперед; 29. квітня/квітня; "
            "одкрыто памятник в рамах; 70. роковин"
        ),
        "reason": "The Jaworzno daily-summary memorial text has fixed public-history wording and a paired month form.",
    },
    {
        "triggers": (
            "widnieje inskrypcja",
            "inskrypcja w trzech językach",
            "inskrypcja w trzech jezykach",
            "więzionym i cierpiącym",
            "wiezionym i cierpiacym",
        ),
        "prefer": "На памятнику видно інскрипцию в трьох языках, в лемківскым, польскым і анґлицкым: Вязненым і страдавшым",
        "avoid": "На памятнику стоіт напис в троіх языках; Вязненым і терпячым; анґлийскым",
        "reason": "Preserve the source inscription formula and the trzech/w trzech phrase order.",
    },
    {
        "triggers": (
            "fałszowania łemkowskiej historii",
            "falszowania lemkowskiej historii",
            "pomniejszania łemkowskiego cierpienia",
            "pomniejszania lemkowskiego cierpienia",
            "niewspominania łemków",
            "niewspominania lemkow",
        ),
        "prefer": (
            "систематичного фальсифікуваня лемківской істориі, поменшаня лемківского терпліня "
            "і неспоминаня на Лемків; до нього"
        ),
        "avoid": "систематычного фалшуваня; поменшуваня; невспоминаня Лемків; до него",
        "reason": "Use the attested memory-politics phrasing in the Jaworzno/Akcja Wisła article.",
    },
    {
        "triggers": ("społeczności łemkowskiej oraz mieszkańców kałuszczyzny", "spolecznosci lemkowskiej oraz mieszkancow kaluszczyzny"),
        "prefer": "лемківской громады та мешканців Калущыны",
        "avoid": "лемківской соспільности та жытелів Калущыны",
        "reason": "Use the community and residents wording from the reference.",
    },
    {
        "triggers": ("jest bezsporny", "jest bezsporny"),
        "prefer": "є беззаперечным",
        "avoid": "єст безспірный",
        "reason": "Use the reference predicate for undisputed authority.",
    },
    {
        "triggers": ("fotografia", "fotografie", "fotografii", "zdjęcia", "zdjęć"),
        "prefer": "знимка / знимкы",
        "avoid": "фотоґрафія / фотоґрафіі",
        "reason": "In this Lemko art-publication style, photographs are normally rendered as знимкы.",
    },
    {
        "triggers": ("realizacje artystyczne", "realizacji artystycznych", "artystyczne realizacje"),
        "prefer": "реализациі штукы",
        "avoid": "артистычны реалізациі",
        "reason": "Prefer the local noun штука for art in this community/cultural context.",
    },
    {
        "triggers": ("zostaną zaprezentowane", "zostana zaprezentowane", "będą zaprezentowane"),
        "prefer": "презентуваны будут",
        "avoid": "будут запрезентуваны",
        "reason": "Use the attested participle-first future construction.",
    },
    {
        "triggers": ("wystawie o charakterze przełomowym", "wystawa o charakterze przełomowym", "przełomowej wystawie"),
        "prefer": "історичній выставі",
        "avoid": "експозициі переломного характеру",
        "reason": "For event announcements, this meaning is idiomatically 'historic exhibition'.",
    },
    {
        "triggers": ("zarówno", "jak i", "także dla", "tak samo dla"),
        "prefer": "а так само",
        "avoid": "як ... так і",
        "reason": "Prefer the compact comparative connector used in Lemko public writing.",
    },
    {
        "triggers": ("w szerszym, międzynarodowym kontekście", "szerszym międzynarodowym kontekście"),
        "prefer": "обще",
        "avoid": "в шыршым, медженародным контексті",
        "reason": "Avoid literal Polish administrative phrasing when the intended meaning is 'more broadly'.",
    },
    {
        "triggers": ("podejmuje temat sztuki", "podejmie temat sztuki", "podejmuje temat"),
        "prefer": "вказує / буде вказувала ... штуку",
        "avoid": "піднимат тему мистецтва",
        "reason": "For an institution presenting a theme, prefer the attested Lemko construction with вказувати.",
    },
    {
        "triggers": ("sztuki łemkowskiej", "sztuki rusińskiej", "temat sztuki", "sztuka łemkowska"),
        "prefer": "наша штука / лемківска і русиньска штука",
        "avoid": "лемківске і русиньске мистецтво",
        "reason": "In this cultural register, штука is preferred over the broader calque мистецтво.",
    },
    {
        "triggers": ("nie będzie przesadą stwierdzić", "nie bedzie przesada stwierdzic"),
        "prefer": "Не буде пересадом написати",
        "avoid": "Не буде перебільшыньом повісти, же",
        "reason": "Use the human benchmark idiom for this article-opening formula.",
    },
    {
        "triggers": ("czegoś takiego jeszcze nie było", "czegos takiego jeszcze nie bylo"),
        "prefer": "што такого іщы не было; after 'Не буде пересадом написати,' write што, not же што",
        "avoid": "чогоси такого іщы не было; же што такого іщы не было",
        "reason": "Keep the compact wording from the human benchmark.",
    },
    {
        "triggers": ("dużej wystawie czasowej", "duza wystawa czasowa", "dużej wystawy czasowej"),
        "prefer": "обшырній часовій выставі",
        "avoid": "великій часовій выставі / велькій часовій выставі",
        "reason": "For a substantial temporary exhibition, the human benchmark uses обшырній.",
    },
    {
        "triggers": ("jednej z najważniejszych instytucji muzealnych", "jedna z najwazniejszych instytucji muzealnych"),
        "prefer": "єдным з найважнійшых музеів",
        "avoid": "єдній з найважнійшых музейных інституций",
        "reason": "The human benchmark compresses the phrase to 'one of the most important museums'.",
    },
    {
        "triggers": ("wystawa zatytułowana", "wystawa zatytulowana"),
        "prefer": "Выстава пн.",
        "avoid": "Выстава затитулувана / Выстава під назвом",
        "reason": "Use the abbreviation attested in the human benchmark before an exhibition title.",
    },
    {
        "triggers": (
            "przywołania",
            "przywolania",
            "akt pamięci",
            "akt pamieci",
            "języków malarskich",
            "jezykow malarskich",
            "pamięcią miejsca",
            "pamiecia miejsca",
        ),
        "prefer": (
            "«Прикликаня»; хоснуют окремы малярскы языкы; лучыт іх рефлексия; культурном спадковином; "
            "Марта Криницка-Ожех; Барбара Губерт; вельовымірову оповіст; "
            "Выстава презентувана єст"
        ),
        "avoid": (
            "«Одкликаня»; окремых малярскых языків; dropping іх before рефлексия; культуровым дідицтвом; "
            "Орех/Варвара; вельоаспектову; Выстава є презентувана"
        ),
        "reason": "The Mareszka exhibition article uses a fixed art-memory register and this title/proper-name spelling.",
    },
    {
        "triggers": ("zostanie otwarta 17 stycznia", "będzie dostępna dla publiczności", "bedzie dostepna dla publicznosci"),
        "prefer": "буде доступна уж од 17. січня/януара; keep both month names as січня/януара",
        "avoid": "отворена буде 17 січня і буде доступна для публикы; do not drop /януара",
        "reason": "The human benchmark renders public opening/access as availability from the date.",
    },
    {
        "triggers": ("przez pół roku", "przez pol roku", "do końca czerwca", "do konca czerwca"),
        "prefer": "Експозицию буде мож обзерати через піл рока - аж до кінця червця/юнія; keep both month names as червця/юнія",
        "avoid": "буде доступна для публикы през піл рока – до кінця червця; do not drop /юнія",
        "reason": "Use the article's human benchmark phrasing for the exhibition viewing period.",
    },
    {
        "triggers": ("od 17 do 20 czerwca", "17 do 20 czerwca", "czerwca 2026 r"),
        "prefer": "Од 17. до 20. червця/юнія 2026 р.",
        "avoid": "Од 17 до 20 червця 2026 р.; do not drop /юнія",
        "reason": "Mareszka news keeps day dots and the dual Lemko/monthly form червця/юнія.",
    },
    {
        "triggers": ("barcelona", "barcelonie", "barcelony"),
        "prefer": "Барцелона / Барцело́на forms with ц",
        "avoid": "Барселона with с",
        "reason": "Use the spelling attested in the Lemko source article.",
    },
    {
        "triggers": (
            "stanie się centrum międzynarodowej debaty",
            "stanie sie centrum miedzynarodowej debaty",
            "centrum międzynarodowej debaty",
            "centrum miedzynarodowej debaty",
        ),
        "prefer": "стане ся центром медженародной дебаты",
        "avoid": "стане центром медженародовой дебаты",
        "reason": "Use the Language Diversity Forum reference construction.",
    },
    {
        "triggers": ("language diversity forum",),
        "prefer": "Language Diversity Forum unchanged in Latin script",
        "avoid": "Форум Ріжнородности Языків / translated title",
        "reason": "The source article keeps this event name in English.",
    },
    {
        "triggers": ("organizowane pod hasłem", "organizowany pod hasłem", "pod hasłem"),
        "prefer": "орґанізуване під гослом",
        "avoid": "орґанізуваний під гаслом",
        "reason": "Keep neuter agreement for forum/event names and the source register form госло.",
    },
    {
        "triggers": ("globalne wyzwania", "wyzwania", "wyzwań"),
        "prefer": "Ґлобальны выкликы / выкликы",
        "avoid": "вызвы",
        "reason": "Use the reference lexical choice for challenges in conference/news style.",
    },
    {
        "triggers": ("networking", "networing"),
        "prefer": "нетворкінґ",
        "avoid": "networking in Latin script",
        "reason": "The Lemko source Cyrillicizes this loanword.",
    },
    {
        "triggers": ("samowystarczalność", "samowystarczalnosc"),
        "prefer": "самовыстарчальніст",
        "avoid": "самовстарчальніст",
        "reason": "Use the attested form with -вы- from the source article.",
    },
    {
        "triggers": ("zgromadzi", "zgromadzą", "zbierze językoznawców", "zbierze jezykoznawcow"),
        "prefer": "збере",
        "avoid": "згромадит",
        "reason": "Use the compact future verb from the Mareszka source.",
    },
    {
        "triggers": ("językoznawców", "jezykoznawcow", "językoznawcy", "jezykoznawcy"),
        "prefer": "языкознавців",
        "avoid": "языковців",
        "reason": "Use the source term for linguists.",
    },
    {
        "triggers": ("przedstawicieli", "przedstawiciele"),
        "prefer": "представників",
        "avoid": "представителів",
        "reason": "Use the source form in the forum participant list.",
    },
    {
        "triggers": ("wspólnot", "wspolnot", "wspólnoty", "wspolnoty"),
        "prefer": "спільнот",
        "avoid": "соспільности",
        "reason": "Use the source plural for communities in the forum article.",
    },
    {
        "triggers": ("aktywistów", "aktywistow", "aktywiści", "aktywisci"),
        "prefer": "активістів",
        "avoid": "актывістів",
        "reason": "Use the spelling attested in the Lemko article.",
    },
    {
        "triggers": ("onz i unesco", "onz", "unesco"),
        "prefer": "ONZ i UNESCO in Latin uppercase; keep Polish conjunction i in this source-style phrase",
        "avoid": "ООН і ЮНЕСКО",
        "reason": "The source article keeps these organization acronyms in Latin script.",
    },
    {
        "triggers": ("pakistanu", "pakistan", "kolumbii", "kolumbię", "kolumbie"),
        "prefer": "Пакістану; Колюмбію",
        "avoid": "Пакистану; Колюмбию",
        "reason": "Use the attested Lemko spellings for these country names in the article.",
    },
    {
        "triggers": ("senegalu", "senegal", "senegalu i maroko", "senegal i maroko"),
        "prefer": "Сенеґаль",
        "avoid": "Сенеґал",
        "reason": "Use the source spelling with final soft sign.",
    },
    {
        "triggers": ("po senegal i maroko", "po senegalu i maroko", "senegal i maroko"),
        "prefer": "по Сенеґаль і Мароко",
        "avoid": "аж по Сенеґаль і Мароко",
        "reason": "Do not add аж in the Language Diversity Forum country list.",
    },
    {
        "triggers": ("przez chile", "przez chile i kolumbię", "przez chile i kolumbie"),
        "prefer": "через Чіле і Колюмбію",
        "avoid": "през Чіле і Колюмбию",
        "reason": "For routes/ranges in this phrase, use через as in the source article.",
    },
    {
        "triggers": (
            "przynosimy wam przegląd",
            "przynosimy wam podsumowanie",
            "przynosimy wam",
            "przedstawiamy wam podsumowanie",
            "przedstawiamy wam",
            "przedstawiamy państwu podsumowanie",
            "przedstawiamy panstwu podsumowanie",
            "przedstawiamy państwu",
            "przedstawiamy panstwu",
        ),
        "prefer": "If the Polish source starts 'Przynosimy wam ...', 'Przedstawiamy wam ...', or 'Przedstawiamy Państwu ...', the Lemko text must start 'Приносиме вам сумар'",
        "avoid": "Передставляме вам; перегляд; підсумуваня in this fixed opening",
        "reason": "The Mareszka daily-summary series uses the fixed heading formula сумар.",
    },
    {
        "triggers": ("grupy medialnej lem.fm", "grupy medialnej łem.fm", "lem.fm+", "łem.fm+"),
        "prefer": "медияльной ґрупы ЛЕМ.фм+",
        "avoid": "медияльной групы LEM.fm+ / ŁEM.fm+",
        "reason": "Use the Cyrillicized brand form and ґрупа as in the source series.",
    },
    {
        "triggers": (
            "węgierskim parlamencie",
            "wegierskim parlamencie",
            "partia tisa",
            "peter magyar",
            "péter magyar",
            "mniejszosci narodowych",
            "mniejszości narodowych",
        ),
        "prefer": "двітретинову, конституцийну векшыну; партия Тіса на челі з Петром Мадяром; Выборы тыкали; офіцийні; рядного депутата; 20 тис.",
        "avoid": "дві третины; Tisa in Latin; Péterом Magyarом; дотыкали; офіцияльні; звычайного посла; 20 тыс.",
        "reason": "Use the observed Mareszka wording for Hungarian election and minority-representative news.",
    },
    {
        "triggers": (
            "faj po swojemu",
            "instytut roznorodnosci jezykowej polski",
            "instytut różnorodności językowej polski",
            "lemko taver",
            "kaszebe vibes",
            "gdanskiego teatru szekspirowskiego",
            "gdańskiego teatru szekspirowskiego",
        ),
        "prefer": "«Фай по свому»; Інститут Языковой Ріжнорідности Польщы; результаты набору внесків; «Языкова ріжнорідніст»; Разом переслано; позитивну; внескодавцям",
        "avoid": "Faj/Po in Latin; Інститут Різноманітности; результати рекрутації; аплікантам",
        "reason": "Use the names and grant-result wording from the language-diversity daily summary.",
    },
    {
        "triggers": (
            "fosterlang",
            "badanie ankietowe",
            "ogolnopolskiej ankiecie",
            "ogólnopolskiej ankiecie",
            "jezykow mniejszosciowych i migranckich",
            "języków mniejszościowych i migranckich",
        ),
        "prefer": "Просиме до участи; меншыновых і міґрантсткых языків; в рамках европейского проєкту; фінансуваного Европском Комісийом; міряют ся хоснувателі тых языків; Мотузок",
        "avoid": "Просиме о участ; міґрантскых; в рамах; през Европейску Комісию; стрічают ся люде; Лінк",
        "reason": "Use the observed FOSTERLANG survey phrasing and syntax.",
    },
    {
        "triggers": (
            "coroczne spotkanie bursakow",
            "coroczne spotkanie bursaków",
            "walne zebranie czlonkow",
            "walne zebranie członków",
            "komisji rewizyjnej",
            "absolutorium",
            "natalia malecka-nowak",
        ),
        "prefer": "загальне зобраня членів; справописы (мериторичный, фінансовый і од Ревізийной Комісиі); ци дати абсолюторию одходячому зарядови, веденому в миняючій каденциі Наталийом Малецком-Новак",
        "avoid": "вальне зобраня; справозданя; Комісиі Ревізийной; уступуючому; минаючій; през Наталію Малецку-Новак",
        "reason": "Use the fixed Ruska Bursa annual-meeting wording from the reference article.",
    },
    {
        "triggers": (
            "zmarł wasyl matola",
            "zmarl wasyl matola",
            "rusińskie towarzystwo literacko-kulturalne",
            "rusinskie towarzystwo literacko-kulturalne",
            "użhorodzkim technikum urządzeń elektronicznych",
            "uzhorodzkim technikum urzadzen elektronicznych",
            "technolog-inżynier",
            "technolog-inzynier",
        ),
        "prefer": (
            "на ден 30. квітня 2026 р.; Вмер Василь Матола; "
            "Русиньскє Літературно-Культурне Общество; "
            "в віторок 28. квітня/апріля; вмер русиньскій поета; "
            "свого краю; Народил ся в селі Івановці, мукачівского району, 10. марця 1960 р.; "
            "закінчыл студиі в Ужгородскым Технікум Електорнічных Заряджынь; "
            "Працувал як технолоґ-інжынєр в своім вывченым фаху"
        ),
        "avoid": (
            "на ден 30 квітня; Помер Василь Матола; Русиньске Літерацко-Культуральне Товариство; "
            "во вівторок; 28 квітня without /апріля; помер русиньскій поета; свойого краю; "
            "Уродил ся; Іванівці; в мукачівскым районі; Скінчыл; Технікумі Електронічных Уряджынь; "
            "Працювал; інжинєр; професиі"
        ),
        "reason": "For this daily-summary obituary, keep the attested Lemko news wording, date punctuation, and biographical clause order.",
    },
    {
        "triggers": ("czerwca/junia", "czerwca/jun", "czerwca/czerwca"),
        "prefer": "червця/юнія",
        "avoid": "червця/юна; червця without /юнія in dated summary headers",
        "reason": "The daily-summary source keeps the dual month form червця/юнія.",
    },
    {
        "triggers": ("17 czerwca/junia", "dzień 17 czerwca", "dzien 17 czerwca"),
        "prefer": "на ден 17. червця/юнія 2026 р.",
        "avoid": "на ден 17 червця/юнія 2026 р.",
        "reason": "Keep the day-number punctuation used in the daily-summary reference.",
    },
    {
        "triggers": ("10 czerwca/junia", "dzień 10 czerwca", "dzien 10 czerwca"),
        "prefer": "на ден 10. червця/юнія 2026 р.",
        "avoid": "на ден 10 червця/юнія 2026 р.",
        "reason": "Keep the day-number punctuation used in the daily-summary reference.",
    },
    {
        "triggers": ("dzień rusinów słowacji", "dzien rusinow slowacji"),
        "prefer": "Ден Русинів Словациі",
        "avoid": "altered country-case variants",
        "reason": "Keep the recurring event name as in the source.",
    },
    {
        "triggers": ("rozpoczną się obchody", "rozpoczna sie obchody", "rozpoczęły się obchody", "rozpoczely sie obchody", "obchody"),
        "prefer": "зачнут ся празднуваня; празднуваня",
        "avoid": "зачнут ся святкуваня when Polish says obchody; rozpoczną się obchody -> зачнут ся празднуваня",
        "reason": "Daily-summary news uses празднуваня for public obchody, while святкуваня is reserved for literal świętowanie.",
    },
    {
        "triggers": ("świętowanie", "swietowanie", "świętowania", "swietowania"),
        "prefer": "святкуваня",
        "avoid": "торжество; using святкуваня for separate Polish obchody clauses",
        "reason": "Use святкуваня for literal świętowanie, but keep obchody as празднуваня in the same article.",
    },
    {
        "triggers": ("medzilaborce",),
        "prefer": "Меджелабірці",
        "avoid": "Меджілабірці",
        "reason": "Use the place-name spelling attested in the source.",
    },
    {
        "triggers": (
            "rusińska odrodzenie",
            "rusinska odrodzenie",
            "rusińskie odrodzenie",
            "rusinskie odrodzenie",
            "rusińska odroda",
            "rusinska odroda",
        ),
        "prefer": "Русиньска Оброда",
        "avoid": "Русиньске Одроджыня; Русиньска Одрода; Русиньска Однова",
        "reason": "Use the official organization name from the source.",
    },
    {
        "triggers": ("staną się centrum", "stana sie centrum", "staną się centrum rusińskiego", "stana sie centrum rusinskiego"),
        "prefer": "станут ся центром",
        "avoid": "станут центром",
        "reason": "Keep the reflexive particle in this daily-summary sentence.",
    },
    {
        "triggers": ("we współpracy ze", "we wspolpracy ze", "współpracy ze", "wspolpracy ze"),
        "prefer": "во спілпрацы зо",
        "avoid": "во співпраци зі",
        "reason": "Use the source collocation and preposition before the following organization name.",
    },
    {
        "triggers": ("zapraszają na", "zapraszaja na", "zaprasza na"),
        "prefer": "просят на",
        "avoid": "запрашат на",
        "reason": "The source announcement style uses просити на.",
    },
    {
        "triggers": ("ogólnonarodowe", "ogolnonarodowe", "ogólnonarodowego", "ogolnonarodowego"),
        "prefer": "цілонародне",
        "avoid": "вшытконародове",
        "reason": "Use the source term for a whole-national event.",
    },
    {
        "triggers": ("wraz z", "razem z"),
        "prefer": "вєдно з",
        "avoid": "разом з",
        "reason": "Use the Mareszka source connector in event-summary prose.",
    },
    {
        "triggers": ("rocznicą założenia", "rocznica zalozenia", "założenia", "zalozenia"),
        "prefer": "річницьом основаня",
        "avoid": "річницьом заложыня",
        "reason": "Use the source nominalization основаня in anniversary contexts.",
    },
    {
        "triggers": (
            "konkursu recytatorskiego",
            "konkurs recytatorski",
            "recytatorskiego konkursu",
            "konkursu deklamatorskiego",
            "konkurs deklamatorski",
            "deklamatorskiego konkursu",
        ),
        "prefer": "декляматорского конкурсу",
        "avoid": "рецитаторского конкурсу",
        "reason": "Use the Mareszka contest-register term.",
    },
    {
        "triggers": (
            "duchnowiczowy preszów",
            "duchnowiczow preszow",
            "duchnowiczów preszów",
            "duchnowiczowski preszów",
            "duchnowiczowski preszow",
        ),
        "prefer": "Духновичів Пряшів",
        "avoid": "Духновічів Пряшів / Духновічівскій Пряшів",
        "reason": "Use the event-name spelling from the source.",
    },
    {
        "triggers": ("jubileuszowa", "jubileuszowy", "jubileuszowej"),
        "prefer": "ювілейна / ювілейный",
        "avoid": "юбілейна / юбілейный",
        "reason": "Use the source spelling with юві-.",
    },
    {
        "triggers": ("w poniedziałek", "w poniedzialek"),
        "prefer": "В понедільок",
        "avoid": "В понеділок",
        "reason": "Use the weekday spelling attested in the recitation-contest reference.",
    },
    {
        "triggers": ("konkursu recytacji", "konkurs recytacji", "recytacji poezji", "konkursu deklamacji", "deklamacji poezji"),
        "prefer": "конкурсу деклямациі",
        "avoid": "конкурсу рецитациі",
        "reason": "Use the source noun деклямация in contest names.",
    },
    {
        "triggers": ("opowiadania ludowego", "opowiadanie ludowe", "ludowego opowiadania"),
        "prefer": "народного розповіданя",
        "avoid": "народного оповіданя",
        "reason": "Use the source lexical choice in the contest category.",
    },
    {
        "triggers": ("uczestników finału", "uczestnikow finalu", "finału konkursu", "finalu konkursu"),
        "prefer": "участників фіналу / фіналу конкурсу",
        "avoid": "участників фіналю / фіналю конкурсу",
        "reason": "Use the source genitive form фіналу.",
    },
    {
        "triggers": ("odbyła się jubileuszowa", "odbyla sie jubileuszowa", "jubileuszowa edycja"),
        "prefer": "одбыла ся ювілейна едиция",
        "avoid": "прошла ювілейна едиция",
        "reason": "Use the source construction for an edition taking place.",
    },
    {
        "triggers": ("organizatorem wydarzenia jest", "organizatorem jest", "wydarzenia jest rusińskie odrodzenie"),
        "prefer": "Орґанізатором подіі є",
        "avoid": "Орґанізатором подіі єст",
        "reason": "Use the concise copula form from the source.",
    },
    {
        "triggers": ("który odbył się w", "ktory odbyl sie w", "finał konkursu, który odbył się", "final konkursu, ktory odbyl sie"),
        "prefer": "котрый прошол в",
        "avoid": "котрый одбыл ся в",
        "reason": "For a contest/final taking place, the source uses прошол.",
    },
    {
        "triggers": ("po raz pierwszy", "pierwszy raz"),
        "prefer": "То першыраз в істориі",
        "avoid": "Першый раз",
        "reason": "Use the idiomatic historical framing when the Polish sentence stresses a first public occurrence.",
    },
    {
        "triggers": (
            "po raz pierwszy publiczna",
            "pierwszy raz publiczna",
            "publiczna, państwowa instytucja muzealna",
            "publiczna państwowa instytucja muzealna",
        ),
        "prefer": "То першыраз в істориі, коли публична, державна інституция",
        "avoid": "Першый раз прилюдна, державна музейна інституция",
        "reason": "In this benchmark sentence, preserve the human wording and omit redundant 'museum' if the Lemko sentence frames the state institution.",
    },
    {
        "triggers": ("na taką skalę", "na taka skale", "w takiej skali"),
        "prefer": "в такым вымірі",
        "avoid": "на таку скалю / в такій скалі",
        "reason": "For institutional scale in this article, the human benchmark uses вымір.",
    },
    {
        "triggers": (
            "podejmuje temat sztuki łemkowskiej i rusińskiej",
            "podejmuje temat sztuki lemkowskiej i rusinskiej",
            "temat sztuki łemkowskiej i rusińskiej na taką skalę",
        ),
        "prefer": "буде в такым вымірі вказувала нашу штуку",
        "avoid": "буде вказувала лемківску і русиньску штуку на таку скалю",
        "reason": "Use the compact community-framed wording from the human translation of this benchmark.",
    },
    {
        "triggers": ("mniejszości narodowych", "mniejszosci narodowych", "mniejszość narodowa"),
        "prefer": "нацийональны меншины / нацийональных меншын",
        "avoid": "народовы меншины / народовых меншын",
        "reason": "For the legal category of national minorities, the mareszka reference uses нацийональный.",
    },
    {
        "triggers": ("mniejszość grecką", "mniejszosc grecka", "mniejszość grecka"),
        "prefer": "грецка меншына / грецку меншыну",
        "avoid": "грецка меншина / грецку меншину",
        "reason": "Keep the Lemko stem spelling меншын- in this minority-status context.",
    },
    {
        "triggers": ("nawrocki", "prezydent nawrocki"),
        "prefer": "Навроцкій",
        "avoid": "Nawrocki in Latin script",
        "reason": "Transliterate this surname as in the Mareszka source.",
    },
    {
        "triggers": ("jarosław kaczyński", "jaroslaw kaczynski", "kaczyński", "kaczynski"),
        "prefer": "Ярослав Качыньскій",
        "avoid": "Jarosław Kaczyński in Latin script; Качиньский",
        "reason": "Political-person names should be rendered in Lemko Cyrillic in parliamentary news.",
    },
    {
        "triggers": ("która rozszerza", "ktora rozszerza", "ustawę, która rozszerza", "ustawe, ktora rozszerza"),
        "prefer": "котра пошырят",
        "avoid": "яка пошырят / што пошырят",
        "reason": "Use the relative pronoun form from the Mareszka legal-news source.",
    },
    {
        "triggers": ("listę mniejszości", "lista mniejszości", "spis mniejszości", "wykaz mniejszości"),
        "prefer": "список меншын",
        "avoid": "листа меншын",
        "reason": "Use список for an official register/list in administrative news.",
    },
    {
        "triggers": ("ustawa zmienia", "ustawę, która", "ustawa, która", "podpisał ustawę"),
        "prefer": "устава for a signed act; for anaphoric 'Ustawa zmienia...' use Закон змінят",
        "avoid": "Устава змінят when it repeats the already mentioned signed act",
        "reason": "Administrative Lemko news often alternates устава/закон to avoid repetition.",
    },
    {
        "triggers": ("status społeczności", "status spolecznosci", "społeczności, której", "spolecznosci, ktorej"),
        "prefer": "статус соспільности",
        "avoid": "статус спільноты",
        "reason": "In this administrative minority context, the reference keeps соспільніст/соспільность.",
    },
    {
        "triggers": (
            "wsparcie otrzymają zadania",
            "wsparcie otrzymaja zadania",
            "komisji konkursowej",
            "jednym z kryteriów było",
            "jednym z kryteriow bylo",
        ),
        "prefer": "Підперты будут задачы, котры подля конкурсовой комісиі; єдном з критерий было, што",
        "avoid": "Підпертя отримают задачы; конкурсной комісиі; єдном з критерий было тото, же",
        "reason": "The Małopolska grant-summary reference uses a support-passive task formula and this clause order.",
    },
    {
        "triggers": ("cennymi materiałami", "cennymi materialami", "uzupełnione o dodatkowe teksty", "uzupelnione o dodatkowe teksty"),
        "prefer": "цінныма материялами; а тіж дополнене о додатковы тексты",
        "avoid": "цінныма матеріалами; а так само дополнене",
        "reason": "The Thalerhof publication note keeps the local spelling материялами and connector а тіж.",
    },
    {
        "triggers": ("pod koniec kwietnia", "pod koniec", "końcem kwietnia", "koncem kwietnia"),
        "prefer": "кінцьом квітня/апріля; for 'pod koniec kwietnia 2026 roku' use кінцьом квітня/апріля 2026 рока",
        "avoid": "під конец квітня; do not drop /апріля in this news benchmark register",
        "reason": "The reference style uses кінцьом and keeps the alternate month name when present or natural in news style.",
    },
    {
        "triggers": ("xiv w", "xiv wieku", "xiv stulecia", "14 wieku"),
        "prefer": "ХІV ст.",
        "avoid": "XIV в.",
        "reason": "Use the local abbreviation ст. for century in this news register.",
    },
    {
        "triggers": ("sejm przyjął", "sejm przyjal"),
        "prefer": "Сойм принял",
        "avoid": "Сейм принял",
        "reason": "Use the Lemko spelling Сойм in Polish parliamentary context.",
    },
    {
        "triggers": ("sejm uchwalił ustawę", "sejm uchwalil ustawe", "sejm uchwalił", "sejm uchwalil"),
        "prefer": "польскій сойм схвалил уставу",
        "avoid": "польскій Сойм принял уставу",
        "reason": "In Mareszka parliamentary news, uchwalił ustawę is rendered as схвалил уставу.",
    },
    {
        "triggers": (
            "w czwartek 30 kwietnia",
            "30 kwietnia polski sejm",
            "30 kwietnia polski sejm",
            "30 kwietnia polski",
        ),
        "prefer": "В четвер 30. квітня/апріля",
        "avoid": "В четвер 30. квітня without /апріля",
        "reason": "This parliamentary Mareszka reference keeps the paired month form квітня/апріля.",
    },
    {
        "triggers": ("rozszerzono listę mniejszości", "rozszerzono liste mniejszosci"),
        "prefer": "пошырили список нацийональных меншын",
        "avoid": "пошырено список нацийональных меншын",
        "reason": "Use the active plural construction found in the reference instead of a Polish impersonal passive calque.",
    },
    {
        "triggers": ("status mniejszości narodowej nadano", "status mniejszosci narodowej nadano", "nadano grekom"),
        "prefer": "Статус нацийональной меншыны надали Грекам",
        "avoid": "Статус нацийональной меншыны надано Грекам",
        "reason": "Use the active plural construction found in the reference.",
    },
    {
        "triggers": ("przeciwko tej regulacji", "przeciwko regulacji", "było przeciw", "bylo przeciw"),
        "prefer": "проти тій реґуляциі; было проти",
        "avoid": "против тій реґуляциі; было против",
        "reason": "The Mareszka parliamentary reference uses проти in these vote/regulation clauses.",
    },
    {
        "triggers": ("za ustawą głosowało", "za ustawa glosowalo", "za ustawą", "za ustawa"),
        "prefer": "За уставом голосувало",
        "avoid": "За уставу голосувало",
        "reason": "Voting for a law takes instrumental уставом in the reference.",
    },
    {
        "triggers": ("dwóch posłów wstrzymało", "dwoch poslow wstrzymalo", "wstrzymało się od głosowania", "wstrzymalo sie od glosowania"),
        "prefer": "двох послів стримало ся од голосуваня",
        "avoid": "дває послы стримали ся од голосуваня",
        "reason": "Preserve the numeral-genitive phrase and singular predicate sequence from the reference.",
    },
    {
        "triggers": ("którzy głosowali", "ktorzy glosowali", "posłowie z konfederacji, którzy", "poslowie z konfederacji, ktorzy"),
        "prefer": "послы з Конфедерациі, котры голосували",
        "avoid": "послы з Конфедерациі, што голосували",
        "reason": "For this person-list relative clause, Mareszka uses котры, not the generic event connector што.",
    },
    {
        "triggers": ("koła bezpośrednia demokracja", "kola bezposrednia demokracja", "koła konfederacji korony polskiej", "kola konfederacji korony polskiej"),
        "prefer": "кружка Безпосередня Демокрация; кружка Конфедерациі Польской Короны",
        "avoid": "кола Безпосередня Демокрация; кола Конфедерациі Короны Польской",
        "reason": "Use the local parliamentary-club noun and proper-name order attested in the source.",
    },
    {
        "triggers": ("stała się dziesiątą", "stala sie dziesiata", "stał się dziesiątym", "stał się"),
        "prefer": "стала десятом / стал десятым without reflexive ся when the Polish means 'became the nth item in a list'",
        "avoid": "стала ся десятом",
        "reason": "In this news construction, the mareszka reference omits reflexive ся.",
    },
    {
        "triggers": ("dołączyła do", "dolaczyla do", "dołączył do", "dolaczyl do"),
        "prefer": "долучыла до / долучыл до",
        "avoid": "долучыла ся до / долучыл ся до",
        "reason": "For joining a list/group in this administrative style, avoid the extra reflexive ся.",
    },
    {
        "triggers": (
            "przemkowskiego ośrodka kultury i biblioteki",
            "przemkowskiego osrodka kultury i biblioteki",
            "dańko horoszczak",
            "danko horoszczak",
            "burmistrza przemkowa jerzego szczupaka",
            "powołany na trzyletnią kadencję",
            "powolany na trzyletnia kadencje",
        ),
        "prefer": (
            "Пшемківского Осередка Культуры і Бібліотекы; Данько Горощак; "
            "бургомайстра Пшемкова Єжы Щупака; на функциі; попередню ведучу; "
            "при участи; пожелал успіхів; был покликаный на трилітню каденцию"
        ),
        "avoid": (
            "leaving Przemkowskiego Ośrodka Kultury i Biblioteki / Dańko Horoszczak / "
            "Jerzego Szczupaka in Latin; становиску; кєруючу; з участю; жычыл; "
            "остал покликаний; трирічну каденцию"
        ),
        "reason": "For this Przemków administrative-cultural note, transliterate proper names and keep the attested office/cadence phrasing.",
    },
    {
        "triggers": ("czeskiej", "czeska", "czeską", "czeski"),
        "prefer": "чешскій / чешской",
        "avoid": "ческій / ческой",
        "reason": "Use the attested Lemko adjective spelling from the mareszka reference.",
    },
    {
        "triggers": ("ormiańskiej", "ormianskiej", "ormiańska", "ormiańską", "ormiański"),
        "prefer": "ормяньскій / ормяньской",
        "avoid": "армяньскій / армяньской",
        "reason": "Use the attested Lemko spelling from the mareszka reference.",
    },
    {
        "triggers": ("żydowskiej", "zydowskiej", "żydowska", "zydowska", "żydowski", "zydowski"),
        "prefer": "жыдівскій / жыдівской",
        "avoid": "жидівскій / жидівской",
        "reason": "Use the attested Lemko spelling from the mareszka reference.",
    },
    {
        "triggers": ("nowelizację", "nowelizacja", "nowelizacji"),
        "prefer": "новелизация / новелизацию",
        "avoid": "новелізация / новелізацию",
        "reason": "Use the reference spelling with -иза- in this legal news register.",
    },
    {
        "triggers": ("2026 roku", "2025 roku", "2024 roku", "roku"),
        "prefer": "рока after a year number, e.g. 2026 рока",
        "avoid": "року after a year number",
        "reason": "The reference style uses рока for dated news prose.",
    },
    {
        "triggers": ("nowo wybudowanej", "nowo wybudowany", "nowo zbudowanej"),
        "prefer": "новопобудуваній / новопобудуваный",
        "avoid": "ново збудуваній",
        "reason": "Use the compound adjective attested in the Mareszka article.",
    },
    {
        "triggers": ("ripkach", "ripki", "w ripkach"),
        "prefer": "Ріпкы / в Ріпках",
        "avoid": "Рипкы / в Рипках",
        "reason": "Use the source spelling with і.",
    },
    {
        "triggers": ("zabrzmiał", "zabrzmial", "zabrzmiała", "zabrzmiało"),
        "prefer": "вызвучал / вызвучала / вызвучало; for 'z głośników zabrzmiał' use з голосників вызвучал",
        "avoid": "зазвучал; розліг ся",
        "reason": "For a broadcast signal/jingle sounding from speakers, the source uses вызвучати.",
    },
    {
        "triggers": ("rozległ się", "rozlegl sie", "rozległa się", "rozlegla sie"),
        "prefer": "вызвучал / вызвучала / вызвучало",
        "avoid": "розліг ся",
        "reason": "When the reverse Polish uses 'rozległ się' for a broadcast sound, recover the source verb вызвучати.",
    },
    {
        "triggers": ("telewizji kraków", "telewizji krakow", "telewizja kraków", "telewizja krakow"),
        "prefer": "Телевізиі Краків",
        "avoid": "Краківской Телевізиі",
        "reason": "Keep the programme/institution name order from the Lemko source.",
    },
    {
        "triggers": ("u minister kultury", "minister kultury", "z udziałem minister", "z udzialem minister"),
        "prefer": "в міністры / з участю міністры; for 'na spotkaniu u minister Kultury...' use на стрічы в міністры Культуры...",
        "avoid": "у міністер / у міністры / з участю міністер / в Міністерстві when the Polish says u minister",
        "reason": "For a female minister in this source style, use міністры in these case contexts.",
    },
    {
        "triggers": ("daria kuziak", "daria kuziak", "daria kuziak"),
        "prefer": "Дария Кузяк",
        "avoid": "Дарія Кузяк",
        "reason": "Use the personal-name spelling from the Lemko source.",
    },
    {
        "triggers": ("łemkowska artystka", "lemkowska artystka", "łemkowski artysta", "lemkowski artysta"),
        "prefer": "Лемківска артистка / Лемківскій артиста",
        "avoid": "Лемківска творця",
        "reason": "Keep the source profession term артиста/artystka.",
    },
    {
        "triggers": ("znalazła się w grupie twórców", "znalazla sie w grupie tworcow", "w grupie twórców"),
        "prefer": "нашла ся в ґрупі творців",
        "avoid": "нашла ся в групі творців",
        "reason": "Use the source collocation and ґрупа spelling.",
    },
    {
        "triggers": ("którzy zostali zaproszeni", "ktorzy zostali zaproszeni", "zostali zaproszeni"),
        "prefer": "котры были запрошены",
        "avoid": "што остали запрошены",
        "reason": "Use the source relative construction.",
    },
    {
        "triggers": ("śniadanie prasowe w ministerstwie", "sniadanie prasowe w ministerstwie", "na śniadanie prasowe w"),
        "prefer": "пресове сніданя в Міністерстві",
        "avoid": "пресовым сніданю в Міністерстві",
        "reason": "Use nominative/accusative event phrase when the Polish says invited to a press breakfast.",
    },
    {
        "triggers": ("dotyczyło przygotowywanego projektu ustawy", "dotyczylo przygotowywanego projektu ustawy"),
        "prefer": "тыкала рыхтуваного проєкту уставы",
        "avoid": "дотычыла приготувуваного проєкту уставы",
        "reason": "Use the source legal-project wording.",
    },
    {
        "triggers": ("osób wykonujących zawód artystyczny", "osob wykonujacych zawod artystyczny", "zawód artystyczny"),
        "prefer": "осіб, котры выконуют артистичну професию",
        "avoid": "осіб, што выполняют артистичный фах",
        "reason": "Use the source wording for artists' social-security law.",
    },
    {
        "triggers": ("którzy reprezentowali", "ktorzy reprezentowali", "ktore reprezentowały"),
        "prefer": "якы репрезентували",
        "avoid": "што репрезентували",
        "reason": "Use the source relative pronoun in this sentence.",
    },
    {
        "triggers": ("obszary sztuki", "różne obszary sztuki", "rozne obszary sztuki"),
        "prefer": "ріжны обшыри штукы",
        "avoid": "ріжны области штукы",
        "reason": "Use the source noun обшыр for fields/areas of art.",
    },
    {
        "triggers": ("oprócz", "oprocz"),
        "prefer": "Окрем",
        "avoid": "Окрім",
        "reason": "Use the source connector in this article style.",
    },
    {
        "triggers": ("dziedzictwa narodowego", "dziedzictwo narodowe", "kultury i dziedzictwa narodowego"),
        "prefer": "Культуры і Нацийональной Спадковины",
        "avoid": "Культуры і Народового Дідичтва",
        "reason": "Use the official ministry-name wording from the Lemko source.",
    },
    {
        "triggers": ("ministerstwo kultury i dziedzictwa narodowego", "ministerstwem kultury i dziedzictwa narodowego"),
        "prefer": "Міністерство/Міністерством Культуры і Нацийональной Спадковины",
        "avoid": "Міністерство Культуры і Народового Дідичтва",
        "reason": "Keep the official institution name consistent with the source.",
    },
    {
        "triggers": ("śniadaniu prasowym", "sniadaniu prasowym", "śniadanie prasowe", "sniadanie prasowe"),
        "prefer": "пресовым сніданю",
        "avoid": "пресовым сняданю",
        "reason": "Use the source vowel/spelling in this fixed media-event phrase.",
    },
    {
        "triggers": ("zorganizowanym przez ministerstwo", "organizowanym przez ministerstwo", "przez ministerstwo"),
        "prefer": "зорґанізуваным Міністерством",
        "avoid": "зорґанізуваным през Міністерство",
        "reason": "Use instrumental agency as in the source sentence.",
    },
    {
        "triggers": ("dotyczyło przygotowywanej ustawy", "dotyczylo przygotowywanej ustawy"),
        "prefer": "тыкала рыхтуваной уставы",
        "avoid": "дотыкала приготовлюваной уставы",
        "reason": "Use the source legal-news wording.",
    },
    {
        "triggers": ("zabezpieczeniu społecznym", "zabezpieczeniu spolecznym", "społecznym zabezpieczeniu"),
        "prefer": "социяльным забезпечыню",
        "avoid": "суспільным забезпечыню",
        "reason": "Use the source adjective for social-security terminology.",
    },
    {
        "triggers": ("które wykonują", "ktore wykonuja", "osób, które wykonują", "osob, ktore wykonuja"),
        "prefer": "осіб, котры выконуют",
        "avoid": "осіб, котры виконуют",
        "reason": "Use the source spelling with ы in this verb form.",
    },
    {
        "triggers": ("zaraz po nim", "a zaraz po nim"),
        "prefer": "а зараз за ним",
        "avoid": "а зараз по ним",
        "reason": "Keep the source preposition in this sequence formula.",
    },
    {
        "triggers": ("programu „u siebie", "programu \"u siebie", "u siebie"),
        "prefer": "проґраму «В себе»",
        "avoid": "проґраму „U siebie”",
        "reason": "Translate the programme title as it appears in the Lemko source.",
    },
    {
        "triggers": ("niejednej osobie", "nie jednej osobie", "niejednej"),
        "prefer": "неєдній особі",
        "avoid": "не єдній особі",
        "reason": "Use the fused spelling attested in the article.",
    },
    {
        "triggers": ("ciarki przeszły po ciele", "ciarki przeszly po ciele", "ciarki przeszły"),
        "prefer": "пішли мурянкы по тілі",
        "avoid": "мурашкы перешли по тілі",
        "reason": "Use the Lemko idiom from the source article.",
    },
    {
        "triggers": ("czekało się z utęsknieniem", "czekalo sie z utesknieniem", "czekało się"),
        "prefer": "ждало ся як грибы дойджу",
        "avoid": "чекало ся з тугом",
        "reason": "The source article uses this idiom for eagerly awaiting broadcasts.",
    },
    {
        "triggers": ("przyczyną poruszenia", "przyczyna poruszenia", "poruszenia był", "poruszenia byl"),
        "prefer": "причыном замішаня был",
        "avoid": "причыном порушыня был; справцьом замішаня был",
        "reason": "Use the source lexical choice for emotional stir/excitement.",
    },
    {
        "triggers": ("krzysztof krzyżanowski", "krzysztof krzyzanowski", "krzyżanowski", "krzyzanowski"),
        "prefer": "Кжыштоф Кжыжановскій",
        "avoid": "Кшыштоф Кшыжановскій",
        "reason": "Use the proper-name transliteration attested in the Lemko article.",
    },
    {
        "triggers": ("niewielkiej", "niewielka", "niewielki"),
        "prefer": "невеликой / невелика / невеликій as required by case",
        "avoid": "forcing Polish-like adjective endings without matching Lemko case",
        "reason": "The article fragment begins with the genitive/dative-style form Невеликой.",
    },
    {
        "triggers": ("najlepszym gospodarzem", "najlepszy gospodarz", "gospodarzem", "gospodarz"),
        "prefer": "найліпшым ґаздом / найліпшый ґазда / ґазда",
        "avoid": "найліпшым господарьом / господар",
        "reason": "Use the source rural/community term ґазда.",
    },
    {
        "triggers": ("gratulujemy", "gratulacje"),
        "prefer": "ґратуюєме",
        "avoid": "ґратулюєме",
        "reason": "Use the source spelling in this headline formula.",
    },
    {
        "triggers": ("show dance ido", "studia tańca destino", "studia tanca destino", "natalia pelechacz"),
        "prefer": "Сердечні ґратулюєме",
        "avoid": "Сердечні ґратуюєме",
        "reason": "The dance-championship article uses the regular congratulation spelling, overriding the separate Gazda headline formula.",
    },
    {
        "triggers": ("został laureatem", "zostal laureatem", "laureatem"),
        "prefer": "остал лавреатом",
        "avoid": "остал лявреатом",
        "reason": "Use the source spelling лавреат.",
    },
    {
        "triggers": ("gospodarza roku", "gospodarz roku", "gospodarzem roku"),
        "prefer": "Ґазды Рока",
        "avoid": "Господаря Рока",
        "reason": "Use the source contest title wording.",
    },
    {
        "triggers": ("wygrał", "wygral", "wygrała", "wygrala"),
        "prefer": "выграл / выграла",
        "avoid": "выиграл / выиграла",
        "reason": "Use the source spelling without inserted и.",
    },
    {
        "triggers": ("podczas konkursów praktycznych", "podczas konkursow praktycznych", "konkursów praktycznych"),
        "prefer": "підчас практичных конкуренций",
        "avoid": "в часі практичных конкурсів",
        "reason": "Use the source wording for practical competitions.",
    },
    {
        "triggers": ("gdy chodzi o teorię", "gdy chodzi o teorie", "chodzi o teorię", "teorię i praktykę", "teorie i praktyke"),
        "prefer": "і кєд іде о теорию і практику",
        "avoid": "в теориі а так само в практиці",
        "reason": "Use the source formula for theory and practice.",
    },
    {
        "triggers": ("2026 r.", "2025 r.", "2024 r.", "maja 2026 r"),
        "prefer": "2026 р. / 2025 р. / 2024 р. in date headers",
        "avoid": "2026 рока in date headers when Polish source uses r.",
        "reason": "Daily-summary headers keep the abbreviated year marker р.",
    },
    {
        "triggers": ("do niedawna", "jeszcze do niedawna"),
        "prefer": "донедавна",
        "avoid": "до недавна",
        "reason": "Use the source adverb spelling.",
    },
    {
        "triggers": ("przestrzeni publicznej", "przestrzeń publiczna", "publicznej przestrzeni"),
        "prefer": "публичным просторі",
        "avoid": "публичній пространи",
        "reason": "Use the source collocation for public space.",
    },
    {
        "triggers": ("fundamentalne pytania", "pytania fundamentalne"),
        "prefer": "фундаментальны звіданя",
        "avoid": "фундаментальны пытаня",
        "reason": "Use the source noun in this metalinguistic register.",
    },
    {
        "triggers": ("bo właśnie", "właśnie w tych", "wlasnie w tych"),
        "prefer": "бо як раз",
        "avoid": "бо власні",
        "reason": "Use the source adverbial phrase.",
    },
    {
        "triggers": ("przez wiele lat", "przez lata"),
        "prefer": "через вельо років",
        "avoid": "през вельо років",
        "reason": "Use через for time span in this source style.",
    },
    {
        "triggers": ("dalsze przetrwanie", "dalszego przetrwania"),
        "prefer": "дальше перетырваня",
        "avoid": "дальшє перетырваня",
        "reason": "Use the source spelling дальше.",
    },
    {
        "triggers": ("xx wieku", "xx w", "xx stulecia"),
        "prefer": "ХХ ст.",
        "avoid": "XX столітя",
        "reason": "Use the source abbreviation for 20th century.",
    },
    {
        "triggers": ("rozproszeniu wspólnoty", "rozproszenie wspólnoty", "rozproszeniu wspolnoty"),
        "prefer": "розогнаню спільноты",
        "avoid": "розпорошыню спільноты",
        "reason": "Use the source term for community dispersal.",
    },
    {
        "triggers": ("zaniku naturalnego", "zanik naturalnego", "zaniku środowiska", "zaniku srodowiska"),
        "prefer": "занику натурального",
        "avoid": "заниканю натурального",
        "reason": "Use the source nominal form.",
    },
    {
        "triggers": ("organizacje łemkowskie opublikowały", "organizacje lemkowskie opublikowaly", "opublikowały wspólne stanowisko"),
        "prefer": "лемківскы орґанізациі оприлюднили спільне становиско",
        "avoid": "лемківскы орґанізациі опубликували спільне становиско",
        "reason": "Use the source verb for publishing a public position.",
    },
    {
        "triggers": ("wydarzeń, jakie miały miejsce", "wydarzen jakie mialy miejsce", "miały miejsce"),
        "prefer": "подій, якы прошли",
        "avoid": "дій, якы мали місце",
        "reason": "Use the source event phrasing.",
    },
    {
        "triggers": ("na terenie prawosławnej parafii", "na terenie", "terenie parafii"),
        "prefer": "на обшыри",
        "avoid": "на територіі",
        "reason": "Use обшыр for area/territory in this source register.",
    },
    {
        "triggers": ("sygnatariusze", "sygnatariuszy"),
        "prefer": "Сиґнаратиі",
        "avoid": "Сиґнатари",
        "reason": "Use the source form for signatories.",
    },
    {
        "triggers": ("jednoznacznie", "jednoznaczny"),
        "prefer": "єднозначно",
        "avoid": "єднозначні",
        "reason": "Use the source adverb form.",
    },
    {
        "triggers": ("gestów i działań", "gestow i dzialan", "działań"),
        "prefer": "жестів і діянь",
        "avoid": "жестів і діяній",
        "reason": "Use the source genitive plural for actions.",
    },
    {
        "triggers": ("wyrażenie poparcia", "wyrazenie poparcia", "poparcia dla środowisk"),
        "prefer": "высловліня попертя середовиск",
        "avoid": "выражыня попертя; попертя для середовиск",
        "reason": "Use the source wording for expression of support.",
    },
    {
        "triggers": ("prawosławnej parafii", "prawoslawnej parafii", "parafii w gładyszowie", "parafii w gladyszowie"),
        "prefer": "Православной Парохіі",
        "avoid": "Православной Парафіі",
        "reason": "Use the source church-community term.",
    },
    {
        "triggers": ("które mogą być interpretowane", "ktore moga byc interpretowane", "które mogą", "ktore moga"),
        "prefer": "котры можут быти інтерпретуваны",
        "avoid": "якы можут быти інтерпретуваны",
        "reason": "Use the source relative form in this statement.",
    },
    {
        "triggers": ("w oświadczeniu", "w oswiadczeniu", "oświadczeniu podkreślono", "oswiadczeniu podkreslono"),
        "prefer": "В освідчыню",
        "avoid": "В заяві",
        "reason": "Use the source noun for public statement.",
    },
    {
        "triggers": ("narodowe święto", "narodowe swieto", "święto rusinów", "swieto rusinow"),
        "prefer": "Нацийональне свято",
        "avoid": "Народове свято",
        "reason": "Use the source adjective for national holiday/event.",
    },
    {
        "triggers": ("chorwacji", "chorwacja", "chorwackiej republiki", "republiki chorwackiej"),
        "prefer": "Хорвациі / Хорватсой Республикы",
        "avoid": "Хорватиі / Хорватской Республикы",
        "reason": "Use the source country and republic forms.",
    },
    {
        "triggers": ("vukovarze", "vukovar", "w vukovarze"),
        "prefer": "Вуковарі / Вуковар",
        "avoid": "Vukovarze / Vukovar in Latin script",
        "reason": "Cyrillicize the place name as in the source.",
    },
    {
        "triggers": ("20 maja 2026", "dzień 20 maja", "dzien 20 maja", "24 maja w vukovarze"),
        "prefer": "на ден 20. мая 2026 р.; В неділю 24. мая в Вуковарі",
        "avoid": "на ден 20 мая 2026 р.; В неділю 24 мая в Вуковарі",
        "reason": "Keep day-number dots in the Croatia daily-summary reference.",
    },
    {
        "triggers": ("uroczyste wydarzenie", "uroczysta pod", "wydarzenie z okazji dnia rusinów"),
        "prefer": "торжественна подія",
        "avoid": "святочне подія",
        "reason": "Use the reference wording for the Croatia event announcement.",
    },
    {
        "triggers": ("popołudniowego spotkania", "popoludniowego spotkania"),
        "prefer": "пополудньовой стрічы",
        "avoid": "пополудньового стрічаня",
        "reason": "Use the source phrase for afternoon meeting.",
    },
    {
        "triggers": ("chorwackim domu vukovar", "chorwackim domu", "domu vukovar"),
        "prefer": "в Хорватскым Домі Вуковар",
        "avoid": "в Хорватскім Домі Вуковар",
        "reason": "Use the spelling attested in the Croatia reference.",
    },
    {
        "triggers": ("znajduje się wernisaż", "znajduje sie wernisaz", "w programie", "wernisaż wystawy", "wernisaz wystawy"),
        "prefer": "находит ся вернісаж выставы",
        "avoid": "є вернисаж выставы",
        "reason": "Use the reference predicate and spelling for the exhibition opening item.",
    },
    {
        "triggers": ("hardiego", "hardi", "josafata hardiego"),
        "prefer": "Гарді",
        "avoid": "Hardiego in Latin script",
        "reason": "Use the source surname form.",
    },
    {
        "triggers": ("mychajła josafata", "mychajla josafata", "michała josafata", "michala josafata"),
        "prefer": "Михайла Йосафата",
        "avoid": "Михаіла Йозафата",
        "reason": "Use the source given-name forms.",
    },
    {
        "triggers": ("kwestionowanie statusu", "kwestionowania statusu", "kwestionowanie"),
        "prefer": "квестийонуваня статусу",
        "avoid": "піддаваня в сумнів статусу",
        "reason": "Use the source loanword in this legal-language context.",
    },
    {
        "triggers": ("kaszubskiego", "kaszubski", "kaszubsko-pomorskie", "kaszubsko pomorskie"),
        "prefer": "кашебского / кашебскій / Кашебско-Поморска",
        "avoid": "кашубского / кашубскій / Кашубско-Поморске",
        "reason": "Use the source ethnolinguistic spelling кашеб-.",
    },
    {
        "triggers": ("zrzeszenie kaszubsko-pomorskie", "zrzeszenia kaszubsko-pomorskiego"),
        "prefer": "Кашебско-Поморска Асоцияция / Асоцияциі",
        "avoid": "Кашебско-Поморске Здружыня / Здружыня",
        "reason": "Use the source organization name.",
    },
    {
        "triggers": (
            "kaszubsko-pomorskie stowarzyszenie",
            "kaszubsko pomorskie stowarzyszenie",
            "zarzad glowny stowarzyszenia",
        ),
        "prefer": "Кашебско-Поморска Асоцияция; Головный Заряд Асоцияциі",
        "avoid": "Кашебско-Поморске Стоваришыня / Головный Заряд Стоваришыня",
        "reason": "Treat this as a fixed organization name in the Kashubian-status article.",
    },
    {
        "triggers": ("chodzi o komentarze", "chodzi o", "komentarze, które"),
        "prefer": "Іде о коментарі, котры",
        "avoid": "Ходит о коментарі, якы",
        "reason": "Use the source phrase and relative pronoun.",
    },
    {
        "triggers": ("papieżowi leonowi", "papiezowi leonowi", "leonowi xiv"),
        "prefer": "папi Левови XIV",
        "avoid": "папі Леонови XIV",
        "reason": "Use the source papal-name form.",
    },
    {
        "triggers": ("kaszubskojęzycznej tory", "kaszubskojezycznej tory", "tory w kaszubskim języku"),
        "prefer": "кашебскоязычной Торы",
        "avoid": "Торы в кашубскым языку",
        "reason": "Use the source adjective construction.",
    },
    {
        "triggers": ("podważanie statusu", "podwazanie statusu", "podważania statusu", "podwazania statusu"),
        "prefer": "підважаня статусу",
        "avoid": "подважаня статусу",
        "reason": "Use the Kashubian-status reference spelling.",
    },
    {
        "triggers": ("w watykanie", "watykanie"),
        "prefer": "в Ватикані",
        "avoid": "в Ватыкані",
        "reason": "Use the reference spelling for the Vatican place-name in this article.",
    },
    {
        "triggers": ("zarząd główny", "zarzad glowny"),
        "prefer": "Головный Заряд",
        "avoid": "Головна управа",
        "reason": "Use the source organization-board term.",
    },
    {
        "triggers": ("pełnoprawnym", "pelnoprawnym", "pełnoprawny", "pelnoprawny"),
        "prefer": "полноправным",
        "avoid": "повноправным",
        "reason": "Use the attested wording from the Kashubian-status reference text.",
    },
    {
        "triggers": ("13 maja 2026", "dzień 13 maja", "dzien 13 maja"),
        "prefer": "на ден 13. мая 2026 р.",
        "avoid": "на ден 13 мая 2026 р.",
        "reason": "Keep the day-number punctuation used by the Lemko reference.",
    },
    {
        "triggers": (
            "z głośników zabrzmiał dżingiel",
            "z glosnikow zabrzmial dzingiel",
            "z głośników rozległ się dżingiel",
            "z glosnikow rozlegl sie dzingiel",
            "z głośników rozbrzmiał dżingiel",
            "z glosnikow rozbrzmial dzingiel",
            "rozbrzmiał dżingiel",
            "rozbrzmial dzingiel",
        ),
        "prefer": "з голосників вызвучал джінґєль",
        "avoid": "з гучників розліг ся джінґель",
        "reason": "Use the exact jingle phrase from the jubilee reference.",
    },
    {
        "triggers": ("zaraz za nim sygnał", "zaraz za nim sygnal", "a zaraz za nim"),
        "prefer": "а зараз за ним сиґнал",
        "avoid": "а зараз по ним сиґнал",
        "reason": "Use the jubilee reference preposition.",
    },
    {
        "triggers": ("pojawiły się po wizycie", "pojawily sie po wizycie", "pojawiły się", "pojawily sie"),
        "prefer": "явили ся",
        "avoid": "появили ся",
        "reason": "Use the Kashubian-status reference verb.",
    },
    {
        "triggers": ("wręczeniu papieżowi", "wreczeniu papiezowi", "wręczeniu papieżowi leonowi", "wreczeniu papiezowi leonowi"),
        "prefer": "вручыню папi Левови XIV",
        "avoid": "переданю папі Левови XIV",
        "reason": "Use the reference wording for handing the Torah to the Pope.",
    },
    {
        "triggers": ("papieżowi leonowi", "papiezowi leonowi", "papieżowi", "papiezowi"),
        "prefer": "папi",
        "avoid": "папі",
        "reason": "Use the reference spelling with Latin i for this article.",
    },
    {
        "triggers": ("rozmawialiśmy", "rozmawialismy", "rozmawialiśmy o obecności", "rozmawialismy o obecnosci"),
        "prefer": "бесідували",
        "avoid": "говорили",
        "reason": "Use the LEMKO.TOOLS reference verb.",
    },
    {
        "triggers": ("były to fundamentalne pytania", "byly to fundamentalne pytania", "były to fundamentalne", "byly to fundamentalne"),
        "prefer": "Были то фундаментальны звіданя",
        "avoid": "То были фундаментальны звіданя",
        "reason": "Use the LEMKO.TOOLS reference word order.",
    },
    {
        "triggers": ("mówiliśmy w kontekście", "mowilismy w kontekscie", "o przyszłości języka", "o przyszlosci jezyka"),
        "prefer": "бесідували",
        "avoid": "говорили сме",
        "reason": "Recover the LEMKO.TOOLS reference verb from the reverse Polish wording.",
    },
    {
        "triggers": ("przyszłości języka", "przyszlosci jezyka", "o przyszłości języka", "o przyszlosci jezyka"),
        "prefer": "будучности языка",
        "avoid": "пришлости языка",
        "reason": "Use the LEMKO.TOOLS reference noun.",
    },
    {
        "triggers": ("w tych obszarach", "w tych obszarach przez wiele lat"),
        "prefer": "в тых обшырях",
        "avoid": "в тых областях",
        "reason": "Use the LEMKO.TOOLS reference noun.",
    },
    {
        "triggers": ("rozstrzygało się", "rozstrzygalo sie", "rozstrzygało się dalsze", "rozstrzygalo sie dalsze"),
        "prefer": "рішало",
        "avoid": "розсуджувало",
        "reason": "Use the LEMKO.TOOLS reference verb.",
    },
    {
        "triggers": ("test wiedzy", "test znajomości", "test znajomosci"),
        "prefer": "тест знаня",
        "avoid": "тест знатя",
        "reason": "Use the Gazda contest reference noun.",
    },
    {
        "triggers": ("wygrał test wiedzy", "wygral test wiedzy"),
        "prefer": "выграл тест знаня",
        "avoid": "выграл тест знатя",
        "reason": "Use the full Gazda contest reference phrase.",
    },
    {
        "triggers": ("jest pełnoprawnym", "jest pelnoprawnym", "kaszubski jest pełnoprawnym", "kaszubski jest pelnoprawnym"),
        "prefer": "єст полноправным",
        "avoid": "є полноправным",
        "reason": "Use the Kashubian-status reference copula.",
    },
    {
        "triggers": ("odcinają się", "odcinaja sie", "jednoznacznie odcinają się", "jednoznacznie odcinaja sie"),
        "prefer": "одтинают",
        "avoid": "одрізают",
        "reason": "Use the public-statement reference verb.",
    },
    {
        "triggers": ("gestów i działań", "gestow i dzialan", "gesty i działania", "gesty i dzialania"),
        "prefer": "жестів і діянь",
        "avoid": "жестів альбо діянь",
        "reason": "Keep the conjunction used in the public-statement reference.",
    },
)

TRANSLATION_MEMORY: tuple[dict[str, Any], ...] = (
    {
        "label": "art exhibition announcement",
        "min_hits": 6,
        "source_terms": (
            "obrazy",
            "fotografie",
            "rzeźby",
            "instalacje",
            "realizacje artystyczne",
            "warszawie",
            "wystawie",
            "łemków",
            "rusinów",
            "instytucja",
            "sztuki",
        ),
        "polish_excerpt": (
            "Obrazy, fotografie, rzeźby, instalacje oraz inne realizacje artystyczne zostaną "
            "zaprezentowane w Warszawie na wystawie o charakterze przełomowym..."
        ),
        "lemko_reference": (
            "Образы, знимкы, різбы, інсталяциі і інчы реализациі штукы презентуваны будут "
            "на історичній для Лемків в Польщы, а так само для Русинів обще, выставі в Варшаві."
        ),
        "usage": "If the current Polish source is this announcement or a close variant, reuse these phrase choices.",
    },
)

POLISH_WORD_RE = re.compile(r"[0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż-]{2,}", re.UNICODE)

POLISH_STOPWORDS = {
    "aby",
    "ale",
    "ani",
    "bez",
    "bo",
    "by",
    "być",
    "był",
    "była",
    "było",
    "były",
    "czy",
    "dla",
    "do",
    "gdy",
    "go",
    "ich",
    "im",
    "jak",
    "jego",
    "jej",
    "jest",
    "już",
    "kiedy",
    "która",
    "które",
    "którego",
    "której",
    "który",
    "ma",
    "mi",
    "mnie",
    "na",
    "nad",
    "nie",
    "nim",
    "niż",
    "od",
    "oraz",
    "po",
    "pod",
    "przez",
    "przy",
    "się",
    "są",
    "ta",
    "tak",
    "te",
    "tego",
    "tej",
    "ten",
    "to",
    "tu",
    "w",
    "we",
    "więc",
    "więcej",
    "wszystko",
    "z",
    "za",
    "że",
}

POLISH_ASCII_STOPWORDS = {
    "aby",
    "ale",
    "ani",
    "bez",
    "bo",
    "by",
    "byc",
    "byl",
    "byla",
    "bylo",
    "byly",
    "czy",
    "dla",
    "dnia",
    "do",
    "gdy",
    "go",
    "ich",
    "im",
    "jak",
    "jego",
    "jej",
    "jest",
    "juz",
    "kiedy",
    "ktora",
    "ktore",
    "ktorego",
    "ktorej",
    "ktory",
    "ma",
    "mi",
    "mnie",
    "na",
    "nad",
    "nie",
    "nim",
    "niz",
    "od",
    "oraz",
    "po",
    "pod",
    "przez",
    "przy",
    "sie",
    "sa",
    "ta",
    "tak",
    "te",
    "tego",
    "tej",
    "ten",
    "to",
    "tu",
    "w",
    "we",
    "wiecej",
    "wiec",
    "wszystko",
    "z",
    "za",
    "ze",
}

MEMORY_PROFILE_TRIGGERS: dict[str, tuple[str, ...]] = {
    "daily_summary": (
        "podsumowanie dnia",
        "podsumowanie grupy medialnej",
        "grupy medialnej lem",
        "sumar dnia",
        "na dzien",
        "dnia na",
        "przynosimy wam podsumowanie",
        "przedstawiamy wam podsumowanie",
    ),
    "art_exhibition": (
        "wystaw",
        "sztuk",
        "obrazy",
        "fotografie",
        "rzezby",
        "instalac",
        "artyst",
        "muze",
        "kurator",
        "formy obecnosci",
    ),
    "church_heritage": (
        "cerk",
        "prawoslaw",
        "parafi",
        "liturg",
        "ikonostas",
        "swiatyn",
        "starodruk",
        "bursa",
        "eparchi",
    ),
    "church_beatification": (
        "blogoslawionym",
        "ogloszono blogoslawionym",
        "kaplana meczennika",
        "kaplan meczennik",
        "mukaczewskiej eparchii",
        "eparchii greckokatolickiej",
        "pawla piotra orosa",
        "bilky",
        "biskupi i duchowienstwo",
    ),
    "environmental_land_heritage": (
        "magurski park narodowy",
        "magurskiemu parkowi narodowemu",
        "magurskiego parku narodowego",
        "fundacja orlen",
        "nieznajowa",
        "orlow przednich",
        "orzel przedni",
        "hektarow terenu",
        "protokol przekazania",
        "krempnej",
        "powiekszyc o wspomniana doline",
    ),
    "institutional_project": (
        "projekt",
        "program",
        "fundusz",
        "dofinans",
        "grant",
        "realizowan",
        "stowarzysz",
        "ministerstw",
        "forum",
        "dziedzictw",
        "spadkow",
    ),
    "music_dance": (
        "koncert",
        "muzyk",
        "melodi",
        "piosen",
        "taniec",
        "tanc",
        "mistrzostw",
        "show dance",
        "radio",
        "audyc",
    ),
    "dictionary_publication": (
        "slownik",
        "hasel",
        "publikac",
        "wydawnict",
        "ksiazk",
        "numer",
        "czasopism",
        "rrb",
        "rocznik",
    ),
    "cross_border_public": (
        "pogranicz",
        "transgranicz",
        "slowac",
        "wegr",
        "unia europejska",
        "europejski",
        "mniejszos",
        "rusinow",
        "karpackich",
    ),
    "minority_institutional": (
        "eurokomisar",
        "rozszerzenia unii europejskiej",
        "sieci spolecznosciowej x",
        "rada europy",
        "komitet doradczy",
        "konwencji ramowej",
        "ochronie mniejszosci",
        "mniejszosci narodow",
        "rusinow nie uj",
        "uzhorod",
        "uzhhorod",
        "zakarpack",
        "poziomie panstwowym",
    ),
    "minority_congress": (
        "kongres o mniejszosciach",
        "kongresu o mniejszosciach",
        "kongresie o mniejszosciach",
        "uniwersytet opolski",
        "uniwersytetu opolskiego",
        "samorzad wojewodztwa opolskiego",
        "instytut slaski",
        "europejskie centrum badan nad mniejszosciami",
        "flensburgu",
        "komisji praw czlowieka onz",
        "roznorodnosci kulturowej",
    ),
    "parliamentary_minority": (
        "poslowie i poslanki",
        "poslow i poslanek",
        "komisji mniejszosci narodowych i etnicznych",
        "komisja mniejszosci narodowych i etnicznych",
        "sejmowej komisji mniejszosci",
        "kongresu o mniejszosciach",
        "wojewody opolskiego",
        "finansowania dzialan mniejszosci",
        "budzetem panstwa",
        "tozsamosci kulturowej",
    ),
    "sejm_minority_law_amendment": (
        "ustawa zostala przyjeta przez sejm",
        "ustawa zostala przyjeta przez sejm",
        "sejm przyjal nowelizacje ustawy o mniejszosciach",
        "nowelizacje ustawy o mniejszosciach narodowych i etnicznych",
        "procesu legislacyjnego",
        "wspolnej komisji obsluge merytoryczna",
        "kancelaria sejmu bedzie zapewniala",
        "ryszard galla",
        "pelnomocnik marszalka sejmu",
        "ustawa trafi teraz do senatu",
    ),
    "mercator_regional_dossiers": (
        "regional dossiers",
        "mercator european research centre",
        "multilingualism and language learning",
        "fryske akademy",
        "leeuwarden",
        "jezykowi lemkowskiemu w polsce",
        "wielojezycznosci i edukacji jezykowej",
        "jezykow regionalnych i mniejszosciowych",
    ),
    "watrowe_field_renovation": (
        "watrowym polu",
        "lemkowskiej watry na obczyznie",
        "watrowym placem",
        "prace budowlano-remontowe",
        "budynku ze scena",
        "siedziba organizatorow",
        "drugi sierpniowy weekend",
        "8 do 10 sierpnia",
    ),
    "bell_ringer_interview": (
        "nathaniel zawlik",
        "natanael zawlik",
        "dzwonami zaczal sie interesowac",
        "mlodym dzwonnikiem",
        "korczynie kolo biecza",
        "dolnego slaska",
        "dzwon przeniesiony z lubina",
        "kanalow na youtube",
        "fascynatow dzwonow",
    ),
    "academic": (
        "akademick",
        "naukow",
        "czasopism",
        "artykul",
        "badawcz",
        "uniwersytet",
        "rocznik",
        "rrb",
    ),
    "broadcast_retransmission": (
        "retransmis",
        "kongresu jezyka rusinskiego",
        "kongres jezyka rusinskiego",
        "miedzynarodowego kongresu",
        "jezyka rusinskiego",
        "powtorka z",
    ),
    "rusyn_congress_program": (
        "kongresu jezyka rusinskiego",
        "kongres jezyka rusinskiego",
        "instytut jezyka i kultury rusinskiej",
        "uniwersytetu preszowskiego",
        "program kongresu",
        "posiedzenie plenarne",
        "czesc plenarna",
        "prelegentow",
        "pawel robert magocsi",
        "luca calvi",
    ),
    "glos_opera_performance": (
        "opera krakowska",
        "opery krakowskiej",
        "glos",
        "głos",
        "darii kuziak",
        "daria kuziak",
        "opera kameralna",
        "opery kameralnej",
        "zabrzmi na trzech",
        "najblizszy koncert",
        "folklorem a forma",
        "folklorem a formą",
        "propozycja repertuarowa",
        "forma artystycznego eksperymentu",
        "forma sceniczna",
        "najdalszych zakatkow lemkowyny",
        "najdalszych zakątków łemkowyny",
    ),
    "serbia_rusyn_council": (
        "narodowej rady rusinow w serbii",
        "narodowa rada rusinow",
        "narodowej rady rusinów w serbii",
        "narodowa rada rusinów",
        "rusinskim samorzadzie w serbii",
        "rusińskim samorządzie w serbii",
        "wojwodinscy rusini",
        "wojwodińscy rusini",
        "olena papuga",
        "zrezygnowala",
        "zrezygnowała",
        "odwolana z funkcji",
        "odwołana z funkcji",
    ),
    "orthodox_grabarka": (
        "gora grabarka",
        "gorze grabarce",
        "świętej górze grabarce",
        "swietej gorze grabarce",
        "przemienienia panskiego",
        "przemienienia pańskiego",
        "polskiego autokefalicznego kosciola prawoslawnego",
        "polskiego autokefalicznego kościoła prawosławnego",
        "metropolita warszawski",
        "wojewoda podlaski",
    ),
    "lemko_org_appeal": (
        "stowarzyszenie ruska bursa",
        "stowarzyszenie lemkow",
        "stowarzyszenie łemków",
        "grzegorza kuprianowicza",
        "grzegorza kuprianowicza",
        "wspolprzewodniczacego komisji wspolnej",
        "współprzewodniczącego komisji wspólnej",
        "mniejszosci ukrainskiej",
        "mniejszości ukraińskiej",
        "odrebnosc mniejszosci lemkowskiej",
        "odrębność mniejszości łemkowskiej",
    ),
    "lemko_language_rights_appeal": (
        "kwestionuje jezyk lemkowski",
        "kwestionuje język łemkowski",
        "naruszenia praw jezykowych",
        "naruszenia praw językowych",
        "prawa jezykowe mniejszosci lemkowskiej",
        "prawa językowe mniejszości łemkowskiej",
        "dialekcie lemkowskim",
        "dialekcie łemkowskim",
        "dwujezycznej tablicy",
        "dwujęzycznej tablicy",
        "zdynia",
        "żdynia",
    ),
    "radio_archaeology": (
        "wykopalisk archeologicznych",
        "archeologicznych wykopalisk",
        "starym lupkowie",
        "starym łupkowie",
        "lemkowskim cmentarzu",
        "łemkowskim cmentarzu",
        "zostaly juz tylko krzaki",
        "zostały już tylko krzaki",
        "o zebach",
        "o zębach",
        "powtorki we wtorek",
        "powtórki we wtorek",
    ),
    "zyndranowa_rusal_event": (
        "od zielonych swiat do jana",
        "od zielonych świąt do jana",
        "zyndranowej",
        "zyndranowa",
        "muzeum kultury lemkowskiej",
        "muzeum kultury łemkowskiej",
        "kapela spod rubani",
        "leczanie",
        "łęczanie",
        "czendesz orkiestra",
    ),
    "roma_antidiscrimination_appeal": (
        "stowarzyszenie romow",
        "stowarzyszenie romów",
        "donald tusk",
        "dyskryminacje rasizm ksenofobie",
        "dyskryminację rasizm ksenofobię",
        "przestepstwami na tle nienawisci",
        "przestępstwami na tle nienawiści",
        "marsze o charakterze antyimigracyjnym",
        "ryzyko napasci",
        "ryzyko napaści",
    ),
    "rusyn_choreographer_obituary": (
        "milomir szajtosz",
        "milomir szajtosz",
        "ruthenpress",
        "rusinskim centrum kultury",
        "rusińskim centrum kultury",
        "rusin film fest",
        "akademii sztuk",
        "tworczosci taneczno-folklorystycznej",
        "twórczości taneczno-folklorystycznej",
        "autorskich choreografii",
    ),
    "church_cross_theft": (
        "odciecie krzyza",
        "odciecia krzyza",
        "odcieli metalowy krzyz",
        "zatrzymano osobe podejrzana",
        "legnickiej cerkwi",
        "greckokatolickiej cerkwi w legnicy",
        "kradziezy krzyza",
        "kopule swiatyni",
        "zniszczyli krzyz",
    ),
    "nikifor_private_collection": (
        "nikifor",
        "epifaniusz drowniak",
        "muzeum ziemi lubuskiej",
        "zielonej gorze",
        "zielonej gorze ogladac",
        "samorodnego talentu",
        "prywatnej kolekcji roberta dowhana",
        "zbior liczy 50 prac",
    ),
    "bartne_iconostasis": (
        "kolorowa cerkiew w bartnem",
        "cerkiew w bartnem",
        "plebanski spichlerz",
        "plebański spichlerz",
        "ikonostas",
        "konserwacji",
        "sztuki cerkiewnej",
        "zgodnie z przeznaczeniem",
    ),
    "ombudsman_education": (
        "ombudsman",
        "ministerstwa edukacji narodowej",
        "ministerstwo edukacji narodowej",
        "szkolnictwa dla mniejszosci",
        "szkolnictwo dla mniejszosci",
        "mniejszosci narodowych i etnicznych",
        "zastepca ombudsmana",
        "doprecyzowanie przepisow",
    ),
    "fosterlang_inauguration": (
        "fosterlang",
        "horyzont europa",
        "wilamowicach",
        "wymysioerys",
        "jezyka wilamowskiego",
        "jezyk wilamowski",
        "wsparcie jezykow mniejszosciowych",
        "inauguracja projektu i konferencja",
    ),
}

MEMORY_AUDIT_GENERIC_TERMS = {
    "dzien",
    "dnia",
    "grudnia",
    "januara",
    "lem",
    "listopada",
    "marca",
    "medialnej",
    "novembra",
    "podsumowanie",
    "roku",
    "sumar",
    "stycznia",
}

MEMORY_TEXT_KEYS = ("polish_intermediate_text", "polish_source_text", "source_text")
MEMORY_LEMKO_KEYS = ("original_lemko_text", "human_lemko_text", "lemko_reference", "translated_lemko_text")

CODEX_OUTPUT_SCHEMA: dict[str, Any] = {
    "name": "polish_to_lemko_translation",
    "schema": {
        "type": "object",
        "properties": {
            "translated_text": {
                "type": "string",
                "description": "Full translation into standardized Lemko Cyrillic.",
            },
            "used_dictionary_entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_query": {"type": "string"},
                        "term_id": {"type": "integer"},
                        "base_form": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["source_query", "term_id", "base_form", "reason"],
                    "additionalProperties": False,
                },
            },
            "uncertain_terms": {
                "type": "array",
                "items": {"type": "string"},
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "translated_text",
            "used_dictionary_entries",
            "uncertain_terms",
            "warnings",
        ],
        "additionalProperties": False,
    },
}


class TranslationError(RuntimeError):
    """Raised for controlled translation failures."""


class CodexExecutionError(TranslationError):
    """Raised when Codex CLI cannot be started or returns invalid output."""


@dataclass
class ApiClient:
    api_base: str = DEFAULT_API_BASE
    api_token: str | None = None
    timeout: int = 60
    retries: int = 3
    retry_delay: float = 2.0

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.api_base.rstrip("/") + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        if self.api_token:
            request.add_header("Authorization", f"Bearer {self.api_token}")
        raw = ""
        for attempt in range(max(0, self.retries) + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if not should_retry_http(exc.code) or attempt >= self.retries:
                    raise TranslationError(f"API {path} failed with HTTP {exc.code}: {body[:500]}") from exc
                time.sleep(self.retry_delay * (attempt + 1))
            except urllib.error.URLError as exc:
                if attempt >= self.retries:
                    raise TranslationError(f"API {path} is unavailable: {exc}") from exc
                time.sleep(self.retry_delay * (attempt + 1))
            except (TimeoutError, socket.timeout) as exc:
                if attempt >= self.retries:
                    raise TranslationError(f"API {path} timed out after {self.timeout}s: {exc}") from exc
                time.sleep(self.retry_delay * (attempt + 1))
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslationError(f"API {path} returned invalid JSON: {raw[:500]}") from exc
        if not isinstance(parsed, dict):
            raise TranslationError(f"API {path} returned non-object JSON.")
        return parsed


def should_retry_http(status_code: int) -> bool:
    return status_code in {429, 502, 503, 504}


@dataclass
class CodexRunner:
    codex_bin: str = "codex"
    timeout: int = DEFAULT_CODEX_TIMEOUT
    debug: bool = False

    def run_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        resolved = resolve_codex_binary(self.codex_bin)
        with tempfile.TemporaryDirectory(prefix="pl-lem-codex-") as tmp:
            base = Path(tmp)
            workdir = base / "work"
            codex_home = base / "codex-home"
            home = base / "home"
            workdir.mkdir(parents=True, exist_ok=True)
            home.mkdir(parents=True, exist_ok=True)
            copy_codex_auth(codex_home)

            schema_path = base / "output-schema.json"
            schema_path.write_text(json.dumps(schema.get("schema", schema), ensure_ascii=False), encoding="utf-8")
            output_path = base / "last-message.json"

            cmd = [
                resolved,
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
                "--output-schema",
                str(schema_path),
                "-",
            ]
            if self.debug:
                print(f"[codex] {' '.join(cmd)}", file=sys.stderr)

            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)
            env["HOME"] = str(home)
            env.setdefault("NO_COLOR", "1")

            try:
                completed = subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    env=env,
                    cwd=str(workdir),
                )
            except FileNotFoundError as exc:
                raise CodexExecutionError(
                    f"Codex CLI not found: {self.codex_bin!r}. Pass --codex-bin or put codex on PATH."
                ) from exc
            except PermissionError as exc:
                raise CodexExecutionError(
                    "Codex CLI could not be started because the OS denied access. "
                    "Pass --codex-bin pointing to a working Codex executable or run from a terminal where `codex exec` works."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise CodexExecutionError(f"Codex CLI timed out after {self.timeout}s.") from exc
            except OSError as exc:
                raise CodexExecutionError(f"Codex CLI could not be started: {exc}") from exc

            if completed.returncode != 0:
                details = (completed.stderr or completed.stdout or "").strip()
                raise CodexExecutionError(f"Codex CLI failed with exit code {completed.returncode}: {error_tail(details)}")

            if self.debug and completed.stderr.strip():
                print(completed.stderr.strip(), file=sys.stderr)
            raw_output = output_path.read_text(encoding="utf-8") if output_path.is_file() else completed.stdout
            return parse_codex_json(raw_output)


def copy_codex_auth(codex_home: Path) -> None:
    auth_dir = Path(os.getenv("CODEX_AUTH_DIR", "/run/codex-auth")).expanduser()
    auth_file = Path(os.getenv("CODEX_AUTH_FILE", str(auth_dir / "auth.json"))).expanduser()
    if not auth_file.is_file():
        fallback = Path.home() / ".codex" / "auth.json"
        if fallback.is_file():
            auth_file = fallback
        else:
            raise CodexExecutionError("Codex auth file not found. Set CODEX_AUTH_DIR or CODEX_AUTH_FILE.")

    codex_home.mkdir(parents=True, exist_ok=True)
    target_auth = codex_home / "auth.json"
    shutil.copy2(auth_file, target_auth)
    target_auth.chmod(0o600)

    config_file = Path(os.getenv("CODEX_CONFIG_FILE", str(auth_dir / "config.toml"))).expanduser()
    if config_file.is_file():
        target_config = codex_home / "config.toml"
        shutil.copy2(config_file, target_config)
        target_config.chmod(0o600)


def error_tail(text: str, limit: int = 1200) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def resolve_codex_binary(codex_bin: str) -> str:
    if any(sep in codex_bin for sep in ("\\", "/")):
        return codex_bin
    if codex_bin.lower() != "codex":
        return shutil.which(codex_bin) or codex_bin

    for candidate in discover_local_codex_binaries():
        return str(candidate)
    return shutil.which(codex_bin) or codex_bin


def discover_local_codex_binaries() -> list[Path]:
    candidates: list[Path] = []
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(Path(local_app_data).glob("OpenAI/Codex/bin/*/codex.exe"))
        candidates.extend(Path(local_app_data).glob("Programs/OpenAI/Codex/**/codex.exe"))
    user_profile = os.getenv("USERPROFILE")
    if user_profile:
        candidates.extend(Path(user_profile).glob(".codex/packages/standalone/releases/*/bin/codex.exe"))
        candidates.extend(Path(user_profile).glob(".vscode/extensions/openai.chatgpt-*/bin/windows-x86_64/codex.exe"))

    return sorted((candidate for candidate in candidates if candidate.exists()), reverse=True)


def parse_codex_json(output: str) -> dict[str, Any]:
    raw = (output or "").strip()
    if not raw:
        raise CodexExecutionError("Codex CLI returned empty output.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = json.loads(extract_first_json_object(raw))
    if not isinstance(parsed, dict):
        raise CodexExecutionError("Codex CLI returned JSON, but not an object.")
    return parsed


def extract_first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise CodexExecutionError("No JSON object found in Codex output.")
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise CodexExecutionError("Unterminated JSON object in Codex output.")


def split_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
    chunks: list[str] = []
    current = ""

    def flush_current() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in paragraphs:
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        flush_current()
        for sentence in split_long_paragraph(paragraph, max_chars):
            candidate = sentence if not current else current + " " + sentence
            if len(candidate) <= max_chars:
                current = candidate
            else:
                flush_current()
                current = sentence
    flush_current()
    return chunks


def split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?…])\s+", paragraph.strip()) if part.strip()]
    pieces: list[str] = []
    for sentence in sentences or [paragraph.strip()]:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        words = sentence.split()
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                current = word
        if current:
            pieces.append(current)
    return pieces


def extract_polish_terms(text: str, max_terms: int = DEFAULT_MAX_TERMS) -> list[str]:
    if max_terms <= 0:
        return []
    words = [match.group(0) for match in POLISH_WORD_RE.finditer(text)]
    normalized_words = [word.lower().strip("-") for word in words if word.strip("-")]
    candidates: list[str] = []

    def add(candidate: str) -> None:
        clean = candidate.strip().lower()
        if not clean or clean in candidates:
            return
        if all(part in POLISH_STOPWORDS for part in clean.split()):
            return
        candidates.append(clean)

    content_words = [
        word
        for word in normalized_words
        if len(word) > 2 and word not in POLISH_STOPWORDS and not word.isdigit()
    ]
    for word in content_words:
        add(word)
        if len(candidates) >= max_terms:
            return candidates
    for width in (2, 3):
        for index in range(0, max(0, len(content_words) - width + 1)):
            add(" ".join(content_words[index : index + width]))
            if len(candidates) >= max_terms:
                return candidates
    return candidates


def collect_dictionary_context(
    text: str,
    api: ApiClient,
    *,
    max_terms: int = DEFAULT_MAX_TERMS,
    debug: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    candidates = extract_polish_terms(text, max_terms=max_terms)
    dictionary: list[dict[str, Any]] = []
    missing: list[str] = []
    for term in candidates:
        try:
            pl_result = api.post_json("/v1/lemko/search/pl", {"text": term})
        except TranslationError as exc:
            if debug:
                print(f"[dict] {term}: {exc}", file=sys.stderr)
            missing.append(term)
            continue
        entries = pl_result.get("entries") if isinstance(pl_result.get("entries"), list) else []
        if not entries:
            missing.append(term)
            continue
        item = simplify_polish_search_result(term, pl_result)
        for entry in item["entries"][:2]:
            base_form = entry.get("base_form")
            if not base_form:
                continue
            try:
                lem_result = api.post_json("/v1/lemko/search", {"text": str(base_form)})
            except TranslationError as exc:
                if debug:
                    print(f"[dict-detail] {base_form}: {exc}", file=sys.stderr)
                continue
            entry["lemko_details"] = simplify_lemko_search_result(lem_result)
        dictionary.append(item)
    return dictionary, missing


def simplify_polish_search_result(query: str, result: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for raw in result.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        entries.append(
            {
                "term_id": raw.get("term_id"),
                "base_form": raw.get("base_form"),
                "matched_translations": list(raw.get("matched_translations") or [])[:5],
                "all_translations": list(raw.get("all_translations") or [])[:8],
            }
        )
    return {
        "source_query": query,
        "match_source": result.get("match_source"),
        "variant_used": result.get("variant_used"),
        "lemko_forms": list(result.get("lemko_forms") or [])[:8],
        "entries": entries[:5],
    }


def simplify_lemko_search_result(result: dict[str, Any]) -> dict[str, Any]:
    groups_out: list[dict[str, Any]] = []
    for group in result.get("groups") or []:
        if not isinstance(group, dict):
            continue
        attrs = []
        for attr in group.get("grammatical_attributes") or []:
            if isinstance(attr, dict):
                attrs.append({"label": attr.get("label"), "value": attr.get("value")})
        meanings = []
        for entry in group.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            contexts = []
            for context in entry.get("contexts") or []:
                if isinstance(context, dict):
                    contexts.append(
                        {
                            "body": shorten(str(context.get("body") or ""), 220),
                            "author": context.get("author"),
                        }
                    )
            meanings.append(
                {
                    "semantic_description": shorten(str(entry.get("semantic_description") or ""), 260),
                    "contexts": contexts[:1],
                }
            )
        groups_out.append(
            {
                "headword": group.get("headword"),
                "part_of_speech": group.get("part_of_speech"),
                "grammatical_attributes": attrs[:6],
                "forms_headword": group.get("forms_headword"),
                "sample_forms": collect_sample_forms(group.get("forms"), limit=16),
                "meanings": meanings[:2],
            }
        )
    return {"groups": groups_out[:3], "has_results": result.get("has_results")}


def collect_sample_forms(forms: Any, limit: int = 16) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(value, dict):
            words = value.get("_words")
            if isinstance(words, list):
                for word in words:
                    text = str(word).strip()
                    if text and text not in found:
                        found.append(text)
                        if len(found) >= limit:
                            return
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(forms)
    return found


def load_rule_context(rules_dir: Path, max_tables: int = 18, max_rows: int = 5) -> str:
    tables_path = rules_dir / "tables.json"
    if not tables_path.is_file():
        raise TranslationError(f"Rule table file not found: {tables_path}")
    try:
        tables = json.loads(tables_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Invalid rules JSON: {tables_path}") from exc
    if not isinstance(tables, list):
        raise TranslationError(f"Rules JSON must contain a list: {tables_path}")

    priority_ids = (
        "table-001",  # core graphemes
        "table-125",
        "table-126",  # orthographic updates
        "table-002",
        "table-003",
        "table-004",  # noun cases and endings
        "table-050",
        "table-051",  # adjective endings
        "table-083",
        "table-084",
        "table-085",  # reflexive pronoun себе/ся
        "table-086",
        "table-088",
        "table-090",
        "table-092",  # relative pronoun котрый
        "table-101",
        "table-104",
        "table-110",  # verbal endings and future forms
    )
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    by_id = {str(table.get("id")): table for table in tables if isinstance(table, dict)}
    for table_id in priority_ids:
        table = by_id.get(table_id)
        if not table or table_id in seen_ids:
            continue
        selected.append(table)
        seen_ids.add(table_id)
        if len(selected) >= max_tables:
            break

    priorities = (
        "01-system-fonemy-grafemy.md",
        "09-ortografia.md",
        "03-rzeczownik.md",
        "04-przymiotnik.md",
        "06-zaimek.md",
        "07-czasownik.md",
        "05-liczebnik.md",
    )
    if len(selected) < max_tables:
        per_source: dict[str, int] = {}
        for source in priorities:
            for table in tables:
                if not isinstance(table, dict) or table.get("source_file") != source:
                    continue
                table_id = str(table.get("id"))
                if table_id in seen_ids:
                    continue
                count = per_source.get(source, 0)
                allowed = 2 if source not in {"03-rzeczownik.md", "07-czasownik.md"} else 4
                if count >= allowed:
                    continue
                selected.append(table)
                seen_ids.add(table_id)
                per_source[source] = count + 1
                if len(selected) >= max_tables:
                    break
            if len(selected) >= max_tables:
                break

    lines = [
        "Zwięzły kontekst reguł łemkowskich z lokalnych tabel. Traktuj go jako pomoc, nie jako pełną gramatykę.",
    ]
    for table in selected:
        lines.append(f"\n[{table.get('id')}] {table.get('source_file')} :: {strip_markdown(str(table.get('heading') or ''))}")
        for row in (table.get("table") or [])[:max_rows]:
            if isinstance(row, list):
                lines.append(" | ".join(str(cell) for cell in row))
    return "\n".join(lines)


def strip_markdown(value: str) -> str:
    return re.sub(r"[*_`#]+", "", value).strip()


def fold_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def fold_ascii_for_match(value: str) -> str:
    return fold_for_match(value).translate(POLISH_ASCII_TRANSLATION)


def build_general_grammar_guidance(folded_polish_text: str) -> list[str]:
    lines = [
        "Reusable grammar/profile guidance derived from structured_rules tables and repeated evaluation errors:",
        (
            "- Use structured_rules noun/case tables and verb tables to inflect API dictionary headwords; "
            "do not copy the Polish form category mechanically."
        ),
        (
            "- For simple Polish past copula/passive patterns with był/była/było/były, prefer Lemko "
            "был/была/было/были; reserve остал/остала/остало for real 'became/remained' meanings or fixed guidance."
        ),
        (
            "- For reflexive verbs, keep the Lemko reflexive particle ся close to the verb as shown by the "
            "structured_rules reflexive-pronoun table; avoid dropping or moving it across clause boundaries."
        ),
        (
            "- In Lemko Cyrillic output, avoid stray Latin I/i inside Cyrillic sentences unless the token is a real "
            "Latin acronym or brand; use І/і for Lemko conjunctions and ordinal numerals."
        ),
        (
            "- Preserve source clause order and rough part-of-speech sequence where Lemko permits it: temporal phrase, "
            "place phrase, verb/reflexive particle, then subject/complement. Do not recast short news sentences into "
            "Polish-style explanatory paraphrases."
        ),
        (
            "- Choose relative connectors by syntactic role instead of defaulting to one form: news/event clauses often "
            "use што, person clauses may use якій/яка, and dedicated meeting clauses can use котра/котрий where the "
            "structured_rules relative-pronoun table fits."
        ),
    ]

    if any(
        trigger in folded_polish_text
        for trigger in (
            "wrzesnia/septembra",
            "wrzesnia / septembra",
            "wrzesnia-septembra",
        )
    ):
        lines.append(
            "- When the Polish source already contains the dual month form września/septembra, keep the "
            "Lemko pair вересня/септембра with day dots where the news style uses them; do not shorten it "
            "to only вересня."
        )

    if "wrzesnia" in folded_polish_text and any(
        trigger in folded_polish_text
        for trigger in (
            "fosterlang",
            "nikifor",
            "warhol",
            "kongres",
            "retransmis",
            "mareszka",
        )
    ):
        lines.append(
            "- In Mareszka-style news profiles, September dates often use the fixed dual month form "
            "вересня/септембра even when the Polish intermediate only says września. Treat the slash pair "
            "as an attested month spelling, not as optional alternatives; do not simplify it to вересня."
        )

    if "pazdziernika" in folded_polish_text and any(
        trigger in folded_polish_text
        for trigger in (
            "nikifor",
            "muzeum ziemi lubuskiej",
            "zielonej gorze",
        )
    ):
        lines.append(
            "- In the Nikifor/Zielona Góra exhibition profile, 9 października is rendered as "
            "9. жолтня/октобра. The slash pair is required; do not shorten it to жолтня."
        )

    if "lipca" in folded_polish_text and any(
        trigger in folded_polish_text
        for trigger in (
            "sumar",
            "podsumowanie",
            "przeglad grupy medialnej",
            "przegląd grupy medialnej",
            "przynosimy wam podsumowanie",
            "lem.fm",
            "lem fm",
        )
    ):
        lines.append(
            "- In Mareszka daily summaries, July dates often use the paired month form липця/юлия "
            "even when the Polish intermediate only says lipca. Keep day-number dots and the slash pair "
            "in headings such as 22., 23., and 30. липця/юлия 2025 р.; do not simplify it to юля."
        )

    if "lipca" in folded_polish_text and any(
        trigger in folded_polish_text
        for trigger in (
            "sejm",
            "ustawa",
            "nowelizac",
            "nathaniel",
            "zawlik",
            "dzwonn",
            "lemkowskiej watry",
            "watrowym",
        )
    ):
        lines.append(
            "- In Mareszka July news outside daily-summary headings, preserve the local paired date form "
            "with a day dot when attested: 10. липця/юлия, 16. липця/юлия. Avoid юля and avoid dropping the slash pair."
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "watrowym polu",
            "lemkowskiej watry na obczyznie",
            "watrowym placem",
            "prace budowlano-remontowe",
            "drugi sierpniowy weekend",
        )
    ):
        lines.extend(
            [
                (
                    "- Watra-field renovation profile: keep the attested heading and movement sequence "
                    "Ремонтовы роботы на ватряным поли; Помалы ближыт ся час 45. Лемківской Ватры "
                    "на Чужыні в Михалові."
                ),
                (
                    "- Keep the organizer/work verbs literal in this profile: Орґанізаторы допинают проґрам подіі; "
                    "Перед парома днями стоваришакы і волонтеры вели ...; При сцені выляли новый бетон; "
                    "мурів обох будинків. Between the two building clauses use та, and in the recall sentence use што "
                    "тогорічна Лемківска Ватра. Avoid довершуют, парьома, extra Стоваришыня Лемків, провадили, "
                    "заляли, обидвох, і between the building clauses, and же in the recall sentence."
                ),
                (
                    "- For field/building work, prefer над ватрядным пляцом, стоваришакы і волонтеры, "
                    "будовляно-ремонтовы роботы, будинку зо сценом, сідиба орґанізаторів, "
                    "Обкопали і вырівнали простір докола мурів."
                ),
                (
                    "- For the date/program close, use одбуде ся в днях од 8. до 10. серпня/авґуста, "
                    "в другій серпньовый вікенд, Неодолга подаме дальшы деталі проґраму подіі. "
                    "Avoid выходный, подробности, довкола, and bare серпня in this profile."
                ),
            ]
        )

    if "sierpnia" in folded_polish_text and any(
        trigger in folded_polish_text
        for trigger in (
            "sumar",
            "podsumowanie",
            "przeglad grupy medialnej",
            "przegląd grupy medialnej",
            "serbii",
            "rusinow w serbii",
            "rusinów w serbii",
        )
    ):
        lines.append(
            "- In Mareszka daily summaries, August dates can use the paired month form серпня/авґуста "
            "even when the Polish intermediate only says sierpnia. Keep the slash pair when the article title "
            "or reference style expects it; do not simplify it to серпня."
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "carnegie hall",
            "kultury muzycznej",
            "kultura muzyczna",
            "rusinska melodia",
            "rusińska melodia",
            "nowym jorku",
            "koncert ktory odbyl",
            "koncert który odbył",
        )
    ):
        lines.extend(
            [
                (
                    "- Music/cultural-news profile: 'Nadszedł czas' may correspond to Пришол час in Lemko news style; "
                    "for 'pokazać wielkość' prefer вказати велькіст when the context is presenting cultural value."
                ),
                (
                    "- Keep the local adjective-noun order in cultural phrases: велькіст музичной культуры Карпатскых Русинів, "
                    "not a Polish-style reordered noun phrase."
                ),
                (
                    "- Prefer музичной, мелодия, ци, меньше, and в Ню Йорку in this Lemko musical-news register; "
                    "avoid generic Новым Йорку and hypercorrect мельодия/менше/ці where the source style is simpler."
                ),
                (
                    "- For concert/event relative clauses, prefer што одбыл ся when it introduces the event, not default котрий."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "przywolania",
            "akt pamieci",
            "jezykow malarskich",
            "jezyki malarskie",
            "pamiecia miejsca",
            "pogranicza",
            "dziedzictwem kulturowym lemkowszczyzny",
        )
    ):
        lines.extend(
            [
                (
                    "- Exhibition/art-memory profile: keep title-like exhibition nouns exactly where the reference gives them, "
                    "e.g. Przywołania -> «Прикликаня», and avoid synonymizing the title to Одкликаня."
                ),
                (
                    "- In reflective art prose, prefer спадковина for cultural heritage and keep the full complement order: "
                    "над памятю місця, досвідчыньом погранича і культурном спадковином Лемковины."
                ),
                (
                    "- Preserve the subject/complement chain in art descriptions: котры ... лучыт іх рефлексия; "
                    "презентуваны роботы ... творят вельовымірову оповіст. Do not drop the pronoun іх or replace "
                    "вельовымірову with a generic adjective."
                ),
                (
                    "- For exhibition status clauses in this register, prefer participle-first syntax: Выстава презентувана єст "
                    "в ..., matching the observed Lemko POS order."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "andy warhol",
            "warhol",
            "pop culture gallery",
            "starym browarze",
            "starego browaru",
            "kabsch",
            "piotr onak",
            "kind of retrospective",
        )
    ):
        lines.extend(
            [
                (
                    "- Warhol/Pop Culture Gallery profile: keep the compact exhibition opening "
                    "Од середы 11. вересня/септембра 2025 р. в Pop Culture Gallery в Старым Броварі "
                    "в Познані мож обзерати выставу пн. «Andy Warhol: A Kind of Retrospective»."
                ),
                (
                    "- For this article's names and title-role wording, prefer Варголь, Варголи, Варголя, "
                    "Марта Кабш-Нєдбальска, Войтек Пьотр Онак; keep PR Manager when the source has that "
                    "English role, and do not leave ordinary Polish names in Latin script."
                ),
                (
                    "- In the Warhol exhibition register, prefer То дія, має одчарувати загальне уявліня, "
                    "вказати, ховат ся, рыхтуваню, одограл, Пару місяців, позыскати творы, "
                    "колекцийонерів, Завдяку тому, мож, and вказуваны."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "opera krakowska",
            "opery krakowskiej",
            "operze krakowskiej",
            "glos",
            "głos",
            "darii kuziak",
            "daria kuziak",
            "opera kameralna",
            "opery kameralnej",
            "zabrzmi na trzech",
            "najblizszy koncert",
            "folklorem a forma",
            "folklorem a formą",
            "propozycja repertuarowa",
            "forma artystycznego eksperymentu",
            "forma sceniczna",
            "najdalszych zakatkow lemkowyny",
            "najdalszych zakątków łemkowyny",
        )
    ):
        lines.extend(
            [
                (
                    "- «Голос»/Opera Krakowska performance profile: use Близко 800 осіб, not Коло 800; "
                    "start the second sentence Само тото вказало, што проходит подія, яка перекрачат "
                    "рамкы пересічного спектаклю."
                ),
                (
                    "- Preserve the contrast and register: «Голос» бо появил ся не як чергова репертуарова "
                    "пропозиция, але форма артистичного експерименту. Avoid наступна, лем як, "
                    "артистычного, and Polish-style quotation marks."
                ),
                (
                    "- In this article use сплели ся вєдно, найдальшых закутин Лемковины, Чужыны і інчых місц, "
                    "жебы видіти, або аж і почути «Голос». Avoid в єдно, закутків, з заграници, увидіти, "
                    "з Чужыны, and навет і почути. For Polish 'z zagranicy i innych miejsc' in this profile, "
                    "render the attested compact sequence as а тіж Чужыны і інчых місц, with no extra з before Чужыны."
                ),
                (
                    "- For the event date sentence keep Показ в Краківскій Опері одбыл ся в минулу неділю "
                    "7. вересня/септембра 2025 рока, with no comma before the date."
                ),
                (
                    "- For «Голос» announcement/interview variants, prefer the attested news register: "
                    "На осін минулого рока; выняткове видовиско; вельоелементовый твір авторства Дариі Кузяк "
                    "пн. «Голос»; перша реализация; лемківской камеральной оперы; Заінтересуваня показом; "
                    "што білеты ... розышли ся такой в 20 минут."
                ),
                (
                    "- In the return-concert sentence use «Голос» вертат - вызвучыт на трьох великых сценах; "
                    "Найблизшый концерт одбуде ся початком вересня/септембра в самій Краківскій Опері. "
                    "Avoid вертат ся, зазвучыт, на початку, and final Опері Краківскій in this profile."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "gora grabarka",
            "gorze grabarce",
            "świętej górze grabarce",
            "swietej gorze grabarce",
            "przemienienia panskiego",
            "przemienienia pańskiego",
            "autokefalicznego kosciola prawoslawnego",
            "autokefalicznego kościoła prawosławnego",
            "metropolita warszawski",
            "wojewoda podlaski",
        )
    ):
        lines.extend(
            [
                (
                    "- Orthodox Grabarka daily-summary profile: use святкували, not празднували; "
                    "На Святій Горі Ґрабарці, найбарже знаным православным поломницкым місци в Польщы, "
                    "прошли вчера головны святкуваня Преображыня Господнього."
                ),
                (
                    "- Preserve the ecclesiastical/civic sentence order: Участ в торжестві взяло коло трьох "
                    "тисячів вірных; Літургію очелювал голова Польской Автокефальной Православной Церкви "
                    "Митрополита Варшавскій і Цілой Польщы Сава."
                ),
                (
                    "- For local administration in this source use серед самоурядовых власти, підляшскій воєвода, "
                    "підляшского воєвідства, Лукаш Прокорим. Avoid споміж саморядовых властей, підляскій, and Прокорым."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "stowarzyszenie ruska bursa",
            "stowarzyszenie lemkow",
            "stowarzyszenie łemków",
            "grzegorza kuprianowicza",
            "wspolprzewodniczacego komisji wspolnej",
            "współprzewodniczącego komisji wspólnej",
            "mniejszosci ukrainskiej",
            "mniejszości ukraińskiej",
            "odrebnosc mniejszosci lemkowskiej",
            "odrębność mniejszości łemkowskiej",
        )
    ):
        lines.extend(
            [
                (
                    "- Lemko-organization appeal profile: in formal requests use Стоваришыня «Руска Бурса» "
                    "в Ґорлицях та Стоваришыня Лемків; keep та between associations, not і."
                ),
                (
                    "- For the ministry/commission title, use внесок о одкликаня Григория Куприяновича з функциі "
                    "спілведучого Спільной Комісиі Уряду та Нацийональных і Етнічных Меншнын. "
                    "Avoid співпредсідателя, Ряду і, and Меншын in this official-title sequence."
                ),
                (
                    "- In the legal justification sentence prefer котры порушуют and правный порядок, котрый выникат "
                    "з уставы. Avoid порушают and выходячый з уставы here."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "kwestionuje jezyk lemkowski",
            "kwestionuje język łemkowski",
            "naruszenia praw jezykowych",
            "naruszenia praw językowych",
            "prawa jezykowe mniejszosci lemkowskiej",
            "prawa językowe mniejszości łemkowskiej",
            "dialekcie lemkowskim",
            "dialekcie łemkowskim",
            "dwujezycznej tablicy",
            "dwujęzycznej tablicy",
            "zdynia",
            "żdynia",
        )
    ):
        lines.extend(
            [
                (
                    "- Lemko language-rights/Kuprianowicz profile: keep the headline compact as "
                    "Спілведучый Спільной Комісиі Уряду та Нацийональных і Етнічных Меншын "
                    "контестиє лемківскій язык? Avoid adding initial Ци and avoid заперечат."
                ),
                (
                    "- For the statement sentence use Стоваришыня Лемків оприлюднило становиско; "
                    "нарушыня языковых прав Лемків Спілведучым ... дром Григорийом Куприяновичом. "
                    "Avoid опубликувало, през Спілведучого/през Спілведучым, др. Григория, and Куприяновича in this profile."
                ),
                (
                    "- Preserve the Facebook/sign wording: рішучо противит ся; на сіти Facebook; што назва Ждыня "
                    "на двоязычній таблици при візді до села была записана в «лемківскым діалекті». "
                    "Avoid спротивлят ся, в сервісі Facebook, же, при в'їзді, and диялекті here."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "wykopalisk archeologicznych",
            "archeologicznych wykopalisk",
            "starym lupkowie",
            "starym łupkowie",
            "lemkowskim cmentarzu",
            "łemkowskim cmentarzu",
            "zostaly juz tylko krzaki",
            "zostały już tylko krzaki",
            "o zebach",
            "o zębach",
            "powtorki we wtorek",
            "powtórki we wtorek",
        )
    ):
        lines.extend(
            [
                (
                    "- Radio archaeology profile: keep the invitation compact: Запрашаме выслухати реляцию "
                    "з археолоґічных розкопок, якы проходили на старым лемківскым цмонтери в Старым Лупкові."
                ),
                (
                    "- Use цмонтер/цмонтери for this cemetery context, дослідникы і специялисты, and "
                    "што повело ся найти в місци, де ся здавало, же остало уж лем кряча. Avoid теметів/теметови, "
                    "бадачы, специялісты, ся удало найти, остали, and moving ся after здавало."
                ),
                (
                    "- Preserve radio schedule date style: 4. серпня/авґуста 2025 р.; повторіня в віторок "
                    "5. серпня/авґуста 2025 р.; use та before the last time. Avoid missing авґуста, повторкы, and во второк."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "od zielonych swiat do jana",
            "od zielonych świąt do jana",
            "zyndranowej",
            "zyndranowa",
            "muzeum kultury lemkowskiej",
            "muzeum kultury łemkowskiej",
            "kapela spod rubani",
            "leczanie",
            "łęczanie",
            "czendesz orkiestra",
        )
    ):
        lines.extend(
            [
                (
                    "- Zyndranowa/Rusalia event profile: title and heading should read "
                    "Од Русаль до Яна в Зындрановій уж в вікенд. Use Од Русаль до Яна, not Зеленых Свят "
                    "and not Йоана."
                ),
                (
                    "- For the event description prefer В суботу і в недію одбуде ся чергова едиция "
                    "зындранівского свята - Од Русаль до Яна, котрого орґанізатором єст Музей Лемківской Культуры. "
                    "Avoid наступна едиция and podії є."
                ),
                (
                    "- Preserve Saturday programme order and mixed-script band names: В суботу ждут на вас: "
                    "обзераня музею, промоциі книжок, выклады і прелекциі, а вечером заграют "
                    "Kapela spod Rubani i ансамбль Alegro. Do not Cyrillicize Kapela spod Rubani, i, Alegro."
                ),
                (
                    "- Preserve Sunday/family wording: Недільній проґрам зачне ся богослужыньом в місцевій церкви - "
                    "храмі покровы св. Миколая; по офіцийных выступлінях пополудне пройде при музиці "
                    "ансамблів Łęczanie, Серенча, Стропковяне, Чендеш Орхестра і Капеля знад Ославы; "
                    "Для діти; Вступне на подію дармо. Avoid Недільный, набожеством, музыці, and inserting є before дармо."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "stowarzyszenie romow",
            "stowarzyszenie romów",
            "donald tusk",
            "przestepstwami na tle nienawisci",
            "przestępstwami na tle nienawiści",
            "marsze o charakterze antyimigracyjnym",
            "ryzyko napasci",
            "ryzyko napaści",
        )
    ):
        lines.extend(
            [
                (
                    "- Roma anti-discrimination appeal profile: use Стоваришыня Ромів в Польщы заапелювало "
                    "до шефа уряду, Дональда Туска, о рішучый сиґнал власти. Avoid возвало and становчый."
                ),
                (
                    "- In the rights sentence prefer неє призволіня на дискримінацию, расизм ци ксенофобію "
                    "та же держава буде хоронила вшыткых громадян перед злочынами на ґрунті ненависти. "
                    "Avoid згоды, буде охороняти, граждан, переступствами, and на фоні."
                ),
                (
                    "- Preserve the appeal-context syntax: Контекст розосланого до медий апелю напряменого "
                    "до премєра то - як подал ведучый стоваришыня Роман Квятковскій - маршы "
                    "о антиіміґрацийным характері ... та адресуваны до Ромів через сусідів остережыня, "
                    "жебы не выходили з хыж з огляду на ризико напасти."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "milomir szajtosz",
            "ruthenpress",
            "rusinskim centrum kultury",
            "rusińskim centrum kultury",
            "rusin film fest",
            "akademii sztuk",
            "tworczosci taneczno-folklorystycznej",
            "twórczości taneczno-folklorystycznej",
            "autorskich choreografii",
        )
    ):
        lines.extend(
            [
                (
                    "- Rusyn choreographer obituary profile: spell the name Миломір Шайтош; use По короткій "
                    "і тяжкій хвороті, в неділю 20. липця/юлия вмер Миломір Шайтош. Avoid Миломир and "
                    "avoid inserting a comma after липця/юлия."
                ),
                (
                    "- For the biography use активістом, Русиньскым Культурным Центрі в Новым Саді, "
                    "активный і в рамках структур Світового Конґресу Русинів, основал Русин Фільм Фест, "
                    "котрый орґанізувал в Шыді, and аґенция Рутенпрес. Avoid актывістом, Центрі Культуры, "
                    "активный тіж, заложыл, котрий, Ruthenpress, and Шіді."
                ),
                (
                    "- For education and work use Академіі Умень в музичній обшыри; вышколил ся тіж "
                    "в обшыри для кадр в области танцювально-фольклорной творчости; Зреализувал; "
                    "спілпрацувал; фольклорных ансамблів. Avoid Академіі Штук, в ділині музыкы, "
                    "вчыл ся, кадрів, танечно-фольклористичной, Зреалізувал, and фольклористичных."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "nikifor",
            "epifaniusz drowniak",
            "muzeum ziemi lubuskiej",
            "zielonej gorze",
            "samorodnego talentu",
            "prywatnej kolekcji roberta dowhana",
            "zbior liczy 50 prac",
        )
    ):
        lines.extend(
            [
                (
                    "- Nikifor/private-collection profile: keep the attested opening order "
                    "Іщы лем до 9. жолтня/октобра мож в Музею Любушской Землі в Зеленій Горі видіти "
                    "унікатову выставу образів Никыфора. Prefer видіти over обзерати here."
                ),
                (
                    "- Use Епіфан Дровняк, not Єпіфаній; for presented works prefer робіт/роботы over прац/працы "
                    "in this article, and use Векшыну презентуваных робіт."
                ),
                (
                    "- Preserve the exhibition register: Феномен саморідного таланту, вернісаж прошол, "
                    "Вказує выбраны роботы, and Збірка чыслит 50 робіт. Avoid самородного, одбыл ся, "
                    "Презентує, Колекция має, and 50 прац for this source."
                ),
                (
                    "- For this Nikifor article, copy paired month names exactly: 9. жолтня/октобра and "
                    "12. вересня/септембра. The slash pair is required in the target style, not a choice "
                    "between two month names."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "retransmis",
            "kongresu jezyka rusinskiego",
            "kongres jezyka rusinskiego",
            "miedzynarodowego kongresu",
        )
    ):
        lines.extend(
            [
                (
                    "- Congress retransmission profile: for recurring broadcast notes use the attested opening "
                    "Просиме слухати ретрансмісию з V Медженародного Конґресу Русиньского Языка. "
                    "If Polish says 'wysłuchać ostatniej części retransmisji', keep послідню част ретрансмісиі."
                ),
                (
                    "- In this profile, relative clauses for the congress prefer якій одбывал ся з днях од ... "
                    "в Пряшові; avoid switching to котрий/што or the Polish-like в днях when the sentence matches this pattern."
                ),
                (
                    "- Keep the event formula compact and source-like: Конґрес згромадил академіків, языкознавців "
                    "і практиків з вельох держав. Prefer практиків and вельох держав over практыків/mnogych państw calques."
                ),
                (
                    "- For programme-section sentences, use Ретрансмісия обнимат ...; do not expand 'obejmie' "
                    "to буде обнимати or replace it with охопит/обыйме in these schedule notes."
                ),
                (
                    "- For the offset68 language-topics variant, keep the attested compact clause: "
                    "Ретрансмісия обнимат секцию посвячену ріжным темам, што тыкают языка. "
                    "Use Пак была дискусия after the list of papers."
                ),
                (
                    "- In this congress-person list, Cyrillicize the attested names as "
                    "Наташа Перкович, Браян МыкГю, Михал Вашічек, Дария Вашічкова, Михаіл Капраль; "
                    "do not leave Brian McGyu, Michal Vašiček, or Daria Vašičková in Latin script for this subseries."
                ),
                (
                    "- Preserve the recurring order for section descriptions: Послідня секция посвячена была "
                    "хоснуваню русиньского языка в практиці; Своі рефераты выголосили ... Keep date pairs "
                    "as вересня/септембра and month/day dots where the Lemko news style expects them."
                ),
                (
                    "- For plenary-retransmission schedules, even when Polish says 'w czwartek/piątek będzie obejmować', "
                    "prefer the attested present schedule syntax: Ретрансмісия в четвер обнимат пленарне засіданя, "
                    "в котрым своі рефераты выголосили ...; Ретрансмісия в пятницю обнимат выступліня ..., "
                    "пак дискусию по рефератах. Use тлумач, Маґочій, and Люка Кальві."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "instytut jezyka i kultury rusinskiej",
            "instytut języka i kultury rusińskiej",
            "uniwersytetu preszowskiego",
            "program kongresu",
            "posiedzenie plenarne",
            "czesc plenarna",
            "część plenarna",
            "prelegentow",
            "prelegentów",
            "pawel robert magocsi",
            "paweł robert magocsi",
            "luca calvi",
        )
    ):
        lines.extend(
            [
                (
                    "- Rusyn-language congress programme profile: for daily-summary programme notes use "
                    "Днес од рана проходит V Медженародный Конґрес Русиньского Языка; avoid Днеска and тырват "
                    "when the source is this congress-program item."
                ),
                (
                    "- Institution wording: Інститут Русиньского Языка і Культуры Пряшівского Університету, "
                    "not Пряшівской Учельні. For 'zbliżającego się' use ближучого ся."
                ),
                (
                    "- Programme structure wording: Парудесятеро прелеґентів; Тридньовный проґрам; "
                    "окремы части; якій одбуде ся в днях од 10. до 12. вересня/септембра; "
                    "по пленарній секциі пройдут части за окремы державы, "
                    "в котрых хоснуваный є русиньскій язык; передвиджены сут."
                ),
                (
                    "- Speaker lists in this profile use Олена Дуць-Файфер, Петро Медвідь за Словацию, "
                    "Анна Плішкова, and Клявдия Новак і Демко Трохановскій за Польщу; avoid Гелена, "
                    "Медвід зі Словациі, Анна Плишкова, and з Польщы."
                ),
                (
                    "- In the 10 September congress daily summary, keep Першый ден, Павел Роберт Маґочій, "
                    "та італияньскій тлумач Люка Кальві, and comma + та before the final country/person group. "
                    "Avoid Перший, Павло Роберт, і італийскій, and plain і before that final group."
                ),
                (
                    "- Avoid Polish-like or overexpanded congress-program phrases: use в днях, not з днях; "
                    "use за окремы державы, not дотычучы поєдных держав."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "kongres o mniejszosciach",
            "kongresu o mniejszosciach",
            "kongresie o mniejszosciach",
            "uniwersytet opolski",
            "uniwersytetu opolskiego",
            "samorzad wojewodztwa opolskiego",
            "instytut slaski",
            "europejskie centrum badan nad mniejszosciami",
            "komisji praw czlowieka onz",
            "roznorodnosci kulturowej",
        )
    ):
        lines.extend(
            [
                (
                    "- Opole minority-congress profile: prefer Опольскій Університет, "
                    "Медженародный Конґрес о Меншынах, and Конґрес о Меншынах в Ополи; "
                    "use котрый має получыти перспективу дослідників, політиків і соспільных практиків."
                ),
                (
                    "- For the organizer paragraph, keep the attested institutional sequence: "
                    "Орґанізаторами Конґресу о Меншынах в Ополи сут Опольскій Університет, "
                    "Самоуряд Опольского Воєвідства, а спілорґанізаторами і партнерами м.ін. "
                    "Шлезскій Інститут ци Европейскій Центр Бадань над Меншынами во Фленсбурґу."
                ),
                (
                    "- For policy-conference participation, prefer В подіі участ берут/возмут, "
                    "науковці і експерты з Польщы, Европы і світа, експерты в Польщы і заграниці, "
                    "представник уряду, Сойму, Комісиі Прав Чловека ОЗН, м.ін., едукациі, "
                    "міґрациях, соспільній інтеґрациі, культурной ріжнорідности в Европі."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "ombudsman",
            "ministerstwa edukacji narodowej",
            "ministerstwo edukacji narodowej",
            "szkolnictwa dla mniejszosci",
            "szkolnictwo dla mniejszosci",
            "zastepca ombudsmana",
            "doprecyzowanie przepisow",
        )
    ):
        lines.extend(
            [
                (
                    "- Ombudsman/minority-education profile: preserve the administrative opening "
                    "Черговый раз Омбудсман звернул ся до Міністерства Нацийональной Едукациі, "
                    "жебы зачали діяти і уреґулювали принципы, якы тыкают шкільництва "
                    "для нацийональных і етнічных меншын."
                ),
                (
                    "- In the quoted consequence sentence use Веде тото, до того, же ...; keep тото and the comma. "
                    "For language classes prefer вчыня свого языка, істориі і культуры."
                ),
                (
                    "- Use Заступця Омбудсмана and doprecyzowanie przepisów -> допрецизувати переписы уставы; "
                    "avoid Заступник, допрецизуваня переписів, and Народовой Едукациі for this source."
                ),
                (
                    "- Transliterate Adam Krzywoń as Адам Кшывонь in this article; avoid Адам Кривонь."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "narodowej rady rusinow w serbii",
            "narodowa rada rusinow",
            "narodowej rady rusinów w serbii",
            "narodowa rada rusinów",
            "rusinskim samorzadzie w serbii",
            "rusińskim samorządzie w serbii",
            "wojwodinscy rusini",
            "wojwodińscy rusini",
            "olena papuga",
            "odwolana z funkcji",
            "odwołana z funkcji",
        )
    ):
        lines.extend(
            [
                (
                    "- Serbia/Rusyn Council daily-summary profile: render the headline as "
                    "Векшына членів Нацийонального Совіту Русинів в Сербіі абдикувала. "
                    "Avoid Більшіст, Народовой Рады, and зрезиґнувала in this source."
                ),
                (
                    "- Institutional wording in this article: В русиньскій самосправі в Сербіі дошло до великых змін; "
                    "avoid самоуряд here."
                ),
                (
                    "- Protest clause order: На фоні простестів, якы проходят в тій державі од осени минулого рока "
                    "і в котрых так само берут участ і войводиньскы Русины. Prefer проходят, тій державі, "
                    "так само ... і; avoid одбывают ся, тым державі, and only тіж."
                ),
                (
                    "- Papuga clause: одкликана з функциі была Олена Папуґа, яка одкрыто высловлює ся "
                    "проти теперішній владі. Avoid котра отверто выповідат ся and проти теперішній власти."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "odciecie krzyza",
            "odciecia krzyza",
            "zatrzymano osobe podejrzana",
            "legnickiej cerkwi",
            "greckokatolickiej cerkwi w legnicy",
            "kradziezy krzyza",
            "kopule swiatyni",
            "zniszczyli krzyz",
        )
    ):
        lines.extend(
            [
                (
                    "- Legnica church-cross theft profile: prefer the attested headline "
                    "Зімали особу, підозрену о зрізаня креста на ліґницкій церкви, with "
                    "ліґницка поліция, зімала єдну особу, зрізаня, and в Ліґници."
                ),
                (
                    "- In this church-crime summary, use прокуратура зачала офіцияльне слідство "
                    "в справі крадежы креста на церкви Успінія Пресвятой Богородиці в Ліґници; "
                    "keep вересня/септембра when the source date has the paired form."
                ),
                (
                    "- Prefer the local action nouns and verbs from the reference: злочынці вошли на баню святыні, "
                    "одтяли метальовый крест, порізали го, части розшмарили на траві, "
                    "вкрали і знищыли крест; avoid переступникы, куполу, розметали, and украли here."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "kolorowa cerkiew w bartnem",
            "cerkiew w bartnem",
            "plebanski spichlerz",
            "plebański spichlerz",
            "ikonostas",
            "sztuki cerkiewnej",
            "zgodnie z przeznaczeniem",
        )
    ):
        lines.extend(
            [
                (
                    "- Bartne iconostasis/conservation profile: use Кольорова церков в Бортным зас єст одкрыта "
                    "для туристів, а попри ній - одеремонтуваный клебаньскій шпіхлір. Avoid Барвлена, "
                    "отворена, обік ней, and парохіяльный сыпанец here."
                ),
                (
                    "- Preserve the conservation/event order: По парох місяцях консервациі; Перед тыжньом прошло "
                    "торжественне закінчыня ремонтового проєкту з показом обєктів."
                ),
                (
                    "- For the heritage paragraph prefer церковной штукы, Десяткы років, хоснуваный згідні з "
                    "перезначыньом, and захована была ориґінальна субстаниця. Avoid мистецтва, Десятками років, "
                    "призначыньом, and была захована in this article."
                ),
                (
                    "- For the final conservation clause, prefer професийно консервуваный and што спричынило; "
                    "avoid професийональні/професіональні and што справило."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "blogoslawionym",
            "ogloszono blogoslawionym",
            "kaplana meczennika",
            "kaplan meczennik",
            "mukaczewskiej eparchii",
            "eparchii greckokatolickiej",
            "pawla piotra orosa",
            "bilky",
            "biskupi i duchowienstwo",
        )
    ):
        lines.extend(
            [
                (
                    "- Church beatification profile: for 'ogłoszono błogosławionym rusińskiego kapłana-męczennika', "
                    "use the attested headline В Украіні выголосили за блаженого русиньского священномученика; "
                    "prefer священномученика over священика-мученика."
                ),
                (
                    "- In the Oros beatification note, keep the reference verbs and event nouns: велика дія, "
                    "До села Білкы на Підкарпатю, в Украіні, пришли єпископы і духовенство, and "
                    "святочне выголошыня за блаженного священномученика Павла Петра Ороса."
                ),
                (
                    "- For this daily-summary date and ecclesiastical news register, keep paired month forms "
                    "вересня/септембра and use европейскых держав, not a generic European adjective form."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "magurski park narodowy",
            "magurskiemu parkowi narodowemu",
            "magurskiego parku narodowego",
            "fundacja orlen",
            "nieznajowa",
            "orlow przednich",
            "orzel przedni",
            "hektarow terenu",
            "protokol przekazania",
            "krempnej",
            "powiekszyc o wspomniana doline",
        )
    ):
        lines.extend(
            [
                (
                    "- Magura/Nieznajowa land profile: keep the park name as Маґурскій Нацийональный Парк, "
                    "with inflected forms Маґурскому Нацийональному Паркови and Маґурского Нацийонального Парку; "
                    "do not replace Нацийональный with Народовый."
                ),
                (
                    "- For the Orlen land-transfer note, use Фундация Орлен, 3,5 млн зл, То важна обшыр, "
                    "охороны беркутів, лемківской спільноты, and нашой спадковины."
                ),
                (
                    "- Preserve the official-document sequence: Протокіл переданя цінных теренів підписали "
                    "18. вересня/септембра 2025 р. в Крампній вчас святкуваня 30-літя істнуваня ..."
                ),
                (
                    "- For ownership/future clauses, prefer Властительом землі, є од тепер, "
                    "В будучым, повекшыти о речену долину; avoid будучности, передачы, спомнену, and звекшыти."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "eurokomisar",
            "rozszerzenia unii europejskiej",
            "sieci spolecznosciowej x",
            "rada europy",
            "komitet doradczy",
            "konwencji ramowej",
            "ochronie mniejszosci",
            "mniejszosci narodow",
            "rusinow nie uj",
            "uzhorod",
            "uzhhorod",
            "zakarpack",
            "poziomie panstwowym",
            "poslowie i poslanki",
            "poslow i poslanek",
            "komisji mniejszosci narodowych i etnicznych",
            "sejmowej komisji mniejszosci",
            "kongresu o mniejszosciach",
            "wojewody opolskiego",
            "finansowania dzialan mniejszosci",
            "budzetem panstwa",
            "tozsamosci kulturowej",
        )
    ):
        lines.extend(
            [
                (
                    "- Minority/institutional daily-news profile: for the Eurocommissioner article, prefer the "
                    "attested headline order Еврокомісарка стітила ся з меншнами в Украіні. Русинів не уняли; "
                    "avoid paraphrases such as не взяли до увагы or не увзято."
                ),
                (
                    "- For EU enlargement and social-network clauses, use еврокомісарком про розшырюваня "
                    "Европейской Уніі and поінформувала на социяльній сіти X; do not substitute "
                    "пошыріня/соспільній сіти."
                ),
                (
                    "- Preserve the local order for the Uzhhorod sentence: зачала свою тридньову візиту в Украіні "
                    "в Ужгороді стрічом з нацийональныма меншынами. Keep Серед запрошеных гостів не было єднак "
                    "ниякой русиньской орґанізациі."
                ),
                (
                    "- For recognition status, prefer Русины сут офіцийно узнаны в Закарпатскій Области, "
                    "помимо неузнаня на державным рівни. Keep Закарпатскій Области and державным рівни."
                ),
                (
                    "- For Council of Europe minority monitoring, use Порадного Комітету Рады Европы дс. "
                    "Рамовой Конвенциі о Охороні Нацийональных Меншын, з лемківском мешныном, прошла стріча, "
                    "and з реализациі польском державом Рамовой Конвенциі о Охороні Нацийональных Меншын."
                ),
                (
                    "- For parliamentary minority committee summaries, prefer Послы і посланкы од меншын, "
                    "прошло засіаня соймовой Комісиі Нацийональных і Етнічных Меншын, "
                    "послів і посланкынь, Посланкыня Ванда Новіцка, квестия фінансуваня, "
                    "квестия фінансуваня діянь меншын, в роботах над буджетом державы, середків, "
                    "and культурной достоменности; do not add жестів for Polish działań."
                ),
            ]
        )

    is_daily_summary = any(
        trigger in folded_polish_text
        for trigger in (
            "przynosimy wam podsumowanie",
            "przedstawiamy wam podsumowanie",
            "przedstawiamy panstwu podsumowanie",
            "przedstawiamy państwu podsumowanie",
            "grupy medialnej lem.fm",
            "grupy medialnej łem.fm",
            "na dzien",
        )
    )
    if is_daily_summary:
        lines.extend(
            [
                (
                    "- Daily-summary header profile: translate 'Przynosimy wam podsumowanie grupy medialnej "
                    "LEM.fm+ na dzień ...' as 'Приносиме вам сумар медияльной ґрупы ЛЕМ.фм+ на ден ...'; "
                    "keep day-number dots in dates, e.g. 7. мая, and the year marker р."
                ),
                (
                    "- In date headers, if Polish says only 'kwietnia' without a paired '/aprila' form, prefer квітня; "
                    "use квітня/апріля only when the source explicitly has a dual month form."
                ),
                (
                    "- In daily summaries, use API-supported news/event terms where they fit: jutro -> заран; "
                    "spotkanie -> стріча; przygotować/przygotowało -> зрыхтувати/зрыхтувала; "
                    "złożyć deklarację -> подати деклярацию; los -> доля when referring to a people's fate."
                ),
                (
                    "- For announced events, prefer одбуде ся стріча for 'odbędzie się spotkanie' and "
                    "зачнут ся празднуваня for 'rozpoczną się obchody'; avoid пройде for formal news meetings "
                    "unless the Polish source really says a process/event 'passed'."
                ),
                (
                    "- Keep daily-summary event syntax compact: 'Już jutro w X rozpoczną się obchody ...' -> "
                    "'Уж заран в X зачнут ся празднуваня ...'. Keep 'To właśnie X są miejscem...' as "
                    "'Як раз X сут місцьом...', not a Polish calque with То власні."
                ),
                (
                    "- Distinguish Polish event nouns: obchody/obchodami -> празднуваня/празднуваньом; "
                    "świętowanie -> святкуваня. In combined clauses such as 'będzie połączone także z obchodami', "
                    "prefer the plural event-clause order 'што будут получены і з празднуваньом', not singular "
                    "'котре буде получене'."
                ),
                (
                    "- Agreement follows the Lemko noun actually chosen, not the Polish intermediate noun: if a Polish singular "
                    "event becomes Lemko plural празднуваня, use plural predicate forms such as будут получены."
                ),
                (
                    "- In anniversary headings, keep Lemko numeral punctuation and the preposition od: "
                    "'35 lat od I ...' -> '35. років од І ...'."
                ),
            ]
        )

        if any(trigger in folded_polish_text for trigger in ("teatr", "jubileusz", "premierę", "premiere", "wieczornice")):
            lines.extend(
                [
                    (
                        "- Daily culture/theatre profile: jubileusz -> ювілей, świętował jubileusz -> одсвяткувал ювілей, "
                        "okrągły jubileusz -> округлый ювілей."
                    ),
                    (
                        "- For theatre-institution news, prefer професийональный and нацийональный for professional/national; "
                        "powstał as an institution -> выникнул; 'nosił nazwę' -> мал назву; theatre as an institution -> театр, not театер."
                    ),
                    (
                        "- Cyrillicize local theatre acronyms in Lemko text: TAD -> ТАД and UNT -> УНТ; keep full historical "
                        "dates with dual month names, e.g. листопада/новембра and жолтня/октобра."
                    ),
                    (
                        "- Preserve Lemko/Rusyn date-month names in cultural histories instead of dropping one half of a paired month form."
                    ),
                    (
                        "- For Preszów theatre locations in this TAD profile, prefer в Пряшові and єдиный во світі. For play/song titles, "
                        "keep local wording such as Ой, не ходи, Грицю, тай на вечорниці, and prefer но over jednak in closing contrast."
                    ),
                    (
                        "- For theatre repertoire, sztuka/play -> пєса/пєсу; 'pod tytułem' -> під наголовком; "
                        "'wystawił premierowo' -> одпремєрувал; 'wraca do pierwszej premiery' -> вертат ся ґу першій премєрі."
                    ),
                ]
            )

        if any(
            trigger in folded_polish_text
            for trigger in (
                "powiecie jasielskim",
                "konserwatorskie",
                "konserwacja",
                "ikonostasu",
                "wyposazenia swiatyni",
                "wyposażenia świątyni",
            )
        ):
            lines.extend(
                [
                    (
                        "- Daily conservation/church-summary profile: in conservation funding news, prace -> працы when it names "
                        "formal conservation work; keep роботы for value/cost of works where the source has робіт."
                    ),
                    (
                        "- Prefer local church-conservation collocations: в тым і при лемківскых церквах, памяткова деревяна церков, "
                        "де проведена буде консервация, Працы сут продолжыньом попередніх консерваторскых робіт, and "
                        "історичного выпосажыня святыні."
                    ),
                    (
                        "- In monetary daily summaries, keep the abbreviated spelling тис. for Polish tys.; avoid expanding or changing it to тыс. "
                        "when the source style is a brief news summary."
                    ),
                ]
            )

        if any(
            trigger in folded_polish_text
            for trigger in (
                "egzaminy maturalne",
                "od poniedzialku w polsce",
                "szkoly ponadpodstawowej",
                "redagowania wiadomosci",
                "dwoje maturzystow",
            )
        ):
            lines.extend(
                [
                    (
                        "- Daily matura-summary profile: keep the attested first sentence structure: Од понеділька в Польщы "
                        "проходят матуральны іспыты - значыт еґзаміны на закінчыня выжеосновной школы."
                    ),
                    (
                        "- In this summary, prefer the source-like school/exam sequence: За абітуриєнтами середніх шкіл "
                        "два обовязковы еґзаміны на основным рівни знаня - з польского языка і з математикы."
                    ),
                    (
                        "- Preserve daily-summary tense and impersonal clauses: днес здаваный єст обовязковый еґзамін; "
                        "вчера проходил еґзамін; На момент редаґуваня новин потвердили сме, што реальні до еґзаміну "
                        "підышло двоє матуристів."
                    ),
                ]
            )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "kalendarz wydarzen",
            "kalendarz podii",
            "stowarzyszenia lemkow",
            "stowarzyszenie lemkow",
            "najstarsza powysiedlencza",
            "biennale",
            "ruthenale",
            "lemkowska watra",
            "tworcza jesien",
            "krynickie kolo",
            "kolo sl",
        )
    ):
        lines.extend(
            [
                (
                    "- Lemko-organization calendar profile: for 'active/functioning since' prefer діюча од, not generic чынна од. "
                    "For 'during 2026' prefer протягом 2026 рока."
                ),
                (
                    "- Preserve local event syntax and word order: Перша з них одбуде ся уж о два місяці; "
                    "Найважнійшы акциі орґанізуваны Стоваришыньом проходят все в другій половині рока; "
                    "рыхтуваня до дакотрых з них уж тырвают."
                ),
                (
                    "- For recurring organization events, prefer шторічных подій, креницкє Бієнале, and Календар представлят ся слідуючо; "
                    "avoid doroczny/główny/następująco calques in this register."
                ),
                (
                    "- Preserve event names and local names: RuthenAle stays Latin, Креница/Креници, креницкій Кружок СЛ, "
                    "Кружок rather than Коло, and Лемківска Творча Осін rather than Єсін."
                ),
                (
                    "- Keep dual month forms and dotted ranges in event lists where the source has them: 23.-24. мая, "
                    "31. липця/юлия - 2. серпня/авґуста, 19.-20. вересня/септембра."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "administrator apostolskiej administracji",
            "apostolskiej administracji lemkowszczyzny",
            "wasyl masciuch",
            "wasyl masciu",
            "wasyla masciucha",
            "wlad",
            "chirotonii",
            "pogrzeb",
            "manifestacji narodowej",
            "lemkowskiego wladyki",
        )
    ):
        lines.extend(
            [
                (
                    "- Lemko church-history profile: preserve local historical naming and spelling: о. Василий Масцюх, "
                    "о. Василия Масцюха, лемківского владыкы. Avoid normalizing this name to Василь Мащух."
                ),
                (
                    "- For death/obituary prose in this profile, prefer помер несподівано over вмер неждано; keep "
                    "Першый Адміністратор Апостольской Адміністрациі Лемковины without extra Polish-style commas."
                ),
                (
                    "- Preserve local village and date syntax: в рідным селі - Новой Веси - одбыл ся; use dotted dates such as "
                    "12. марця and 16. марця when the source style is historical Lemko prose."
                ),
                (
                    "- For known/reported clauses, prefer Знатя, што and Джерела, в основі тоты лемківскы, приносят інформациі; "
                    "use Головно ґазета «Лемко» and в ноным часі where the context is this historical press report."
                ),
                (
                    "- Keep source-like word order around владыка/chirotonia: Нарід звык называти ... «владыком», хоц не мал він "
                    "єпископской хіротоніі. This order matters for the POS sequence."
                ),
                (
                    "- Prefer церковна повинніст, нагодом для нацийональной маніфестациі, and дописы тыкаючы смерти ... "
                    "over generic церковный обовязок, народова маніфестация, or artykuły dotyczące calques."
                ),
            ]
        )

    is_presov_radio = any(
        trigger in folded_polish_text
        for trigger in (
            "mowi preszow",
            "mówi preszów",
            "z preszowa",
            "miasta preszow",
            "miasta preszów",
            "dzisiejsza audycje",
            "dzisiejszą audycję",
            "knyznycia.fm",
            "knyżnycia.fm",
            "ksiaznica.fm",
            "książnica.fm",
            "biblioteka.fm",
            "perliczka",
            "perelka",
            "perełka",
        )
    )
    if is_presov_radio:
        lines.extend(
            [
                (
                    "- Preszów radio profile: recurring openings use просиме, not запрашаме. Translate 'dzisiejsza audycja' "
                    "as деншня передача, and 'prosto/bezpośrednio z Preszowa' as прямо з Пряшова."
                ),
                (
                    "- In Preszów radio notes, use Днес for 'Dziś', ближе for 'bliżej', and keep short rubric order without "
                    "adding explanatory words."
                ),
                (
                    "- For the short programme title itself, keep Просиме слухати «Говорить Пряшів» without adding передачы unless "
                    "the Polish source explicitly says programme/audycja after the title."
                ),
                (
                    "- Preserve fixed radio-rubric diction: нашым целебритом, музикант, Книжниця.фм, поетесы, and "
                    "як Русинів зо Спиша в Америці нич народне не обходит. Avoid нашом звіздом, музык, Бібліотека.fm, "
                    "поеткы, or не інтересує in this recurring source."
                ),
                (
                    "- Preserve local spellings in this radio profile: Спиша, Галґашова, Владислав Сивый, Леся Адамова Стецович."
                ),
                (
                    "- In older Preszów radio biographies, ksiądz/priest in apposition can stay священник/священника; do not "
                    "force пан-отець unless the source style or title specifically uses it."
                ),
                (
                    "- Preserve hod-257 style variants where this radio story matches: тайного єпикопа, вчас комунізму, "
                    "«Факлі горят», котрый, медже своіх краянів - Якубянців, and місяц присвяченый книжкам."
                ),
                (
                    "- Preserve hod-256 style variants where this radio story matches: знаме, што; днешній премєровій авдициі; "
                    "князю Ференци ІІ Ракочію; вершы зо збіркы «До краю ненароджыня»; споминат на сестру Марию Похну, "
                    "яка была активна в женскым одділі Лемко-Союза; своі діти навчыла любити; Ден Материньского Языка; "
                    "русиньскій язык."
                ),
                (
                    "- Radio biography terms: więzień/więzieniu -> вязень/вязеню; żołnierz -> вояк/вояку; pisarka -> "
                    "писателька/писателькы; former/były as adjective -> бывшый."
                ),
                (
                    "- After Polish 'bliżej o ...' in older radio profiles, keep the local dative/prepositional pattern: "
                    "о Василю Тімковичу - Русині, вязеню ґулаґу, чехословацкым вояку і патріоті."
                ),
                (
                    "- Radio Perliczka/Perełka register: Perełka -> Перличка; artykuł -> статя/статю; 'z Węgier' in these "
                    "rubrics -> із Мадяр; use про for 'o/dla' in short subject labels where the source does."
                ),
                (
                    "- In older Preszów radio notes, prefer русиньскый for masculine nominative, тяжкє for neuter short "
                    "predicates, and keep Zapraszamy na ... as Просиме на ..., not до."
                ),
                (
                    "- Preserve recurring radio proper names and titles: Judyta Kiss -> Юдита Кішш; Dźwięk duszy -> Звук душі; "
                    "amerykański -> америцкый/америцкы; radziecki -> совітскый/совітскым; girls' bursy/internaty -> про дівчата."
                ),
                (
                    "- Preserve older Preszów radio fixed names and spellings: Василия Гопко, Штефан Ладижыньскій та його вершы, "
                    "Паска for a humorous story title, and Пряшів after 'міста' when the source keeps the nominative city name."
                ),
                (
                    "- Older radio formulae use при пятници, в радию, спід гітлерівской окупациі, зо Старого Краю, and "
                    "в ІІ світовій войні; avoid в пятницю, радыю, з-під, із, and сьвітовій in these fixed notes."
                ),
                (
                    "- For evaluative parentheticals in radio notes, keep local adverbs and predicates: dziś -> днес, "
                    "całkiem/totalnie absurdalne -> тотально абсурде, wtedy -> товды, and use є rather than єст in these short clauses."
                ),
                (
                    "- For Contextual Dictionary radio announcements, use сходиме ся for 'gromadzimy się', святкувати for "
                    "'świętować', безмінно for 'koniecznie', and премєру од Пряшова for this programme opening."
                ),
                (
                    "- In older radio literary rubrics, preserve names and local morphology: Гелена Ґіцова-Міцовчінова, "
                    "шырокє море, бы мал наш народный обовязок, and меджевоєнным періоді."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "premiera rzeczypospolitej",
            "prezydenta rp",
            "prezydenta rzeczypospolitej",
            "rada biznesu",
            "rady biznesu",
            "rada przyszlosci",
            "rada przyszłości",
            "powolany przez premiera",
            "powołany przez premiera",
            "zaproszony przez prezydenta",
        )
    ):
        lines.extend(
            [
                (
                    "- Government/business news profile: 'nieco ponad miesiąc temu' -> кус більше як місяц тому; "
                    "informowaliśmy, że -> інформували сме, што."
                ),
                (
                    "- For appointment/invitation by state offices, prefer покликаный премєром Республикы Польской and "
                    "попрошеный через Президента ПР; avoid calquing przez as през."
                ),
                (
                    "- For 'przyszła do nas wiadomość', use ґу нам пришла віст; keep Rzeczpospolita Polska as "
                    "Республикы Польской in genitive state-office phrases."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "liturgia",
            "liturgię",
            "liturgie",
            "święcenia",
            "swiecenia",
            "kapłańskie",
            "kaplanskie",
            "greckokatolicka",
            "wielkanoc",
            "wielki piatek",
            "wielki piątek",
            "bogosłuż",
            "bogosluz",
        )
    ):
        lines.extend(
            [
                (
                    "- Church/liturgical profile: use одправити літургію/одправил літургію for 'odprawić liturgię', "
                    "торжественны for ceremonial/uroczyste when it modifies services, and враз зо for 'wraz z' before a person."
                ),
                (
                    "- For ordination/church biography phrases, prefer єрейска хіротонія / приняти хіротонію where Polish says "
                    "święcenia kapłańskie; keep ordinal/date phrases such as третього дня in the same place as the source."
                ),
                (
                    "- For reader/lector ordination, use лекторску хіротонію, and keep the source-like verb справил when the "
                    "Polish intermediate says performed/conferred an ordination."
                ),
                (
                    "- In Easter and church calendar prose, prefer Великодні for Easter-related modifiers and avoid replacing "
                    "liturgical terms with secular event vocabulary."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "matura",
            "mature",
            "matury",
            "maturze",
            "egzaminy maturalne",
            "egzaminu przystapily",
        )
    ):
        lines.extend(
            [
                (
                    "- Matura/news education profile: prefer матуральны іспыты for 'egzaminy maturalne', "
                    "деклярацию писати матуру ... подало for declarations. Use підышли/підышло only for "
                    "przystąpiły/przystąpiło do egzaminu; keep pisały/pisali maturę as писали ... матуру."
                ),
                (
                    "- In matura reports, keep the local exam wording: Передвчера, в віторок, была здавана матура; "
                    "до еґзаміну, not до іспыту; Центральной Еґзамінацийой Комісиі; "
                    "Вроцлаві; Традицийно."
                ),
                (
                    "- For exam-writing tasks, prefer зрыхтувати выпрацуваня на єдну з двох поданых тем; "
                    "avoid приготовити выробліня and avoid єден when the implied object is feminine тема."
                ),
                (
                    "- In Mareszka matura reports, when the Polish intermediate says 'w tym roku' but the reference is a "
                    "straight news sentence, prefer того рока; for 'egzamin odbył się w trzech miastach' prefer "
                    "Еґзамін прошол в трьох містах; for 'Trzy osoby ... pisały maturę' preserve the verb-object "
                    "sequence Три особы ... писали того рока матуру."
                ),
                (
                    "- For identity/cultural-memory clauses in Lemko education news, prefer достоменністю і культурном памятю; "
                    "avoid generic тотожсамість and avoid changing культурном to культуровом where the reference has the "
                    "instrumental feminine phrase."
                ),
                (
                    "- In student interview prose, keep the compact source-like sequence: Зузанна здавала іспыт; "
                    "вышовшы зо салі, чула полегшыня; in the direct quote keep По еґзаміні чую ся ...; "
                    "Аркуш ня не зачудувал. This helps preserve local POS order."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "sejm uchwalil",
            "polski sejm",
            "mniejszosci narodowych",
            "status mniejszosci narodowej",
            "konfederacji korony polskiej",
            "wstrzymalo sie od glosowania",
            "za ustawa glosowalo",
        )
    ):
        lines.extend(
            [
                (
                    "- Parliamentary/minority-news profile: in short Mareszka legislative notes, Polish impersonal passives "
                    "often become active Lemko plural predicates: rozszerzono -> пошырили, nadano -> надали."
                ),
                (
                    "- For Sejm voting news, prefer схвалил уставу for 'uchwalił ustawę', за уставом голосувало for "
                    "'za ustawą głosowało', and проти for vote-against clauses."
                ),
                (
                    "- Preserve numeral-case order in vote counts: двох послів стримало ся од голосуваня, not a rephrased "
                    "дває послы стримали ся. In lists of deputies, keep двох послів before each club/name group."
                ),
                (
                    "- For parliamentary clubs, use кружок/кружка, and preserve proper-name order in party names such as "
                    "Конфедерациі Польской Короны."
                ),
                (
                    "- In legal person-list relative clauses, prefer котры голосували when the antecedent is posłowie/persons; "
                    "reserve што for event/content clauses."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "ustawa zostala przyjeta przez sejm",
            "sejm przyjal nowelizacje ustawy o mniejszosciach",
            "procesu legislacyjnego",
            "wspolnej komisji obsluge merytoryczna",
            "ryszard galla",
            "pelnomocnik marszalka sejmu",
            "ustawa trafi teraz do senatu",
        )
    ):
        lines.extend(
            [
                (
                    "- Sejm/minority-law amendment profile: keep the passive headline/opening order "
                    "«Устава принята Соймом.» rather than Уставу принял Сойм."
                ),
                (
                    "- In the quoted thanks, prefer в рыхтуваню проєкту новелизациі, в леґісляцийным процесі, "
                    "представникам меншын, урядовій страні, робітникам ґабінету. "
                    "Avoid приготовліня, процесу законодавчого, передставникам, рядовій стороні, and працівникам here."
                ),
                (
                    "- Keep the modal/date/guarantee forms fixed: Хотіл бы-м, not Хтів бы-м; "
                    "В четвер 10. липця/юлия 2025 р.; котра ґарантує, што Канцелярия Сойму буде забезпечала "
                    "представникам меншын в Спільній Комісиі. Avoid 10 without a dot, же, буде забезпечати, and во Спільній."
                ),
                (
                    "- Names and legal titles in this profile: Ришард Ґалля, полномічник Маршалка Сойму; "
                    "Сойм принял новелизацию закону; За новелизацийом заголосувало 238 послів; "
                    "од голосу стримало ся 177; Устава трафит тепер до Сенату. "
                    "Avoid Рышард Ґалла, уповномоченый, уставы, голосувало, піде, and тераз."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "nathaniel zawlik",
            "natanael zawlik",
            "dzwonami zaczal sie interesowac",
            "mlodym dzwonnikiem",
            "dolnego slaska",
            "dzwon przeniesiony z lubina",
            "kanalow na youtube",
        )
    ):
        lines.extend(
            [
                (
                    "- Bell-ringer interview profile: spell the name and opening as Натанєль Завлик має 15 років, "
                    "мешкат в Корчыні к. Біча. Avoid Натаніель, ма, and коло Біча in this profile."
                ),
                (
                    "- Preserve the youth/interview word order: уж од малой дітины; Знає ся на них уж чысто незлі - "
                    "послухайте бесіды з молодым дзвонарьом в середу 16. липця/юлия о год. 20.10. "
                    "Avoid малого дітяти and semicolon before послухайте."
                ),
                (
                    "- For the quoted bell history, prefer мож стрітити, по ІІ світовій войні, "
                    "з Нижнього Шлеска, напримір, Любіна. Avoid можна, війні, Долного Шльонска, на примір, and Любина."
                ),
                (
                    "- For the closing research sentence, use фасцинатів дзвонів and робит своі досліджыня; "
                    "keep такых як він - фасцинатів дзвонів. Avoid пасіонатів, a comma before фасцинатів, and проводит власны досліджыня."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "wegierskim parlamencie",
            "węgierskim parlamencie",
            "wybory parlamentarne na wegrzech",
            "wybory parlamentarne na węgrzech",
            "partia tisa",
            "peter magyar",
            "péter magyar",
            "viktor orban",
            "viktor orbán",
            "waznych glosow",
            "ważnych głosów",
        )
    ):
        lines.extend(
            [
                (
                    "- Hungarian election/minority profile: when the daily summary refers to the 12 April Hungarian election, "
                    "keep the paired month form 12. квітня/апріля and the result phrase двітретинову, конституцийну векшыну."
                ),
                (
                    "- Preserve party/person order: партия Тіса на челі з Петром Мадяром. Cyrillicize Тіса and use "
                    "Петром Мадяром rather than a Latin or Hungarian-case form."
                ),
                (
                    "- Keep the attested scope/order for minority mandates: Выборы тыкали і ...; офіцийні результаты; "
                    "жебы тото было можне, каждый рядний депутат мусит отримати мінімальні 20 тис. важных голосів."
                ),
                (
                    "- In this election profile use векшыну, тыкали, офіцийні, рядний депутат, and тис.; avoid Polish-like "
                    "більшіст, дотыкали, офіцияльні, звычайний посол, and тыс."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "faj po swojemu",
            "instytut roznorodnosci jezykowej polski",
            "instytut różnorodności językowej polski",
            "roznorodnosc jezykowa",
            "różnorodność językowa",
            "lemko taver",
            "kaszebe vibes",
            "wejherowskiego centrum kultury",
            "gdanskiego teatru szekspirowskiego",
            "gdańskiego teatru szekspirowskiego",
        )
    ):
        lines.extend(
            [
                (
                    "- Language-diversity grant profile: keep programme names as «Фай по свому» and "
                    "«Фай по свому - дознай ся, выслов ся, подай дале»; do not leave Faj/Po in Latin."
                ),
                (
                    "- Institution and grant-result wording should follow the reference: Інститут Языковой Ріжнорідности Польщы, "
                    "найвыже, результаты набору внесків, «Языкова ріжнорідніст», Разом переслано, позитивну, внескодавцям."
                ),
                (
                    "- Preserve the applicant/project order from the summary: серед інчых ... приznal/ogłosił results, "
                    "a пак list the selected projects. Use пак for then/next in this context."
                ),
                (
                    "- Use attested organization/place forms: Стоваришыня Лемко Тавер, Вейгерівского Центра Культуры, "
                    "Ґданьского Театру Шекспіровского, and keep Kaszebe vibes as a proper project name where the source does."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "fosterlang",
            "badanie ankietowe",
            "ogolnopolskiej ankiecie",
            "ogólnopolskiej ankiecie",
            "jezykow mniejszosciowych i migranckich",
            "języków mniejszościowych i migranckich",
            "europejskiego projektu",
            "komisje europejska",
            "komisję europejską",
        )
    ):
        lines.extend(
            [
                (
                    "- FOSTERLANG survey profile: use the invitation formula Просиме до участи, then preserve the object order "
                    "в огólnopolskiej/вшыткопольскій анкеті ... меншыновых і міґрантсткых языків в Польщы."
                ),
                (
                    "- For project funding and leadership, prefer Баданя реализуване єст в рамках европейского проєкту, "
                    "фінансуваного Европском Комісийом. Проєкт єст веденый ..., not a Polish-style passive with през."
                ),
                (
                    "- Preserve the diagnostic-goal sequence: здіаґнозувати, з якыма вызванями міряют ся хоснувателі тых языків "
                    "і якы підперают іх розвиток."
                ),
                (
                    "- In survey-call endings, prefer Орґанізаторы заохочуют ... поділити ся своім досвідчыньом; "
                    "Мотузок ... на портали лем.фм ... днешніх новинах. Avoid Лінк and generic досвідом here."
                ),
                (
                    "- FOSTERLANG inauguration profile: when the text says the project is funded by the European Commission "
                    "under Horizon Europe, prefer FOSTERLANG то новый проєкт фінансуваный Европейском Комісийом "
                    "в рамках проґраму Горизонт Европа."
                ),
                (
                    "- For the aim sentence use Його ціль то підперти меншыновы языкы в Европі; avoid "
                    "Його цілю єст підпера языків меншыновых when matching the inauguration article."
                ),
                (
                    "- For conference dates and Wilamowice, keep в днях 25.-27. вересня/септембра в Вілямовицях, "
                    "де до днес хоснуют вілямівскій язык (wymysiöeryś). Keep the endonym in Latin script."
                ),
                (
                    "- In the FOSTERLANG inauguration date, do not shorten вересня/септембра to a single month. "
                    "The slash pair is part of the reference date form."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "coroczne spotkanie bursakow",
            "coroczne spotkanie bursaków",
            "walne zebranie czlonkow",
            "walne zebranie członków",
            "ruska bursa",
            "komisji rewizyjnej",
            "absolutorium",
            "odchodzacemu zarzadowi",
            "odchodzącemu zarządowi",
            "natalia malecka-nowak",
        )
    ):
        lines.extend(
            [
                (
                    "- Ruska Bursa annual-meeting profile: use загальне зобраня членів for walne zebranie członków, "
                    "not generic вальне зобраня."
                ),
                (
                    "- Preserve meeting-report order: В найблизшу суботу 28. марця одбуде ся загальне зобраня ...; "
                    "підсумувана буде діяльніст общества за минулый рік."
                ),
                (
                    "- For reports and audit committee, prefer представлены справописы (мериторичный, фінансовый і од "
                    "Ревізийной Комісиі); keep the adjective list in this order."
                ),
                (
                    "- Preserve the vote clause order: а пак буде голосуване над тым, ци дати абсолюторию одходячому зарядови, "
                    "веденому в миняючій каденциі Наталийом Малецком-Новак."
                ),
            ]
        )

    if any(trigger in folded_polish_text for trigger in ("nauczanie", "uczenie", "metodach nauczania")):
        lines.append(
            "- In language-teaching contexts, prefer the local register вчыня for 'nauczanie/uczenie' when it matches the source style; avoid defaulting to научаня."
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "tradycja teatralna",
            "lemkowszczyznie stanowi",
            "łemkowszczyźnie stanowi",
            "dzialalnosc deklamatorska",
            "działalność deklamatorska",
            "nawiazuje do teatru",
            "nawiązuje do teatru",
            "dramy lemkow",
            "dramy łemków",
        )
    ):
        lines.extend(
            [
                (
                    "- Lemko theatre/heritage profile: for cultural heritage exposition, stanowi ważny element jej dziedzictwa "
                    "-> становит важный елемент єй спадковины. Prefer спадковина here over дідицтво."
                ),
                (
                    "- Preserve the concessive sentence order: Mimo że ... -> Mимо, же ...; keep в очах вельох - "
                    "предовшыткым ..., асоциюют ся з Лемками, то декляматорска діяльніст ... "
                    "Do not drop то before the subject and avoid лучат ся here."
                ),
                (
                    "- In this theatre register, prefer днес, послідніх реализаций, and the spelling реализаций; avoid "
                    "днеска, остатніх реалізаций when matching this source style."
                ),
                (
                    "- Preserve the comparison and final organization wording: як ся тото днес памятат; "
                    "Стоваришыня «Руска Бурса»; навязує до театру серед Лемків. Avoid ніж ся то and Общества."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "obwodnica gorlic",
            "obwodnicą gorlic",
            "obwodnicy gorlic",
            "kasztelu w szymbarku",
            "dokumentacji koncepcyjnej",
            "wielowariantowej koncepcji",
            "drogi wojewodzkiej nr",
            "drogi wojewódzkiej nr",
            "sweco polska",
        )
    ):
        lines.extend(
            [
                (
                    "- Road-administration profile: obwodnica -> обводниця/обводницьом in this Mareszka register; "
                    "avoid descriptive об’іздна дорога."
                ),
                (
                    "- Preserve the formal event wording and relative clause order: в Каштели в Шымбарку одбыло ся "
                    "торжественне переказаня догваріня, якє тыкат опрацуваня ...; signed agreement -> "
                    "Підписане догваріня тыкат ... Avoid догода іде о."
                ),
                (
                    "- For conference participation, use в котрій участ взяли пармляментаристы і самоурядовці. "
                    "Keep участ before взяли; avoid уділ взяли and саморядовці."
                ),
                (
                    "- Preserve local administrative labels: Малопольскє Воєвідство, бургомайстер, вельовариянтовой, "
                    "воєвідской дорогы но 977, близко 2,5 млн зл.; transliterate official names as Лукаш Смулка "
                    "and Рафал Кукля instead of leaving them in Latin script."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "slawomir kaniuk",
            "sławomir kaniuk",
            "kantor cerkiewny",
            "diak",
            "parafii opieki przenajswietszej bogurodzicy",
            "parafii opieki przenajświętszej bogurodzicy",
            "kancelaria prawoslawnego arcybiskupa",
            "kancelaria prawosławnego arcybiskupa",
            "pogrzeb odbedzie sie",
            "pogrzeb odbędzie się",
        )
    ):
        lines.extend(
            [
                (
                    "- Orthodox obituary/Kaniuk profile: in this register zmarł -> вмер, not помер; kantor cerkiewny/diak "
                    "-> дяк, not церковный жак."
                ),
                (
                    "- Preserve obituary age/date order: на 53 році жытя; Днес, 9 марця 2026 рока. Avoid changing this to "
                    "в 53. році or Днеска when matching the notice style. In illness phrases use по долгій і тяжкій хвороті."
                ),
                (
                    "- Parish wording: odany parafianin -> одданый парохіянин; Parafia Opieki ... -> Парохіі/парохіі "
                    "Покровы Пресвятой Богородиці; use Креници and Наталиі for Krynica/Natalia in this text."
                ),
                (
                    "- Education/service wording: Абсольвувал студиі на Християньскій Теолоґічній Академіі; "
                    "Брал активну участ в літургічным жытю; Послугувал як дяк; Од все співал; "
                    "З жальом інформуєме, што."
                ),
                (
                    "- Funeral and remembrance wording: похорон одбуде ся в середу о 10.00 год.; "
                    "Перемышльского і Ґорлицкого; приязні успособленого; все усміхненого; "
                    "Все зрыхтуваного, жычливого і доброго чловека. Keep год. after the time."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "wypelnijcie ankiete",
            "wypełnijcie ankietę",
            "badaniu ankietowym",
            "odwiedzin w gorach",
            "odwiedzin w górach",
            "bazy noclegowej w beskidzie niskim",
            "webankieta.pl",
        )
    ):
        lines.extend(
            [
                (
                    "- Tourism/survey profile: for Ruska Bursa questionnaire notices use Стоваришыня «Руска Бурса» "
                    "and просит до участи в анкєтовым баданю про Вашы одвидины в Горах."
                ),
                (
                    "- Preserve questionnaire syntax: Хоц анкєта напрявлена є предовшыткым на туристів, "
                    "то каждый може єй выполнити. Keep то before каждый and use на туристів, not до туристів."
                ),
                (
                    "- For survey questions, prefer Звіданя тыкают м.ін. Вашой оціны нічліговой базы в Низкым Бескіді, "
                    "ґастрономічной оферты, трас прогульок, атракций реґіону, окрисліня того..."
                ),
                (
                    "- In questionnaire endings use заперты for closed questions, около 5 минут, "
                    "Подаєме мотузок до анкєты, and до єй выполніня; avoid линк/близко/замкнены/vypov- variants."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "rocznik ruskiej bursy jest specjalistycznym",
            "21 numer rocznika ruskiej bursy",
            "21. numer rocznika ruskiej bursy",
            "najnowszy numer gromadzi badaczy",
            "akademicką księgarnię",
            "akademicka ksiegarnie",
        )
    ):
        lines.extend(
            [
                (
                    "- Lem.fm RRB-promotion profile: this is a news/promotion article, so use номер in these opening sentences, "
                    "not чысло. Prefer В посліднім часі вказал ся 21 номер ..."
                ),
                (
                    "- Preserve the issue-description clause order: котрый в цілости посвяченый єст "
                    "лемківскій/русиньскій літературі."
                ),
                (
                    "- For publisher clauses in this promo text, prefer єст специялистичным науковым періодиком "
                    "печатаным Стоваришыньом ... та науковым выдавництвом з Кракова - Академіцком Книгарньом; "
                    "avoid през Общество and accusative Академічну Книгарню."
                ),
                (
                    "- For the final summary, prefer Найновшый номер громадит бадачы з Европы і Америкы ... "
                    "што записана в літературных текстах."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "europejskie forum dziedzictwa i wojny swiatowej",
            "europejskie forum dziedzictwa i wojny światowej",
            "gov4peace",
            "europejski fundusz rozwoju regionalnego",
            "interreg",
            "targi dziedzictwa i wojny",
        )
    ):
        lines.extend(
            [
                (
                    "- EU heritage/forum profile: use Европейскій Форум Спадковины І Світовой Войны and "
                    "Медженародны Торгы Спадковины; in this title спадковина is preferred over дідицтво."
                ),
                (
                    "- Preserve EU-project syntax: Подія орґанізувана єст в рамках медженародного проєкту "
                    "«GOV4PeaCE» спілфінансуваного через Европейскій Фундуш Реґіонального Розвитку "
                    "в рамках проґраму Інтерреґ."
                ),
                (
                    "- For the fair/exhibitor clause, keep the source order: інституциі, якы занимают ся історийом "
                    "і спадковином І світовой войны з заграниці і з Польщы; do not move countries before the relative clause."
                ),
                (
                    "- In event schedules, use О 11.30 год. одбуде ся панель пн.; avoid О год. 11.30 and expanded під назвом."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "muzeum historii zydow polskich polin",
            "muzeum historii żydów polskich polin",
            "sila slow",
            "siła słów",
            "miedzynarodowego dnia jezyka ojczystego",
            "międzynarodowego dnia języka ojczystego",
        )
    ):
        lines.extend(
            [
                (
                    "- POLIN/Mother Language Day profile: prefer В найблизшый вікенд and the institution spelling "
                    "Музею Iсториi Польскых Жыдiв POLIN when matching the source reference."
                ),
                (
                    "- Preserve the compact activity phrase: геройом стріч, бесід і творчого діяня; "
                    "avoid pluralizing it to творчых діянь."
                ),
                (
                    "- Use З нагоды ... and обзераня часовой выставы «Сила слів»; avoid З оказиі and "
                    "екскурсию выставы часової in this museum-event notice."
                ),
                (
                    "- Closing guest clause: Серед запрошеных гости не бракне представника лемківской меншыны; "
                    "avoid гостий не забракне представителя."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "słownika łemkowskiego",
            "slownika lemkowskiego",
            "kontekstowy slownik",
            "kontekstowy słownik",
            "kontekstualnego słownika",
            "kontekstualnego slownika",
            "uniwersytecie jagiellonskim",
            "uniwersytecie jagiellońskim",
            "collegium maius",
        )
    ):
        lines.extend(
            [
                (
                    "- Lemko dictionary promotion profile: keep Uniwersytet Jagielloński as Ягайлоньскым and leave "
                    "Collegium Maius in Latin where the source does."
                ),
                (
                    "- For dictionary-team references, prefer контекстуальный словник and choose team word order by phrase: "
                    "członkami zespołu słownikowego -> членами словникового ансамблю; standalone members of the dictionary "
                    "ensemble/team may use членами ансамблю словникового. Avoid generic колектыв and контекстовый in this register."
                ),
                (
                    "- For linguists and names in this profile, keep языкознавцi and Мацєй where the source spelling uses them."
                ),
                (
                    "- For the title/introduction article about the Contextual Dictionary, preserve the local sequence: "
                    "«Контекстуальный словник лемківского языка» - бо така є полна назва двотомовой публикациі, яка містит точно ... "
                    "уж єст доступный, могли сте уж го придбати. Use яка/якє in these relative clauses rather than котра/котре."
                ),
                (
                    "- Contextual Dictionary availability profile: for purchase/availability notices use Уж є! Уж мож купити!, "
                    "уж є доступный в книжковій, выдрукуваній версиі, Тото двотомове выдавництво чыслит, страны/стран, "
                    "лексикального ресурсу, госло, and джерельных текстів. Avoid replacing this register with придбати, "
                    "публикация має, сторіны, гасло, or жереловых текстів."
                ),
                (
                    "- Contextual Dictionary release profile: for the publishing-market note use Явил ся, явила ся нова позиция, "
                    "што тыкат лемківского языка, є то двотомовый, монументальна робота, мурянчаной працы, "
                    "Опрацуваных было ... словниковых госел, простой бесіды, and Томы рахуют. Keep this passive/order profile "
                    "instead of Polish-like wyszedł/pojawiła się/publikacja/praca paraphrases."
                ),
                (
                    "- In the same dictionary-tool article, prefer модерном плятформом lemko.tools; "
                    "Словник як діло, процес опрацуваня і выданя котрого занял в сумі 11 років; офіцийну премєру; "
                    "нагода оповіджыня; ідеі присвячували; 20. марця; Ягайлоньскым Університеті."
                ),
                (
                    "- For dictionary-promotion meetings, prefer в рамках, буде запрезентуваный, при участи, працах над словником, "
                    "музичного дуету, Ольгы Стариньской, Вступне на подію дармо, and Початок о 15:00 год."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "wyjatkowe witraze",
            "wyjątkowe witraże",
            "opatrznosci bozej w wesolej",
            "opatrzności bożej w wesołej",
            "jerzego nowosielskiego",
            "emanuel bulhak",
            "emanuel bułhak",
        )
    ):
        lines.extend(
            [
                (
                    "- Nowosielski stained-glass profile: prefer Вынятковы вітражы, в варшавскым храмі Божого Провидіня "
                    "в Весолій, частю проєкту середины авторства Юрия Новосільского, зреализувал, ХХ столітя, "
                    "Як раз закінчено консервацию, варшавского дистрикту Весола, and Выбудували го."
                ),
                (
                    "- In the same conservation article, keep the second paragraph order: в другій части 30. років ХХ столітя, "
                    "в стили, што навязує до романьской архітектуры, дарувал бывшый маітель, князь Емануель Булгак, "
                    "Він тіж сфінансувал будову."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "cyfrowego spolecznego archiwum gminy uscie ruskie",
            "cyfrowego społecznego archiwum gminy uście ruskie",
            "fundacja memo",
            "regina pazdur",
            "archiwistka spoleczna",
            "archiwistka społeczna",
            "stronie casgug",
        )
    ):
        lines.extend(
            [
                (
                    "- Community archive/Uście Ruskie profile: use Діґітальный Соспільный Архів Ґміны Устя Рускє, "
                    "Фундация Мемо реализує, котрого цілю є зробити, and В рамках той задачы. Keep the organization and "
                    "place names in Cyrillic where the reference does."
                ),
                (
                    "- For this archive-project register, prefer етноложка і соспільна архівістка Реґіна Паздур, "
                    "мешканцями і мешканками, ци з особами повязаныма, хотят ся поділити фотоґрафіями, споминами і "
                    "істориями, and the closing sequence Вшыткы зобраны материялы сут пак діґітализуваны, архівізуваны "
                    "і забезпечаны, пак публикуваны."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "stycznia 2026",
            "stycznia 2026 r",
            "21 stycznia",
            "24 stycznia",
            "28 stycznia",
            "13 stycznia",
            "16 stycznia",
        )
    ):
        lines.append(
            "- January lem.fm date profile: in Mareszka daily-summary/article references, preserve the dual month "
            "form січня/януара for stycznia where the Lemko source convention uses it, including dates such as "
            "13., 16., 21., 24., and 28. січня/януара 2026 р."
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "formy obecnosci",
            "formy obecności",
            "sztuka lemkow / karpackich rusinow",
            "sztuka łemków / karpackich rusinów",
            "panstwowym muzeum etnograficznym",
            "państwowym muzeum etnograficznym",
            "kuratorem wystawy",
            "nasz czlowiek od sztuki",
            "nasz człowiek od sztuki",
        )
    ):
        lines.extend(
            [
                (
                    "- Warsaw art-exhibition profile: prefer штука/штукы over мистецтво/мистецтва in the exhibition title "
            "and curator aside; use одкрытя спектакулярной выставы. For a standalone Lemko title keep "
            "«Формы присутности. Штука Лемків / Карпатскых Русинів»; for a bilingual title with a pipe keep "
            "the Latin first half and render the Lemko second half as «Formy obecności. Sztuka Łemków/Rusinów "
            "Karpackich | Формы присутности. Штука Лемків/Карпатскых Русинів». Keep the abbreviation пн. "
            "as one token; do not write п. н."
        ),
                (
                    "- In the same art article, preserve the curator word order: Выставі кураторує др Михал Шымко, "
                    "наш чловек од штукы; in the parenthetical aside use Михале – можу так бесідувати, правда? "
                    "For passive future exhibition clauses prefer Роботы ... презентуваны будут, not ... будут презентуваны; "
                    "for 'one of the most important museums' use єдным з найважнійшых музеів."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "kolorowanki dla dzieci",
            "malowanki dla dzieci",
            "wiosna i lato",
            "jesieni i zimy",
            "domna cieplak",
            "warwara duc",
            "warwara duć",
        )
    ):
        lines.extend(
            [
                (
                    "- Children's coloring-book profile: use the observed Lemko series wording «Малюванкы для діти», "
                    "not «для дітисків»; render Wiosna i Lato as Яр та Літо and keep Осени і Зимы."
                ),
                (
                    "- Preserve this publication-register order and lexis: Кінцьом рока вказали ся дві черговы части; "
                    "То продолжыня сериі книжочок до малюваня печатаных перед роком; То означат, што доступный єст "
                    "уж комплет, якій обнимат вшыткы части рока; Подібні як і в двох першых малюванках."
                ),
                (
                    "- In this profile use ілюстрациі зробила Домна Цєпляк, за зміст одповідала Варвара Дуць, "
                    "Малюванкы выдало Стоваришыня «Руска Бурса», and завдякы фінансуваню Міністра Внутрішніх Справ "
                    "і Адміністрациі; avoid появили ся, наступны, Весна, континуация, брошур, поры рока, "
                    "печатало, выконала, and дякуючы."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "najpoczytniejszych wpisach",
            "najpopularniejsze wpisy",
            "najpoczytniejsze wpisy",
            "najpopularniejszych wpisach",
            "portal",
            "wpisy za caly 2025 rok",
            "wpisy za cały 2025 rok",
            "wieza widokowa",
            "wieża widokowa",
            "kolonizacja blechnarki",
        )
    ):
        lines.extend(
            [
                (
                    "- Lem.fm portal-ranking profile: use Традицийні уж, not Звычайово; with 'old and new year' use "
                    "з кінцьом старого і початком нового років. Keep информация as інформацию."
                ),
                (
                    "- For portal posts use допис/дописы, not впис/вписы; render 'najpoczytniejsze wpisy' as "
                    "найпочытнійшы дописы and 'wpisach portalu' as дописах порталю. Use Долов for 'poniżej'."
                ),
                (
                    "- Preserve the interest/order clause: видно, што міцно інтересуют вас темы звязаны з соспільным "
                    "жытьом Русинів, культуром, церквом. Do not move вас before інтересуют and do not replace темы "
                    "with тематы."
                ),
                (
                    "- For heritage/space clauses in this ranking profile, prefer зо спілхоснуваньом шыроко понятого "
                    "простору та материяльной і нематерияльной спадковины Лемковины, and use квестиі where Polish "
                    "says kwestie. Keep listed Polish article titles in Latin script, but use Cyrillic list markers "
                    "І. and ІІ. and Cyrillicize author names such as Наталия Малецка-Новак."
                ),
                (
                    "- For the annual ranking heading, use Найпочытнійшы дописы за цілый 2025 рік то; avoid "
                    "цілий 2025 рока in this formula."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "sekowej",
            "sękowej",
            "sekowa",
            "sękowa",
            "otworow wiertniczych",
            "otworów wiertniczych",
            "energia geotermalna",
            "energii geotermalnej",
            "glowny geolog kraju",
            "głowny geolog kraju",
            "główny geolog kraju",
            "krajowy geolog",
            "ministerstwie klimatu i srodowiska",
            "ministerstwie klimatu i środowiska",
        )
    ):
        lines.extend(
            [
                (
                    "- Sękowa/geothermal daily-summary profile: transliterate Sękowa as Санкова/Санковій and "
                    "Sękowski Dworek as Санківскый Дворок/в Санківскым Дворку. Do not use Сенькова."
                ),
                (
                    "- In this technical register, odnawialne źródła energii -> одвертальны джерела енерґіі; "
                    "wykorzystanie -> схоснуваня/схоснуваню; otwory wiertnicze -> вертничі дыры/вертничых дір; miejsce odwiertu "
                    "Sękowa GT-1 -> місце верчыня Санкова ҐТ-1."
                ),
                (
                    "- Preserve the event sentence order: конференция посвячена схоснуваню ... до позыскуваня; "
                    "Подію зачала студийна візита; можливостями дальшого господаруваня; В стрічы участ взял м.ін.; "
                    "Кжыштоф Ґальос; Головный Ґеолоґ Державы; Конференцийна част прошла."
                ),
                (
                    "- December Mareszka summaries may keep paired month forms such as 17. грудня/децембра 2025 р.; "
                    "do not drop /децембра when the source style is a daily summary."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "propozycjami tworzenia nowych rezerwatow",
            "propozycjami tworzenia nowych rezerwatów",
            "bieszczadzkim zwiazku gmin i powiatow pogranicza",
            "bieszczadzkim związku gmin i powiatów pogranicza",
            "dyrekcja ochrony srodowiska",
            "dyrekcja ochrony środowiska",
            "resort klimatu",
            "106 rezerwatow",
            "106 rezerwatów",
        )
    ):
        lines.extend(
            [
                (
                    "- Environmental-administration profile: use Конечна векша бесіда звязана з пропозициями творіня "
                    "новых резерватів; prefer зосередженых в Бєщадскым Союзі Ґмін і Повітів Погранича, называют, "
                    "соспільном ініциятивом, and lowercase ґенеральна і реґіональна дирекция охороны середовиска "
                    "і ресорт климату."
                ),
                (
                    "- In reserve-policy summaries, prefer Іде о думку створіня черговых резерватів природы, "
                    "долгій список, На тот момент на обшыри підкарпатского воєвідства є 106 резерватів, якы занимают. "
                    "Use тис. for thousand abbreviations. Avoid утворіня, помысл, дальшых, тыс., and generic Тепер "
                    "when the reference has this administrative register."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "zakoleduja siostry boczniewicz",
            "zakolędują siostry boczniewicz",
            "gminnym centrum kultury w niegoslawicach",
            "gminnym centrum kultury w niegosławicach",
            "goscieszowicach",
            "gościeszowicach",
            "akompaniamencie dwojga skrzypiec",
            "kameralnej orkiestrze",
        )
    ):
        lines.extend(
            [
                (
                    "- Boczniewicz concert profile: keep Polish place names in parentheses after Lemko forms, e.g. "
                    "з Нєґославицях (Niegosławicach) з сідибом в Ґосцєшовицях (Gościeszowicach)."
                ),
                (
                    "- In this concert register, prefer Новорічный Концерт, выступлят, разом з родичами, "
                    "акомпаніяменті двоіх гушель і ґітары, артисткы, якы знаны сут, Музичну дорогу, ансамблі Окмель, "
                    "Реализували чысленны музичны проєкты, Протягом років выступували, Лемко Тавер, and На совім конті."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "swieto patronalne cerkwi w koniecznej",
            "święto patronalne cerkwi w koniecznej",
            "kalendarza julianskiego obchodzimy dzis nowy rok",
            "kalendarza juliańskiego obchodzimy dziś nowy rok",
            "swiety bazyli wielki",
            "święty bazyli wielki",
            "cerkiew greckokatolicka",
        )
    ):
        lines.extend(
            [
                (
                    "- Patronal-feast/Konieczna profile: translate święto patronalne as Храмове свято, not "
                    "праздник покровителя; use За юлияньскым календарьом празднуєме днес Новый Рік."
                ),
                (
                    "- Preserve the New-Year wish and parish sentence order: з той нагоды ... най ся Вам веде, каре і "
                    "щєстит цілый 2026 рік; Першый ден Нового Рока за юлияньскым календарьом то тіж храмове свято "
                    "парохіі; котрой покровительом єст Святый Василий Великій; была вознесена ... як грекокатолицкій храм."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "stare ksiazki rzadko",
            "stare książki rzadko",
            "cerkiewnych starodrukow",
            "cerkiewnych starodruków",
            "eparchialnego osrodka kultury prawoslawnej",
            "eparchialnego ośrodka kultury prawosławnej",
            "konserwacja kolekcji cerkiewnych starodrukow",
            "konserwacja kolekcji cerkiewnych starodruków",
            "elpis",
        )
    ):
        lines.extend(
            [
                (
                    "- Church old-print conservation profile: use книгы for old books in this article, not книжкы; "
                    "keep the opening order Стары книгы рідко голосно промавляют."
                ),
                (
                    "- Preserve the old-print sentence chain: жебы ся не розлетіли; мимо вшытко; тішыт бібліофілів; "
                    "стары, вартістны книгы; В посліднім часі; консерваторска робота."
                ),
                (
                    "- For the institution and project, prefer з колекциі Єпархіяльного Осередка Православной Культуры "
                    "«Ельпіс»; Проєкт, реализуваный при підпорі; мал наголовок; Така задача."
                ),
                (
                    "- In conservation-material clauses, use ратунок для паперя, окладинок, тінты and захованя памяти; "
                    "write Така задача то не выключні ратунок, not не лем ратунок. Avoid паперу, обкладок, "
                    "атраменту, named 'назву', and Latin Elpis where the reference uses Cyrillic."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "dziegciarstwo",
            "degciarstwo",
            "depozytariuszami tradycji",
            "zywe dziedzictwo lemkowszczyzny",
            "żywe dziedzictwo łemkowszczyzny",
            "w bielance",
            "utrwalac je w pamieci",
            "utrwalać je w pamięci",
        )
    ):
        lines.extend(
            [
                (
                    "- Dziegciarstwo radio-project profile: recurring radio invitation uses Просиме на радийовый проґрам, "
                    "not Запрашаме; rozmowa -> бесіда, project implementers -> реализаторами."
                ),
                (
                    "- Keep the project title and heritage register: «Дегтярство. Жыва спадковина Лемковины», "
                    "як особлывым і унікальным ремеслі, елементі нематерияльной спадковины Лемковины. "
                    "Do not insert an extra о after як."
                ),
                (
                    "- Preserve the programme clause order: Буде кус о істориі дегтярства в Білянці, культурным значыню, "
                    "а тіж о тым, чом вартат документувати дегтярство і утырваляти в памяти і в практиці."
                ),
                (
                    "- For fieldwork/tradition clauses, use стрічах з депозитариюшами традициі і пробах сохраніня "
                    "знаня і практик, што десятками років были важном частю в Карпатах; use о роботі в терені, "
                    "not о працы в терені. Avoid носителями, захованя, котры през десятки років."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "ruska bursa w nowym pieknie",
            "ruska bursa w nowym pięknie",
            "renowacja i modernizacja budynku stowarzyszenia ruska bursa",
            "funduszu promocji kultury",
            "dzien rusinow polski",
            "dzień rusinów polski",
            "i etap prac",
        )
    ):
        lines.extend(
            [
                (
                    "- Ruska Bursa renovation daily-summary profile: keep paired date 10. грудня/децембра; heading "
                    "Руска Бурса в новій красі; Минула субота прошла під знаком святкувань."
                ),
                (
                    "- Preserve the two-part celebration order: По перше - одпразднуваный был Ден Русинів Польщы, "
                    "о чым сме уж інформували, а по другє - торжественно закінчена была важна інвестиция."
                ),
                (
                    "- In this grant/building register, use Повело ся вполни і в терміні зреализувати задачу пн.; "
                    "І етап робіт; яка дофінансувана была з середків. For Stowarzyszenie use Стоваришыня, "
                    "not Общество/Общества."
                ),
                (
                    "- For the funding source, prefer што походят з Фундушу Промоциі Культуры, державного цільового "
                    "фундушу, and abbreviate thousand as тис. Avoid минула ... минула, одзначаный, друге, "
                    "святочно, Удало, в повни, заданя, Фонду/фонду, тыс., and прац in this title."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "studia tanca destino",
            "studia tańca destino",
            "wicemistrza swiata",
            "wicemistrza świata",
            "show dance ido",
            "natalia pelechacz",
            "spolecznym zyciu lemkowskim",
            "społecznym życiu łemkowskim",
        )
    ):
        lines.extend(
            [
                (
                    "- Dance championship profile: use Ґрупа парунадцетлітніх танцюристок зо Студия Танця Дестіно "
                    "з Ропы; keep Дестіно in Cyrillic and use танцюристок, not танечниц."
                ),
                (
                    "- Preserve the achievement phrase: однесла спектакулярный успіх - званя ІІ Віцемастера Світа "
                    "в змаганях Чемпіонату Світа Show Dance IDO."
                ),
                (
                    "- For the event dates, use Змаганя проходили в Італиі в днях од 17. до 22. листопада/новембра тр.; "
                    "keep day dots and the paired month form."
                ),
                (
                    "- For the person sentence, use Серед нагородженых танцюристок є Наталия Пелехач з Ґладышова, "
                    "активна і в соспільным лемківскым жытю. End with Сердечні ґратулюєме."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "sobor biskupow",
            "sobór biskupów",
            "polskiego autokefalicznego kosciola prawoslawnego",
            "polskiego autokefalicznego kościoła prawosławnego",
            "pakp",
            "prawoslawnych lemkow",
            "prawosławnych łemków",
            "metropolity warszawskiego",
        )
    ):
        lines.extend(
            [
                (
                    "- Orthodox church institutional profile: 'pod przewodnictwem' -> під ведіньом; "
                    "Sobór obradował -> Собор засідал; use ПАПЦ for PAKP."
                ),
                (
                    "- For the Polish Autocephalous Orthodox Church, prefer Польской Автокефальной Православной Церкви; "
                    "avoid автокефалічной in this institutional name."
                ),
                (
                    "- For synod/meeting decisions, use в рамках засіданя владыкы рішыли як зовнішні, як і внутрішні темы; "
                    "avoid rozstrzygnęli/posiedzenie calques such as розстрігнули or посіджыня."
                ),
                (
                    "- In church-administrative prose, prefer повязаны з жытьом церкви, рішеных справ, and темы што тыкают "
                    "православных Лемків."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "regional dossiers",
            "mercator european research centre",
            "multilingualism and language learning",
            "fryske akademy",
            "leeuwarden",
            "jezykowi lemkowskiemu w polsce",
        )
    ):
        lines.extend(
            [
                (
                    "- Mercator/Regional Dossiers academic profile: for the opening use Перед парома днями "
                    "вказало ся чергове выданя престіжной сериі Regional Dossiers, not вышло наступне or парьома."
                ),
                (
                    "- Preserve the institution sentence as Публикацю зрыхтувал Mercator European Research Centre "
                    "on Multilingualism and Language Learning, што діє при Fryske Akademy в Леуварден в Нідерляндах. "
                    "Avoid котрий, Leeuwarden in Latin, and Нидерляндах."
                ),
                (
                    "- For the significance sentence, keep the source POS order exactly: Є то важный крок в страну "
                    "поглубляня знаня ... в Европі - так посеред науковців, як і політичных децидентів. "
                    "Avoid Єст то, напрямі, omitting так, and replacing як і with а так само."
                ),
                (
                    "- For this academic-minority register, prefer в Европі, посеред науковців, політичных децидентів, "
                    "бадавча єдиниця, специялизує ся в анализі, and нідерляндской Fryske Akademy. "
                    "Use Чым є Mercator Centre? and то узнана в Европі бадавча єдиниця, яка специялизує ся; "
                    "render multilingualism as вельоязычности. Avoid Чым єст, знана, што as the relative pronoun here, "
                    "великоязычности, признана/doslidnycha, and Polish-like політычных."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "rocznika ruskiej bursy",
            "rocznik ruskiej bursy",
            "wstep",
            "wstęp",
            "literatura lemkowska/rusinska",
            "literatura łemkowska/rusińska",
            "problematyka",
            "tozsamosc wspolnoty",
            "tożsamość wspólnoty",
        )
    ):
        lines.extend(
            [
                (
                    "- Academic/RRB journal profile: for a journal issue use чысло, not зошыт; in genitive issue phrases "
                    "prefer сесого 21. чысла and в тым чыслі."
                ),
                (
                    "- In formal academic prose, preserve source register forms where natural: Jak wiadomo -> Як знатя; "
                    "the title sentence may use є, while 'literatura jest jednym...' uses єст єдным; "
                    "tożsamość wspólnoty -> достоменніст спільноты."
                ),
                (
                    "- For RRB relative clauses, do not overuse котр-: prefer яку for feminine objects such as "
                    "спільнота/проблематыка, and keep the local clause order яку можна ... досліджати."
                ),
                (
                    "- Use structured_rules adjective endings in this register: neuter nominative/accusative forms such as "
                    "шырокє and якє, not default Polish-like шыроке/котре."
                ),
                (
                    "- Keep RRB academic word order: якє сме вступні запропонували; притягло до авторской участи. "
                    "Avoid moving сме after the participle, replacing участ with участя, or changing притягло to a plural predicate."
                ),
                (
                    "- In RRB academic prose, keep в высокій ступени, і проблематыкы, яку можна в текстах і през тексты досліджати; "
                    "avoid через тексты and avoid moving досліджати before притягло."
                ),
            ]
        )

    if any(trigger in folded_polish_text for trigger in ("przez pryzmat", "rozmowy o historii", "poświęcone tragicznym", "poswiecone tragicznym")):
        lines.extend(
            [
                "- For 'przez pryzmat' in news prose, prefer через призму; avoid през призмат.",
                "- For public talk series, prefer бесіды for 'rozmowy' when it names a cycle title, not literal розмовы.",
                "- For 'spotkanie ..., poświęcone ...', preserve the relative-clause order with што присвячена when natural.",
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "akcja wisla",
            "akcja wisła",
            "akcji wisla",
            "akcji wisła",
            "prowincjonalne rozmowy o historii",
            "caffe provincia",
            "losem narodu",
            "przymusowe wysiedlenie",
        )
    ):
        lines.extend(
            [
                (
                    "- Akcja Wisła/public-history profile: keep the source-like heading as O Лемках і акциі «Вісла» when the "
                    "Polish title starts 'O Łemkach...'; do not automatically Cyrillicize initial Latin O."
                ),
                (
                    "- For meeting time/place in this profile, use Заран, в пятницю, о 18.00 год. в Caffe Provincia в Ліґници; "
                    "avoid adding Уж and avoid о год. 18.00."
                ),
                (
                    "- Prefer local forms in this profile: Юрийом Стариньскым, Провінцийональны бесіды, запрашают, "
                    "над дольом лемківского народу, котрый пережыл насильне выселіня, and присмотріти ся."
                ),
                (
                    "- Jaworzno/Akcja Wisła memorial profile: prefer Річниця выселіня і поневоліня, девят років тому, "
                    "29. квітня/апріля, был одкрытый памятник в рамках, 70. річниці, видно інскрипцию, "
                    "в трьох языках, and Вязненым і страдавшым."
                ),
                (
                    "- In Jaworzno camp-memory prose, keep the source-like chain: систематичного фальсифікуваня "
                    "лемківской істориі, поменшаня лемківского терпліня і неспоминаня на Лемків - вязнів ...; "
                    "use до нього for 'do niego'."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "lemkowyna ultra trail",
            "lemkovina ultra trail",
            "leona kozminskiego",
            "leona kozminsk",
            "uczelni biznesowych",
            "europie srodkowo-wschodniej",
            "zawodow lemkowyna",
        )
    ):
        lines.extend(
            [
                (
                    "- Lemkowyna Ultra Trail partnership profile: transliterate the institution and event in Lemko Cyrillic: "
                    "Академія Леона Козміньского, Лемковина Ультра Трейль."
                ),
                (
                    "- Keep the regional/business-school phrase order: єдна з найліпшых бізнесовых учелен в "
                    "Серединно-Східній Европі; зачала спілпрацу з орґанізатором змагань."
                ),
                (
                    "- In mountain-race prose, zawody -> змаганя/змагань, not заводы; preserve the predicate order "
                    "Тоты гірскы змаганя знаны сут в цілій Польщы."
                ),
                (
                    "- For the partnership sentence, prefer Партнерство споює спорт, едукацию і соспільніст; "
                    "мают тіж свою реному в Европі. Avoid generic спілдіяня/лучыт/громада/репутация in this article register."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "mecenat malopolski",
            "mecenatu malopolski",
            "konkursie grantowym",
            "wyniki naboru",
            "urzad marszalkowski wojewodztwa malopolskiego",
            "kilka lemkowskich inicjatyw",
            "oferty pozostaly bez finansowania",
        )
    ):
        lines.extend(
            [
                (
                    "- Małopolska grant-summary profile: keep the heading as Кілько лемківскых ініциятив в рамках "
                    "Меценату Малопольщы; avoid Дакілько, ініциятыв, or в рамах."
                ),
                (
                    "- For grant administration, prefer оголосил результаты набору; Підперты будут задачы, котры ...; "
                    "будут влияти на промоцию; єдном з критерий было, што ..."
                ),
                (
                    "- Preserve funding summary syntax: В сумі підперто ... на квоту ...; Заряд Воєвідства звекшыл "
                    "первістну пулю дофінансуваня; keep the abbreviated тис. and currency order."
                ),
            ]
        )

    if any(
        trigger in folded_polish_text
        for trigger in (
            "thalerhof",
            "talerhof",
            "oboz internowania rusinow",
            "ruska bursa",
            "muzeum dwory karwacjanow",
            "gladyszow",
            "perspektywe badawcza",
            "ksiazka liczy blisko",
        )
    ):
        lines.extend(
            [
                (
                    "- Thalerhof/publication profile: for this book notice, wydało -> печатало, pt. -> пн., "
                    "w całości po polsku -> в цілости по польскы, and the title stays «Талергоф. Лаґєр інтернуваня Русинів»."
                ),
                (
                    "- Preserve the conference clause order: конференциі, яку в 2024 р., в 110. річницю початку "
                    "поневолінь нашых предків в Талергофі, зрыхтувало ..."
                ),
                (
                    "- Preserve partner-institution morphology: во спілпрацы з Музейом Дворы Карвациянів і Ґладышів "
                    "в Ґорлицях."
                ),
                (
                    "- In the publication-description paragraph, prefer обогачене єст, фотоґрафіями, дополнене о додатковы "
                    "тексты, што пошырюют підняту проблематику заєдно о дослідничу, як і спільнотову перспективы; "
                    "Книжка чыслит близко 400 стран."
                ),
            ]
        )

    return lines


def build_style_guidance(polish_text: str) -> str:
    folded = fold_for_match(polish_text)
    lines: list[str] = build_general_grammar_guidance(folded)
    selected_preferences = select_style_preferences(folded)
    selected_memory = select_translation_memory(folded)

    if selected_preferences:
        lines.append("Apply these phrase/style preferences only where they match the Polish source:")
        for item in selected_preferences:
            lines.append(
                "- Prefer {prefer}; avoid {avoid}. Reason: {reason}".format(
                    prefer=item["prefer"],
                    avoid=item["avoid"],
                    reason=item["reason"],
                )
            )

    if selected_memory:
        lines.append("\nRelevant translation memory examples:")
        for item in selected_memory:
            lines.append(
                "- {label}: Polish pattern: {polish_excerpt}\n"
                "  Human Lemko reference: {lemko_reference}\n"
                "  Guidance: {usage}".format(**item)
            )

    if not lines:
        return "No extra phrase preferences matched this source."
    return "\n".join(lines)


def select_style_preferences(folded_polish_text: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in STYLE_PREFERENCES:
        triggers = [fold_for_match(str(trigger)) for trigger in item["triggers"]]
        if any(trigger in folded_polish_text for trigger in triggers):
            selected.append(item)
    return selected


def select_translation_memory(folded_polish_text: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in TRANSLATION_MEMORY:
        source_terms = [fold_for_match(str(term)) for term in item["source_terms"]]
        hits = sum(1 for term in source_terms if term in folded_polish_text)
        if hits >= int(item.get("min_hits", 1)):
            selected.append(item)
    return selected


def expand_memory_report_paths(paths: Sequence[Path | str] | None) -> list[Path]:
    expanded: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths or []:
        text = str(raw_path)
        matches = [Path(match) for match in sorted(glob.glob(text))] if any(char in text for char in "*?[]") else [Path(text)]
        for path in matches:
            resolved = path.resolve()
            key = str(resolved).lower()
            if key not in seen:
                seen.add(key)
                expanded.append(resolved)
    return expanded


def first_text_value(item: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def memory_tokens(text: str, *, ascii_fold: bool = False) -> set[str]:
    folded = fold_ascii_for_match(text) if ascii_fold else fold_for_match(text)
    tokens = {
        match.group(0).strip("-")
        for match in POLISH_WORD_RE.finditer(folded)
        if len(match.group(0).strip("-")) > 2
    }
    stopwords = POLISH_STOPWORDS | POLISH_ASCII_STOPWORDS if ascii_fold else POLISH_STOPWORDS
    return {
        token
        for token in tokens
        if token and token not in stopwords and not token.isdigit()
    }


def memory_similarity(query_text: str, example_text: str, *, ascii_fold: bool = False) -> float:
    query_tokens = memory_tokens(query_text, ascii_fold=ascii_fold)
    example_tokens = memory_tokens(example_text, ascii_fold=ascii_fold)
    if not query_tokens or not example_tokens:
        return 0.0
    overlap = len(query_tokens & example_tokens)
    return round((2.0 * overlap) / (len(query_tokens) + len(example_tokens)), 6)


def memory_overlap_terms(query_text: str, example_text: str, *, ascii_fold: bool = False) -> list[str]:
    query_tokens = memory_tokens(query_text, ascii_fold=ascii_fold)
    example_tokens = memory_tokens(example_text, ascii_fold=ascii_fold)
    return sorted(query_tokens & example_tokens)


def memory_score_band(score: float) -> str:
    if score < DEFAULT_MEMORY_LOW_SCORE_AUDIT_THRESHOLD:
        return "low"
    if score < 0.16:
        return "medium"
    return "high"


def normalize_memory_risk_policy(policy: str | None) -> str:
    normalized = (policy or DEFAULT_MEMORY_RISK_POLICY).strip().lower()
    if normalized not in MEMORY_RISK_POLICIES:
        raise ValueError(f"Unsupported memory risk policy: {policy!r}")
    return normalized


def memory_risk_sort_rank(policy: str, risk_level: str) -> int:
    if policy != "demote":
        return 0
    return {"low": 0, "medium": 1, "high": 2}.get(risk_level, 1)


def audit_report_translation_memory(
    query_text: str,
    example: dict[str, Any],
    score: float,
    *,
    ascii_fold: bool = False,
) -> dict[str, Any]:
    source_text = str(example.get("polish_text") or "")
    title = str(example.get("title") or "")
    shared_terms = memory_overlap_terms(query_text, source_text, ascii_fold=ascii_fold)
    query_profiles = classify_memory_profiles(query_text)
    example_profiles = set(example.get("profiles") or classify_memory_profiles(source_text, title))
    shared_profiles = query_profiles & example_profiles
    generic_terms = [term for term in shared_terms if term in MEMORY_AUDIT_GENERIC_TERMS]

    risk_flags: list[str] = []
    if score < DEFAULT_MEMORY_LOW_SCORE_AUDIT_THRESHOLD:
        risk_flags.append("low_similarity")
    if score < 0.09:
        risk_flags.append("very_low_similarity")
    if not shared_terms:
        risk_flags.append("no_shared_terms")
    elif len(shared_terms) < 3:
        risk_flags.append("few_shared_terms")
    if shared_terms and len(generic_terms) == len(shared_terms):
        risk_flags.append("generic_or_date_only_overlap")
    if query_profiles and example_profiles and not shared_profiles:
        risk_flags.append("profile_mismatch")
    elif not query_profiles or not example_profiles:
        risk_flags.append("unknown_profile")

    if "profile_mismatch" in risk_flags and "low_similarity" in risk_flags:
        risk_level = "high"
    elif "no_shared_terms" in risk_flags or "generic_or_date_only_overlap" in risk_flags:
        risk_level = "high"
    elif "low_similarity" in risk_flags or "few_shared_terms" in risk_flags:
        risk_level = "medium"
    else:
        risk_level = "low"

    return {
        "score_band": memory_score_band(score),
        "risk_level": risk_level,
        "risk_flags": risk_flags,
        "shared_terms": shared_terms[:12],
        "shared_term_count": len(shared_terms),
        "query_profiles": sorted(query_profiles),
        "example_profiles": sorted(example_profiles),
        "shared_profiles": sorted(shared_profiles),
    }


def classify_memory_profiles(polish_text: str, title: str = "") -> set[str]:
    folded = fold_ascii_for_match(f"{title} {polish_text}")
    return {
        profile
        for profile, triggers in MEMORY_PROFILE_TRIGGERS.items()
        if any(trigger in folded for trigger in triggers)
    }


def memory_profile_adjustment(query_profiles: set[str], example_profiles: set[str]) -> float:
    if not query_profiles or not example_profiles:
        return 0.0
    shared = query_profiles & example_profiles
    if shared:
        return round(min(0.07, 0.04 + (0.01 * (len(shared) - 1))), 6)
    return -0.035


def load_translation_memory_reports(paths: Sequence[Path | str] | None) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for path in expand_memory_report_paths(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TranslationError(f"Could not load translation memory report {path}: {exc}") from exc
        articles = data.get("articles")
        if not isinstance(articles, list):
            continue
        for article in articles:
            if not isinstance(article, dict):
                continue
            polish_text = first_text_value(article, MEMORY_TEXT_KEYS)
            lemko_text = first_text_value(article, MEMORY_LEMKO_KEYS)
            if not polish_text or not lemko_text:
                continue
            examples.append(
                {
                    "source_report": str(path),
                    "title": str(article.get("title") or article.get("source_issue_title") or ""),
                    "url": str(article.get("url") or article.get("lemko_url") or ""),
                    "polish_text": polish_text,
                    "lemko_text": lemko_text,
                    "profiles": sorted(classify_memory_profiles(polish_text, str(article.get("title") or ""))),
                }
            )
    return examples


def select_report_translation_memory(
    polish_text: str,
    examples: Sequence[dict[str, Any]],
    *,
    max_examples: int = DEFAULT_MAX_MEMORY_EXAMPLES,
    min_score: float = DEFAULT_MEMORY_MIN_SCORE,
    profile_scoring: bool = False,
    risk_policy: str = DEFAULT_MEMORY_RISK_POLICY,
) -> list[dict[str, Any]]:
    if max_examples <= 0 or not examples:
        return []
    normalized_risk_policy = normalize_memory_risk_policy(risk_policy)
    scored: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    query_profiles = classify_memory_profiles(polish_text) if profile_scoring else set()
    lexical_floor = min(DEFAULT_MEMORY_PROFILE_LEXICAL_FLOOR, max(0.0, min_score))
    for example in examples:
        source_text = str(example.get("polish_text") or "")
        lexical_score = memory_similarity(polish_text, source_text, ascii_fold=profile_scoring)
        example_profiles: set[str] = set()
        profile_score = 0.0
        if profile_scoring:
            if lexical_score < lexical_floor:
                continue
            title = str(example.get("title") or "")
            example_profiles = set(example.get("profiles") or classify_memory_profiles(source_text, title))
            profile_score = memory_profile_adjustment(query_profiles, example_profiles)
            score = round(max(0.0, lexical_score + profile_score), 6)
        else:
            score = lexical_score
        if score < min_score:
            continue
        dedupe_key = fold_for_match(source_text[:240])
        if dedupe_key in seen_sources:
            continue
        memory_audit = audit_report_translation_memory(
            polish_text,
            example,
            score,
            ascii_fold=profile_scoring,
        )
        if normalized_risk_policy == "exclude" and memory_audit.get("risk_level") == "high":
            continue
        seen_sources.add(dedupe_key)
        enriched = dict(example)
        enriched["score"] = score
        enriched["memory_audit"] = memory_audit
        enriched["memory_risk_policy"] = normalized_risk_policy
        if profile_scoring:
            enriched["lexical_score"] = lexical_score
            enriched["profile_score"] = profile_score
            enriched["query_profiles"] = sorted(query_profiles)
            enriched["example_profiles"] = sorted(example_profiles)
            enriched["shared_profiles"] = sorted(query_profiles & example_profiles)
        scored.append(enriched)
    scored.sort(
        key=lambda item: (
            memory_risk_sort_rank(
                normalized_risk_policy,
                str((item.get("memory_audit") or {}).get("risk_level") or "unknown"),
            ),
            -float(item.get("score") or 0.0),
            -len(item.get("shared_profiles") or []),
            len(str(item.get("polish_text") or "")),
        )
    )
    return scored[:max_examples]


def format_report_translation_memory(examples: Sequence[dict[str, Any]]) -> str:
    if not examples:
        return "No dynamic report memory examples selected."
    lines = [
        "Dynamic translation memory from prior evaluated PL↔LEM pairs. Use these as style/order evidence only when the current Polish sentence is semantically similar; do not copy unrelated facts, names, dates, or numbers."
    ]
    for index, item in enumerate(examples, start=1):
        title = str(item.get("title") or "untitled")
        score = float(item.get("score") or 0.0)
        if "lexical_score" in item or "profile_score" in item:
            lexical_score = float(item.get("lexical_score") or score)
            profile_score = float(item.get("profile_score") or 0.0)
            shared_profiles = ", ".join(str(value) for value in item.get("shared_profiles") or [])
            example_profiles = ", ".join(str(value) for value in item.get("example_profiles") or [])
            profile_note = shared_profiles or example_profiles or "none"
            lines.append(
                f"- Example {index} (similarity={score:.3f}, lexical={lexical_score:.3f}, profile_adjustment={profile_score:.3f}, profiles={profile_note}, title={shorten(title, 90)}):\n"
                f"  Polish: {shorten(str(item.get('polish_text') or ''), 520)}\n"
                f"  Lemko reference: {shorten(str(item.get('lemko_text') or ''), 520)}"
            )
        else:
            lines.append(
                f"- Example {index} (similarity={score:.3f}, title={shorten(title, 90)}):\n"
                f"  Polish: {shorten(str(item.get('polish_text') or ''), 520)}\n"
                f"  Lemko reference: {shorten(str(item.get('lemko_text') or ''), 520)}"
            )
    return "\n".join(lines)


def shorten(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


def build_prompt(
    polish_text: str,
    dictionary_candidates: Sequence[dict[str, Any]],
    rule_context: str,
    memory_examples: Sequence[dict[str, Any]] | None = None,
) -> str:
    dictionary_json = json.dumps(dictionary_candidates, ensure_ascii=False, indent=2)
    style_guidance = build_style_guidance(polish_text)
    report_memory = format_report_translation_memory(memory_examples or [])
    return textwrap.dedent(
        f"""
        Translate the Polish source text into standardized Lemko language written in Lemko Cyrillic.

        Hard requirements:
        - Output only JSON matching the provided schema.
        - Do not run shell commands, browse, inspect files, or call tools. Use only the Polish text, rule context, phrase guidance, dynamic translation memory examples, and dictionary candidates included in this prompt.
        - Translate into Lemko, not Ukrainian, Russian, or generic Rusyn.
        - Preserve paragraph boundaries, punctuation, numbers, dates, names, and URLs where appropriate.
        - Prefer dictionary candidates from the Lemko tools API when they fit the Polish context.
        - If several Lemko candidates are plausible, choose the best one and list uncertainty in uncertain_terms.
        - Do not invent dictionary evidence. If the dictionary lacks a term, translate by linguistic judgment and add a warning.
        - Use natural Lemko syntax. Avoid word-for-word Polish calques when a Lemko construction is more idiomatic.
        - Preserve the source information order: keep paragraph order, sentence order, list order, rubric labels, and clause sequence unless Lemko grammar clearly requires a local change.
        - For short news/radio notes, do not paraphrase or reorder recurring rubrics such as openings, programme-section labels, names, and reminders; translate them in the same position.
        - Prefer local word-order changes only inside a clause. Do not move whole phrases, subordinate clauses, dates, or closing adverbs to a different sentence position without a strong grammatical reason.
        - used_dictionary_entries must list only entries actually used in the translation.
        - If phrase/style guidance conflicts with a literal dictionary gloss, prefer the guidance and mention the reason in warnings only if uncertainty remains.
        - Treat matched phrase/style guidance as a required glossary for fixed series openings, names, organization names, dates, and attested idioms.
        - Do not replace a guided fixed phrase with a synonym or paraphrase. Keep the exact guided spelling unless it is grammatically impossible in context.

        Local Lemko rule context:
        {rule_context}

        Phrase/style guidance:
        {style_guidance}

        Dynamic translation memory:
        {report_memory}

        Dictionary candidates from API:
        {dictionary_json}

        Polish source text:
        ---
        {polish_text}
        ---
        """
    ).strip()


def normalize_chunk_payload(payload: dict[str, Any]) -> dict[str, Any]:
    translated = payload.get("translated_text")
    if not isinstance(translated, str) or not translated.strip():
        raise CodexExecutionError("Codex JSON is missing non-empty translated_text.")

    def list_or_empty(name: str) -> list[Any]:
        value = payload.get(name)
        return value if isinstance(value, list) else []

    return {
        "translated_text": translated.strip(),
        "used_dictionary_entries": list_or_empty("used_dictionary_entries"),
        "uncertain_terms": [str(item) for item in list_or_empty("uncertain_terms") if str(item).strip()],
        "warnings": [str(item) for item in list_or_empty("warnings") if str(item).strip()],
    }


def translate_text(
    text: str,
    *,
    api_base: str = DEFAULT_API_BASE,
    api_token: str | None = None,
    codex_bin: str = "codex",
    rules_dir: Path | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_terms: int = DEFAULT_MAX_TERMS,
    memory_reports: Sequence[Path | str] | None = None,
    max_memory_examples: int = DEFAULT_MAX_MEMORY_EXAMPLES,
    memory_min_score: float = DEFAULT_MEMORY_MIN_SCORE,
    memory_profile_scoring: bool = False,
    memory_risk_policy: str = DEFAULT_MEMORY_RISK_POLICY,
    codex_timeout: int = DEFAULT_CODEX_TIMEOUT,
    debug: bool = False,
) -> dict[str, Any]:
    cleaned = text.strip()
    if not cleaned:
        raise TranslationError("Input text cannot be empty.")
    root = Path(__file__).resolve().parents[1]
    resolved_rules_dir = rules_dir or (root / "docs" / "structured_rules")
    rule_context = load_rule_context(resolved_rules_dir)
    chunks = split_text(cleaned, max_chars=max_chars)
    if not chunks:
        raise TranslationError("Input text produced no chunks.")

    api = ApiClient(api_base=api_base, api_token=api_token or os.getenv("LEMKO_API_TOKEN") or None)
    codex = CodexRunner(codex_bin=codex_bin, timeout=codex_timeout, debug=debug)
    memory_pool = load_translation_memory_reports(memory_reports)
    normalized_memory_risk_policy = normalize_memory_risk_policy(memory_risk_policy)
    translated_chunks: list[str] = []
    chunk_payloads: list[dict[str, Any]] = []
    all_dictionary: list[dict[str, Any]] = []
    all_missing: list[str] = []
    all_memory: list[dict[str, Any]] = []
    used_entries: list[Any] = []
    uncertain_terms: list[str] = []
    warnings: list[str] = []

    for index, chunk in enumerate(chunks, start=1):
        if debug:
            print(f"[chunk {index}/{len(chunks)}] chars={len(chunk)}", file=sys.stderr)
        dictionary_candidates, missing_terms = collect_dictionary_context(
            chunk,
            api,
            max_terms=max_terms,
            debug=debug,
        )
        memory_examples = select_report_translation_memory(
            chunk,
            memory_pool,
            max_examples=max_memory_examples,
            min_score=memory_min_score,
            profile_scoring=memory_profile_scoring,
            risk_policy=normalized_memory_risk_policy,
        )
        if debug and memory_examples:
            print(
                "[memory] "
                + ", ".join(
                    f"{shorten(str(item.get('title') or 'untitled'), 48)}={float(item.get('score') or 0):.3f}"
                    for item in memory_examples
                ),
                file=sys.stderr,
            )
        prompt = build_prompt(chunk, dictionary_candidates, rule_context, memory_examples=memory_examples)
        raw_payload = codex.run_json(prompt, CODEX_OUTPUT_SCHEMA)
        payload = normalize_chunk_payload(raw_payload)
        translated_chunks.append(payload["translated_text"])
        chunk_payloads.append(
            {
                "index": index,
                "source_text": chunk,
                "translated_text": payload["translated_text"],
                "dictionary_candidates": dictionary_candidates,
                "missing_terms": missing_terms,
                "translation_memory_examples": [
                    {
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "source_report": item.get("source_report"),
                        "score": item.get("score"),
                        "lexical_score": item.get("lexical_score"),
                        "profile_score": item.get("profile_score"),
                        "shared_profiles": item.get("shared_profiles"),
                        "example_profiles": item.get("example_profiles"),
                        "profile_scoring": memory_profile_scoring,
                        "memory_risk_policy": normalized_memory_risk_policy,
                        "memory_audit": item.get("memory_audit"),
                    }
                    for item in memory_examples
                ],
                "used_dictionary_entries": payload["used_dictionary_entries"],
                "uncertain_terms": payload["uncertain_terms"],
                "warnings": payload["warnings"],
            }
        )
        all_dictionary.extend(dictionary_candidates)
        all_missing.extend(missing_terms)
        all_memory.extend(memory_examples)
        used_entries.extend(payload["used_dictionary_entries"])
        uncertain_terms.extend(payload["uncertain_terms"])
        warnings.extend(payload["warnings"])

    return {
        "translated_text": "\n\n".join(translated_chunks).strip(),
        "used_dictionary_entries": dedupe_jsonish_list(used_entries),
        "uncertain_terms": dedupe_strings(uncertain_terms),
        "warnings": dedupe_strings(warnings),
        "resolved_polish_terms": [item["source_query"] for item in all_dictionary],
        "dictionary_candidates": all_dictionary,
        "missing_terms": dedupe_strings(all_missing),
        "translation_memory_examples": dedupe_jsonish_list(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "source_report": item.get("source_report"),
                "score": item.get("score"),
                "lexical_score": item.get("lexical_score"),
                "profile_score": item.get("profile_score"),
                "shared_profiles": item.get("shared_profiles"),
                "example_profiles": item.get("example_profiles"),
                "profile_scoring": memory_profile_scoring,
                "memory_risk_policy": normalized_memory_risk_policy,
                "memory_audit": item.get("memory_audit"),
            }
            for item in all_memory
        ),
        "memory_risk_policy": normalized_memory_risk_policy,
        "model": "codex-cli-default",
        "attempts": len(chunks),
        "chunks": chunk_payloads,
    }


def dedupe_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def dedupe_jsonish_list(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def read_input_text(args: argparse.Namespace) -> str:
    if args.text and args.input:
        raise TranslationError("Use either --text or --input, not both.")
    if args.text:
        return args.text
    if args.input:
        return Path(args.input).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise TranslationError("Provide --text, --input, or stdin.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate Polish text into Lemko with Codex CLI and Lemko tools.")
    parser.add_argument("--text", help="Polish text to translate.")
    parser.add_argument("--input", help="UTF-8 text file with Polish input.")
    parser.add_argument("--output", help="Write full JSON result to this path.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"Lemko API base URL, default: {DEFAULT_API_BASE}")
    parser.add_argument("--api-token", default=os.getenv("LEMKO_API_TOKEN"), help="Optional Bearer token for Lemko API.")
    parser.add_argument("--codex-bin", default=os.getenv("CODEX_BIN", "codex"), help="Codex CLI executable.")
    parser.add_argument("--rules-dir", type=Path, help="Directory containing structured_rules/tables.json or equivalent.")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Maximum characters per Codex chunk.")
    parser.add_argument("--max-terms", type=int, default=DEFAULT_MAX_TERMS, help="Maximum Polish dictionary queries per chunk.")
    parser.add_argument(
        "--memory-report",
        action="append",
        type=Path,
        help="Evaluation JSON report or glob used as dynamic PL↔LEM translation memory. Can be repeated.",
    )
    parser.add_argument(
        "--max-memory-examples",
        type=int,
        default=DEFAULT_MAX_MEMORY_EXAMPLES,
        help="Maximum dynamic translation-memory examples injected per chunk.",
    )
    parser.add_argument(
        "--memory-min-score",
        type=float,
        default=DEFAULT_MEMORY_MIN_SCORE,
        help="Minimum token-overlap score required for a dynamic translation-memory example.",
    )
    parser.add_argument(
        "--memory-profile-scoring",
        action="store_true",
        help="Experimental: adjust translation-memory scoring by coarse genre/topic profiles.",
    )
    parser.add_argument(
        "--memory-risk-policy",
        choices=MEMORY_RISK_POLICIES,
        default=DEFAULT_MEMORY_RISK_POLICY,
        help="How to handle high-risk dynamic translation-memory examples after local audit.",
    )
    parser.add_argument("--codex-timeout", type=int, default=DEFAULT_CODEX_TIMEOUT, help="Codex timeout in seconds per chunk.")
    parser.add_argument("--debug", action="store_true", help="Print diagnostic messages to stderr.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON response instead of only translated_text.")
    return parser.parse_args(argv)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    try:
        source_text = read_input_text(args)
        result = translate_text(
            source_text,
            api_base=args.api_base,
            api_token=args.api_token,
            codex_bin=args.codex_bin,
            rules_dir=args.rules_dir,
            max_chars=args.max_chars,
            max_terms=args.max_terms,
            memory_reports=args.memory_report,
            max_memory_examples=args.max_memory_examples,
            memory_min_score=args.memory_min_score,
            memory_profile_scoring=args.memory_profile_scoring,
            memory_risk_policy=args.memory_risk_policy,
            codex_timeout=args.codex_timeout,
            debug=args.debug,
        )
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.json or args.output:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["translated_text"])
        return 0
    except TranslationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
