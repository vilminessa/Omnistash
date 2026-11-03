"""Нейтральные публичные тексты (контракт: имя и домены взрослой площадки не в публичных текстах).

Проверяем то, что уходит наружу: скрипты и разметка окна, README и правовые
документы, плюс собранный превью (preview.html - сборный artifact, его может
не быть в свежем checkout: тогда он пропускается, а не валит проверку).

Что проверяется:
  * форма названия (PornHub / PORNHUB / Pornhub) нигде в публичных текстах;
  * домены взрослой площадки (pornhub.com/.org/.net) нигде в публичных
    текстах и в собранной странице;
  * нейтральная подпись используется («взрослая площадка»);
  * собранный превью актуален: он собран из тех же скриптов, что в ui_src
    (иначе проверка «в превью нет имени» была бы ложью).

Внутренние id площадок (`platform === "pornhub"` в коде) и домены в реестре
(app/platforms.py) - функциональная часть, они остаются: запрет касается
ТЕКСТОВ, которые читает человек.
"""

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

# Публичные тексты в репозитории (то, что человек видит в окне и читает).
REPO_FILES = [
    "ui_src/app.js",
    "ui_src/app.css",
    "ui_src/index.html",
    "ui_src/watch.html",
    "ui_src/watch.js",
    "README.md",
    "docs/ACCEPTABLE_USE.md",
    "docs/LEGAL_FAQ.md",
]
# Собирается из ui_src (gitignored): проверяем, только когда он есть.
PREVIEW = "preview.html"

# Название площадки в любой регистровой форме. Внутренний id («pornhub» в
# коде) сюда не попадает: это идентификатор, не текст, который читает человек.
NAME = re.compile(r"PornHub|PORNHUB|Pornhub")
DOMAINS = re.compile(r"pornhub\.com|pornhub\.org|pornhub\.net")
NEUTRAL = "взрослая площадка"
# В разметке вкладка названа нейтрально без слова «площадка» («взрослая
# вкладка»): там достаточно нейтрального признака, имя площадки запрещено.
NEUTRAL_ANY = "взрослая"


def read(path: str) -> str:
    target = SRC / path
    if not target.is_file():
        raise AssertionError(f"нет публичного файла: {path}")
    return target.read_text(encoding="utf-8")


def preview_text() -> str | None:
    """Собранная страница окна или None, если превью не собран."""
    target = SRC / PREVIEW
    return target.read_text(encoding="utf-8") if target.is_file() else None


def flagged(text: str) -> list[tuple[int, str]]:
    return [(index + 1, line.strip()[:90])
            for index, line in enumerate(text.splitlines())
            if NAME.search(line) or DOMAINS.search(line)]


class TestNeutralPublicTexts(unittest.TestCase):
    def test_no_name_or_domains_in_repo_texts(self):
        for path in REPO_FILES:
            self.assertEqual(flagged(read(path)), [],
                             f"{path}: название/домен в тексте")

    def test_no_name_or_domains_in_built_preview(self):
        preview = preview_text()
        if preview is None:
            self.skipTest("preview.html не собран (python tools/build_preview.py)")
        self.assertEqual(flagged(preview), [],
                         "preview.html: название/домен в собранной странице")

    def test_neutral_wording_present(self):
        """Нейтральная форма есть там, где раньше звучало имя площадки."""
        for path in ("ui_src/app.js", "README.md"):
            self.assertIn(NEUTRAL, read(path), f"{path}: нет нейтральной подписи")
        self.assertIn(NEUTRAL_ANY, read("ui_src/index.html"),
                      "ui_src/index.html: нет нейтральной подписи вкладки")
        preview = preview_text()
        if preview is not None:
            self.assertIn(NEUTRAL, preview, "preview.html: нет нейтральной подписи")

    def test_preview_matches_current_scripts(self):
        """Превью собран из текущих скриптов: проверка не смотрит на старый файл."""
        preview = preview_text()
        if preview is None:
            self.skipTest("preview.html не собран (python tools/build_preview.py)")
        marker = "Забрать адрес"        # кнопка панели площадки (этап D)
        self.assertIn(marker, read("ui_src/index.html"))
        self.assertIn(marker, preview,
                      "preview.html не пересобран после правок ui_src "
                      "(python tools/build_preview.py)")

    def test_bundled_mock_uses_placeholder_url(self):
        """Мок карточек взрослой площадки в превью - с placeholder-доменом."""
        js = read("ui_src/app.js")
        self.assertIn("adult-site.invalid", js)
        self.assertFalse(DOMAINS.search(js))

    def test_schema_hint_is_neutral(self):
        from app import settings_schema
        hints = {field["key"]: field.get("hint") or ""
                 for field in settings_schema.schema()}
        for key, hint in hints.items():
            self.assertFalse(NAME.search(hint) or DOMAINS.search(hint),
                             f"{key}: hint называет площадку: {hint[:80]}")

    def test_ui_texts_dictionary_is_neutral(self):
        from app import i18n
        blob = "\n".join(value for entry in i18n._TEXTS.values()
                         for value in entry.values())
        self.assertFalse(NAME.search(blob) or DOMAINS.search(blob),
                         "словарь текстов называет площадку")
        self.assertIn("Взрослая площадка", blob)


if __name__ == "__main__":
    unittest.main()
