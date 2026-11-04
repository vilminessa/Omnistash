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
        "ru": "По умолчанию «только этот компьютер» (127.0.0.1). Сеть "
              "(LAN), Tailscale и обычный порт (0.0.0.0) требуют явного "
              "согласия «что уходит наружу»: без него слушатель остаётся на "
              "этом компьютере, а отказ назван в журнале. Обычный порт "
              "кроме того требует allowlist - списка адресов или CIDR.",
        "en": "By default \"this computer only\" (127.0.0.1). LAN, Tailscale "
              "and a plain port (0.0.0.0) need the explicit consent about "
              "what leaves outward; without it the listener stays on this "
              "machine and the refusal is written to the log. A plain port "
              "also needs an allowlist - a list of addresses or CIDRs."},
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
    # ---- SV-C S6: экспозиция, allowlist и привязка -----------------------
    # Согласие `serve_expose_consent` в словаре НЕ покрывается: поле скрыто в
    # карточке (решение принимает диалог), а отказ о прямой записи называет
    # подпись `http.expose_consent` - переводимого текста карточки здесь нет.
    "settings.serve_allowlist.label": {"ru": "Allowlist адресов (IP или CIDR)",
                                       "en": "Allowlist of addresses (IP or CIDR)"},
    "settings.serve_allowlist.hint": {
        "ru": "Список через запятую или пробел: 192.168.1.7, 10.0.0.0/24. "
              "Для обычного порта (0.0.0.0) обязателен: без него слушатель "
              "не поднимается. Битый элемент списка назван причиной - "
              "«половина списка понята» было бы доступом, которого нет в "
              "плане. Loopback-клиент всегда свой.",
        "en": "Comma- or space-separated list: 192.168.1.7, 10.0.0.0/24. "
              "Required for a plain port (0.0.0.0); without it the listener "
              "does not start. A broken entry is named - \"half the list was "
              "understood\" would be access nobody planned. A loopback "
              "client is always local."},
    "settings.serve_pair_scope.label": {"ru": "Права, которые выдаёт привязка",
                                        "en": "Rights issued by pairing"},
    "settings.serve_pair_scope.hint": {
        "ru": "Код привязки выдаёт ключ с пресетом прав: «читатель и плеер» "
              "(read+watch), «читатель, закачка и пометки» "
              "(read+watch+queue+mark), «оператор» (все скоупы, кроме "
              "нативного окна). По умолчанию читатель: клиент смотрит и "
              "играет, но не качает и не меняет настройки.",
        "en": "The pairing code issues a key with a preset: \"reader and "
              "player\" (read+watch), \"reader, downloads and marks\" "
              "(read+watch+queue+mark), \"operator\" (every scope except the "
              "native window). Reader by default: the client looks and plays, "
              "but does not download or change settings."},
    "settings.serve_pair_minutes.label": {"ru": "Срок кода привязки, минут",
                                          "en": "Pairing code lifetime, minutes"},
    "settings.serve_pair_minutes.hint": {
        "ru": "Код одноразовый и живёт это время; истёкший код отказан "
              "подписью, а не молчанием. После 5 неверных попыток с одного "
              "адреса перебор затормозен, задержка растёт и видна в журнале.",
        "en": "The code is single-use and lives this long; an expired code is "
              "refused with a caption, not silence. After 5 wrong attempts "
              "from one address the brute force is throttled, the delay grows "
              "and is visible in the log."},
    "settings._serve_access.label": {
        "ru": "Состояние, согласие и ключи доступа",
        "en": "State, consent and access keys"},
    "settings._serve_access.hint": {
        "ru": "Показывает, поднят ли слушатель, куда он слушает и что "
              "согласие говорит про наружу; отсюда же принимается согласие "
              "«что уходит наружу» и показывается одноразовый код привязки "
              "(значение кода отдаёт только окно сервера). Список ключей "
              "показывает метки и скоупы - значений токенов здесь нет, "
              "ключ можно отозвать.",
        "en": "Shows whether the listener is up, where it listens and what "
              "the consent says about outward traffic; the \"what leaves "
              "outward\" consent is accepted here, and the single-use pairing "
              "code is shown here (its value is given to the server window "
              "only). The key list shows labels and scopes - no token values, "
              "and a key can be revoked."},

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
    "http.get_only": {"ru": "этот маршрут читается только GET",
                      "en": "this route is read with GET only"},
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
    # Подписи байтовых маршрутов (§3, S2): что не найдено и почему отказали.
    "http.thumb_missing": {"ru": "обложка не найдена", "en": "thumbnail not found"},
    "http.file_missing": {"ru": "файл не найден или пропал с диска",
                          "en": "file not found or gone from disk"},
    "http.subs_missing": {"ru": "субтитры не найдены", "en": "subtitles not found"},
    "http.subs_format": {"ru": "формат субтитров браузер не читает",
                         "en": "the browser cannot read this subtitle format"},
    "http.range": {"ru": "запрошенная часть файла не читается",
                   "en": "the requested byte range is not readable"},
    # Подписи HLS (§6, S3): отказ называет кодек или инструмент, а не молчит.
    "http.hls_codec": {"ru": "браузер не играет этот кодек без перекодирования "
                              "(включите «Перекодировать видео для браузера»)",
                       "en": "the browser cannot play this codec without "
                             "re-encoding (enable \"Transcode video for the browser\")"},
    "http.hls_encoder": {"ru": "в сборке ffmpeg нет нужного энкодера",
                         "en": "this ffmpeg build has no needed encoder"},
    "http.hls_no_ffmpeg": {"ru": "ffmpeg не установлен - перекладка невозможна "
                                  "(установка - кнопка в окне)",
                           "en": "ffmpeg is not installed - remux is impossible "
                                 "(install is a button in the window)"},
    "http.hls_probe_failed": {"ru": "кодек файла не прочитан",
                              "en": "the file codec was not read"},
    "http.hls_failed": {"ru": "HLS не построен", "en": "HLS was not built"},
    "http.hls_busy": {"ru": "перекладка этого видео уже идёт - подождите",
                      "en": "remux of this video is already running - wait"},
    "http.hls_segment": {"ru": "плейлист или сегмент не найдены в кеше",
                         "en": "playlist or segment not found in the cache"},
    # Страница воспроизведения клиента (S4) и её подписи: отказ называет
    # состояние строки, а не пустой плеер.
    "http.watch_missing": {"ru": "страница воспроизведения не собрана "
                                  "(нет ui_src/watch.html)",
                           "en": "the playback page is not built "
                                 "(ui_src/watch.html is missing)"},
    "watch.back": {"ru": "← Библиотека", "en": "← Library"},
    "watch.title": {"ru": "Воспроизведение", "en": "Playback"},
    "watch.loading": {"ru": "загрузка…", "en": "loading…"},
    "watch.audio": {"ru": "аудио", "en": "audio"},
    "watch.no_track": {"ru": "дорожка не скачана", "en": "track not downloaded"},
    "watch.browser_hls": {"ru": "браузер не играет HLS - нужен hls.js или Safari",
                          "en": "the browser cannot play HLS - hls.js or Safari is needed"},
    "watch.failed": {"ru": "воспроизведение не получилось", "en": "playback failed"},
    "watch.subs_show": {"ru": "Показать субтитры", "en": "Show captions"},
    "watch.subs_hide": {"ru": "Скрыть субтитры", "en": "Hide captions"},
    "watch.captions": {"ru": "Субтитры", "en": "Captions"},
    "watch.bytes": {"ru": "байты файла", "en": "file bytes"},
    "watch.thumb": {"ru": "обложка", "en": "thumbnail"},
    "watch.files_head": {"ru": "Файлы этого видео", "en": "Files of this video"},
    "watch.files_none": {"ru": "файлы не найдены", "en": "no files found"},
    "watch.hash": {"ru": "хеш выдан", "en": "hash granted"},
    "watch.hash_none": {"ru": "хеш не вычислен", "en": "hash not computed"},
    "watch.gone": {"ru": "файл пропал с диска", "en": "file gone from disk"},

    # ---- привязка и экспозиция (SV-C S6, docs/SERVER-CLIENT.md §5, §7) ----
    # Причина отказа называется всегда: «код не принят», «код истёк»,
    # «сервер не показывал код», «перебор затормозен», «адрес не в allowlist».
    "http.pair_denied": {"ru": "код привязки не принят - привязка не завершена",
                         "en": "pairing code not accepted - pairing not completed"},
    "http.pair_used": {"ru": "код привязки уже использован - покажите новый код",
                       "en": "the pairing code was already used - show a new code"},
    "http.pair_expired": {"ru": "код привязки истёк - покажите новый код",
                          "en": "the pairing code expired - show a new code"},
    "http.pair_empty": {"ru": "код привязки не введён",
                        "en": "pairing code not entered"},
    "http.pair_no_code": {"ru": "сервер не показывал код привязки - покажите код в окне",
                          "en": "the server did not show a pairing code - show the code in the window"},
    "http.pair_throttle": {"ru": "слишком много неверных попыток с этого адреса - подождите",
                           "en": "too many wrong attempts from this address - wait"},
    "http.pair_post_only": {"ru": "привязка - только POST",
                            "en": "pairing must use POST"},
    "http.pair_code_local": {"ru": "код привязки показан в окне сервера: по HTTP его значение не отдаётся",
                             "en": "the pairing code is shown in the server window: its value is not served over HTTP"},
    "http.pair_listener_down": {
        "ru": "слушатель не поднят - привязка невозможна (включите доступ по HTTPS)",
        "en": "the listener is not up - pairing is impossible (turn HTTPS access on)"},
    "http.allowlist": {"ru": "адрес клиента не в allowlist - доступ не выдан",
                       "en": "the client address is not in the allowlist - access is not granted"},
    "http.allowlist_required": {
        "ru": "обычный порт (0.0.0.0) требует allowlist: без списка адресов "
              "слушатель не поднимается",
        "en": "a plain port (0.0.0.0) needs an allowlist: without a list of "
              "addresses the listener is not started"},
    "http.expose_consent": {"ru": "согласие «что уходит наружу» принимает человек на сервере: по HTTP не принимается",
                            "en": "consent about what leaves outward is accepted by a person on the server: not accepted over HTTP"},
    "http.tokens_revoked": {"ru": "ключ отозван", "en": "key revoked"},
    "http.tokens_absent": {"ru": "ключ не найден - отзывать нечего",
                           "en": "key not found - nothing to revoke"},

    # ---- диалог согласия и привязка в интерфейсе --------------------------
    "expose.title": {"ru": "Что уходит наружу", "en": "What leaves outward"},
    "expose.text": {
        "ru": "Слушатель за пределами этого компьютера отдаёт: подписи строк "
              "библиотеки, обложки, длительности, размеры, счётчики состояния "
              "(poll) и байты медиа по запросу клиента; журнал обращений "
              "пишется на сервере. Пути дисков наружу не уходят: байты "
              "раздаются по id строки и id файла. Доступ решают скоупы на "
              "ключе, который выдал привязка; перебор кода затормозен; "
              "число ключей ограничено.",
        "en": "A listener outside this computer serves: library row captions, "
              "thumbnails, durations, sizes, state counters (poll) and media "
              "bytes on request; the access journal is written on the server. "
              "Disk paths never leave: bytes are served by row id and file id. "
              "Rights come from scopes on the key that pairing issued; code "
              "brute force is throttled; the number of keys is limited."},
    "expose.check": {"ru": "Согласие «что уходит наружу» принято",
                     "en": "Consent about what leaves outward is accepted"},
    "expose.denied": {"ru": "Согласие не принято - доступ наружу не включается",
                      "en": "Consent not accepted - outward access is not turned on"},
    "pair.title": {"ru": "Привязка к серверу", "en": "Pairing with the server"},
    "pair.code.label": {"ru": "Код привязки (6 цифр или 4 слова)",
                        "en": "Pairing code (6 digits or 4 words)"},
    "pair.code.ph": {"ru": "код из окна сервера…", "en": "the code from the server window…"},
    "pair.begin": {"ru": "Показать код привязки", "en": "Show the pairing code"},
    "pair.submit": {"ru": "Привязать этот браузер", "en": "Pair this browser"},
    "pair.note": {"ru": "Код показывает окно сервера; права выдаёт сервер по "
                       "пресету в настройках; код одноразовый и истёкший код "
                       "не примется",
                  "en": "The code is shown by the server window; the server "
                       "grants rights by the preset in settings; the code is "
                       "single-use and an expired code will not be accepted"},
    "pair.pairing": {"ru": "ключ принят - права:", "en": "key accepted - rights:"},
    "pair.keys": {"ru": "Ключи доступа", "en": "Access keys"},
    "pair.keys_none": {"ru": "ключей не выдано", "en": "no keys issued"},
    "pair.revoke": {"ru": "отозвать", "en": "revoke"},
    "pair.store_none": {"ru": "реестр ключей не открыт - привязка невозможна",
                        "en": "the key store is not open - pairing is impossible"},
    "tokens.store_none": {"ru": "реестр ключей не открыт - отзывать нечего",
                          "en": "the key store is not open - nothing to revoke"},
    # Состояние доступа по HTTPS (виджет карточки): поднят ли слушатель, куда,
    # принято ли согласие, активен ли код и сколько было неверных попыток.
    "pair.running": {"ru": "слушатель поднят", "en": "listener is up"},
    "pair.down": {"ru": "слушатель не поднят", "en": "listener is not up"},
    "pair.mode": {"ru": "режим:", "en": "mode:"},
    "pair.address": {"ru": "слушает:", "en": "listens on:"},
    "pair.consent_on": {"ru": "согласие принято", "en": "consent accepted"},
    "pair.consent_off": {"ru": "согласия нет", "en": "no consent"},
    "pair.attempts": {"ru": "неверных попыток привязки:",
                      "en": "wrong pairing attempts:"},
    "pair.code.active": {"ru": "код активен", "en": "code is active"},
    "pair.code.none": {"ru": "код не показан", "en": "no code shown"},
    "pair.code.expires": {"ru": "истекает через", "en": "expires in"},
    "pair.code.words": {"ru": "4 слова", "en": "4 words"},
    "pair.code.digits": {"ru": "6 цифр", "en": "6 digits"},
    "pair.allow_none": {"ru": "allowlist пуст", "en": "allowlist is empty"},
    # Виджет состояния доступа: подписи «прочитать» и «ещё не прочитано» -
    # молчания и выдуманных «ноль ключей» нет.
    "serve.refresh": {"ru": "Прочитать состояние доступа",
                      "en": "Read the access state"},
    "serve.pending": {"ru": "состояние доступа не прочитано - нажмите «Прочитать состояние»",
                      "en": "the access state was not read - press Read the access state"},
    "http.no_server": {"ru": "страница открыта не сервером - привязка невозможна",
                       "en": "the page was not opened by the server - pairing is impossible"},
    # Кнопки диалога согласия и плеера привязки.
    "expose.accept": {"ru": "Принять согласие", "en": "Accept consent"},
    "expose.decline": {"ru": "Не принимать", "en": "Do not accept"},
    "expose.dpi": {
        "ru": "Порт в зоне перехвата DPI (1024-65535, а также 80/443): трафик "
              "может замедляться или искажаться. Держите порт вне зоны "
              "(рекомендуем 444-1023, по умолчанию 1001); выключать "
              "DPI-утилиту не нужно.",
        "en": "A port inside the DPI interception zone (1024-65535, plus "
              "80/443) can be slowed or mangled. Keep the port outside that "
              "zone (444-1023 recommended, 1001 by default); turning the DPI "
              "tool off is not needed."},

    # ---- права ключа клиента (SV-C S5, docs/SERVER-CLIENT.md §4, §8) ------
    # Права клиент читает у сервера (GET /whoami). Чего не выдано - называется
    # подписью, а не серой кнопкой и не молчанием; имена скоупов не переводим:
    # их говорит сервер в 403 и они те же в реестре токенов.
    "access.granted": {"ru": "права ключа:", "en": "key rights:"},
    "access.missing": {"ru": "не выдано:", "en": "not granted:"},
    "access.unknown": {"ru": "права ключа не прочитаны",
                       "en": "key rights were not read"},
    "access.none": {"ru": "на ключе нет ни одного скоупа - доступ не выдан",
                    "en": "the key has no scopes - access is not granted"},
    "access.native": {"ru": "часть целей доступна только на сервере: диалог "
                            "папки, проводник, окно площадки, вход в аккаунт",
                      "en": "some targets are available on the server only: "
                            "the folder picker, the explorer, the site window, "
                            "the account sign-in"},

    # ---- «где я» (DESIGN-UI §1 №25) ---------------------------------------
    # На широком экране активная вкладка читается цветом + underline + фоном;
    # на узком строка вкладок может не помещаться, поэтому добавлен второй
    # индикатор - текстовая строка «Где я».
    "ui.where": {"ru": "Где я:", "en": "Where am I:"},
    "ui.where.pool": {"ru": "весь пул", "en": "whole pool"},
    "ui.where.platform": {"ru": "площадка", "en": "site"},

    # ---- M13: повтор упавших «другой дорогой» -----------------------------
    "queue.retry.label": {"ru": "Повторить упавшие", "en": "Retry failed"},
    "queue.chain.label": {"ru": "Попробовать другую дорогу",
                          "en": "Try another road"},
    "queue.chain.tip": {
        "ru": "Для упавших строк: забыть дорогу, которая выиграла, и прогнать "
              "цепочку дорог выкачки заново. Сеть пользователя не меняется - "
              "обход DPI не трогается.",
        "en": "For failed rows: forget the road that won and walk the download "
              "road chain again. The user's network is not changed - the DPI "
              "bypass is not touched."},
    "queue.chain.on": {"ru": "другая дорога прогоняется заново",
                       "en": "another road is walked again"},
    "queue.chain.off": {"ru": "прежная выигравшая дорога площадки",
                        "en": "the site road that won stays"},

    # ---- локальные предпочтения клиента (§8) ------------------------------
    # Тема, шрифт и язык применяются к этому клиенту и лежат в localStorage:
    # окно сервера от них не меняется.
    "prefs.local.note": {
        "ru": "вид этого клиента: значение сохраняется у вас и не меняет окно "
              "сервера",
        "en": "this client's view: the value is saved for you and does not "
              "change the server window"},
    "prefs.local.denied": {
        "ru": "localStorage недоступен - настройка клиента не сохранится",
        "en": "localStorage is unavailable - the client setting will not be "
              "saved"},

    # ---- поле пути вместо диалога папки (§8) ------------------------------
    # Клиент не имеет проводника сервера: путь пишется текстом, а доступность
    # проверяет сервер (storage_check и storage_set_path отвечают причиной).
    "storage.path.label": {"ru": "Путь папки на сервере",
                           "en": "Folder path on the server"},
    "storage.path.apply": {"ru": "Применить путь", "en": "Apply path"},
    "storage.add.label": {"ru": "+ Добавить папку", "en": "+ Add folder"},
    "storage.check.label": {"ru": "Проверить доступность",
                            "en": "Check availability"},
    "storage.path.tip": {
        "ru": "диалог папки доступен только на сервере: путь пишется здесь, "
              "доступность проверяет сервер",
        "en": "the folder picker is available on the server only: the path is "
              "typed here, availability is checked by the server"},
    "storage.path.none": {"ru": "путь не указан - сервер ничего не проверяет",
                          "en": "path not given - the server checks nothing"},
    "storage.client.note": {
        "ru": "хранилища правит сервер: путь и доступность читает он, проводник "
              "этого клиента отсутствует",
        "en": "storages are owned by the server: it reads the path and "
              "availability, this client has no explorer"},
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

