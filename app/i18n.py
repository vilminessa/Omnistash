"""Подписи интерфейса (M15): русский по умолчанию и английский как второй.

Словарь покрывает то, что РЕАЛЬНО выводится в окне: вкладки, кнопки шапки,
колонки списка, разделы карточки настроек, новые поля темы/шрифта/языка,
диалоги согласий и нейтральное имя взрослой площадки. Всё, что словарь
не покрывает - остаётся русской подписью: UI держит русскую строку как
fallback и не показывает пустоту (никто не переводит «всё подряд»).

Нейтральные публичные тексты: возрастная площадка называется «взрослая
площадка» / "adult site", без имени площадки и её доменов. Внутренние id
и домены реестра (app/platforms.py, БД) от этого не меняются - функционал
сохраняется.
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "ru"

LANGS: tuple = (("ru", "Русский"), ("en", "English"))

# Подписи по ключам. Ключ, где есть только ru - сознательно не переводим:
# UI в этом случае показывает русскую строку (fallback из разметки/кода).
_TEXTS: dict = {
    # ---- вкладки -----------------------------
    "tab.library": {"ru": "Библиотека", "en": "Library"},
    "tab.sync": {"ru": "Синхронизация", "en": "Sync"},
    "tab.queue": {"ru": "Очередь", "en": "Queue"},
    "tab.settings": {"ru": "Настройки", "en": "Settings"},

    # ---- шапка и кнопки ----------------------
    "btn.rescan": {"ru": "Переиндексировать", "en": "Reindex"},
    "btn.rescan.tip": {"ru": "Пройти папки библиотеки и обновить индекс",
                       "en": "Scan library folders and refresh the index"},
    "btn.stop": {"ru": "Отмена", "en": "Cancel"},
    "btn.add": {"ru": "Добавить", "en": "Add"},
    "btn.add.tip": {"ru": "Добавить плейлист или канал",
                    "en": "Add a playlist or a channel"},

    # ---- колонки списка ----------------------
    "col.status": {"ru": "Статус", "en": "Status"},
    "col.title": {"ru": "Название", "en": "Title"},
    "col.channel": {"ru": "Канал", "en": "Channel"},
    "col.dur": {"ru": "Время", "en": "Duration"},
    "col.date": {"ru": "Дата", "en": "Date"},
    "col.size": {"ru": "Размер", "en": "Size"},

    # ---- поля ввода --------------------------
    "ph.search": {"ru": "Поиск по названию, описанию, тегам…",
                  "en": "Search by title, description, tags…"},
    "ph.site": {"ru": "Поиск на площадке…", "en": "Search on the site…"},

    # ---- разделы карточки настроек -----------
    "sec.library": {"ru": "Библиотека", "en": "Library"},
    "sec.download": {"ru": "Загрузка", "en": "Downloads"},
    "sec.mesh": {"ru": "Mesh", "en": "Mesh"},
    "sec.schedule": {"ru": "Расписание", "en": "Schedule"},
    "sec.account": {"ru": "Аккаунт Google", "en": "Google account"},
    "sec.appearance": {"ru": "Внешний вид", "en": "Appearance"},
    "sec.other": {"ru": "Прочее", "en": "Other"},

    # ---- новые поля темы/шрифта/языка --------
    "settings.theme.label": {"ru": "Тема", "en": "Theme"},
    "settings.theme.hint": {
        "ru": "Палитра окна: тёмная, светлая, контрастная или своя. "
              "Цвета задаются CSS-переменными, читаемость проверяется "
              "по контрасту подписей и подложек.",
        "en": "Window palette: dark, light, high contrast, or custom. "
              "Colors are CSS variables; readability is checked by the "
              "contrast of text against surfaces."},
    "settings.font_size.label": {"ru": "Размер шрифта, пикселов",
                                 "en": "Font size, pixels"},
    "settings.font_size.hint": {
        "ru": "Базовый размер текста окна. Строки списка растут вместе с "
              "текстом (высота строки --row-h), плитка остаётся "
              "фиксированной - layout не ломается.",
        "en": "Base text size of the window. List rows grow with the text "
              "(row height --row-h), tiles stay fixed - the layout does not "
              "break."},
    "settings.font_scale.label": {"ru": "Множитель размера текста",
                                  "en": "Text size multiplier"},
    "settings.font_scale.hint": {
        "ru": "Умножает базовый размер (100 = как задан). Множитель "
              "поднимает и высоту строк списка, поэтому подписи не "
              "обрезаются.",
        "en": "Multiplies the base size (100 = as set). The multiplier also "
              "grows list row heights, so captions are not clipped."},
    "settings.font_family.label": {"ru": "Семейство шрифта",
                                   "en": "Font family"},
    "settings.font_family.hint": {
        "ru": "Системные стек-семейства: ничего не скачивается и не "
              "вшивается - окно использует то, что есть в системе.",
        "en": "System font stacks: nothing is downloaded or embedded - the "
              "window uses what the system already has."},
    "settings.language.label": {"ru": "Язык интерфейса", "en": "Interface language"},
    "settings.language.hint": {
        "ru": "Русский по умолчанию; английский - второй язык. Подписи, "
              "которых словарь не покрывает, остаются русскими.",
        "en": "Russian by default; English is the second language. Captions "
              "the dictionary does not cover stay Russian."},
    "settings.show_adult_tab.label": {"ru": "Показывать вкладку взрослой площадки",
                                      "en": "Show the adult site tab"},
    "settings.show_adult_tab.hint": {
        "ru": "По умолчанию выключено: вкладка взрослой площадки появляется "
              "только после явного подтверждения 18+ (согласие фиксируется "
              "отдельно от настроек). Без подтверждения в интерфейсе нет "
              "ссылки на эту площадку, её карточек и фильтров; гейты "
              "18+ (добавление, массовые операции, каталог) не обходятся.",
        "en": "Off by default: the adult site tab appears only after an "
              "explicit 18+ confirmation (the consent is stored apart from "
              "settings). Without confirmation the interface has no link to "
              "that site, no cards and no filters; the 18+ gates (add, bulk "
              "operations, catalog) are not bypassed."},

    # ---- диалоги согласий --------------------
    "dlg.adult.title": {"ru": "Возрастной контент", "en": "Adult content"},
    "dlg.adult.text": {
        "ru": "Группа содержит строки взрослой площадки. Подтвердите, что "
              "вам есть 18 лет, вы применяете эти материалы только для "
              "личного пользования и несёте ответственность за скачиваемое "
              "(docs/ACCEPTABLE_USE.md).",
        "en": "The group contains rows from the adult site. Confirm that you "
              "are 18 or older, that you use these materials for personal "
              "use only, and that you are responsible for what you download "
              "(docs/ACCEPTABLE_USE.md)."},
    "dlg.adult.check": {"ru": "Мне есть 18 лет - подтверждаю",
                        "en": "I am 18 or older - I confirm"},
    "dlg.catalog_adult.text": {
        "ru": "Каталог этой площадки содержит материалы для взрослых. "
              "Подтвердите, что вам есть 18 лет и использование — личное "
              "(docs/ACCEPTABLE_USE.md).",
        "en": "The catalog of this site contains adult materials. Confirm "
              "that you are 18 or older and that the use is personal "
              "(docs/ACCEPTABLE_USE.md)."},
    "dlg.add_adult.text": {
        "ru": "Эта площадка содержит материалы для взрослых. Подтвердите, "
              "что вам есть 18 лет, вы применяете эти материалы только для "
              "личного пользования и несёте ответственность за скачиваемое "
              "(docs/ACCEPTABLE_USE.md).",
        "en": "This site contains adult materials. Confirm that you are 18 "
              "or older, that you use these materials for personal use only, "
              "and that you are responsible for what you download "
              "(docs/ACCEPTABLE_USE.md)."},
    "dlg.aup.title": {"ru": "Правила использования", "en": "Usage rules"},
    "dlg.aup.check": {"ru": "Правила использования приняты",
                      "en": "Usage rules accepted"},
    # Только русская подпись: английский вариант этого длинного диалога
    # пока не переводим (UI показывает русскую строку из разметки).
    "dlg.first_send.text": {
        "ru": "Файл уходит другим лицам - только в рамках ваших прав."},

    # ---- служебные подсказки -------------------
    "toast.saved": {"ru": "Сохранено", "en": "Saved"},
    "toast.adult_denied": {"ru": "Подтверждение 18+ не получено",
                           "en": "18+ confirmation not received"},
    "toast.adult_hidden": {"ru": "Вкладка взрослой площадки скрыта: "
                                "подтвердите 18+",
                           "en": "The adult site tab is hidden: confirm 18+"},
    "adult.site": {"ru": "Взрослая площадка", "en": "Adult site"},
    "adult.tab_note": {"ru": "Возрастной контент", "en": "Adult content"},
}

_BY_KEY = dict(_TEXTS)


def languages() -> list:
    """[[код, подпись], ...] - choices поля «Язык интерфейса»."""
    return [[code, label] for code, label in LANGS]


def language_ids() -> tuple:
    return tuple(code for code, _ in LANGS)


_BY_LANG = {code for code, _ in LANGS}


def normalize(value) -> str:
    """Значение настройки языка: битое -> дефолт (русский)."""
    return value if value in _BY_LANG else DEFAULT_LANGUAGE


def has(key: str) -> bool:
    """Есть ли подпись в словаре под ключом."""
    return key in _BY_KEY


def t(key: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Подпись по ключу и языку: покрытый ключ - язык, иначе русский.

    Ключ без перевода (и вообще неизвестный ключ) - русская подпись,
    если она в словаре есть; пустая строка для совсем неизвестного
    ключа: UI сам подставит свою русскую строку из разметки.
    """
    entry = _BY_KEY.get(key)
    if entry is None:
        return ""
    lang = normalize(language)
    if lang != DEFAULT_LANGUAGE:
        value = entry.get(lang)
        if value:
            return value
    return entry.get(DEFAULT_LANGUAGE, "")


def ui_texts(language: str = DEFAULT_LANGUAGE) -> dict:
    """Карта подписей для UI: покрытые ключи на языке, остальные - русские.

    Английский вариант, которого в словаре нет, в карту НЕ попадает: UI
    тогда показывает русскую подпись из разметки/кода (fallback), и мы не
    переводим то, чего словарь не покрывает.
    """
    lang = normalize(language)
    out = {}
    for key, entry in _BY_KEY.items():
        if lang == DEFAULT_LANGUAGE:
            out[key] = entry.get(DEFAULT_LANGUAGE, "")
            continue
        value = entry.get(lang)
        if value:
            out[key] = value
    return out


def schema_labels(language: str = DEFAULT_LANGUAGE) -> dict:
    """Перевод подписей карточки настроек: {key: {label, hint}}.

    Для русского языка возвращает пустую карту: карточка и так рисуется
    русскими подписями из схемы (ничего не «переписываем» на русском).
    Поля, которых словарь не покрывает, в карту не попадают - остаются
    подписями из settings_schema (русскими).
    """
    lang = normalize(language)
    if lang == DEFAULT_LANGUAGE:
        return {}
    out = {}
    for key, entry in _BY_KEY.items():
        if not key.startswith("settings."):
            continue
        field = key[len("settings."):]
        if "." not in field:
            continue
        name, part = field.rsplit(".", 1)
        value = entry.get(lang)
        if not value:
            continue
        item = out.setdefault(name, {})
        item[part] = value
    return out


def sections(language: str = DEFAULT_LANGUAGE) -> dict:
    """Перевод названий разделов карточки: {русское_имя: подпись на языке}."""
    lang = normalize(language)
    out = {}
    for key, entry in _BY_KEY.items():
        if not key.startswith("sec."):
            continue
        value = entry.get(lang) or entry.get(DEFAULT_LANGUAGE)
        ru = entry.get(DEFAULT_LANGUAGE, "")
        if value:
            out[ru] = value
    return out


def adult_site_name(language: str = DEFAULT_LANGUAGE) -> str:
    """Нейтральная подпись взрослой площадки (без имени и доменов)."""
    return t("adult.site", language)

