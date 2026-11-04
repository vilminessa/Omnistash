"""Страж S7: сборка клиентского бандла, его пины и конвейер CI.

Проверяется, что бандл - это то, что действительно раздаёт `--serve`, а не
«ещё один список файлов, который мог расходиться»:

  * состав бандла = страницы (главная и воспроизведения) + файлы по карте
    `CLIENT_ASSETS` + вендор с метадатой и текстом лицензии;
  * пин вендора и пин текста лицензии сверяются по sha256, несоответствие
    называется причиной и бандл не пишется;
  * чужой адрес в рантайме (абсолютный `fetch` или `src=`/`href=` на чужой
    хост) - отказ с именем файла: рантайм грузит только свой адрес (§6, §10);
  * страница плеера обязана ссылаться на `/app.css`, `/watch.js` и
    `/vendor/hls.js` - иначе плеер не соберётся на клиенте;
  * архив не включает самого себя (иначе сборка виснет на собственном
    растущем файле), и метка сервера в бандле отсутствует: её ставит
    слушатель при раздаче;
  * CI: линт пинов и сборка бандла идут после проверочных шагов набора,
    ассет бандла публикуется в релизе, а SLSA-подпись считается по exe;
  * документы: строка SV-C в README, пункт hls.js в notices и в LEGAL-REVIEW,
    ui_src в spec, client_dist в gitignore.
"""

import importlib.util
import json
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import ui
from app.http.server import CLIENT_ASSETS

ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """Загрузить tools/build_client.py: tools - не пакет, имя не импортируется."""
    spec = importlib.util.spec_from_file_location(
        "build_client", ROOT / "tools" / "build_client.py")
    assert spec and spec.loader, "tools/build_client.py не загружается"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load_tool()
CI = ROOT / ".github" / "workflows" / "ci.yml"
BETA = ROOT / ".github" / "workflows" / "beta.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "README.md"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
LEGAL = ROOT / "docs" / "LEGAL-REVIEW.md"
SPEC = ROOT / "Omnistash.spec"
GITIGNORE = ROOT / ".gitignore"
LICENSE_FILE = ROOT / "ui_src" / "vendor" / "hls.js.LICENSE.txt"
LICENSE_META = ROOT / "ui_src" / "vendor" / "hls.js.meta.json"
WORKFLOW_LINT = ROOT / "tools" / "check_workflows.py"


class TestBundleComposition(unittest.TestCase):
    """Состав бандла сверяется с тем, что раздаёт слушатель."""

    def test_pages_and_assets_cover_the_served_routes(self):
        pages = BUILD.bundle_pages()
        assets = BUILD.bundle_assets()
        self.assertEqual(sorted(pages), ["index.html", ui.WATCH_PAGE])
        self.assertEqual(sorted(assets),
                         sorted(name for name, _ in CLIENT_ASSETS.values()),
                         "бандл не покрывает карту раздачи клиента")
        self.assertNotIn("window.OMNISTASH_HTTP =", pages["index.html"].decode("utf-8"),
                         "в бандле появилась метка сервера: страница без --serve "
                         "стала бы битым клиентом")
        served = ui.build_page(marker={"base": "https://127.0.0.1:1001"})
        self.assertIn("window.OMNISTASH_HTTP =", served,
                      "метку ставит --serve - проверьте, что она существует")

    def test_vendor_and_license_pins_match_the_files(self):
        digest, meta = BUILD.vendor_pin()
        self.assertEqual(digest, meta["sha256"])
        self.assertEqual(meta["license"], "Apache-2.0")
        self.assertEqual(BUILD.sha256_bytes(LICENSE_FILE.read_bytes()),
                         meta["licenseSha256"],
                         "текст лицензии не совпадает с пином в метадате")
        self.assertTrue(meta.get("licenseSource"),
                        "метадата не говорит, откуда взят текст лицензии")

    def test_license_text_is_the_upstream_notice(self):
        """Apache-2.0 требует текст лицензии рядом с файлом (§4)."""
        text = LICENSE_FILE.read_text(encoding="utf-8")
        self.assertIn("Apache License, Version 2.0", text)
        self.assertIn("Dailymotion", text)
        self.assertIn("Brightcove", text,
                      "производные части (videojs-contrib-hls) названы не были")


class TestHonestRefusals(unittest.TestCase):
    """Отказ называется причиной и бандл не пишется молча."""

    def test_broken_vendor_pin_is_named(self):
        original = ui.asset

        def tampered(name):
            body = original(name)
            if name == BUILD.VENDOR_NAME:
                return body + b"/* tampered bytes appended */"
            return body

        with mock.patch.object(ui, "asset", tampered):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(errors, "несовпадение пина молча прошло")
        self.assertIn("пин вендора не совпадает", errors[0])

    def test_broken_license_pin_is_named(self):
        original = ui.asset

        def tampered(name):
            body = original(name)
            if name == BUILD.VENDOR_LICENSE:
                return body.replace(b"Dailymotion", b"Dailymotionx")
            return body

        with mock.patch.object(ui, "asset", tampered):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(errors)
        self.assertIn("пин текста лицензии не совпадает", errors[0])

    def test_wrong_license_is_named(self):
        meta = json.loads(LICENSE_META.read_text(encoding="utf-8"))
        meta["license"] = "GPL-3.0"
        original = ui.asset

        def tampered(name):
            if name == BUILD.VENDOR_META:
                return json.dumps(meta, ensure_ascii=False).encode("utf-8")
            return original(name)

        with mock.patch.object(ui, "asset", tampered):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(errors)
        self.assertIn("лицензия вендора не Apache-2.0", errors[0])

    def test_missing_watch_page_is_named(self):
        with mock.patch.object(ui, "watch_page_available", lambda: False):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(errors)
        self.assertIn("страница воспроизведения не найдена", errors[0])

    def test_foreign_script_source_is_named(self):
        """Страница плеера грузит вендор со своего адреса, а не из чужого хоста."""
        cdn = ("<html><body><script src=\"https://cdn.jsdelivr.net/npm/hls.js\">"
               "</script></body></html>")
        with mock.patch.object(ui, "build_watch_page", lambda marker=None: cdn):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("чужой адрес в разметке" in line for line in errors),
                        "cdn в странице плеера прошёл бы как «свой адрес»")

    def test_absolute_fetch_is_named(self):
        original = ui.asset

        def tampered(name):
            body = original(name)
            if name == "watch.js":
                return body + b"\nfetch(\"https://evil.example/seg.ts\")\n"
            return body

        with mock.patch.object(ui, "asset", tampered):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("чужой адрес в коде" in line for line in errors))

    def test_player_without_own_routes_is_named(self):
        """Страница плеера без ссылок на свой адрес - плеер не соберётся на клиенте."""
        hollow = ("<html><body><script src=\"/watch.js\"></script></body></html>")
        with mock.patch.object(ui, "build_watch_page", lambda marker=None: hollow):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("/vendor/hls.js" in line for line in errors),
                        "плеер без своего вендора молча собрался бы")
        self.assertTrue(any("/app.css" in line for line in errors))


class TestBundleWrites(unittest.TestCase):
    """Запись бандла: файлы, manifest и архив без самого себя."""

    def setUp(self):
        self._tmp = Path(__file__).resolve().parent  # не используется
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="omnistash-bundle-"))
        patcher = mock.patch.object(BUILD, "DIST", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_check_mode_writes_nothing(self):
        code, errors = BUILD.build(check_only=True, want_zip=True)
        self.assertEqual((code, errors), (0, []))
        self.assertEqual(list(self.dir.rglob("*")), [],
                         "режим проверки записал файлы: это не «только сверка»")

    def test_bundle_files_and_manifest(self):
        code, errors = BUILD.build(check_only=False, want_zip=True)
        self.assertEqual((code, errors), (0, []), errors)
        written = sorted(p.relative_to(self.dir).as_posix()
                         for p in self.dir.rglob("*") if p.is_file())
        self.assertEqual(written, sorted(["index.html", ui.WATCH_PAGE, "app.css",
                                          "watch.js", "manifest.json",
                                          BUILD.VENDOR_NAME, BUILD.VENDOR_META,
                                          BUILD.VENDOR_LICENSE, BUILD.ZIP_NAME]),
                         "бандл не покрывает то, что раздаёт --serve (плюс лицензия)")
        manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["vendor"]["sha256"],
                         BUILD.sha256_bytes((self.dir / BUILD.VENDOR_NAME).read_bytes()))
        self.assertEqual(manifest["vendor"]["licenseSha256"],
                         BUILD.sha256_bytes((self.dir / BUILD.VENDOR_LICENSE).read_bytes()))
        self.assertEqual(manifest["served_routes"], sorted(CLIENT_ASSETS))
        self.assertEqual(manifest["pages"]["index.html"]["bytes"],
                         (self.dir / "index.html").stat().st_size)
        self.assertIn("из сети ничего не скачивается", manifest["note"])

    def test_zip_excludes_itself(self):
        """Архив, читающий собственный растущий файл, вешает сборку навсегда."""
        BUILD.build(check_only=False, want_zip=True)
        zip_path = self.dir / BUILD.ZIP_NAME
        with zipfile.ZipFile(zip_path) as pack:
            names = sorted(pack.namelist())
        self.assertNotIn(BUILD.ZIP_NAME, names,
                         "архив содержит себя - запись читает растущий файл")
        self.assertIn("vendor/hls.js", names,
                      "путь внутри архива потерял каталог vendor: клиентский "
                      "маршрут /vendor/hls.js не совпадает с бандлом")
        self.assertIn("vendor/hls.js.LICENSE.txt", names,
                      "лицензия вендора не уехала в архив (§4 Apache-2.0)")
        self.assertIn("manifest.json", names)


class TestPipelineAndDocs(unittest.TestCase):
    """Конвейер и документы: бандл собирается после проверок и публикуется."""

    def test_ci_runs_pin_lint_and_bundle_after_the_suite(self):
        text = CI.read_text(encoding="utf-8")
        tests_at = text.index("python -m unittest discover")
        check_at = text.index("tools/build_client.py --check")
        build_at = text.index("tools/build_client.py --zip")
        self.assertLess(tests_at, check_at,
                        "линт пинов идёт до набора тестов - битый пин увидится позже")
        self.assertLess(check_at, build_at,
                        "бандл собирается до сверки пинов - несоответствие молча")

    def test_release_and_beta_publish_the_client_asset(self):
        for path in (RELEASE, BETA):
            text = path.read_text(encoding="utf-8")
            self.assertIn("tools/build_client.py --check", text,
                          f"{path.name}: пин вендора не проверяется до сборки exe")
            self.assertIn("tools/build_client.py --zip", text,
                          f"{path.name}: бандл не собирается")
            self.assertIn("client_dist/omnistash-client.zip", text,
                          f"{path.name}: архив бандла не выкладывается ассетом")
            self.assertIn("name: Omnistash-client", text,
                          f"{path.name}: артефакт бандла без имени")
            self.assertIn("client/omnistash-client.zip", text,
                          f"{path.name}: бандл не публикуется в релизе")

    def test_provenance_stays_on_the_exe_only(self):
        """Бандл публикуется без провенанса - это написано в тексте релиза."""
        for path in (RELEASE, BETA):
            text = path.read_text(encoding="utf-8")
            self.assertIn("Провенанс SLSA", text,
                          f"{path.name}: релизный текст не называет провенанс")
            self.assertIn("провенанс не распространяется", text,
                          f"{path.name}: не сказано, что на бандл провенанс "
                          "не распространяется - иначе «подписано всё» было бы враньём")

    def test_workflow_lint_passes_after_the_edits(self):
        spec = importlib.util.spec_from_file_location("check_workflows", WORKFLOW_LINT)
        assert spec and spec.loader
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)
        for path in (CI, BETA, RELEASE):
            self.assertEqual(lint.check(path), [], f"{path.name}: линт отказал")

    def test_readme_names_the_client_mode_and_the_bundle(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("**SV-C**", text, "в дорожной карте нет строки SV-C")
        self.assertIn("omnistash.py --serve", text, "флаг --serve не описан")
        self.assertIn("tools/build_client.py", text, "бандл клиента не описан")
        self.assertIn("omnistash-client.zip", text,
                      "ассет бандла в релизе не описан")

    def _row_text(self, path: Path, needle: str) -> str:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines()
                 if needle in line]
        self.assertTrue(lines, f"{path.name}: нет строки про {needle}")
        return "\n".join(lines)

    def test_notices_cover_the_vendored_player(self):
        row = self._row_text(NOTICES, "hls.js")
        self.assertIn("Apache-2.0", row)
        self.assertIn("ui_src/vendor/hls.js", row)
        self.assertIn("sha256", row, "пин вендора не назван в notices")
        self.assertIn("LICENSE.txt", row, "текст лицензии не назван в notices")
        self.assertIn("Dailymotion", row, "правообладатель не назван")
        self.assertIn("Brightcove", row,
                      "производные части не названы: notice был бы неполным")
        # Прежнее «стороннего кода в исходниках нет» читалось бы как враньё.
        self.assertIn("Сторонний код в исходниках один",
                      NOTICES.read_text(encoding="utf-8"))

    def test_legal_review_covers_the_vendored_player(self):
        row = self._row_text(LEGAL, "hls.js")
        self.assertIn("Apache-2.0", row)
        self.assertIn("да", row, "бандл не признан входящим в exe")
        self.assertIn("licenseSha256", row,
                      "LEGAL-REVIEW не говорит, что пин текста лицензии есть")
        self.assertIn("tools/build_client.py", row,
                      "не назван инструмент, который кладёт лицензию в бандл")

    def test_spec_bundles_the_client_files_and_serves(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn('datas += [("ui_src", "ui_src")]', text,
                      "ui_src не вшит в exe: клиенту нечем раздавать страницу")
        self.assertIn("watch.html", text, "spec не говорит про страницу плеера")
        self.assertIn("hls.js", text, "spec не говорит про вендор в бандле")
        self.assertIn("--serve", text, "spec не говорит, что режим сервера в том же exe")

    def test_gitignore_keeps_the_bundle_out_of_git(self):
        text = GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("client_dist/", text,
                      "бандл попал бы в git: это сборка, а не источник")

    def test_new_text_has_no_cjk_or_latin_glue(self):
        import re
        cjk = re.compile(r"[\u3400-\u9fff\uac00-\udfff]")
        glue = re.compile(r"[A-Za-z][а-яё]|[а-яё][A-Za-z]")
        skip = ("http.", "hls.", "ui_src", "tools/", "app/", "tests/", "sha256",
                "json", "fetch", "src=", "href=", "cdn", "apache-2.0", "license",
                "dailymotion", "brightcove", "videojs", "npm", "serve", "client",
                "dist", "zip", "manifest", "builder", "python", "def ", "import ",
                "from ", "t(", "--serve", "--zip", "--check", "build_client",
                "check_workflows", "omnistash", "github", "slsa", "artifact",
                "download", "upload", "release", "beta", "readme", "spec",
                "gitignore", "sv-c", "hls.js.license.txt", "vendor", "ui.",
                "client_dist", "watch.", "app.css", "index.html", "data-l")
        for path in (ROOT / "tools" / "build_client.py", Path(__file__), README,
                    NOTICES, LEGAL, SPEC, GITIGNORE, LICENSE_META, CI, BETA, RELEASE,
                    ROOT / "app" / "ui.py", ROOT / "docs" / "SERVER-CLIENT.md"):
            text = path.read_text(encoding="utf-8")
            hits = [f"{n}: {c}" for n, line in enumerate(text.splitlines(), 1)
                    for c in line if cjk.search(c)]
            self.assertEqual(hits, [], f"{path.name}: CJK в тексте {hits[:3]}")
            glued = []
            for match in glue.finditer(text):
                fragment = text[max(0, match.start() - 25):match.end() + 25]
                if any(token in fragment.lower() for token in skip):
                    continue
                glued.append((path.name, fragment.replace("\n", " ")))
            self.assertEqual(glued, [],
                             f"{path.name}: склейка латиницы с кириллицей {glued[:3]}")


if __name__ == "__main__":
    unittest.main()
