"""Страж LC2: тексты лицензий бандла в юр-пакете и их карта.

Weak-notice лицензии (MIT, BSD, Apache, MPL) требуют, чтобы каждый, кто
получает копию программы, получил и их тексты - не только THIRD_PARTY_NOTICES.md
со списком имён. Проверяем, что тексты лежат в репозитории, уезжают в пакете
рядом с exe и что они сверяются, а не декларируются:

  * карта `licenses/manifest.json` - единственный список: она называет компонент,
    имя лицензии, откуда взят текст (dist-info установленного пакета или URL с
    датой) и пин по sha256 на каждый файл;
  * пин сверяется по байтам файла, размер - по карте; подмена текста или пина
    называется причиной;
  * имя лицензии обязано совпадать с текстом файла (маркеры текста), а не с
    метадатой пакета: cffi - MIT-0 (MIT No Attribution) по тексту самого
    пакета, proxy_tools - BSD 2-Clause с правообладателями по тексту upstream;
  * компонент без текста обязан быть назван с причиной (SQLite Public Domain,
    WebView2 - Microsoft не публикует текст файлом, hls.js - текст рядом с
    файлом в исходниках, ffmpeg и zeroconf - не поставляются, magic-wormhole -
    не входит в exe); молчание не право;
  * пакет пишет тексты лицензий и карту, архив несёт их; manifest пакета
    описывает состав текстов;
  * документы: README, THIRD_PARTY_NOTICES и LEGAL-REVIEW §5.2 называют тексты;
    gitignore не прячет licenses/ (это источник, а не сборка).
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "build_legal_pack", ROOT / "tools" / "build_legal_pack.py")
    assert spec and spec.loader, "tools/build_legal_pack.py не загружается"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load_tool()
LICENSES = ROOT / "licenses"
CARD = ROOT / BUILD.LICENSES_MANIFEST
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
README = ROOT / "README.md"
LEGAL = ROOT / "docs" / "LEGAL-REVIEW.md"
GITIGNORE = ROOT / ".gitignore"


def card() -> dict:
    return json.loads(CARD.read_text(encoding="utf-8"))


def card_paths(source: dict) -> list[str]:
    return [file["path"] for entry in source["entries"] for file in entry["files"]]


class TestLicenseCard(unittest.TestCase):
    """Карта текстов лицензий: пины, происхождение и покрытие состава."""

    def test_card_exists_and_is_the_only_list(self):
        self.assertTrue(CARD.is_file(), "карта текстов лицензий не найдена")
        self.assertTrue((LICENSES / "manifest.json").is_file(),
                        "карта не лежит рядом с текстами - пакет не найдёт её")
        texts, errors, summary = BUILD.license_texts()
        self.assertEqual(errors, [], errors)
        self.assertEqual(sorted(texts), sorted(card_paths(card())),
                        "список текстов расходится с картой")
        self.assertEqual(summary["files"], len(texts))

    def test_pins_match_the_files_on_disk(self):
        for entry in card()["entries"]:
            for file in entry["files"]:
                body = (ROOT / file["path"]).read_bytes()
                self.assertEqual(BUILD.sha256_bytes(body), file["sha256"],
                                 f"{file['path']}: текст не совпадает с пином карты")
                self.assertEqual(len(body), file["bytes"],
                                 f"{file['path']}: размер не совпадает с картой")
                self.assertTrue(body, f"{file['path']}: пустой текст лицензии")

    def test_every_entry_names_where_the_text_came_from(self):
        for entry in card()["entries"]:
            source = entry["source"]
            self.assertTrue(source["kind"], "не названо, откуда текст")
            self.assertTrue(source["detail"], "не назван путь или URL текста")
            self.assertTrue(entry["license"], "не названо имя лицензии")

    def test_upstream_texts_name_url_and_date(self):
        collected = card()["collected"]
        self.assertTrue(collected, "карта не называет дату сбора текстов")
        upstream = [entry for entry in card()["entries"]
                    if entry["source"]["kind"] == "upstream по URL"]
        named = {entry["component"] for entry in upstream}
        for component in ("OpenSSL", "UPX", "curl_cffi", "libcurl",
                          "curl-impersonate", "proxy_tools"):
            self.assertIn(component, named,
                          f"{component}: текст не назван взятый с upstream - источник "
                          "не сверяется")
        for entry in upstream:
            self.assertTrue(entry["source"]["detail"].startswith("https://"),
                            f"{entry['component']}: URL текста не назван")
            self.assertEqual(entry["source"]["fetched"], collected,
                             f"{entry['component']}: дата получения текста не названа")

    def test_python_text_from_the_interpreter_that_builds_the_exe(self):
        entry = next(item for item in card()["entries"] if item["component"] == "Python")
        self.assertIn("интерпретатор", entry["source"]["kind"])
        self.assertTrue(entry["version"], "версия CPython не названа: текст не сверяется")
        text = (ROOT / entry["files"][0]["path"]).read_text(encoding="utf-8")
        self.assertIn("PYTHON SOFTWARE FOUNDATION LICENSE", text)

    def test_license_names_match_the_texts_not_the_metadata_guess(self):
        """cffi и proxy_tools: имена сверены по текстам файлов, а не по гаданию."""
        texts, errors, _summary = BUILD.license_texts()
        self.assertEqual(errors, [], errors)
        by_component = {entry["component"]: entry for entry in card()["entries"]}
        self.assertIn("MIT-0", by_component["cffi"]["license"])
        self.assertIn("MIT No Attribution",
                      (ROOT / by_component["cffi"]["files"][0]["path"]).read_text(
                          encoding="utf-8"))
        self.assertIn("BSD", by_component["proxy_tools"]["license"])
        proxy_text = (ROOT / by_component["proxy_tools"]["files"][0]["path"]).read_text(
            encoding="utf-8")
        self.assertIn("Armin Ronacher", proxy_text)
        self.assertIn("Jonathan Tushman", proxy_text)
        self.assertIn("Dan-Haim",
                      (ROOT / by_component["PySocks"]["files"][0]["path"]).read_text(
                          encoding="utf-8"))

    def test_dual_license_components_ship_both_texts(self):
        by_component = {entry["component"]: entry for entry in card()["entries"]}
        for component in ("cryptography", "packaging"):
            names = [Path(file["path"]).name for file in by_component[component]["files"]]
            self.assertEqual(sorted(names), ["LICENSE", "LICENSE.APACHE", "LICENSE.BSD"],
                             f"{component}: двойная лицензия покрыта одним текстом")
            apache = (ROOT / f"licenses/{component}/LICENSE.APACHE").read_text(
                encoding="utf-8")
            bsd = (ROOT / f"licenses/{component}/LICENSE.BSD").read_text(encoding="utf-8")
            self.assertIn("Apache License", apache)
            self.assertIn("Redistribution and use", bsd)

    def test_apache_components_ship_the_notice_file(self):
        """Apache-2.0 требует NOTICE рядом с текстом: requests его имеет."""
        by_component = {entry["component"]: entry for entry in card()["entries"]}
        names = [Path(file["path"]).name for file in by_component["requests"]["files"]]
        self.assertIn("NOTICE", names, "Apache-2.0 без NOTICE - notice неполный")
        notice = (ROOT / "licenses/requests/NOTICE").read_text(encoding="utf-8")
        self.assertIn("Kenneth Reitz", notice)

    def test_components_without_text_are_named_with_a_reason(self):
        reasons = {item["component"]: item["reason"] for item in card()["notRequired"]}
        self.assertTrue(reasons, "карта не называет компоненты без текста")
        self.assertIn("SQLite", reasons)
        self.assertIn("общественное достояние", reasons["SQLite"].lower())
        self.assertIn("Microsoft WebView2", reasons)
        self.assertIn("не публикует", reasons["Microsoft WebView2"])
        self.assertIn("hls.js", reasons)
        self.assertIn("ui_src/vendor/hls.js.LICENSE.txt", reasons["hls.js"])
        self.assertIn("ffmpeg", reasons)
        self.assertIn("не поставляет", reasons["ffmpeg"])
        self.assertIn("zeroconf", reasons)
        self.assertIn("не поставляет", reasons["zeroconf"])
        self.assertIn("magic-wormhole", reasons)
        self.assertIn("не входит в exe", reasons["magic-wormhole"])
        for component, reason in reasons.items():
            self.assertTrue(reason.strip(), f"{component}: без текста без причины")

    def test_check_reports_clean_and_covers_every_bundled_component(self):
        code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual((code, errors), (0, []), errors)
        covered = {entry["component"] for entry in card()["entries"]}
        reasons = {item["component"] for item in card()["notRequired"]}
        for component in BUILD.LICENSES_OF:
            self.assertTrue(component in covered or component in reasons,
                            f"{component}: ни текста, ни причины")


class TestLicenseRefusals(unittest.TestCase):
    """Подмена текста, пина или карты называется причиной, пакет не пишется."""

    def test_tampered_text_is_named_by_pin(self):
        original = BUILD.read

        def read(target):
            body = original(target)
            if target == "licenses/yt-dlp/LICENSE":
                return body.replace(b"public domain", b"public domainx")
            return body

        with mock.patch.object(BUILD, "read", read):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("пин текста лицензии не совпадает" in line for line in errors),
                        "изменённый текст лицензии прошёл бы как «покрыт»")

    def test_tampered_card_pin_is_named(self):
        def transform(text):
            data = json.loads(text)
            data["entries"][0]["files"][0]["sha256"] = "f" * 64
            return json.dumps(data, ensure_ascii=False)

        original = BUILD.read

        def read(target):
            body = original(target)
            if target == BUILD.LICENSES_MANIFEST:
                return transform(body.decode("utf-8")).encode("utf-8")
            return body

        with mock.patch.object(BUILD, "read", read):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("пин текста лицензии не совпадает" in line for line in errors))

    def test_missing_license_entry_is_named(self):
        def transform(text):
            data = json.loads(text)
            data["entries"] = [entry for entry in data["entries"]
                               if entry["component"] != "cffi"]
            return json.dumps(data, ensure_ascii=False)

        original = BUILD.read

        def read(target):
            body = original(target)
            if target == BUILD.LICENSES_MANIFEST:
                return transform(body.decode("utf-8")).encode("utf-8")
            return body

        with mock.patch.object(BUILD, "read", read):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("текст лицензии не собран для cffi" in line for line in errors),
                        "снятый текст выглядит покрытием молча")

    def test_missing_reason_is_named(self):
        def transform(text):
            data = json.loads(text)
            data["notRequired"] = [{"component": "SQLite", "reason": ""}]
            return json.dumps(data, ensure_ascii=False)

        original = BUILD.read

        def read(target):
            body = original(target)
            if target == BUILD.LICENSES_MANIFEST:
                return transform(body.decode("utf-8")).encode("utf-8")
            return body

        with mock.patch.object(BUILD, "read", read):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("без причины" in line for line in errors))

    def test_license_name_without_matching_text_is_named(self):
        """Имя лицензии обязано читаться из текста: MIT-0 без «No Attribution» - отказ."""
        def transform(text):
            data = json.loads(text)
            for entry in data["entries"]:
                if entry["component"] == "cffi":
                    entry["files"] = []
            return json.dumps(data, ensure_ascii=False)

        original = BUILD.read

        def read(target):
            body = original(target)
            if target == BUILD.LICENSES_MANIFEST:
                return transform(body.decode("utf-8")).encode("utf-8")
            return body

        with mock.patch.object(BUILD, "read", read):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("cffi назван без файла" in line for line in errors),
                        "компонент без файла выглядит покрытым")

    def test_missing_card_is_named(self):
        original = BUILD.read

        def read(target):
            if target == BUILD.LICENSES_MANIFEST:
                raise ValueError(f"файл пакета не найден ({target})")
            return original(target)

        with mock.patch.object(BUILD, "read", read):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(errors)
        self.assertIn("файл пакета не найден", errors[0],
                      "отказ обязан назвать отсутствующий файл, а не молчать")


class TestPackShipsLicenseTexts(unittest.TestCase):
    """Запись и архив: тексты лицензий бандла и карта уезжают рядом с exe."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="omnistash-licenses-"))
        patcher = mock.patch.object(BUILD, "DIST", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def test_pack_writes_license_texts_and_the_card(self):
        code, errors = BUILD.build(check_only=False, want_zip=True)
        self.assertEqual((code, errors), (0, []), errors)
        paths = card_paths(card())
        for path in paths:
            target = self.dir / path
            self.assertTrue(target.is_file(), f"{path}: текст не записан в пакет")
            self.assertEqual(BUILD.sha256_bytes(target.read_bytes()),
                             next(file["sha256"] for entry in card()["entries"]
                                  for file in entry["files"] if file["path"] == path),
                             f"{path}: в пакете лежит другой текст")
        self.assertTrue((self.dir / BUILD.LICENSES_MANIFEST).is_file(),
                        "карта текстов не записана в пакет")

    def test_zip_carries_the_texts(self):
        BUILD.build(check_only=False, want_zip=True)
        with zipfile.ZipFile(self.dir / BUILD.ZIP_NAME) as pack:
            names = sorted(pack.namelist())
        for component in ("yt-dlp", "pywebview", "curl_cffi", "curl", "curl-impersonate",
                          "certifi", "cffi", "proxy_tools", "PyInstaller", "python",
                          "openssl", "upx"):
            self.assertTrue(any(name.startswith(f"licenses/{component}/") for name in names),
                            f"licensing: текст {component} не уехал в архив")
        self.assertIn("licenses/manifest.json", names)
        self.assertNotIn(BUILD.ZIP_NAME, names, "архив содержит себя")

    def test_pack_manifest_describes_the_texts(self):
        BUILD.build(check_only=False, want_zip=False)
        manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
        summary = manifest["licenseTexts"]
        self.assertEqual(summary["dir"], BUILD.LICENSES_DIR)
        self.assertEqual(summary["manifest"], BUILD.LICENSES_MANIFEST)
        self.assertEqual(summary["files"], len(card_paths(card())))
        self.assertEqual(summary["components"], len(card()["entries"]))
        self.assertEqual(summary["collected"], card()["collected"])
        self.assertTrue(summary["bytes"] > 0, "байты текстов не названы")
        self.assertIn("из установленного дистрибутива", summary["note"])


class TestDocsForLicenseTexts(unittest.TestCase):
    """Документы называют тексты лицензий бандла, а не только список имён."""

    def test_notices_name_the_texts_and_the_card(self):
        text = NOTICES.read_text(encoding="utf-8")
        self.assertIn("licenses/", text)
        self.assertIn("licenses/manifest.json", text)
        self.assertIn("байт-в-байт", text)
        self.assertIn("sha256", text)
        self.assertIn("MIT-0", text, "cffi назван по тексту файла, а не по гаданию")
        self.assertIn("Armin Ronacher", text, "proxy_tools назван BSD с правообладателями")
        self.assertIn("Dan-Haim", text)

    def test_readme_names_the_texts_of_the_pack(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("licenses/", text)
        self.assertIn("licenses/manifest.json", text)
        self.assertIn("тексты лицензий бандла", text)

    def test_legal_review_closes_the_second_packaging_item(self):
        text = LEGAL.read_text(encoding="utf-8")
        self.assertIn("[x] Рядом с exe — тексты лицензий бандла", text,
                      "пункт §5.2 о текстах лицензий бандла не закрыт")
        self.assertIn("2. Выполнено: тексты лицензий бандла кладутся рядом с exe", text,
                      "пункт «Добавить перед релизом» про тексты бандла не закрыт")
        self.assertIn("MIT-0", text,
                      "review не говорит, что cffi = MIT-0 по тексту самого пакета")

    def test_gitignore_does_not_hide_the_texts(self):
        text = GITIGNORE.read_text(encoding="utf-8")
        self.assertNotIn("licenses/", text,
                         "тексты лицензий бандла - источник, их нельзя прятать в git")

    def test_texts_have_no_cjk_or_latin_glue(self):
        import re
        cjk = re.compile(r"[\u3400-\u9fff\uac00-\udfff]")
        glue = re.compile(r"[A-Za-z][а-яё]|[а-яё][A-Za-z]")
        skip = ("manifest", "licenses", "license", "licence", "mit", "bsd", "gpl",
                "lgpl", "mpl", "apache", "psf", "unlicense", "sha256", "curl",
                "cffi", "openssl", "upx", "python", "pywebview", "sirokov",
                "dailymotion", "brightcove", "reitz", "dan-haim", "armin",
                "jonathan", "reinartz", "oberhumer", "molnar", "stenberg",
                "dist-info", "url", "fetched", "uri", "http", "https", "git",
                "docs", "ui_src", "vendor", "hls.js", "sqlite", "zeroconf",
                "wormhole", "ffmpeg", "webview", "microsoft", "readme", "legal",
                "review", "build_legal_pack", "omnistash", "import", "def ",
                "json", "pathlib", "mock", "tempfile", "test", "assert", "re.",
                "compile", "zipfile", "sha", "bytes", "data-l", "public domain")
        for path in [CARD, ROOT / "tools" / "build_legal_pack.py", Path(__file__),
                     NOTICES, README, LEGAL]:
            text = path.read_text(encoding="utf-8")
            hits = []
            for index, line in enumerate(text.splitlines(), 1):
                for char in line:
                    if cjk.search(char):
                        hits.append(f"{index}: {char}")
            self.assertEqual(hits, [], f"{path.name}: CJK в тексте {hits[:3]}")
            glued = []
            for match in glue.finditer(text):
                fragment = text[max(0, match.start() - 30):match.end() + 30]
                if any(token in fragment.lower() for token in skip):
                    continue
                glued.append((path.name, fragment.replace("\n", " ")))
            self.assertEqual(glued, [],
                             f"{path.name}: склейка латиницы с кириллицей {glued[:3]}")


if __name__ == "__main__":
    unittest.main()
