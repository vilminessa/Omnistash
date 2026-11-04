"""Сборка юридического пакета для релиза (docs/LEGAL-REVIEW.md §5.1 и §5.2).

Условие «Notices» в PolyForm Noncommercial требует, чтобы каждый, кто получил
копию программы, получил вместе с ней текст условий, строку `Required Notice:`
и уведомления о третьих сторонах. Релизный конвейер до этого шага отдавал
только `Omnistash.exe` и бандл клиента: получатель exe не получал ни LICENSE, ни
THIRD_PARTY_NOTICES.md — условие выглядело выполненным в репозитории и не
выполненным для человека, который скачал бинарник. Этот инструмент кладёт
рядом с exe то, что требует лицензия:

    LICENSE                     — текст PolyForm Noncommercial 1.0.0 + Required Notice
    README.md                   — раздел о лицензии, Disclaimer и условиях распространения
    THIRD_PARTY_NOTICES.md      — уведомления о сторонах по фактическому составу exe
    docs/ACCEPTABLE_USE.md      — правила использования (AUP)
    docs/LEGAL_FAQ.md           — правовые вопросы и ответы
    ui_src/vendor/hls.js.LICENSE.txt — текст Apache-2.0 для вендора в бандле

    python tools/build_legal_pack.py           # собрать release_pack/ и manifest
    python tools/build_legal_pack.py --check   # сверить состав и тексты, не писать
    python tools/build_legal_pack.py --zip     # дополнительно release_pack/omnistash-notices.zip

Из сети ничего не скачивается: файлы читаются из репозитория. Канонический текст
лицензии закреплён по sha256 в `docs/POLYFORM-NONCOMMERCIAL-1.0.0.txt` (получен
один раз вручную), поэтому «совпадает с каноническим» проверяется офлайн и без
правка текста лицензии: лицензии «не улучшают» — редистрибьюторы сверяют их
по SPDX. Отсутствие файла, несовпадение пина, непокрытый компонент или имя
взрослой площадки в публичном тексте называются причиной; молчания нет.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "release_pack"
ZIP_NAME = "omnistash-notices.zip"
REQUIREMENTS = "requirements.txt"

# Что уезжает в пакет. Пути внутри архива те же, что в репозитории: получатель
# копии должен увидеть те же имена файлов, которые называет README и notices.
PACK_FILES = (
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/ACCEPTABLE_USE.md",
    "docs/LEGAL_FAQ.md",
    "ui_src/vendor/hls.js.LICENSE.txt",
)

# Канонический текст лицензии (офлайн-якорь сверки). Получен вручную с
# polyformproject.org 2026-11-04; рантайм и CI его не скачивают.
CANONICAL_FILE = "docs/POLYFORM-NONCOMMERCIAL-1.0.0.txt"
CANONICAL_SHA256 = "80cd822a66644c9059138217143c5a6f98d9e53db8db0a67ea5ab0e79e260c8e"
CANONICAL_SOURCE = "https://polyformproject.org/licenses/noncommercial/1.0.0.txt"
CANONICAL_SPDX = "PolyForm-Noncommercial-1.0.0"
CANONICAL_FETCHED = "2026-11-04"
# URL самих условий, который обязан стоять в LICENSE (раздел «Notices»
# лицензии допускает текст условий или их URL).
LICENSE_URL = "https://polyformproject.org/licenses/noncommercial/1.0.0"

# Компоненты, которые обязаны быть в уведомлениях, и лицензии, которыми они
# названы. Проверка по строке таблицы: компонент обязан присутствовать, и его
# лицензия названа тем же именем, что у стороны (не «почти то же»).
LICENSES_OF = {
    "Python": ("PSF",),
    "OpenSSL": ("Apache-2.0",),
    "SQLite": ("Public Domain",),
    "yt-dlp": ("Unlicense",),
    "curl_cffi": ("MIT", "curl"),          # плюс notice от libcurl
    "pywebview": ("BSD", "Roman Sirokov", "2014-2017"),
    "cryptography": ("Apache-2.0", "BSD-3"),
    "requests": ("Apache-2.0",),
    "urllib3": ("MIT",),
    "certifi": ("MPL-2.0",),
    "charset-normalizer": ("MIT",),
    "idna": ("BSD",),
    "PySocks": ("BSD",),
    "bottle": ("MIT",),
    "proxy_tools": ("MIT",),
    "cffi": ("MIT",),                      # MIT, не «MIT-0» (неточность в перечне)
    "pycparser": ("BSD",),
    "pythonnet": ("MIT",),
    "clr_loader": ("MIT",),
    "pywin32-ctypes": ("BSD",),
    "packaging": ("Apache-2.0", "BSD"),
    "altgraph": ("MIT",),
    "pefile": ("MIT",),
    "setuptools": ("MIT",),
    "PyInstaller": ("GPL-2.0-or-later", "exception"),
    "UPX": ("GPL-2.0-or-later", "exception", "неизменённого"),
    "hls.js": ("Apache-2.0", "sha256"),
}

# Прямые зависимости из requirements.txt обязаны быть в перечне: перечень по
# одному имени из списка был бы враньём о составе exe.
# name в requirements -> name в перечне уведомлений
PIP_TO_NOTICE = {"pyinstaller": "PyInstaller", "curl_cffi": "curl_cffi"}

# Что обязано быть названо в уведомлениях где угодно в файле (не в одной строке).
NOTICES_ASSERTIONS = (
    "Условия для тех, кто распространяет Omnistash",
    "Dailymotion",
    "Brightcove",
    "WebView2Loader.dll",
    "WebView2",
    "Microsoft",
    "SmartScreen",                       # WebView2 по умолчанию включает SmartScreen
    "https://aka.ms/privacy",
    "POLYFORM-NONCOMMERCIAL-1.0.0.txt",  # канонический якорь сверки лицензии
    "tools/build_legal_pack.py",         # пакет, который уезжает рядом с exe
    "omnistash-notices.zip",
    "ffmpeg",
    "GPL",
    "zeroconf",
    "LGPL",
    "magic-wormhole",
    "не поставляет",
)

# Обязательные утверждения в каждом файле пакета: отсутствие называется файлом.
STATEMENTS = {
    "LICENSE": (
        "PolyForm Noncommercial License 1.0.0",
        LICENSE_URL,
        "Required Notice: Copyright © 2026 vilminessa",
        "## Notices",
        "Any noncommercial purpose is a permitted purpose",
    ),
    "README.md": (
        "не аффилирован, не одобрен и не спонсируется",
        "Кто распространяет программу",
        "THIRD_PARTY_NOTICES.md",
        "docs/ACCEPTABLE_USE.md",
        "docs/LEGAL_FAQ.md",
        "PolyForm Noncommercial",
        "взрослая площадка",
        "omnistash-notices.zip",
        "tools/build_legal_pack.py",
    ),
    "THIRD_PARTY_NOTICES.md": NOTICES_ASSERTIONS,
    "docs/ACCEPTABLE_USE.md": (
        "1. Контент",
        "2. Передача файлов другим людям",
        "3. Ретрансляция",
        "4. Аккаунты",
        "5. Возрастное подтверждение",
        "6. Чего не должно быть",
        "7. Ответственность",
        "8. Жалобы",
        "не проверяет возраст",
        "Подтверждения с датой",
        "взрослая площадка",
    ),
    "docs/LEGAL_FAQ.md": (
        "не юридическая консультация",
        "Required Notice",
        "THIRD_PARTY_NOTICES.md",
        "ACCEPTABLE_USE.md",
        "18 лет",
        "не проверяет возраст",
        "куки",
        "relay",
        "ffmpeg",
        "zapret",
        "omnistash-notices.zip",
    ),
    "ui_src/vendor/hls.js.LICENSE.txt": (
        "Apache License, Version 2.0",
        "Dailymotion",
        "Brightcove",
    ),
}

# Название взрослой площадки и её домены: в публичных текстах их быть не должно
# (контракт из docs/REVIEW-WAVE1.md, п.4). Внутренние id и домены классификатора
# в пакете нет: пакет - тексты, которые читает человек.
ADULT_NAME = re.compile(r"PornHub|PORNHUB|Pornhub")
ADULT_DOMAINS = re.compile(r"pornhub\.(com|org|net)", re.I)

# Утверждения, которые читались бы как inducement, circumvention или
# endorsement. Отрицания («не поставляет», «не проверяет возраст») сюда не
# попадают: они честны и нужны.
FORBIDDEN_CLAIMS = (
    (re.compile(r"обходит\s+(ограничения|блокировки|защиту|запреты|анти-бот|капчу)", re.I),
     "звучит как circumvent (YouTube ToS п.2)"),
    (re.compile(r"преодолевает\s+(блокировки|запреты|ограничения)", re.I),
     "звучит как обход ограничений площадки"),
    (re.compile(r"обход\s+(блокиров|DPI|цензур|анти-бот|капчи)", re.I),
     "публичный текст об обходе фильтров/цензуры"),
    (re.compile(r"анти-?DPI|zapret[-\s]обход", re.I),
     "формулировка «средство обхода DPI»"),
    (re.compile(r"ломает\s+капчу|обходит\s+анти-?бот", re.I),
     "звучит как обход анти-бот/капчи"),
    (re.compile(r"снимает\s+DRM|расшифровыв", re.I),
     "прямой circumvention-claim (DRM)"),
    (re.compile(r"скачивает\s+любой\s+контент|качает\s+всё\s+подряд", re.I),
     "описывает инструмент как mass-download"),
    (re.compile(r"официальный\s+клиент|клиент\s+(YouTube|Google)", re.I),
     "statement о partnership/endorsement (API ToS §13)"),
    (re.compile(r"не\s+несёт\s+ответственности\s+за\s+(незаконн\w*|любые\s+действия)", re.I),
     "выглядит как «инструмент допускает незаконное»"),
    (re.compile(r"(?<!не\s)(?<!не\s)проверяет\s+возраст|(?<!не\s)верифицирует\s+возраст", re.I),
     "ложное утверждение о verification возраста"),
    (re.compile(r"неограниченная\s+квота|безлимитн\w*\s+(квот|API)", re.I),
     "противоречит квотам API (ToS §15)"),
    (re.compile(r"license-free|public\s+domain\s+весь\s+код", re.I),
     "утверждение, что у кода нет правообладателя"),
)

# Запрещённая неточность: cffi распространяется под MIT, «MIT-0» в перечне был
# выдумкой (см. docs/LEGAL-REVIEW.md §5.1).
WRONG_LICENSE = ("MIT-0",)

LINK_MARKDOWN = re.compile(r"\[([^\]]+)\]\([^)]*\)")
BACKTICK = chr(96)


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def read(name: str) -> bytes:
    """Файл из репозитория. Отсутствие называется причиной, а не пустым текстом."""
    target = ROOT / name
    if not target.is_file():
        raise ValueError(f"файл пакета не найден ({name}): юридический пакет не "
                         "собирается - текст условия лицензии отсутствует")
    return target.read_bytes()


def normalize_license(text: str) -> list[str]:
    """Канонический вид текста лицензии: без markdown-ссылок, emphasis и markup.

    Ссылки `[Distribution License](#distribution-license)` сводятся к имени,
    `**bold**` и `` `code` `` снимаются, заголовки теряют решки, цитата - хвост
    `>`. Пробелы внутри абзаца схлопываются: перенос строк в тексте лицензии -
    форматирование, а не содержательная правка.
    """
    text = LINK_MARKDOWN.sub(lambda m: m.group(1), text)
    text = text.replace(BACKTICK, "").replace("*", "")
    paragraphs: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^>\s*", "", line)
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(re.sub(r"\s+", " ", line))
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def license_checks(license_text: str) -> list[str]:
    """Сверка LICENSE с каноническим текстом и с Required Notice."""
    errors: list[str] = []
    try:
        canonical = read(CANONICAL_FILE).decode("utf-8")
    except ValueError as exc:
        return [str(exc)]
    digest = sha256_bytes(canonical.encode("utf-8"))
    if digest != CANONICAL_SHA256:
        errors.append(f"канонический текст лицензии не совпадает с пином: "
                      f"{CANONICAL_FILE} файл {digest}, пин {CANONICAL_SHA256} "
                      f"(источник {CANONICAL_SOURCE})")
        return errors

    canon_paras = normalize_license(canonical)
    ours_paras = normalize_license(license_text)
    missing = [para for para in canon_paras if para not in ours_paras]
    if missing:
        errors.append(f"LICENSE не покрывает канонический текст лицензии "
                      f"({CANONICAL_SPDX}): отсутствуют абзацы - "
                      f"{[para[:60] for para in missing]}")
    kept = [para for para in ours_paras if para in canon_paras]
    if kept != canon_paras:
        errors.append("LICENSE отличается от канонического текста по порядку "
                      "разделов - правки в тексте лицензии не «улучшают» его, "
                      "они расходятся со сверкой по SPDX")
    extras = [para for para in ours_paras if para not in canon_paras]
    stray = [para for para in extras if not para.startswith("Required Notice:")]
    if stray:
        errors.append(f"в LICENSE появились лишние абзацы, которых нет в "
                      f"каноническом тексте лицензии ({CANONICAL_SOURCE}): "
                      f"{[para[:60] for para in stray]} - текст лицензии править нельзя")
    notices = [para for para in extras if para.startswith("Required Notice:")]
    if not notices:
        errors.append("в LICENSE нет строки `Required Notice:` - условие "
                      "«Notices» самой лицензии требует её рядом с текстом")
    elif not any("vilminessa" in para for para in notices):
        errors.append("строка `Required Notice:` не называет правообладателя "
                      "(vilminessa) - notice неполный")
    return errors


def notice_rows(text: str) -> list[tuple[str, str]]:
    """Строки таблиц уведомлений: (первая колонка, вся строка целиком)."""
    rows = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("", "Компонент") or set(cells[0]) <= {"-", " ", ":"}:
            continue
        rows.append((cells[0], line))
    return rows


def direct_dependencies(text: str) -> list[str]:
    """Прямые зависимости из requirements.txt (имена пакетов pip)."""
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for part in line.split(","):
            name = re.split(r"[<>=!~;\[\s]", part.strip())[0].strip()
            if name:
                names.append(name)
    return names


def notices_checks(notices_text: str, requirements_text: str) -> list[str]:
    """Покрытие уведомлений: компоненты, их лицензии и прямые зависимости."""
    errors: list[str] = []
    rows = notice_rows(notices_text)
    for component, tokens in LICENSES_OF.items():
        matching = [row for head, row in rows if component.lower() in head.lower()]
        if not matching:
            errors.append(f"THIRD_PARTY_NOTICES.md не называет компонент {component} - "
                          "уведомления неполны для распространяемого состава exe")
            continue
        joined = "\n".join(matching)
        absent = [token for token in tokens if token.lower() not in joined.lower()]
        if absent:
            errors.append(f"THIRD_PARTY_NOTICES.md называет {component}, но лицензия "
                          f"не указана так, как у стороны: нет {absent}")
    for name in direct_dependencies(requirements_text):
        notice_name = PIP_TO_NOTICE.get(name.lower(), name)
        if not any(name.lower() in head.lower() or notice_name.lower() in head.lower()
                   for head, _row in rows):
            errors.append(f"THIRD_PARTY_NOTICES.md не покрывает прямую зависимость "
                          f"{name} (requirements.txt) - состав exe описан не полным "
                          "перечнем")
    for assertion in NOTICES_ASSERTIONS:
        if assertion not in notices_text:
            errors.append(f"THIRD_PARTY_NOTICES.md не называет {assertion!r} - "
                          "условие стороны или состав сборки описаны не полностью")
    for wrong in WRONG_LICENSE:
        if wrong in notices_text:
            errors.append(f"THIRD_PARTY_NOTICES.md несёт неверное имя лицензии {wrong!r}: "
                          "cffi распространяется под MIT - выдуманное SPDX-имя в "
                          "уведомлениях не право")
    return errors


def claim_hit(pattern: re.Pattern, line: str, match: re.Match) -> bool:
    """Совпадение считается утверждением, а не честным отрицанием или вопросом.

    «не поставляет средства обхода», «программа не проверяет возраст» и
    вопрос «Вы поставляете инструменты обхода?» - это то, что проект говорит
    о себе правду; запретной формулировкой они не становятся.
    """
    if "?" in line:
        return False
    before = line[max(0, match.start() - 15):match.start()].lower()
    return not (before.startswith("не ") or "не " in before or "нет " in before
                or before.endswith("не") or before.endswith("ни"))


def neutral_checks(bodies: dict[str, str]) -> list[str]:
    """Публичные тексты пакета: имя/домены взрослой площадки и запретные утверждения."""
    errors: list[str] = []
    for name, text in bodies.items():
        for index, line in enumerate(text.splitlines(), 1):
            if ADULT_NAME.search(line) or ADULT_DOMAINS.search(line):
                errors.append(f"{name}:{index}: название или домен взрослой площадки "
                              "в публичном тексте пакета - нейтральная форма "
                              "«взрослая площадка»")
            for pattern, reason in FORBIDDEN_CLAIMS:
                for match in pattern.finditer(line):
                    if claim_hit(pattern, line, match):
                        errors.append(f"{name}:{index}: {reason}: "
                                      f"«{match.group(0)[:60]}»")
    return errors


def statements_checks(bodies: dict[str, str]) -> list[str]:
    """Обязательные утверждения: отсутствие названо файлом, а не «тихо прошло»."""
    errors: list[str] = []
    for name, needles in STATEMENTS.items():
        text = bodies.get(name)
        if text is None:
            continue
        absent = [needle for needle in needles if needle not in text]
        if absent:
            errors.append(f"{name}: нет обязательных формулировок {absent} - пакет "
                          "передаёт условия не полностью")
    return errors


def checks(bodies: dict[str, str]) -> list[str]:
    """Все сверки пакета: отказ называется причиной, молчания нет."""
    errors = license_checks(bodies["LICENSE"])
    errors += notices_checks(bodies["THIRD_PARTY_NOTICES.md"],
                             read(REQUIREMENTS).decode("utf-8"))
    errors += neutral_checks(bodies)
    errors += statements_checks(bodies)
    return errors


def manifest(bodies: dict[str, bytes]) -> dict:
    return {
        "tool": "tools/build_legal_pack.py",
        "note": ("пак читается из репозитория, из сети ничего не скачивается; "
                 "рядом с exe уезжают текст условий лицензии, Required Notice, "
                 "уведомления о третьих сторонах, правила использования и FAQ"),
        "license": {
            "spdx": CANONICAL_SPDX,
            "canonicalFile": CANONICAL_FILE,
            "canonicalSha256": CANONICAL_SHA256,
            "source": CANONICAL_SOURCE,
            "fetched": CANONICAL_FETCHED,
            "requiredNotice": "Required Notice: Copyright © 2026 vilminessa",
        },
        "files": {name: {"bytes": len(body), "sha256": sha256_bytes(body)}
                  for name, body in sorted(bodies.items())},
        "archive": ZIP_NAME,
    }


def build(check_only: bool, want_zip: bool) -> tuple[int, list[str]]:
    bodies: dict[str, str] = {}
    raw: dict[str, bytes] = {}
    try:
        for name in PACK_FILES:
            body = read(name)
            raw[name] = body
            bodies[name] = body.decode("utf-8", "replace")
    except ValueError as exc:
        return 1, [str(exc)]

    try:
        errors = checks(bodies)
    except ValueError as exc:  # сверка требует requirements.txt - отказ назван
        return 1, [str(exc)]
    if errors:
        return 1, errors

    if check_only:
        print(f"юр-пак проверен: файлов {len(raw)}, текст лицензии совпадает с "
              f"каноническим ({CANONICAL_SPDX}), уведомления покрывают "
              f"{len(LICENSES_OF)} компонентов и прямые зависимости, публичные "
              "тексты нейтральны")
        return 0, []

    DIST.mkdir(parents=True, exist_ok=True)
    for name, body in raw.items():
        target = DIST / name
        # Пути в пакете - те же, что в репозитории (docs/, ui_src/vendor/),
        # поэтому промежуточный каталог создаётся вместе с вложенным.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    (DIST / "manifest.json").write_text(
        json.dumps(manifest(raw), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"собрано: {DIST} ({len(raw)} файла плюс manifest.json; пути внутри - "
          "те же, что в репозитории)")
    if want_zip:
        zip_path = DIST / ZIP_NAME
        # Список берётся до создания архива и без самого архива: иначе zipfile
        # читает собственный растущий файл и сборка виснет.
        members = [name for name in sorted(DIST.rglob("*"))
                   if name.is_file() and name != zip_path]
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as pack:
            for name in members:
                pack.write(name, name.relative_to(DIST).as_posix())
        print(f"архив: {zip_path} ({zip_path.stat().st_size} байт)")
    return 0, []


def main(argv: list[str]) -> int:
    code, errors = build("--check" in argv, "--zip" in argv)
    for line in errors:
        print(f"отказ: {line}", file=sys.stderr)
    if code:
        print("юр-пак не собран: причины названы выше - молчания нет", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
