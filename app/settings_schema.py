"""Схема настроек: ключи, типы, границы и подписи для карточки настроек.

Что значит каждый ключ - здесь, а не в GUI: окно получает эту схему и
рисует карточку из неё, поэтому добавление настройки не требует правки
разметки. Проверка типов при чтении (`coerce`) - из того же источника,
значит настройки в файле не могут разойтись с тем, чему доверяет код.
"""

from __future__ import annotations

# Типы полей, которые понимает карточка:
#   str    - текстовое поле
#   bool   - флажок
#   int    - число, границы min/max
#   choice - выпадающий список: choices = [[значение, подпись], ...]
#   path   - текст + кнопка «выбрать папку»
#   roots  - список корней библиотеки (путь / рекурсия / включён)
#   action - кнопка действия (поле transient: в settings.json не пишется)
FIELDS: tuple[dict, ...] = (
    # ---- библиотека ----
    {
        "key": "_storages",
        "type": "storages",
        "section": "Библиотека",
        "label": "Хранилища",
        "hint": "Папки, которые индексируются и в которые можно качать. "
                "Каждое можно отключить, отвязать (забыть) или указать "
                "путь заново, если диск переименовали.",
        # transient: поле-виджет, а не настройка - в settings.json не пишется.
        "transient": True,
        "default": [],
    },
    {
        "key": "default_storage_id",
        "type": "str",
        "section": "",
        "label": "",
        "hint": "Хранилище, предвыбранное при загрузке (задаётся в списке хранилищ).",
        "hidden": True,
        "default": "",
    },
    {
        "key": "default_sync_mode",
        "type": "choice",
        "section": "Библиотека",
        "label": "Режим синхронизации по умолчанию",
        "hint": "Что делать с видео, которых ещё нет в библиотеке: "
                "«Полная» - качать сразу, «Частичная» - только метаданные, "
                "выбор контента ручной, «Ручная» - обновлять лишь по кнопке.",
        "choices": [["partial", "Частичная"], ["full", "Полная"],
                    ["manual", "Ручная"]],
        "default": "partial",
    },
    {
        "key": "sync_depth",
        "type": "choice",
        "section": "Библиотека",
        "label": "Глубина синка",
        "hint": "«Полная» (по умолчанию) - по запросу на каждое видео: "
                "лайки, комменты, дата публикации, описание, обложка; "
                "медленнее и ближе к анти-боту. «Быстрая» - только то, что "
                "есть в списке (длительность, просмотры, обложка), одним "
                "запросом на чанк - для больших библиотек.",
        "choices": [["full", "Полная (лайки, комменты, даты)"],
                    ["fast", "Быстрая (только список)"]],
        "default": "full",
    },
    {
        "key": "keep_sidecar",
        "type": "bool",
        "section": "Библиотека",
        "label": "Сохранять post.json рядом с файлом",
        "hint": "Sidecar с полными метаданными и хешем: библиотеку можно "
                "переиндексировать после переезда или переименования папок.",
        # Служебный флаг режима «файлы рядом»: в карточке его заменил
        # выбор «Метаданные рядом с видео», но старые settings.json с
        # explicit False уважаются и по сей день.
        "hidden": True,
        "default": True,
    },
    {
        "key": "sidecar_mode",
        "type": "choice",
        "section": "Библиотека",
        "label": "Метаданные рядом с видео",
        "hint": "«Всё в видео»: обложка и метаданные (название, автор, дата, "
                "описание) вшиваются в mp4, а сведения всех видео папки "
                "лежат в одном .omnistash.json - на каждое видео один файл. "
                "«Файлы рядом»: как раньше - post.json и обложка отдельными "
                "файлами. Оба формата читаются сканом, при переключении "
                "метаданные не теряются. Встраивание требует ffmpeg - без "
                "него обложка остаётся файлом, очередь предупредит.",
        "choices": [["embed", "Всё в видео (+ .omnistash.json на папку)"],
                    ["files", "Файлы рядом с видео (post.json, обложка)"]],
        "default": "embed",
    },
    {
        "key": "save_thumb",
        "type": "bool",
        "section": "Библиотека",
        "label": "Сохранять обложку рядом с файлом",
        "hint": "URL обложек площадок протухают со временем - локальная "
                "копия остаётся вместе с библиотекой.",
        "default": True,
    },
    {
        "key": "compute_hash",
        "type": "bool",
        "section": "Библиотека",
        "label": "Считать хеш при скане",
        "hint": "Нужен, чтобы находить перемещённые файлы и дубли. "
                "Выключите на большой библиотеке - скан станет быстрее, "
                "но опознавание перемещений отключится.",
        "default": True,
    },
    {
        "key": "view_mode",
        "type": "choice",
        "section": "Библиотека",
        "label": "Вид библиотеки",
        "hint": "Список - строки; плитка - обложки. Превью берутся только "
                "из локальных файлов и только когда попадают на экран.",
        "choices": [["list", "Список"], ["grid", "Плитка с превью"]],
        "default": "list",
    },
    {
        "key": "tile_size",
        "type": "choice",
        "section": "Библиотека",
        "label": "Размер плитки",
        "hint": "Действует в режиме «Плитка»: сколько колонок поместится, "
                "решает ширина окна.",
        "choices": [["small", "Мелкая"], ["medium", "Средняя"],
                    ["large", "Крупная"]],
        "default": "medium",
    },
    {
        "key": "col_views",
        "type": "bool",
        "section": "Библиотека",
        "label": "Колонка «Просмотры»",
        "hint": "Видимость колонок списка; лишние можно выключить здесь.",
        "default": True,
    },
    {
        "key": "col_likes",
        "type": "bool",
        "section": "Библиотека",
        "label": "Колонка «Лайки»",
        "hint": "Счётчик площадки на момент последнего синка.",
        "default": True,
    },
    {
        "key": "col_comments",
        "type": "bool",
        "section": "Библиотека",
        "label": "Колонка «Комменты»",
        "hint": "Счётчик площадки на момент последнего синка.",
        "default": True,
    },
    {
        "key": "col_sync",
        "type": "bool",
        "section": "Библиотека",
        "label": "Колонка «Синк»",
        "hint": "Когда данные этой строки в последний раз обновлялись "
                "с площадки (отдельно от даты скачивания файла).",
        "default": True,
    },
    {
        "key": "_refresh_meta",
        "type": "action",
        "section": "Библиотека",
        "label": "Метаданные с площадки",
        "hint": "Подтянуть лайки, комменты, даты и обложки по уже "
                "известным видео - без перекачки файлов. Прогресс и "
                "остановка - в списке библиотеки, рядом с «Целостность».",
        "action": "refresh-meta",
        "action_label": "Обновить метаданные…",
        # transient: поле-кнопка, а не настройка - в settings.json не пишется.
        "transient": True,
        "default": [],
    },
    {
        "key": "sort_col",
        "type": "str",
        "section": "",
        "label": "",
        "hint": "Последняя колонка сортировки списка (задаётся кликом).",
        "hidden": True,
        "default": "",
    },
    {
        "key": "sort_dir",
        "type": "str",
        "section": "",
        "label": "",
        "hint": "Направление сортировки списка (asc/desc).",
        "hidden": True,
        "default": "desc",
    },
    # ---- загрузка ----
    # Куда качать - не настройка: путь берётся из выбранного хранилища
    # (панель выделения / настройка канала / глобальный выбор).
    {
        "key": "output_template",
        "type": "str",
        "section": "Загрузка",
        "label": "Шаблон имени и пути",
        "hint": "Синтаксис yt-dlp (%(title)s, %(id)s, ...). Часть "
                "[%(id)s] в имени обязательна: по ID файлы потом "
                "узнаются в индексе.",
        "default": "%(channel)s/%(upload_date)s - %(title)s [%(id)s].%(ext)s",
    },
    {
        "key": "quality",
        "type": "choice",
        "section": "Загрузка",
        "label": "Качество",
        "hint": "«Исходное» берёт максимально доступное, «Высокое» "
                "ограничивает размер без заметной потери.",
        "choices": [["best", "Исходное"], ["high", "Высокое"],
                    ["mid", "Среднее"], ["low", "Низкое"]],
        "default": "high",
    },
    {
        "key": "subtitles",
        "type": "choice",
        "section": "Загрузка",
        "label": "Субтитры",
        "choices": [["none", "Не качать"], ["ru", "Русские"],
                    ["en", "Английские"], ["all", "Все"]],
        "default": "none",
    },
    {
        "key": "transcode",
        "type": "choice",
        "section": "Загрузка",
        "label": "Перекодировка",
        "hint": "Перекодировать скачанное в HEVC (H.265): место меньше. "
                "Какие кодировщики реально есть - спрашивается у ffmpeg; "
                "недоступные в вашей сборке помечены в списке. Требует "
                "ffmpeg (см. строку ниже).",
        "choices": [["none", "Не перекодировать"],
                    ["libx265", "HEVC (x265, программный)"],
                    ["nvenc", "HEVC NVIDIA NVENC"],
                    ["amf", "HEVC AMD AMF"],
                    ["qsv", "HEVC Intel QSV"]],
        "default": "none",
    },
    {
        "key": "_ffmpeg",
        "type": "action",
        "section": "Загрузка",
        "label": "FFmpeg",
        "hint": "Склейка видео+аудио, метаданные, субтитры в файл и "
                "перекодировка требуют ffmpeg. Мы его НЕ вшиваем (GPL): "
                "ставите сами кнопкой - сборка gyan.dev, встаёт в "
                "%LOCALAPPDATA%\\Omnistash\\bin, удаляется удалением папки.",
        "action": "ffmpeg-install",
        "action_label": "Скачать ffmpeg",
        # transient: поле-кнопка, а не настройка - в settings.json не пишется.
        "transient": True,
        "default": [],
    },
    # ---- аккаунт Google ----
    # Глобальной галки больше нет: куки применяются только там, где
    # аккаунт явно привязан к источнику (playlists.account_id).
    {
        "key": "browser_cookies",
        "type": "choice",
        "section": "Аккаунт Google",
        "label": "Куки из браузера (без копий)",
        "hint": "yt-dlp читает куки вашего браузера напрямую - копий у нас "
                "не появляется. Firefox работает; Chrome/Edge v127+ шифруют "
                "куки (App-Bound Encryption) и прочитать их нельзя - для "
                "них используйте импорт cookies.txt. Привязанный аккаунт "
                "имеет приоритет над этим режимом.",
        "choices": [["none", "Не использовать"],
                    ["firefox", "Firefox"],
                    ["edge", "Edge (не работает в v127+)"],
                    ["chrome", "Chrome (не работает в v127+)"],
                    ["brave", "Brave (не работает в v127+)"]],
        "default": "none",
    },
    {
        "key": "_google_account",
        "type": "account",
        "section": "Аккаунт Google",
        "label": "Вход в Google",
        "hint": "Вход в окне приложения (куки снимаются сами) или импорт "
                "cookies.txt из браузерного расширения. Куки вашего браузера "
                "напрямую мы читать не будем.",
        # transient: виджет состояния, а не настройка.
        "transient": True,
        "default": [],
    },
    # Метки аккаунта (email/способ входа и дата) - не секреты, но живут в
    # схеме как скрытые поля: set_value молча отбрасывает неизвестные ключи.
    {
        "key": "google_account_label",
        "type": "str",
        "section": "",
        "label": "",
        "hint": "Подпись аккаунта в статусе.",
        "hidden": True,
        "default": "",
    },
    {
        "key": "google_account_since",
        "type": "str",
        "section": "",
        "label": "",
        "hint": "Когда куки были сохранены (ISO).",
        "hidden": True,
        "default": "",
    },
    {
        "key": "delay_ms",
        "type": "int",
        "section": "Загрузка",
        "label": "Пауза между запросами, мс",
        "hint": "Меньше - быстрее, но площадка может начать резать поток.",
        "min": 0, "max": 60000,
        "default": 500,
    },
    {
        "key": "retries",
        "type": "int",
        "section": "Загрузка",
        "label": "Повторов при сбое",
        "min": 0, "max": 20,
        "default": 3,
    },
    {
        "key": "resume_queue",
        "type": "bool",
        "section": "Загрузка",
        "label": "Продолжать очередь при запуске",
        "hint": "Открыт окно - и строки, которые стояли в очереди, "
                "поехали дальше сами. Выключите, если хотите запускать "
                "загрузку только вручную.",
        "default": True,
    },
    # ---- mesh ----
    {
        "key": "mesh_enabled",
        "type": "bool",
        "section": "Mesh",
        "label": "Помогать участникам (и получать от них)",
        "hint": "Если площадка не отдаёт видео (удалено, приватное, "
                "«подтвердите, что вы не бот»), программа сможет запросить "
                "файл у других участников. Наружу уходят: ваш адрес (когда "
                "сервер поднят) и id запрашиваемых видео; соединение "
                "шифруется (TLS, аттестация ключа участника). Метаданные "
                "НИКОГДА не берутся от участников - только файлы, и те "
                "проверяются: sha256, структура (длительность против "
                "вашей строки), стоп-лист «несовпадений». По умолчанию "
                "выключено.",
        "default": False,
    },
    {
        "key": "mesh_peers",
        "type": "str",
        "section": "Mesh",
        "label": "Адреса участников",
        "hint": "Через запятую: ip:port (порт mesh-сервера участника). "
                "Сюда адреса друзей и соседей по сети; DHT ищет участников "
                "и без этого списка (если включено участие).",
        "default": "",
    },
    {
        "key": "mesh_port",
        "type": "int",
        "section": "Mesh",
        "label": "Порт своего mesh-сервера",
        "hint": "Сервер поднимается при включённом участии и отдаёт только "
                "проверенные файлы (с известным sha256). Порт фиксированный, "
                "чтобы адрес стабилен между перезапусками; при занятости "
                "сервер не поднимется (журнал скажет). ВАЖНО для "
                "совместимости: DPI-утилиты (zapret и подобные) по умолчанию "
                "перехватывают порты 1024-65535 (а также 80/443) - наш трафик "
                "им не мешает и они не мешают нам ЛУЧШЕ всего, когда порт "
                "ВНЕ этой зоны: рекомендуется 444-1023 (по умолчанию 1000). "
                "Выключать DPI-программу не нужно.",
        "min": 100, "max": 65535,
        "default": 1000,
    },
    {
        "key": "mesh_upnp",
        "type": "bool",
        "section": "Mesh",
        "label": "Проброс порта через роутер (UPnP)",
        "hint": "Роутер откроет ваш порт наружу, чтобы участникам из "
                "интернета было куда подключаться (запрашиваются пробросы "
                "TCP и UDP - это меняет настройки роутера). Внешний адрес "
                "берётся у самого роутера, сторонним сервисам ничего не "
                "уходит. Не вышло или выключено - участникам доступна "
                "только ваша сеть (LAN), в журнале будет причина.",
        "default": True,
    },
    {
        "key": "mesh_encrypt",
        "type": "bool",
        "section": "Mesh",
        "label": "Шифровать перенос под свой ключ",
        "hint": "Канал и так шифрован (TLS с проверкой ключа участника); "
                "эта опция шифрует и сам файл блоками по 128 КБ под ваш "
                "ключ (Ed25519 -> X25519): прочитать байты по пути сможет "
                "только обладатель вашего ключа - защита на случай "
                "компрометации канала после проверки и основа для "
                "будущего ретранслятора. Дозапись при обрыве начнётся "
                "заново. По умолчанию выключено.",
        "default": False,
    },
    {
        "key": "mesh_mdns",
        "type": "bool",
        "section": "Mesh",
        "label": "Искать участников в сети (mDNS)",
        "hint": "Стандартное объявление в локальной сети (как принтеры): "
                "участники одной сети видят друг друга без ручных адресов. "
                "Требует библиотеки zeroconf (устанавливается отдельно: "
                "pip install zeroconf - LGPL, в exe не вшивается); без "
                "неё обнаружение работает старыми способами (приветствие, "
                "DHT, ручные адреса).",
        "default": True,
    },
    {
        "key": "mesh_nick",
        "type": "str",
        "section": "Mesh",
        "label": "Ваш никнейм",
        "hint": "Объявляется в DHT-каталоге имён: любой участник, знающий "
                "ник, найдёт ваш адрес и ключ (в записи НЕТ списка ваших "
                "видео - только ник, адрес, ключ). Регистр не важен, "
                "2-32 символа: буквы, цифры, пробел, дефис, подчёркивание. "
                "Менять можно часто - старое имя живёт до суток (TTL). "
                "Пусто = не объявляться.",
        "default": "",
    },
    {
        "key": "mesh_max_mb",
        "type": "int",
        "section": "Mesh",
        "label": "Лимит размера файла, МиБ",
        "hint": "Сколько максимум отдавать и принимать через mesh одним "
                "файлом (по умолчанию 10): тестовые переносы не тянут "
                "гигабайты, отказ виден в журнале - «файл больше лимита». "
                "0 - без лимита, пойдут и большие файлы. Действует с "
                "обеих сторон независимо.",
        "min": 0, "max": 10240,
        "default": 10,
    },
    {
        "key": "mesh_rate_mbit",
        "type": "int",
        "section": "Mesh",
        "label": "Ограничение скорости, Мбит/с",
        "hint": "Отдача и приём через mesh не быстрее этого (по умолчанию "
                "10): перенос не съедает канал целиком, параллельные задачи "
                "не встают. 0 - без ограничения. Каждая сторона сдерживает "
                "свою часть; итог - медленнее из двух.",
        "min": 0, "max": 10000,
        "default": 10,
    },
    {
        "key": "_peers",
        "type": "action",
        "section": "Mesh",
        "label": "Участники",
        "hint": "Мой никнейм и отпечаток ключа, поиск участников по нику "
                "(каталог в DHT) и проверка адреса.",
        "action": "peer-dialog",
        "action_label": "Профиль и поиск участников…",
        # transient: поле-кнопка, а не настройка - в settings.json не пишется.
        "transient": True,
        "default": [],
    },
    # ---- расписание ----
    {
        "key": "scan_interval_min",
        "type": "int",
        "section": "Расписание",
        "label": "Автоскан каждые, мин",
        "hint": "Переиндексация хранилищ. 0 - выключено. Работает, пока "
                "открыто окно; для запуска без окна есть "
                "omnistash.py --sync под планировщик Windows.",
        "min": 0, "max": 1440,
        "default": 0,
    },
    {
        "key": "sync_interval_min",
        "type": "int",
        "section": "Расписание",
        "label": "Автосинк каждые, мин",
        "hint": "Переснапшот источников и применение diff. 0 - выключено. "
                "Новые видео в очередь попадут только в режиме «Полная».",
        "min": 0, "max": 1440,
        "default": 0,
    },
    # ---- внешний вид ----
    {
        "key": "theme",
        "type": "choice",
        "section": "Внешний вид",
        "label": "Тема",
        "choices": [["dark", "Тёмная"], ["light", "Светлая"]],
        "default": "dark",
    },
    # ---- служебные ----
    {
        "key": "app_version",
        "type": "str",
        "section": "",
        "label": "",
        "hint": "Версия, записавшая настройки (не редактируется).",
        "hidden": True,
        "default": "",
    },
)

_BY_KEY = {f["key"]: f for f in FIELDS}


def field(key: str) -> dict | None:
    """Описание поля по ключу (None - ключа нет в схеме: он будет отброшен при записи)."""
    return _BY_KEY.get(key)


def defaults() -> dict:
    """Все значения по умолчанию из схемы (без transient-полей-виджетов)."""
    return {f["key"]: _copy(f["default"]) for f in FIELDS
            if not f.get("transient")}


def schema() -> list[dict]:
    """Схема для карточки настроек: только видимые поля, как есть."""
    return [dict(f) for f in FIELDS if not f.get("hidden")]


def _copy(value):
    """Копия списка/словаря, чтобы вызывающий не делил мутабельный дефолт."""
    if isinstance(value, list):
        return [dict(v) if isinstance(v, dict) else v for v in value]
    if isinstance(value, dict):
        return dict(value)
    return value


def _flag(value, default: bool = True) -> bool:
    """Значение чекбокса в корне: принимаем и строки в духе «нет»/«off».

    bool("нет") дал бы True - а в настройках, пришедших из старых форматов
    или прописанных руками, это ровно та ловушка, которая включает
    выключенный корень.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("false", "0", "no", "off", "нет", "выкл", ""):
            return False
        if low in ("true", "1", "yes", "on", "да", "вкл"):
            return True
        return default
    if value is None:
        return default
    return bool(value)


def _coerce_roots(value, default: list) -> list:
    """Список корней: принимаем и старый формат (просто путь строкой)."""
    if not isinstance(value, list):
        return _copy(default)
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().strip('"')
        if not path or path in seen:
            continue
        seen.add(path)
        out.append({
            "path": path,
            "recursive": _flag(item.get("recursive"), True),
            "enabled": _flag(item.get("enabled"), True),
        })
    return out


def coerce(key: str, value):
    """Привести значение к типу поля; невалидное -> дефолт.

    Никогда не бросает исключений: битый settings.json не должен мешать
    запуску - лучше молча вернуть дефолт, чем падать на старте.
    """
    spec = _BY_KEY.get(key)
    if spec is None:
        return value
    kind = spec["type"]
    default = spec.get("default")

    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        return bool(default)

    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                return default
        low = spec.get("min")
        high = spec.get("max")
        if low is not None and number < low:
            number = low
        if high is not None and number > high:
            number = high
        return number

    if kind == "choice":
        allowed = [c[0] for c in spec.get("choices", [])]
        return value if value in allowed else default

    if kind == "roots":
        return _coerce_roots(value, default or [])

    # str / path: строка как строка, прочее -> дефолт
    if isinstance(value, str):
        return value if kind == "str" or value.strip() else (default if not value else value)
    return _copy(default) if isinstance(default, list) else default
