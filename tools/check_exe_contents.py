"""Сверка состава собранного Omnistash.exe по TOC (docs/LEGAL-REVIEW.md §5.2).

TOC - списки кортежей (имя, путь, тип), которые PyInstaller пишет в
`build/Omnistash/EXE-00.toc`, `PKG-00.toc` и `PYZ-00.toc`. Сверка читает их и
называет причиной то, что против ожидания: «в exe есть ffmpeg.exe», «текстов
лицензий бандла в exe нет», «страница плеера не вшита». Молчания нет:
отсутствующий или битый TOC - отказ с именем файла.

    python tools/check_exe_contents.py            # сверка после сборки exe
    python tools/check_exe_contents.py --exe Путь # другой артефакт
    python tools/check_exe_contents.py --builddir Каталог # TOC из другой сборки

Что проверяется и почему:
  * бинарников, которых в exe не должно быть: ffmpeg.exe и ffprobe.exe (GPL -
    проект не поставляет, их ставит пользователь), и пакетов zeroconf (LGPL),
    Twisted и PyNaCl (не входят: magic-wormhole живёт в venv профиля). Имя
    модуля `ffmpeg.py` у yt-dlp - Python-код, который вызывает внешний ffmpeg,
    и оно не читается как «ffmpeg внутри exe»: сверка ищет именно .exe и
    именно чужие пакеты (путь вне репозитория), а не подстроку в тексте;
  * рантайма, который обязан быть: python3xx.dll, libcrypto-3 и libssl-3
    (OpenSSL для TLS), _sqlite3.pyd (индекс библиотеки), бинари WebView2 -
    их условия названы в THIRD_PARTY_NOTICES.md;
  * клиентских файлов, которые раздаёт --serve: ui_src/{index.html,app.css,
    app.js,watch.html,watch.js,vendor/hls.js,vendor/hls.js.meta.json,
    vendor/hls.js.LICENSE.txt} - без них сервер нечем раздавать страницу
    плеера, а notice Apache-2.0 не уезжает вместе с файлом;
  * текстов лицензий бандла внутри exe: условия MIT/BSD/Apache-2.0/MPL требуют,
    чтобы их получил каждый, кто получил копию программы. Каталог `licenses/`
    вшит в exe (`Omnistash.spec`), поэтому копия exe, которую прислали мимо
    релиза, тоже несёт тексты. Единственный список того, что уезжает - карта
    `licenses/manifest.json`: сверка требует каждый файл из неё и называет
    отказом файл каталога, которого в карте нет;
  * размера exe: ниже 20 МБ сборка пустая или обрезанная - граница та же, что
    проверяет шаг CI «Проверка артефакта»;
  * curl_cffi - необязательный: нет в среде сборки - exe собирается без него,
    имитация TLS-отпечатка выключается мягко (настройка «Имитация браузера»).
    Это замечание, а не отказ - в CI пакет ставится из requirements.txt;
  * UPX - замечание по логу сборки: стаб не найден - exe не сжат; если сборка
    сжата, стаб обязан быть официальным и неизменённым (условие исключения).

Сверка ничего не скачивает: читает TOC, карту текстов и артефакт.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "Omnistash"
EXE = ROOT / "Omnistash.exe"
TOCS = ("EXE-00.toc", "PKG-00.toc", "PYZ-00.toc")
EXE_MIN_BYTES = 20_000_000

# Бинари, которые проект не распространяет: GPL-сборки ffmpeg и ffprobe.
FORBIDDEN_BINARIES = ("ffmpeg.exe", "ffprobe.exe")
# Чужие пакеты, которых в exe не должно быть (docs/LEGAL-REVIEW.md §2 и §5.2).
FORBIDDEN_PACKAGES = ("zeroconf", "twisted", "pynacl", "nacl", "wormhole")
# Рантайм и компоненты, которые обязаны быть внутри exe.
RUNTIME = ("python3", "libcrypto-3", "libssl-3", "_sqlite3",
           "webview2loader.dll", "microsoft.web.webview2")
# Файлы раздачи --serve (S4-S7) - те же, что собирает tools/build_client.py.
CLIENT_DATA = ("ui_src/index.html", "ui_src/app.css", "ui_src/app.js",
               "ui_src/watch.html", "ui_src/watch.js",
               "ui_src/vendor/hls.js", "ui_src/vendor/hls.js.meta.json",
               "ui_src/vendor/hls.js.LICENSE.txt")
LICENSES_DIR = "licenses"
LICENSES_MANIFEST = "licenses/manifest.json"
# Необязательные компоненты: их отсутствие называется замечанием, не отказом.
OPTIONAL = ("curl_cffi",)


class Stub:
    """Заглушка для вызовов versioninfo внутри EXE-00.toc."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __call__(self, *args, **kwargs):
        return Stub(*args, **kwargs)

    def __getattr__(self, name):
        return Stub()


def load_toc(path: pathlib.Path) -> object:
    if not path.is_file():
        raise ValueError(f"{path.name} не найден ({path}) - сборка не закончилась, "
                         "сверка состава невозможна")
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return eval(text, {"__builtins__": {}}, {"versioninfo": Stub()})  # noqa: S307
    except Exception as exc:  # noqa: BLE001 - битый TOC называется причиной
        raise ValueError(f"{path.name} не разбирается: {type(exc).__name__}: {exc}") from exc


def triples(node, out: list[tuple[str, str, str]]) -> None:
    """Собирает кортежи из трёх строк (имя, путь, тип) из произвольного узла."""
    if isinstance(node, tuple) and len(node) == 3 and all(isinstance(x, str) for x in node):
        name, where, kind = node
        out.append((name.replace("\\", "/"), where.replace("\\", "/"), kind.lower()))
        return
    if isinstance(node, (list, tuple)):
        for child in node:
            triples(child, out)
    elif isinstance(node, dict):
        for child in node.values():
            triples(child, out)
    elif isinstance(node, Stub):
        for child in node.args:
            triples(child, out)
        for value in node.kwargs.values():
            triples(value, out)


def read_entries(build: pathlib.Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(записи всех TOC, отказ) - битый или отсутствующий TOC называется именем."""
    entries: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for toc in TOCS:
        try:
            triples(load_toc(build / toc), entries)
        except ValueError as exc:
            errors.append(str(exc))
    return entries, errors


def license_card(root: pathlib.Path) -> tuple[list[str], list[str]]:
    """Пути текстов лицензий из карты - единственного списка того, что уезжает.

    Возвращает (файлы, отказ). Карта сверяется с каталогом на диске: файл
    каталога, которого в карте нет, назван отказом - иначе в exe мог бы
    попасть текст, который никто не закрепил по sha256.
    """
    errors: list[str] = []
    path = root / LICENSES_MANIFEST
    if not path.is_file():
        return [], [f"карта {path} не найдена - состав текстов лицензий не назван, "
                    "сверка состава exe невозможна"]
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [], [f"карта {path.name} читается не как JSON: {exc} - "
                    "сверка состава невозможна"]
    files: list[str] = []
    for entry in card.get("entries") or []:
        for item in entry.get("files") or []:
            name = str(item.get("path") or "")
            if not name:
                errors.append(f"карта {path.name}: компонент {entry.get('component')} "
                              "не называет путь текста лицензии")
            elif not name.startswith(LICENSES_DIR + "/"):
                errors.append(f"карта {path.name}: путь {name} вне каталога "
                              f"{LICENSES_DIR}/ - карта обязана называть только его")
            else:
                files.append(name)
    if not files:
        errors.append(f"карта {path.name}: текстов лицензий не названо ни одного - "
                      "юр-пакет и exe были бы без текстов условий")
    if not (root / LICENSES_DIR).is_dir():
        return [], [f"каталога {LICENSES_DIR}/ нет ({root / LICENSES_DIR}) - текстов "
                    "лицензий бандла в репозитории нет, сверка состава невозможна"]
    on_disk = sorted(p.relative_to(root).as_posix()
                     for p in (root / LICENSES_DIR).rglob("*") if p.is_file())
    if LICENSES_MANIFEST not in on_disk:
        errors.append(f"файла карты {LICENSES_MANIFEST} на диске нет - "
                      "карта названа, но её текст не собран")
    extra = sorted(set(on_disk) - set(files) - {LICENSES_MANIFEST})
    if extra:
        errors.append(f"в каталоге {LICENSES_DIR}/ есть файлы, которых карта не "
                      f"называет: {extra} - текст без пина sha256 не уезжает")
    missing = sorted(set(files) - set(on_disk))
    if missing:
        errors.append(f"карта называет файлы, которых на диске нет: {missing} - "
                      "сверка по пинам невозможна")
    return sorted(files + [LICENSES_MANIFEST]), errors


def is_third_party(where: str, root: pathlib.Path) -> bool:
    """Запись не из репозитория: чужой пакет (site-packages) или stdlib."""
    lower = where.lower()
    if "site-packages" in lower or "dist-info" in lower:
        return True
    try:
        return root.resolve() not in pathlib.Path(where).resolve().parents
    except (OSError, ValueError):
        return False


def check(root: pathlib.Path, build: pathlib.Path, exe: pathlib.Path) -> tuple[list[str], list[str]]:
    """(отказы, замечания). Отказ - то, что против ожидания; замечание - факт."""
    entries, errors = read_entries(build)
    wanted, card_errors = license_card(root)
    errors += card_errors
    if errors:
        return errors, []

    third = [n for n, where, _k in entries if is_third_party(where, root)]
    all_blob = "\n".join(name for name, _w, _k in entries).lower()

    for token in FORBIDDEN_BINARIES:
        if any(name.lower().endswith(token) for name, _w, k in entries if k == "binary"):
            errors.append(f"в exe есть {token} - проект это не поставляет "
                          "(THIRD_PARTY_NOTICES.md): сборка нарушает условие "
                          "лицензии GPL")
    for token in FORBIDDEN_PACKAGES:
        hit = [name for name in third
               if re.search(rf"(^|[/.]){token}", name.lower())]
        if hit:
            errors.append(f"в exe есть пакет {token} ({hit[0]}) - он не входит в "
                          "состав проекта (условия LGPL/BSD для чужого кода): "
                          "сборка вшивает то, что должно ставиться отдельно")

    for token in RUNTIME:
        if token not in all_blob:
            errors.append(f"в exe нет {token} - рантайм или компонент потерян "
                          "(python3xx.dll, OpenSSL для TLS, SQLite, WebView2)")

    data = {name.lower() for name, _w, k in entries if k == "data"}
    for token in CLIENT_DATA:
        if token.lower() not in data:
            errors.append(f"в exe нет {token} - --serve нечем раздавать страницу "
                          "клиента, или notice вендора не уезжает с файлом")

    packed = [name for name in wanted if name.lower() not in data]
    if packed:
        errors.append(f"в exe нет текстов лицензий бандла: {packed[:6]} "
                      + (f" (всего {len(packed)})" if len(packed) > 6 else "")
                      + " - получатель копии exe не получил условия MIT/BSD/"
                        "Apache-2.0/MIT-0/MPL этих компонентов")
    if not any(name.startswith(LICENSES_DIR + "/") for name in data):
        errors.append(f"в exe нет каталога {LICENSES_DIR}/ - тексты лицензий бандла "
                      "не вшиты (условие weak-notice для получателя копии)")

    # Тексты лицензий (data) не доказывают, что модуль или его нативная либа
    # вошли в сборку: для необязательных компонентов смотрим код и бинари.
    code_blob = "\n".join(name for name, _w, k in entries
                          if k in ("pymodule", "binary", "extension")).lower()
    notes: list[str] = []
    for token in OPTIONAL:
        if token not in code_blob:
            notes.append(f"{token} не в exe: пакета нет в среде сборки - имитация "
                         "TLS-отпечатка выключается мягко (настройка «Имитация "
                         "браузера»), это замечание, а не отказ")
    log = build.parent / "build_log.txt"
    if log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        if re.search(r"upx", text, re.IGNORECASE):
            notes.append("UPX упомянут в логе сборки: сжатый exe распространяется по "
                         "исключению UPX только с официальным неизменённым стабом")
        else:
            notes.append("UPX в логе сборки не упомянут: стаб не найден - exe не сжат")
    else:
        notes.append(f"лога {log} нет: UPX сверяется по выводу джоба сборки "
                     "(Omnistash.spec ставит upx=True, стаб ставится отдельно)")

    if not exe.is_file():
        errors.append(f"артефакт не найден ({exe}) - сверка размера невозможна")
    elif exe.stat().st_size <= EXE_MIN_BYTES:
        errors.append(f"{exe.name} весит {exe.stat().st_size} байт - ниже границы "
                      f"{EXE_MIN_BYTES}: сборка пустая или обрезанная")
    else:
        notes.append(f"размер {exe.name}: {exe.stat().st_size} байт "
                     f"(граница {EXE_MIN_BYTES} пройдена)")
    notes.append(f"записей в TOC: {len(entries)} (EXE, PKG и PYZ)")
    notes.append(f"текстов лицензий бандла внутри exe: {len(wanted) - len(packed)} "
                 f"из {len(wanted)} (карта licenses/manifest.json)")
    notes.append("LICENSE и THIRD_PARTY_NOTICES.md в exe не кладутся: их несёт "
                 "ассет релиза omnistash-notices.zip (tools/build_legal_pack.py)")
    notes.append("секретов по байтам exe сверка не проверяет: PYZ сжат zlib, "
                 "строк внутри не видно; проверка делается по исходникам и "
                 "тестам (tests/test_oauth.py - PKCE без client_secret, "
                 "tests/test_google_account.py - значения на диске шифрованы)")
    return errors, notes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--exe", default=str(EXE))
    parser.add_argument("--builddir", default=str(BUILD))
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args(argv)
    if args.help:
        print(__doc__)
        return 0

    errors, notes = check(pathlib.Path(args.root), pathlib.Path(args.builddir),
                          pathlib.Path(args.exe))
    for note in notes:
        print(f"  - {note}")
    for error in errors:
        print(f"отказ: {error}", file=sys.stderr)
    if errors:
        print("состав exe не прошёл сверку: причины названы выше - молчания нет",
              file=sys.stderr)
        return 1
    print("состав exe сверен: запретных бинарей и чужих пакетов нет, рантайм на "
          "месте, клиентские файлы раздачи и тексты лицензий бандла внутри exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
