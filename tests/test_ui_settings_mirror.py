"""Зеркало настроек в превью (ui_src/app.js) сверяется со схемой бэкенда.

Превью работает без бэкенда и держит свой список полей, поэтому оно обязано
показывать ровно то, что показывает настоящее окно: ключ, которого нет в
зеркале, человек в превью не увидит (так из проверяемого превью молча пропали
поля OAuth/Data API, токен выкачки и дороги обхода).

Что сверяется (структура поля):
  * key / type / section / label / choices / min / max / default;
  * лишнего в зеркале нет: поля, которых нет в схеме бэкенда, не рисуются;
  * виджеты и кнопки (account/action/storages) и скрытые поля не сверяются:
    их рисует код окна, а hidden бэкенд окну не присылает;
  * секретные ключи есть в зеркале, и app.js читает признак «установлен»
    (`*_set`) вместо значения, которое backend не отдаёт (as_public);
  * подсказка (hint) в превью сокращена намеренно - её смысл человек читает в
    настоящем окне, поэтому тексты hint здесь не сверяются.
"""

import re
import unittest
from pathlib import Path

from app import i18n, settings, settings_schema

SRC = Path(__file__).resolve().parent.parent
JS = SRC / "ui_src" / "app.js"

# Поля, которые рисует код окна (виджет аккаунта, кнопка действия, хранилища,
# виджет доступа по HTTPS S6).
WIDGET_TYPES = {"account", "action", "storages", "serve"}

ENTRY = re.compile(r"\{[^{}]*\}", re.S)          # один объект поля внутри списка
FIELD = re.compile(r'(key|type|section|label|hidden):\s*("[^"]*"|true|false)')
PAIR = re.compile(r'\[\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\]')


def mirror_region(text: str) -> str:
    """Текст списка полей зеркала: от `var schema = [` до закрывающего `]`."""
    start = text.index("var schema = [")
    depth = 0
    for index in range(start + len("var schema = "), len(text)):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError("в app.js не найден список полей зеркала")


def _value(raw: str):
    if raw.startswith('"'):
        return raw[1:-1]
    return raw == "true"


def _choices(chunk: str, start: int):
    """Список choices целиком (пары ["id","подпись"]); разбор по скобкам."""
    index = chunk.index("[", start)
    depth = 0
    for position in range(index, len(chunk)):
        if chunk[position] == "[":
            depth += 1
        elif chunk[position] == "]":
            depth -= 1
            if depth == 0:
                break
    else:
        return None
    return [list(pair) for pair in PAIR.findall(chunk[index:position + 1])]


def _default(chunk: str, start: int):
    match = re.match(r'\s*("[^"]*"|true|false|-?\d+)', chunk[start:])
    if not match:
        return None            # default: [] у виджетов - нам не нужен
    raw = match.group(1)
    if raw.startswith('"'):
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    return int(raw)


def mirror_fields(text: str) -> dict[str, dict]:
    """Поля зеркала по ключу: key/type/section/label/hidden/choices/min/max/default."""
    out: dict[str, dict] = {}
    for chunk in ENTRY.findall(mirror_region(text)):
        entry: dict = {}
        for name, raw in FIELD.findall(chunk):
            entry[name] = _value(raw)
        for name in ("choices", "default"):
            marker = chunk.find(name + ":")
            if marker >= 0:
                entry[name] = (
                    _choices(chunk, marker) if name == "choices"
                    else _default(chunk, marker + len(name) + 1))
        for name in ("min", "max"):
            match = re.search(rf'{name}:\s*(-?\d+)', chunk)
            if match:
                entry[name] = int(match.group(1))
        if "key" in entry:
            out[entry["key"]] = entry
    if not out:
        raise AssertionError("в app.js не найден список полей зеркала")
    return out


def plain_schema() -> dict[str, dict]:
    """Поля, которые настоящее окно показывает как обычные карточки."""
    return {field["key"]: field for field in settings_schema.schema()
            if not field.get("hidden") and field["type"] not in WIDGET_TYPES}


def visible_mirror(text: str) -> dict[str, dict]:
    return {key: entry for key, entry in mirror_fields(text).items()
            if not entry.get("hidden") and entry.get("type") not in WIDGET_TYPES}


SETTING_PAIR = re.compile(r'([A-Za-z0-9_]+)\s*:\s*("[^"]*"|true|false|-?\d+)')


def _normalize(raw: str):
    """Значение из JS-литерала: строка без кавычек, true/false, число."""
    if raw.startswith('"'):
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    return int(raw)


def mock_settings_values(text: str) -> dict[str, object]:
    """Значения снимка настроек в превью (`var settings = { ... }`)."""
    marker = text.index("var settings = {")
    depth = 0
    for index in range(marker + len("var settings = "), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                region = text[marker:index]
                return {key: _normalize(raw)
                        for key, raw in SETTING_PAIR.findall(region)}
    raise AssertionError("в app.js не найден снимок настроек превью")


class TestSettingsMirror(unittest.TestCase):
    def setUp(self):
        self.text = JS.read_text(encoding="utf-8")
        self.mirror = visible_mirror(self.text)
        self.schema = plain_schema()

    def test_plain_fields_cover_the_schema(self):
        missing = sorted(set(self.schema) - set(self.mirror))
        self.assertEqual(missing, [],
                         f"в зеркале превью нет полей схемы: {missing}")

    def test_mirror_has_no_fields_outside_the_schema(self):
        extra = sorted(set(self.mirror) - set(self.schema))
        self.assertEqual(extra, [],
                         f"зеркало показывает поля, которых нет в схеме: {extra}")

    def test_field_shape_matches_the_schema(self):
        for key, field in sorted(self.schema.items()):
            entry = self.mirror[key]
            self.assertEqual(entry["type"], field["type"], f"{key}: тип расходится")
            self.assertEqual(entry.get("section") or "",
                             field.get("section") or "", f"{key}: секция расходится")
            self.assertEqual(entry.get("label") or "",
                             field.get("label") or "", f"{key}: подпись расходится")
            self.assertEqual(entry.get("choices"), field.get("choices"),
                             f"{key}: список значений расходится")
            self.assertEqual(entry.get("min"), field.get("min"),
                             f"{key}: нижняя граница расходится")
            self.assertEqual(entry.get("max"), field.get("max"),
                             f"{key}: верхняя граница расходится")
            self.assertEqual(entry.get("default"), field.get("default"),
                             f"{key}: значение по умолчанию расходится")

    def test_mock_snapshot_covers_every_shown_field(self):
        """Снимок значений покрывает показанные поля, иначе превью рисует пустые поля.

        Значения берутся из `var settings = { ... }`; `fieldHtml` не подставляет
        default из схемы, поэтому отсутствующий ключ даёт пустое поле или
        «undefined» в числовом поле.
        """
        values = mock_settings_values(self.text)
        gaps = sorted(set(self.mirror) - set(values))
        self.assertEqual(gaps, [],
                         f"в снимке превью нет значений для полей: {gaps}")
        for key, entry in sorted(self.mirror.items()):
            if entry["type"] != "choice":
                continue
            allowed = [pair[0] for pair in entry.get("choices") or []]
            self.assertIn(values[key], allowed,
                          f"{key}: значение «{values[key]}» вне списка схемы")

    def test_widgets_and_hidden_fields_stay_out_of_the_parity_check(self):
        """Виджеты окна есть в зеркале, скрытые поля бэкенда не рисуются."""
        all_mirror = mirror_fields(self.text)
        hidden_backend = {field["key"] for field in settings_schema.FIELDS
                          if field.get("hidden")}
        widgets = {"_storages", "_refresh_meta", "_peers", "_ffmpeg",
                   "_google_account", "_serve_access"}
        for key in widgets:
            self.assertIn(key, all_mirror, f"{key}: виджет пропал из превью")
        shown = set(self.schema) | widgets | hidden_backend
        self.assertEqual(sorted(set(all_mirror) - shown), [],
                         "в зеркале есть поле, которого бэкенд не присылает")
        # Скрытые поля зеркала соответствуют скрытым полям бэкенда: превью не
        # держит своих секретных ключей, которых в схеме нет.
        hidden_mirror = {key for key, entry in all_mirror.items()
                         if entry.get("hidden")}
        self.assertEqual(sorted(hidden_mirror - hidden_backend), [],
                         "в зеркале скрыто поле, которого нет в схеме бэкенда")

    def test_secret_keys_are_in_the_mirror_and_never_shown(self):
        mirror = mirror_fields(self.text)
        values = mock_settings_values(self.text)
        for key in sorted(settings.SECRET_KEYS):
            self.assertIn(key, mirror, f"{key}: секретного поля нет в зеркале")
            self.assertEqual(values.get(key, ""), "",
                             f"{key}: превью показывает секретное значение")
            self.assertTrue(re.search(rf'{key}_set\s*[:=]\s*(true|false)', self.text),
                            f"{key}: признак «установлен» не читается")

    def test_ui_reads_the_set_flag_instead_of_the_value(self):
        """Признак «установлен / не установлен» читается из `*_set`."""
        self.assertIn('field.key + "_set"', self.text)
        # Подсказка склеена из двух литералов - сверяем обе части.
        self.assertIn("значение сохранено в профиле и скрыто от ", self.text)
        self.assertIn("окна; впишите новое, чтобы заменить", self.text)

    def test_second_language_card_matches_the_dictionary(self):
        """Английская карточка превью рисует те же подписи, что настоящее окно.

        Окно берёт английские подписи полей и названий разделов из app/i18n.py
        (`schema_labels`/`sections`), превью - из своих карт MOCK_LABELS_EN и
        MOCK_SECTIONS_EN. Расхождение показало бы в превью другой английский,
        чем человек видит в окне, и проверка переключения языка в превью
        стала бы пустой.
        """
        labels_block = re.search(r"var MOCK_LABELS_EN\s*=\s*\{(.*?)\n    \};",
                                 self.text, re.DOTALL)
        assert labels_block, "в превью нет карты английских подписей полей"
        sections_block = re.search(r"var MOCK_SECTIONS_EN\s*=\s*\{(.*?)\};",
                                   self.text, re.DOTALL)
        assert sections_block, "в превью нет карты английских названий разделов"

        mock_labels = {}
        for match in re.finditer(r"([a-z_]+):\s*\{([^{}]*)\}", labels_block.group(1)):
            label = re.search(r'label:\s*"([^"]+)"', match.group(2))
            mock_labels[match.group(1)] = label.group(1) if label else ""
        window_labels = i18n.schema_labels("en")
        self.assertTrue(window_labels, "словарь не покрывает английский карточки")
        for key, item in sorted(window_labels.items()):
            self.assertIn(key, mock_labels, f"{key}: превью не переводит это поле")
            self.assertEqual(mock_labels[key], item["label"],
                             f"{key}: английская подпись в превью ≠ словарю окна")

        mock_sections = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"',
                                        sections_block.group(1)))
        window_sections = i18n.sections("en")
        names = {field["section"] for field in settings_schema.schema()
                 if field["section"]}
        # Вторичный слой («Дополнительно») схема не присылает: его имя рисует
        # код превью из своего списка - оно тоже обязано совпадать с окном.
        extra = re.search(r'var EXTRA_NAME = "([^"]+)"', self.text)
        assert extra, "имя вторичного слоя пропало из превью"
        names.add(extra.group(1))
        for name in sorted(names):
            self.assertIn(name, mock_sections,
                          f"раздел «{name}» не переведён в превью: окно на "
                          "английском показывает одно имя, превью - другое")
            self.assertEqual(mock_sections[name], window_sections[name],
                             f"раздел «{name}»: английский в превью ≠ словарю")

    def test_poll_signature_is_cheap(self):
        """Сигнатура снимка не строит JSON больших ключей на каждый тик."""
        self.assertIn("function signature(value)", self.text)
        self.assertIn("function hashText(text)", self.text)
        self.assertNotIn("var sig = JSON.stringify(data)", self.text)


if __name__ == "__main__":
    unittest.main()
