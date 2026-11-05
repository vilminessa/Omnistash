"""Страж сверки состава собранного exe (tools/check_exe_contents.py).

Сверка читает TOC, которые пишет PyInstaller (`EXE-00.toc`, `PKG-00.toc` и
`PYZ-00.toc`), карту текстов лицензий `licenses/manifest.json` и размер
артефакта. Здесь она проверяется на синтетических TOC во временной папке:
отказ обязан называть причиной именно то, что против ожидания - битый или
отсутствующий TOC, чужой бинарь, потерянный файл раздачи, потерянный текст
лицензии, обрезанный exe. Молчания нет: ни один из этих случаев не должен
давать «OK».

Замечания - факты, а не отказ: curl_cffi нет в среде сборки (в CI он
ставится из requirements.txt), UPX не найден (стаб ставится отдельно, и exe
не сжат), секретов по байтам exe сверка не проверяет - PYZ сжат zlib.
"""

import importlib.util
import json
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "Omnistash.spec"
CI = ROOT / ".github" / "workflows" / "ci.yml"
BETA = ROOT / ".github" / "workflows" / "beta.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CHECKER = ROOT / "tools" / "check_exe_contents.py"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
DEBUG_BAT = ROOT / "debug.bat"
LEGAL = ROOT / "docs" / "LEGAL-REVIEW.md"


def _load_tool():
    spec = importlib.util.spec_from_file_location("check_exe_contents", CHECKER)
    assert spec and spec.loader, "tools/check_exe_contents.py не загружается"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load_tool()

# Минимальная карта текстов лицензий: два компонента, оба с текстом на диске.
CARD_COMPONENTS = {
    "cffi": "licenses/cffi/LICENSE",
    "yt-dlp": "licenses/yt-dlp/LICENSE",
}

CLIENT_FILES = [
    "ui_src/index.html", "ui_src/app.css", "ui_src/app.js",
    "ui_src/watch.html", "ui_src/watch.js",
    "ui_src/vendor/hls.js", "ui_src/vendor/hls.js.meta.json",
    "ui_src/vendor/hls.js.LICENSE.txt",
]

SEP = "\\"


def repo_path(root: pathlib.Path, name: str) -> str:
    """Путь внутри репозитория с обратными слэшами - так их пишет PyInstaller."""
    return str(root).replace("/", SEP) + SEP + name.replace("/", SEP)


def foreign_path(name: str) -> str:
    """Путь чужого пакета (site-packages) - не из репозитория."""
    return f"C:{SEP}site-packages{SEP}" + name.replace("/", SEP)


def good_entries(root: pathlib.Path) -> list[tuple[str, str, str]]:
    """Состав, который обязан пройти сверку: рантайм, раздача, тексты лицензий."""
    entries = [
        ("python314.dll", repo_path(root, "python314.dll"), "BINARY"),
        ("libcrypto-3.dll", f"C:{SEP}Python314{SEP}DLLs{SEP}libcrypto-3.dll", "BINARY"),
        ("libssl-3.dll", f"C:{SEP}Python314{SEP}DLLs{SEP}libssl-3.dll", "BINARY"),
        ("_sqlite3.pyd", f"C:{SEP}Python314{SEP}DLLs{SEP}_sqlite3.pyd", "EXTENSION"),
        ("webview2loader.dll", foreign_path("webview/lib/webview2loader.dll"), "BINARY"),
        ("Microsoft.Web.WebView2/Core/WebView2.dll",
         foreign_path("webview/lib/Microsoft.Web.WebView2/Core/WebView2.dll"), "BINARY"),
        # Наш модуль, а не чужой пакет: имя wormhole не должно читаться как
        # «magic-wormhole внутри exe».
        ("app.wormhole", repo_path(root, "app/wormhole.py"), "PYMODULE"),
        # Python-код yt-dlp, который вызывает внешний ffmpeg: бинаря здесь нет.
        ("yt_dlp/postprocessor/ffmpeg.py", foreign_path("yt_dlp/postprocessor/ffmpeg.py"),
         "PYMODULE"),
        ("yt_dlp/__init__.py", foreign_path("yt_dlp/__init__.py"), "PYMODULE"),
        ("certifi/cacert.pem", foreign_path("certifi/cacert.pem"), "DATA"),
    ]
    for name in CLIENT_FILES:
        entries.append((name, repo_path(root, name), "DATA"))
    for name in ("licenses/manifest.json", "licenses/cffi/LICENSE",
                 "licenses/yt-dlp/LICENSE"):
        entries.append((name, repo_path(root, name), "DATA"))
    return entries


def write_tocs(build: pathlib.Path, entries: list[tuple[str, str, str]]) -> None:
    """TOC как текст Python-списка кортежей (то, что пишет PyInstaller)."""
    body = "[\n" + ",\n".join(
        f"    ({name!r}, {where!r}, {kind.upper()!r})"
        for name, where, kind in entries) + "\n]"
    for toc in CHECK.TOCS:
        (build / toc).write_text(body, encoding="utf-8")


def make_env(exe_bytes: int = 21_000_000):
    """(tmp, root, build, exe) во временной папке: карта, тексты, TOC, артефакт.

    Состав TOC пишет make_env по good_entries; вызывающий тест правит его под
    свой случай через env(mutate=...).
    """
    tmp = tempfile.TemporaryDirectory(prefix="omnistash-exe-check-")
    root = pathlib.Path(tmp.name)
    for path in CARD_COMPONENTS.values():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("MIT No Attribution\n", encoding="utf-8")
    card = {
        "tool": "сбор текстов лицензий бандла",
        "collected": "2026-11-05",
        "note": "тексты кладутся байт-в-байт и закрепляются по sha256",
        "entries": [{"component": name, "files": [{"path": path, "bytes": 20,
                                                   "sha256": "0" * 64}]}
                    for name, path in CARD_COMPONENTS.items()],
    }
    (root / CHECK.LICENSES_MANIFEST).write_text(
        json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    build = root / "build" / "Omnistash"
    build.mkdir(parents=True)
    write_tocs(build, good_entries(root))
    exe = root / "Omnistash.exe"
    exe.write_bytes(b"\0" * exe_bytes)
    return tmp, root, build, exe


def env(mutate=None, exe_bytes: int = 21_000_000):
    """Окружение с составом TOC, изменённым под случай теста."""
    tmp, root, build, exe = make_env(exe_bytes)
    entries = good_entries(root)
    write_tocs(build, mutate(entries) if mutate else entries)
    return tmp, root, build, exe


def without(name: str):
    """Убрать запись из состава (имитация: файл не вошёл в сборку)."""
    return lambda items: [item for item in items if item[0] != name]


def with_extra(*items: tuple[str, str, str]):
    """Добавить записи в состав (имитация: лишнее попало внутрь exe)."""
    return lambda base: base + list(items)


class TestCheckRefusals(unittest.TestCase):
    """Отказ называет причиной то, что против ожидания - молчания нет."""

    def test_good_composition_passes_and_names_the_facts(self):
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        errors, notes = CHECK.check(root, build, exe)
        self.assertEqual(errors, [], "\n".join(errors))
        joined = "\n".join(notes)
        self.assertIn("текстов лицензий бандла внутри exe: 3 из 3", joined,
                      "сверка не называет, сколько текстов лицензий в exe")
        self.assertIn("размер Omnistash.exe", joined)
        self.assertIn("записей в TOC", joined)

    def test_forbidden_binaries_are_refused(self):
        tmp, root, build, exe = env(with_extra(
            ("ffmpeg.exe", f"C:{SEP}bin{SEP}ffmpeg.exe", "BINARY"),
            ("ffprobe.exe", f"C:{SEP}bin{SEP}ffprobe.exe", "BINARY"),
        ))
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        self.assertEqual(len(errors), 2, errors)
        joined = "\n".join(errors)
        self.assertIn("ffmpeg.exe", joined)
        self.assertIn("ffprobe.exe", joined)
        self.assertIn("GPL", joined, "отказ не называет лицензию GPL чужого бинаря")

    def test_foreign_packages_are_refused(self):
        tmp, root, build, exe = env(with_extra(
            ("zeroconf", foreign_path("zeroconf/__init__.py"), "PYMODULE"),
            ("twisted.internet.base", foreign_path("twisted/internet/base.py"), "PYMODULE"),
            ("wormhole", foreign_path("wormhole/__main__.py"), "PYMODULE"),
            ("pynacl", foreign_path("pynacl-1.0.dist-info/METADATA"), "DATA"),
        ))
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        joined = "\n".join(errors)
        for token in ("zeroconf", "twisted", "wormhole", "pynacl"):
            self.assertIn(token, joined, f"{token}: чужой пакет не назван отказом")

    def test_own_module_names_are_not_refused(self):
        """Точность сверки: наш `app.wormhole` и модуль ffmpeg у yt-dlp -
        Python-код, а не чужой пакет и не бинарь ffmpeg."""
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_missing_client_file_is_refused(self):
        tmp, root, build, exe = env(without("ui_src/watch.html"))
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        self.assertIn("ui_src/watch.html", "\n".join(errors),
                      "страница плеера потеряна: --serve нечем раздавать, а отказ молчит")

    def test_missing_vendor_license_is_refused(self):
        tmp, root, build, exe = env(without("ui_src/vendor/hls.js.LICENSE.txt"))
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        self.assertIn("hls.js.LICENSE.txt", "\n".join(errors),
                      "notice Apache-2.0 не уехал вместе с файлом вендора")

    def test_license_texts_inside_exe_are_required(self):
        """Каталог текстов бандла обязан быть внутри exe: получатель копии exe,
        которого нет в релизе, должен получить тексты условий."""
        tmp, root, build, exe = env(
            lambda items: [item for item in items
                           if not item[0].startswith("licenses/")])
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        joined = "\n".join(errors)
        self.assertIn("licenses/", joined, "текстов лицензий в exe нет - сверка молчит")
        self.assertIn("получатель копии exe", joined,
                      "отказ не называет, кому не пришли тексты условий")

    def test_one_missing_license_text_is_named(self):
        tmp, root, build, exe = env(without("licenses/yt-dlp/LICENSE"))
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        self.assertIn("licenses/yt-dlp/LICENSE", "\n".join(errors),
                      "карта называет текст, которого в exe нет - сверка его не назвала")

    def test_card_is_the_only_list_and_extra_text_is_refused(self):
        """Файл каталога, которого карта не называет, - отказ: текст без пина
        sha256 не должен уезжать в exe и в пакет."""
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        extra = root / "licenses" / "chudo" / "LICENSE"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("текст без пина\n", encoding="utf-8")
        errors, _notes = CHECK.check(root, build, exe)
        joined = "\n".join(errors)
        self.assertIn("карта не называет", joined, "лишний текст каталога не назван отказом")
        self.assertIn("chudo", joined)

    def test_broken_or_missing_toc_is_named(self):
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        (build / "EXE-00.toc").unlink()
        errors, _notes = CHECK.check(root, build, exe)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("EXE-00.toc", errors[0], "отсутствующий TOC не назван именем")
        self.assertIn("не найден", errors[0])
        (build / "EXE-00.toc").write_text("это не список кортежей (", encoding="utf-8")
        errors, _notes = CHECK.check(root, build, exe)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("EXE-00.toc", errors[0], "битый TOC не назван именем")
        self.assertIn("не разбирается", errors[0])

    def test_missing_license_card_is_named(self):
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        (root / CHECK.LICENSES_MANIFEST).unlink()
        errors, _notes = CHECK.check(root, build, exe)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("manifest.json", errors[0], "карта текстов не названа причиной")
        self.assertIn("не найдена", errors[0])

    def test_small_exe_is_refused(self):
        tmp, root, build, exe = make_env(exe_bytes=1_000_000)
        self.addCleanup(tmp.cleanup)
        errors, _notes = CHECK.check(root, build, exe)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("1000000", errors[0], "размер обрезанной сборки не назван")
        self.assertIn("20000000", errors[0], "граница сверки не названа в отказе")

    def test_missing_artifact_is_refused(self):
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        exe.unlink()
        errors, _notes = CHECK.check(root, build, exe)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("не найден", errors[0], "отсутствующий артефакт - молчание")


class TestCheckNotes(unittest.TestCase):
    """Замечания называют факты сборки, а не отказ."""

    def test_curl_cffi_absent_is_a_note_not_a_refusal(self):
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        errors, notes = CHECK.check(root, build, exe)
        self.assertEqual(errors, [])
        joined = "\n".join(notes)
        self.assertIn("curl_cffi не в exe", joined,
                      "curl_cffi отсутствует в среде сборки - замечание не названо")
        self.assertIn("замечание, а не отказ", joined)

    def test_curl_cffi_present_is_not_a_note(self):
        tmp, root, build, exe = env(with_extra(
            ("curl_cffi", foreign_path("curl_cffi/__init__.py"), "PYMODULE"),
            ("libcurl.dll", foreign_path("curl_cffi/lib/libcurl.dll"), "BINARY"),
        ))
        self.addCleanup(tmp.cleanup)
        errors, notes = CHECK.check(root, build, exe)
        self.assertEqual(errors, [])
        self.assertNotIn("curl_cffi не в exe", "\n".join(notes))

    def test_upx_note_names_the_stub_condition(self):
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        log = build.parent / "build_log.txt"
        log.write_text("INFO: Compression using upx 3.96\n", encoding="utf-8")
        errors, notes = CHECK.check(root, build, exe)
        self.assertEqual(errors, [])
        joined = "\n".join(notes)
        self.assertIn("UPX упомянут", joined)
        self.assertIn("неизменённым", joined,
                      "условие исключения UPX (официальный неизменённый стаб) не назван")
        log.write_text("INFO: Building EXE completed\n", encoding="utf-8")
        errors, notes = CHECK.check(root, build, exe)
        self.assertEqual(errors, [])
        self.assertIn("UPX в логе сборки не упомянут", "\n".join(notes),
                      "exe не сжат: сверка обязана назвать это фактом, а не молчать")

    def test_secrets_are_not_claimed_from_exe_bytes(self):
        """Сверка не заявляет «секретов в exe нет» по байтам: PYZ сжат zlib,
        строк внутри не видно. Ограничение названо в замечаниях."""
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        errors, notes = CHECK.check(root, build, exe)
        self.assertEqual(errors, [])
        self.assertIn("PYZ сжат", "\n".join(notes),
                      "ограничение сверки секретов не названо - вывод был бы враньём")

    def test_cli_reports_ok_and_failure_with_exit_code(self):
        tmp, root, build, exe = make_env()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(CHECK.main([f"--exe={exe}", f"--builddir={build}",
                                     f"--root={root}"]), 0)
        exe.unlink()
        self.assertEqual(CHECK.main([f"--exe={exe}", f"--builddir={build}",
                                     f"--root={root}"]), 1,
                         "битый состав обязан дать ненулевой код выхода")


class TestSpecCarriesTheLicenseTexts(unittest.TestCase):
    """Каталог текстов бандла вшивается в exe (условие weak-notice для копии)."""

    def test_spec_adds_the_licenses_dir(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn('datas += [("licenses", "licenses")]', text,
                      "текстов лицензий бандла в exe нет: копия exe, переданная мимо "
                      "релиза, не несёт условий MIT/BSD/Apache/MIT-0/MPL")
        self.assertIn("check_exe_contents.py", text,
                      "spec не называет, чем сверяется состав exe")
        self.assertIn("licenses/manifest.json", text,
                      "spec не называет, что карта - единственный список текстов")
        self.assertLess(text.index('datas += [("ui_src", "ui_src")]'),
                        text.index('datas += [("licenses", "licenses")]'),
                        "клиентский каталог и тексты лицензий добавлены в непорядке")

    def test_upx_stays_a_flag_not_a_claim(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn("upx=True", text,
                      "spec не ставит upx: сжатие выключено, и это должно быть видно")


class TestPipelineRunsTheCheck(unittest.TestCase):
    """Конвейер и консоль сверяют состав exe после проверки размера и до хеша."""

    def test_beta_and_release_run_the_check_after_size_before_hash(self):
        for path in (BETA, RELEASE):
            text = path.read_text(encoding="utf-8")
            self.assertIn("python tools/check_exe_contents.py", text,
                          f"{path.name}: состав exe не сверяется - чужой бинарь или "
                          "потерянный текст лицензии уехал бы в релиз молча")
            size_at = text.index("assert s > 20_000_000")
            check_at = text.index("python tools/check_exe_contents.py")
            hash_at = text.index("digests.b64")
            self.assertLess(size_at, check_at,
                            f"{path.name}: сверка состава идёт до проверки размера")
            self.assertLess(check_at, hash_at,
                            f"{path.name}: хеш считается до сверки состава - битый "
                            "состав был бы подписан провенансом")
            self.assertIn("Сверка состава exe по TOC", text,
                          f"{path.name}: шаг сверки без имени - читаемый отчёт потерян")

    def test_ci_does_not_claim_an_exe_check(self):
        """ci.yml exe не собирает: сверка состава там невозможна, и её там нет."""
        text = CI.read_text(encoding="utf-8")
        self.assertNotIn("check_exe_contents.py", text,
                         "ci сверял бы состав exe, которого ci не строит - это "
                         "утверждение без сборки")

    def test_debug_console_builds_and_checks(self):
        debug = DEBUG_BAT.read_text(encoding="utf-8", errors="replace")
        self.assertIn("[F] Собрать exe и сверить состав по TOC", debug,
                      "консоль не даёт собрать exe и сверить его состав")
        self.assertIn("goto op_build_exe", debug, "пункт меню не проводит к операции")
        self.assertIn("python -m PyInstaller Omnistash.spec", debug,
                      "сборка exe в консоли не названа командой")
        self.assertIn("tools\\check_exe_contents.py", debug,
                      "сверка состава после сборки в консоли не названа")
        self.assertLess(debug.index("python -m PyInstaller Omnistash.spec"),
                        debug.index("tools\\check_exe_contents.py"),
                        "сверка идёт до сборки exe - сверялся бы прошлый состав")


class TestDocsSayTheTextsAreInsideExe(unittest.TestCase):
    """Документы называют, что тексты условий вшиты в exe и чем это сверяется."""

    def test_notices_name_the_embedded_texts(self):
        text = NOTICES.read_text(encoding="utf-8")
        self.assertIn("вшиваются в exe", text,
                      "notices не называет, что тексты бандла внутри exe")
        self.assertIn("Omnistash.spec", text)
        self.assertIn("tools/check_exe_contents.py", text,
                      "notices не называет инструмент сверки состава exe")
        self.assertIn("мимо релиза", text,
                      "не названо, для кого вшивание нужно: копия exe без релиза")

    def test_readme_names_the_check_and_the_step(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("python tools/check_exe_contents.py", text,
                      "README не называет команду сверки состава exe")
        self.assertIn("Сверка состава exe по TOC", text,
                      "README не описывает шаг конвейера после проверки размера")
        self.assertIn("до хеша для SLSA", text,
                      "не сказано, что сверка идёт до хеша и подписи провенанса")
        self.assertIn("debug.bat F", text, "README не называет пункт консоли сборки exe")

    def test_contributing_lists_the_check(self):
        text = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn("tools/check_exe_contents.py", text,
                      "CONTRIBUTING не называет сверку состава exe")
        self.assertIn("PyInstaller Omnistash.spec", text,
                      "не названо, что сверке нужна сборка exe")

    def test_legal_review_closes_the_exe_items(self):
        text = LEGAL.read_text(encoding="utf-8")
        for fragment in (
            "[x] В собранном exe **нет** ffmpeg/ffprobe",
            "[x] Проверка, что PyInstaller и UPX не превращают exe в GPL",
            "[x] Никаких ключей/секретов в exe",
            "~30.4 МБ",
            "сверяется нижняя граница 20 МБ, а не размер",
            "30 433 478 байт",
            "PYZ сжат zlib",
            "вшивается в exe",
            "tests/test_adult_gate.py` (16 проверок",
        ):
            self.assertIn(fragment, text, f"пункт review не закрыт и не назван: {fragment}")

    def test_no_invisible_or_glued_characters(self):
        cjk = re.compile(r"[\u3400-\u9fff\uac00-\udfff\u3000-\u303f\uff00-\uffff]")
        glue = re.compile(r"[A-Za-z][а-яё]|[а-яё][A-Za-z]")
        files = [SPEC, CI, BETA, RELEASE, CHECKER, NOTICES, README, CONTRIBUTING,
                 DEBUG_BAT, LEGAL, pathlib.Path(__file__)]
        hits = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for index, line in enumerate(text.splitlines(), 1):
                for char in line:
                    if cjk.search(char):
                        hits.append(f"CJK {path.name}:{index} U+{ord(char):04X}")
                for match in glue.finditer(line):
                    hits.append(f"склейка {path.name}:{index}: "
                                f"{line[max(0, match.start() - 30):match.end() + 30]}")
        self.assertEqual(hits, [], "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
