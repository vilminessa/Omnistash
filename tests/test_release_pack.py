"""Страж LC1: юридический пакет рядом с exe, его состав и конвейер CI.

Условие «Notices» в PolyForm Noncommercial требует, чтобы получатель копии
программы получил текст условий, `Required Notice:` и уведомления о третьих
сторонах. До этого шага релиз отдавал только `Omnistash.exe` и бандл клиента:
для человека, скачавшего бинарник, условие не выполнялось. Проверяем, что
пакет действительно кладётся и что его тексты сверяются, а не декларируются:

  * состав пакета = LICENSE, README, THIRD_PARTY_NOTICES.md, ACCEPTABLE_USE.md,
    LEGAL_FAQ.md и текст лицензии вендора `hls.js`; пути в архиве - те же, что
    в репозитории;
  * `LICENSE` сверяется с каноническим текстом по закреплённому sha256: правки
    в тексте лицензии (изменённый абзац, новый абзац, другой порядок) - отказ с
    причиной, а не «тихо прошло»; строка `Required Notice:` обязана быть;
  * `THIRD_PARTY_NOTICES.md` покрывает распространяемый состав exe и прямые
    зависимости из `requirements.txt`; выдуманное имя лицензии («MIT-0») и
    потерянный правообладатель BSD-3 - отказ;
  * публичные тексты пакета нейтральны: имя и домен взрослой площадки и
    claim-формулировки (circumvention, DRM, endorsement, verification возраста)
    названы строкой файла; честные отрицания и вопросы claim'ом не считаются;
  * отсутствие файла называется причиной, --check ничего не пишет, архив не
    содержит себя;
  * CI: сверка пакета идёт после набора тестов и до сборки exe, ассет
    `Omnistash-notices` выкладывается и публикуется рядом с exe, текст релиза
    говорит, что кладётся рядом, провенанс остаётся по exe, а наличие ассета
    сверяется скачиванием из релиза;
  * документы: README, notices, AUP (возрастной раздел и датированные факты),
    FAQ (ответ про возраст и про пакет), LEGAL-REVIEW §5.2, gitignore.
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
    """Загрузить tools/build_legal_pack.py: tools - не пакет, имя не импортируется."""
    spec = importlib.util.spec_from_file_location(
        "build_legal_pack", ROOT / "tools" / "build_legal_pack.py")
    assert spec and spec.loader, "tools/build_legal_pack.py не загружается"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load_tool()
CI = ROOT / ".github" / "workflows" / "ci.yml"
BETA = ROOT / ".github" / "workflows" / "beta.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "README.md"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
LICENSE = ROOT / "LICENSE"
LICENSE_CANON = ROOT / "docs" / "POLYFORM-NONCOMMERCIAL-1.0.0.txt"
AUP = ROOT / "docs" / "ACCEPTABLE_USE.md"
FAQ = ROOT / "docs" / "LEGAL_FAQ.md"
LEGAL = ROOT / "docs" / "LEGAL-REVIEW.md"
GITIGNORE = ROOT / ".gitignore"
WORKFLOW_LINT = ROOT / "tools" / "check_workflows.py"


def tamper(name: str, transform) -> mock.mock:
    """Подменить один файл пакета (текст, не байты): отказ обязан быть назван."""
    original = BUILD.read

    def read(target):
        body = original(target)
        if target == name:
            return transform(body.decode("utf-8")).encode("utf-8")
        return body

    return mock.patch.object(BUILD, "read", read)


def drop_lines(text: str, needle: str) -> str:
    """Убрать строки, содержащие подписанную строку (компонент перечня)."""
    return "\n".join(line for line in text.splitlines() if needle not in line)


class TestPackComposition(unittest.TestCase):
    """Состав пакета: то, что требует лицензия, и ничего лишнего."""

    def test_pack_files_are_the_notice_texts(self):
        self.assertEqual(sorted(BUILD.PACK_FILES), sorted([
            "LICENSE", "README.md", "THIRD_PARTY_NOTICES.md",
            "docs/ACCEPTABLE_USE.md", "docs/LEGAL_FAQ.md",
            "ui_src/vendor/hls.js.LICENSE.txt",
        ]), "пакет не покрывает тексты условия «Notices»")
        for name in BUILD.PACK_FILES:
            self.assertTrue((ROOT / name).is_file(), f"нет файла пакета: {name}")

    def test_license_matches_canonical_text(self):
        """Канонический текст - якорь: LICENSE сверяется с ним абзац в абзац."""
        self.assertEqual(BUILD.sha256_bytes(LICENSE_CANON.read_bytes()),
                         BUILD.CANONICAL_SHA256,
                         "канонический текст лицензии изменён - пин называется")
        self.assertEqual(BUILD.license_checks(LICENSE.read_text(encoding="utf-8")), [],
                         "LICENSE расходится с каноническим текстом лицензии")
        self.assertTrue(BUILD.CANONICAL_SOURCE.startswith("https://polyformproject.org/"),
                        "якорь сверки назван не на источнике PolyForm")
        self.assertIn("PolyForm Noncommercial License 1.0.0",
                      LICENSE_CANON.read_text(encoding="utf-8"),
                      "канонический якорь назван не той лицензией")
        self.assertTrue(BUILD.CANONICAL_FETCHED,
                        "не названа дата, когда канонический текст получен")

    def test_notices_cover_the_bundled_components(self):
        text = NOTICES.read_text(encoding="utf-8")
        requirements = (ROOT / BUILD.REQUIREMENTS).read_text(encoding="utf-8")
        self.assertEqual(BUILD.notices_checks(text, requirements), [],
                         "уведомления не покрывают распространяемый состав exe")
        deps = BUILD.direct_dependencies(requirements)
        self.assertIn("yt-dlp", deps)
        self.assertIn("curl_cffi", deps)
        self.assertTrue(BUILD.LICENSES_OF,
                        "карта лицензий пуста - «покрытие» было бы ложью")

    def test_check_mode_reports_clean(self):
        code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual((code, errors), (0, []), errors)


class TestLicenseGuard(unittest.TestCase):
    """Текст лицензии нельзя «улучшать»: правка названа отказом."""

    def test_reworded_paragraph_is_named(self):
        def transform(text):
            return text.replace("Any noncommercial purpose is a permitted purpose.",
                                "Any purpose is a permitted purpose.")

        with tamper(BUILD.PACK_FILES[0], transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(errors)
        self.assertIn("не покрывает канонический текст лицензии", errors[0])

    def test_added_clause_is_named(self):
        def transform(text):
            return text.replace("## Fair Use",
                                "Commercial use is allowed for any purpose.\n\n## Fair Use")

        with tamper(BUILD.PACK_FILES[0], transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("лишние абзацы" in line for line in errors),
                        "новое условие в тексте лицензии молча прошло бы")

    def test_missing_required_notice_is_named(self):
        def transform(text):
            return text.replace(
                "Required Notice: Copyright © 2026 vilminessa (https://github.com/vilminessa)",
                "Copyright 2026")

        with tamper(BUILD.PACK_FILES[0], transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("Required Notice" in line for line in errors),
                        "условие «Notices» самой лицензии потерян молча")

    def test_notice_without_holder_is_named(self):
        def transform(text):
            return text.replace(
                "Required Notice: Copyright © 2026 vilminessa (https://github.com/vilminessa)",
                "Required Notice: Copyright 2026")

        with tamper(BUILD.PACK_FILES[0], transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("не называет правообладателя" in line for line in errors))

    def test_canonical_pin_mismatch_is_named(self):
        def transform(text):
            return text + "\n<!-- tampered canonical anchor -->\n"

        with tamper(BUILD.CANONICAL_FILE, transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("не совпадает с пином" in line for line in errors),
                        "канонический якорь изменён - сверка молча прошла бы")


class TestNoticesGuard(unittest.TestCase):
    """Уведомления: компонент, его лицензия, прямые зависимости и notice'ы сторон."""

    def test_dropped_component_and_direct_dependency_are_named(self):
        cases = {
            "| certifi ": "не называет компонент certifi",
            "| curl_cffi ": "не покрывает прямую зависимость curl_cffi",
            "| cryptography ": "не называет компонент cryptography",
        }
        for needle, reason in cases.items():
            with tamper("THIRD_PARTY_NOTICES.md",
                        lambda text, needle=needle: drop_lines(text, needle)):
                code, errors = BUILD.build(check_only=True, want_zip=False)
            self.assertEqual(code, 1, f"{needle}: снятый компонент прошёл молча")
            self.assertTrue(any(reason in line for line in errors),
                            f"{needle}: отказ назван не тем текстом - {errors[:2]}")

    def test_license_name_contradicting_the_text_is_named(self):
        def transform(text):
            return text.replace("| cffi | MIT-0 (MIT No Attribution) |",
                                "| cffi | MIT |")

        with tamper("THIRD_PARTY_NOTICES.md", transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("cffi" in line for line in errors),
                        "имя лицензии, которое не совпадает с текстом файла, "
                        "прошло бы как «покрыто»")

    def test_bsd_notice_without_holder_is_named(self):
        def transform(text):
            return text.replace("BSD 3-Clause (Copyright © Roman Sirokov, 2014-2017)",
                                "BSD 3-Clause")

        with tamper("THIRD_PARTY_NOTICES.md", transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("pywebview" in line and "Roman Sirokov" in line
                            for line in errors),
                        "BSD-3 notice без правообладателя и года неполный")

    def test_smartscreen_notice_is_required(self):
        """WebView2 по умолчанию включён со SmartScreen - Microsoft требует notice."""
        text = NOTICES.read_text(encoding="utf-8")
        self.assertIn("SmartScreen", text)
        self.assertIn("https://aka.ms/privacy", text)
        self.assertIn("IsReputationCheckingRequired", text)

        with tamper("THIRD_PARTY_NOTICES.md",
                    lambda text: drop_lines(text, "SmartScreen")):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("SmartScreen" in line for line in errors),
                        "уведомление Microsoft для SmartScreen потерян молча")

    def test_release_pack_is_named_in_notices(self):
        text = NOTICES.read_text(encoding="utf-8")
        self.assertIn("tools/build_legal_pack.py", text)
        self.assertIn("omnistash-notices.zip", text)


class TestNeutralPublicTexts(unittest.TestCase):
    """Публичные тексты пакета: имя взрослой площадки и запретные claims."""

    def test_current_pack_texts_are_neutral(self):
        bodies = {name: (ROOT / name).read_text(encoding="utf-8")
                  for name in BUILD.PACK_FILES}
        self.assertEqual(BUILD.neutral_checks(bodies), [],
                         "публичный текст пакета называет площадку или claim")

    def test_adult_name_and_domain_are_named(self):
        for replacement in ("PornHub", "pornhub.com"):
            def transform(text, value=replacement):
                return text.replace("взрослая площадка", value)

            with tamper("README.md", transform):
                code, errors = BUILD.build(check_only=True, want_zip=False)
            self.assertEqual(code, 1, f"{replacement}: имя площадки молча прошло")
            self.assertTrue(any("название или домен взрослой площадки" in line
                                for line in errors),
                            f"{replacement}: отказ назван не тем текстом")

    def test_circumvention_claim_is_named(self):
        with tamper("docs/LEGAL_FAQ.md",
                    lambda text: text + "\nПрограмма обходит ограничения площадки.\n"):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("circumvent" in line for line in errors),
                        "формулировка о circumvention прошла бы как нейтральная")

    def test_endorsement_claim_is_named(self):
        with tamper("README.md",
                    lambda text: text + "\nЭто официальный клиент YouTube.\n"):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("endorsement" in line for line in errors))

    def test_drm_claim_is_named(self):
        with tamper("docs/LEGAL_FAQ.md",
                    lambda text: text + "\nПрограмма снимает DRM у потоков.\n"):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("DRM" in line for line in errors))

    def test_age_verification_claim_is_named(self):
        with tamper("docs/ACCEPTABLE_USE.md",
                    lambda text: text + "\nПрограмма проверяет возраст зрителя.\n"):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("verification" in line for line in errors),
                        "ложное утверждение о verification молча прошло бы")

    def test_honest_negations_and_questions_are_not_claims(self):
        """«не поставляет», «не проверяет возраст» и вопрос - правда проекта, не claim."""
        lines = ["Нет. Проект не поставляет средства обхода блокировок.",
                 "Программа не проверяет возраст.",
                 "Вы поставляете инструменты обхода блокировок?",
                 "Omnistash не несёт ответственности за ваше использование.",
                 "Правила площадки ограничивают скачивание всегда."]
        bodies = {"docs/LEGAL_FAQ.md": "\n".join(lines)}
        self.assertEqual(BUILD.neutral_checks(bodies), [],
                         "честное отрицание или вопрос считались бы claim'ом")

    def test_missing_statement_is_named_by_file(self):
        def transform(text):
            return text.replace("## 5. Возрастное подтверждение", "## 5. Подтверждение")

        with tamper("docs/ACCEPTABLE_USE.md", transform):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(any("docs/ACCEPTABLE_USE.md: нет обязательных формулировок"
                            in line for line in errors))

    def test_missing_file_is_named_not_skipped(self):
        original = BUILD.read

        def read(target):
            if target == "THIRD_PARTY_NOTICES.md":
                raise ValueError(f"файл пакета не найден ({target})")
            return original(target)

        with mock.patch.object(BUILD, "read", read):
            code, errors = BUILD.build(check_only=True, want_zip=False)
        self.assertEqual(code, 1)
        self.assertTrue(errors)
        self.assertIn("файл пакета не найден", errors[0])


class TestPackWrites(unittest.TestCase):
    """Запись пакета: файлы, manifest и архив без самого себя."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="omnistash-pack-"))
        patcher = mock.patch.object(BUILD, "DIST", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def test_check_mode_writes_nothing(self):
        code, errors = BUILD.build(check_only=True, want_zip=True)
        self.assertEqual((code, errors), (0, []))
        self.assertEqual(list(self.dir.rglob("*")), [],
                         "режим проверки записал файлы: это не «только сверка»")

    def test_pack_files_and_manifest(self):
        code, errors = BUILD.build(check_only=False, want_zip=True)
        self.assertEqual((code, errors), (0, []), errors)
        written = sorted(p.relative_to(self.dir).as_posix()
                         for p in self.dir.rglob("*") if p.is_file())
        card = json.loads((ROOT / BUILD.LICENSES_MANIFEST).read_text(encoding="utf-8"))
        license_paths = [file["path"] for entry in card["entries"]
                         for file in entry["files"]]
        self.assertEqual(written, sorted(list(BUILD.PACK_FILES) + license_paths +
                                         [BUILD.LICENSES_MANIFEST, "manifest.json",
                                          BUILD.ZIP_NAME]),
                         "пакет не покрывает тексты условия лицензии и тексты "
                         "лицензий бандла")
        manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["files"]),
                         sorted(list(BUILD.PACK_FILES) + license_paths +
                                  [BUILD.LICENSES_MANIFEST]),
                         "manifest называет не тот состав пакета")
        self.assertEqual(manifest["licenseTexts"]["dir"], BUILD.LICENSES_DIR)
        self.assertEqual(manifest["licenseTexts"]["files"], len(license_paths))
        self.assertIn("hls.js", manifest["licenseTexts"]["notRequired"],
                      "вендор без текста в licenses/ не назван с причиной")
        for name, entry in manifest["files"].items():
            body = (self.dir / name).read_bytes()
            self.assertEqual(entry["sha256"], BUILD.sha256_bytes(body),
                             f"{name}: manifest говорит о другом содержимом")
            self.assertEqual(entry["bytes"], len(body))
        self.assertEqual(manifest["license"]["canonicalSha256"],
                         BUILD.sha256_bytes(LICENSE_CANON.read_bytes()))
        self.assertEqual(manifest["license"]["spdx"], BUILD.CANONICAL_SPDX)
        self.assertEqual(manifest["license"]["requiredNotice"],
                         "Required Notice: Copyright © 2026 vilminessa")
        self.assertEqual(manifest["archive"], BUILD.ZIP_NAME)
        self.assertIn("из сети ничего не скачивается", manifest["note"])

    def test_zip_excludes_itself_and_keeps_repo_paths(self):
        BUILD.build(check_only=False, want_zip=True)
        zip_path = self.dir / BUILD.ZIP_NAME
        with zipfile.ZipFile(zip_path) as pack:
            names = sorted(pack.namelist())
        self.assertNotIn(BUILD.ZIP_NAME, names,
                         "архив содержит себя - запись читает растущий файл")
        self.assertIn("docs/ACCEPTABLE_USE.md", names,
                      "путь внутри архива потерял каталог docs: получатель не "
                      "узнает, какой файл называет README")
        self.assertIn("ui_src/vendor/hls.js.LICENSE.txt", names,
                      "текст лицензии вендора не уехал в пакет (§4 Apache-2.0)")
        self.assertIn("licenses/yt-dlp/LICENSE", names,
                      "текст лицензии бандла не уехал: получатель копии "
                      "Unlicense не получил")
        self.assertIn("licenses/manifest.json", names,
                      "карта текстов лицензий не уехала: источник текста "
                      "остался бы не назван")
        self.assertIn("LICENSE", names)
        self.assertIn("manifest.json", names)


class TestPipelineAndDocs(unittest.TestCase):
    """Конвейер и документы: пакет сверяется до сборки и публикуется рядом с exe."""

    def test_ci_checks_the_pack_after_the_suite(self):
        text = CI.read_text(encoding="utf-8")
        tests_at = text.index("tools/run_tests.py")
        check_at = text.index("tools/build_legal_pack.py --check")
        build_at = text.index("tools/build_legal_pack.py --zip")
        self.assertLess(tests_at, check_at,
                        "сверка пакета идёт до набора тестов - битый состав "
                        "увидится позже")
        self.assertLess(check_at, build_at,
                        "пакет собирается до сверки текстов - отказ не назван")

    def test_beta_and_release_build_and_publish_the_pack(self):
        for path in (BETA, RELEASE):
            text = path.read_text(encoding="utf-8")
            self.assertIn("tools/build_legal_pack.py --check", text,
                          f"{path.name}: тексты пакета не сверяются до сборки exe")
            self.assertIn("tools/build_legal_pack.py --zip", text,
                          f"{path.name}: пакет не собирается")
            self.assertIn("release_pack/omnistash-notices.zip", text,
                          f"{path.name}: архив пакета не выкладывается ассетом")
            self.assertIn("name: Omnistash-notices", text,
                          f"{path.name}: артефакт пакета без имени")
            self.assertIn("notices/omnistash-notices.zip", text,
                          f"{path.name}: пакет не публикуется в релизе")
            self.assertIn("условие «Notices»", text,
                          f"{path.name}: не сказано, зачем пакет кладётся рядом с exe")
            self.assertIn("провенанс не распространяется", text,
                          f"{path.name}: не сказано, что на пакет провенанс не "
                          "распространяется - иначе «подписано всё» было бы враньём")

    def test_pack_asset_is_verified_from_the_release(self):
        """Наличие условия «Notices» сверяется по факту: ассет скачивается из релиза."""
        for path in (BETA, RELEASE):
            text = path.read_text(encoding="utf-8")
            self.assertIn("-p omnistash-notices.zip", text,
                          f"{path.name}: наличие пакета в релизе не проверяется")

    def test_workflow_lint_passes_after_the_edits(self):
        spec = importlib.util.spec_from_file_location("check_workflows", WORKFLOW_LINT)
        assert spec and spec.loader
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)
        for path in (CI, BETA, RELEASE):
            self.assertEqual(lint.check(path), [], f"{path.name}: линт отказал")

    def test_readme_notices_and_legal_review_name_the_pack(self):
        readme = README.read_text(encoding="utf-8")
        self.assertIn("tools/build_legal_pack.py", readme,
                      "README не описывает инструмент юр-пакета")
        self.assertIn("omnistash-notices.zip", readme,
                      "README не говорит, что уезжает рядом с exe")
        self.assertIn("POLYFORM-NONCOMMERCIAL-1.0.0.txt", readme,
                      "канонический якорь сверки лицензии не назван в README")
        self.assertIn("licenses/manifest.json", readme,
                      "тексты лицензий бандла в README не названы")
        self.assertIn("не аффилирован, не одобрен и не спонсируется", readme)
        self.assertIn("SmartScreen", readme,
                      "README не передаёт уведомление, которое требует Microsoft")

        notices = NOTICES.read_text(encoding="utf-8")
        self.assertIn("tools/build_legal_pack.py", notices)
        self.assertIn("POLYFORM-NONCOMMERCIAL-1.0.0.txt", notices)
        self.assertIn("MIT-0", notices,
                      "cffi распространяется под MIT-0 (MIT No Attribution) - "
                      "имя обязано стоять по тексту файла")
        self.assertIn("Roman Sirokov", notices)
        self.assertIn("licenses/manifest.json", notices,
                      "тексты лицензий бандла в перечне не названы")

        legal = LEGAL.read_text(encoding="utf-8")
        self.assertIn("tools/build_legal_pack.py", legal,
                      "LEGAL-REVIEW §5.2 не называет инструмент, который кладёт "
                      "тексты условий в релиз")
        self.assertIn("[x] В архив/ассеты релиза", legal,
                      "пункт §5.2 не закрыт - чек-лист врёт о разрыве")

    def test_aup_and_faq_cover_age_and_the_pack(self):
        aup = AUP.read_text(encoding="utf-8")
        self.assertIn("Возрастное подтверждение", aup,
                      "в AUP нет возрастного раздела (черновик 3.4 из LEGAL-REVIEW)")
        self.assertIn("не проверяет возраст", aup,
                      "AUP не говорит честно, что verification возраста не выполняется")
        self.assertIn("Подтверждения с датой", aup,
                      "датированные факты согласия не описаны в AUP")
        self.assertIn("взрослая площадка", aup)

        faq = FAQ.read_text(encoding="utf-8")
        self.assertIn("не юридическая консультация", faq)
        self.assertIn("18 лет", faq, "FAQ не отвечает на вопрос о возрасте")
        self.assertIn("не проверяет возраст", faq)
        self.assertIn("omnistash-notices.zip", faq,
                      "FAQ не говорит, что тексты условий уезжают рядом с exe")

    def test_adult_gate_unchanged_by_text_edits(self):
        """Тексты AUP/FAQ не ослабляют гейты: flags и accept - только по нажатию."""
        from app import legal
        source = (ROOT / "app" / "legal.py").read_text(encoding="utf-8")
        self.assertIn("aup_accepted_at", source)
        self.assertIn("adult_ok", source)
        self.assertIn("first_send_accepted", source)
        self.assertTrue(hasattr(legal, "accept"))

    def test_gitignore_keeps_the_pack_out_of_git(self):
        self.assertIn("release_pack/", GITIGNORE.read_text(encoding="utf-8"),
                      "пакет попал бы в git: это сборка, а не источник")

    def test_new_text_has_no_cjk_or_latin_glue(self):
        import re
        cjk = re.compile(r"[\u3400-\u9fff\uac00-\udfff]")
        glue = re.compile(r"[A-Za-z][а-яё]|[а-яё][A-Za-z]")
        skip = ("build_legal_pack", "omnistash", "release_pack", "notices", "zip",
                "manifest", "sha256", "license", "licence", "readme", "aup", "faq",
                "legal", "review", "gitignore", "smartscreen", "webview", "webview2",
                "webview2loader", "isreputationcheckingrequired", "aka.ms", "privacy",
                "polyform", "noncommercial", "dailymotion", "brightcove", "hls.js",
                "vendor", "docs", "ui_src", "pywebview", "sirokov", "copyright",
                "curl", "curl_cffi", "cffi", "certifi", "yt-dlp", "unlicense", "mit",
                "mpl", "bsd", "gpl", "lgpl", "psf", "apache", "upx", "zeroconf",
                "wormhole", "requirements", "unittest", "discover", "check", "build",
                "download", "upload", "artifact", "github", "slsa", "beta", "release",
                "ci.yml", "workflow", "permissions", "import", "def ", "mock", "re.",
                "compile", "tempfile", "json", "pathlib", "test", "assert", "http",
                "adult-site.invalid", "pornhub", "data-l", "smart", "screen")
        files = [ROOT / "tools" / "build_legal_pack.py", Path(__file__), README,
                 NOTICES, LICENSE, LICENSE_CANON, AUP, FAQ, LEGAL, GITIGNORE, CI,
                 BETA, RELEASE, ROOT / BUILD.LICENSES_MANIFEST]
        for path in files:
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
