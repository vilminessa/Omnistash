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
    # Раздел G2 (OAuth/Data API) добавлен сводкой: название раздела тоже
    # переводится, иначе карточка настроек на втором языке показывает русское.
    "sec.metadata": {"ru": "Метаданные API", "en": "Metadata API"},
    "sec.appearance": {"ru": "Внешний вид", "en": "Appearance"},
    "sec.other": {"ru": "Прочее", "en": "Other"},
    # §3.5 шаг 6: вторичный слой редких полей (mesh, обход, PO-токены).
    "sec.extra": {"ru": "Дополнительно", "en": "Advanced"},
    # SV-C S1: доступ по HTTPS для клиента (docs/SERVER-CLIENT.md §7).
    "sec.http": {"ru": "Доступ по HTTPS", "en": "HTTPS access"},

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

    # ---- новые поля SV-C S1: доступ по HTTPS ------------------------------
    # Подписи на русском берёт схема (settings_schema), английская карта нужна
    # только второму языку: карточка настроек не остаётся русской.
    "settings.serve_enabled.label": {"ru": "Доступ по HTTPS (--serve)",
                                     "en": "HTTPS access (--serve)"},
    "settings.serve_enabled.hint": {
        "ru": "Тот же процесс Omnistash поднимает HTTPS-слушатель рядом с "
              "окном: браузер-клиент читает ту же библиотеку и работает с "
              "той же очередью, а база и хранилища остаются у сервера. По "
              "умолчанию выключено: включение - явное согласие, права "
              "клиента задаются скоупами на ключе доступа, обращения "
              "пишутся в журнал аудита.",
        "en": "The same Omnistash process opens an HTTPS listener next to "
              "the window: a browser client reads the same library and works "
              "with the same queue, while the database and storages stay on "
              "the server. Off by default: turning it on is explicit consent, "
              "client rights come from scopes on the access key, and every "
              "call is written to the audit log."},
    "settings.serve_bind.label": {"ru": "Куда привязать слушатель",
                                  "en": "Where to bind the listener"},
    "settings.serve_bind.hint": {
        "ru": "Сейчас доступен только «этот компьютер» (127.0.0.1): наружу "
              "ничего не уходит. Сеть (LAN), Tailscale и обычный порт с "
              "allowlist придут следующим шагом и потребуют того же согласия "
              "«что уходит наружу».",
        "en": "Only \"this computer\" (127.0.0.1) is available now: nothing "
              "leaves the machine. LAN, Tailscale and a plain port with an "
              "allowlist come in the next step and need the same consent "
              "about what leaves outward."},
    "settings.serve_port.label": {"ru": "Порт HTTP-сервера клиента",
                                  "en": "Client HTTP server port"},
    "settings.serve_port.hint": {
        "ru": "TLS на этом порту, адрес стабилен между перезапусками; при "
              "занятости слушатель не поднимется, причина - в журнале. "
              "ВАЖНО для совместимости: DPI-утилиты (zapret и подобные) по "
              "умолчанию перехватывают порты 1024-65535 (а также 80/443) - "
              "держите порт ВНЕ этой зоны: рекомендуется 444-1023, по "
              "умолчанию 1001 (рядом с mesh 1000). Выключать DPI-программу "
              "не нужно.",
        "en": "TLS on this port; the address stays stable across restarts, "
              "and a busy port means the listener does not start (the reason "
              "goes to the log). IMPORTANT for compatibility: DPI tools "
              "(zapret and similar) intercept ports 1024-65535 and 80/443 by "
              "default - keep the port OUTSIDE that zone: 444-1023 is "
              "recommended, 1001 by default (next to mesh 1000). Turning the "
              "DPI program off is not needed."},
    "settings.serve_rate_mbit.label": {"ru": "Ограничение отдачи клиенту, Мбит/с",
                                       "en": "Client delivery limit, Mbit/s"},
    "settings.serve_rate_mbit.hint": {
        "ru": "Отдача клиенту не быстрее этого (по умолчанию 10); 0 - без "
              "ограничения. Тот же ограничитель скорости, что у mesh-сервера: "
              "быстрая выкачка не выжирает канал.",
        "en": "Delivery to a client is no faster than this (10 by default); "
              "0 means no limit. The same rate limiter the mesh server uses: "
              "a fast pull does not eat the whole channel."},
    "settings.serve_watchers_max.label": {"ru": "Одновременных зрителей, максимум",
                                          "en": "Max concurrent viewers"},
    "settings.serve_watchers_max.hint": {
        "ru": "Сколько клиентов играют одновременно (байты медиа и HLS); "
              "сверх лимита - честный отказ «сервер ограничил отдачу», полоса "
              "не выдумывается. 0 - без лимита.",
        "en": "How many clients may play at once (media bytes and HLS); over "
              "the limit the server answers honestly \"the server limited "
              "delivery\" and no progress bar is invented. 0 means no limit."},
    "settings.serve_hls_transcode.label": {"ru": "Перекодировать видео для браузера",
                                           "en": "Transcode video for the browser"},
    "settings.serve_hls_transcode.hint": {
        "ru": "Ре-encode HEVC/VP9/AV1 жрёт CPU и качество: по умолчанию "
              "выключено, отказ называет кодек и говорит, что браузер не "
              "играет этот файл без перекодирования. Включается, когда байты "
              "файла браузеру действительно не годятся.",
        "en": "Re-encoding HEVC/VP9/AV1 burns CPU and quality: off by "
              "default, and the refusal names the codec and says the browser "
              "cannot play this file without re-encoding. Turn it on only "
              "when the file bytes really do not work for the browser."},

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

    # ---- подписи HTTP-клиента (docs/SERVER-CLIENT.md S0) -------------------
    # Причина отказа приходит со стороны сервера; когда тела ответа нет, UI
    # показывает эту подпись по коду - без выдуманных данных.
    "http.pairing": {"ru": "ключ доступа не принят - привязка не завершена",
                     "en": "access key not accepted - pairing not completed"},
    "http.no_token": {"ru": "ключ доступа не получен - привязка не завершена",
                      "en": "access key not received - pairing not completed"},
    "http.scope": {"ru": "право не выдано сервером",
                   "en": "the server did not grant this right"},
    "http.missing": {"ru": "не найдено", "en": "not found"},
    "http.busy": {"ru": "на сервере уже выполняется другая операция",
                  "en": "another operation is already running on the server"},
    "http.too_large": {"ru": "слишком большой запрос для сервера",
                       "en": "request is too large for the server"},
    "http.rate": {"ru": "сервер ограничил отдачу (лимит скорости или зрителей)",
                  "en": "the server limited delivery (rate or viewer limit)"},
    "http.server": {"ru": "сервер ответил ошибкой",
                    "en": "the server answered with an error"},
    "http.bad_answer": {"ru": "неожиданный ответ сервера",
                        "en": "unexpected answer from the server"},
    "http.bad_json": {"ru": "сервер ответил не JSON",
                      "en": "the server answered with non-JSON"},
    "http.offline": {"ru": "сервер не отвечает",
                     "en": "the server does not answer"},
    "http.desktop": {"ru": "доступно только на сервере",
                     "en": "available on the server only"},
    "http.local": {"ru": "это локальная настройка клиента, серверу не передаётся",
                   "en": "this is a local client setting, not sent to the server"},

    # ---- подписи HTTP-сервера (docs/SERVER-CLIENT.md S1) -------------------
    # Их называет сервер в теле ответа: клиент показывает причину как есть,
    # а не подставляет свою подпись, когда сервер молчит.
    "http.scope_denied": {"ru": "не выдан скоуп", "en": "scope not granted"},
    "http.post_only": {"ru": "вызов метода - только POST",
                       "en": "method calls must use POST"},
    "http.bad_body": {"ru": "тело запроса не разобрано",
                      "en": "the request body was not parsed"},
    "http.body_too_large": {"ru": "тело запроса слишком большое",
                            "en": "the request body is too large"},
    "http.scopes_none": {"ru": "пустой список скоупов - доступа нет",
                         "en": "empty scope list - no access"},
    "http.tokens_broken": {"ru": "файл токенов не расшифровывается "
                                 "(DPAPI или битый файл)",
                           "en": "the token file cannot be decrypted "
                                 "(DPAPI or a broken file)"},
    "http.tokens_dpapi": {"ru": "DPAPI не удалось зашифровать токены",
                          "en": "DPAPI could not encrypt the tokens"},
    "http.tokens_limit": {"ru": "ключей достигнут лимит - сначала отзовите лишний",
                          "en": "key limit reached - revoke an extra key first"},
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

