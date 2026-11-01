"""Дороги выкачки: устойчивость к отказам САМОЙ площадки (M13).

Что здесь решается и что перенесено из Synfronia (поведение и имена, не код
чужой архитектуры):

  * конечный детерминированный список вариантов и перебор с ограничением —
    в Synfronia это кнопка «Проверить все» (dpi.py:1346-1423 `scan()`:
    прогоняет все стратегии установки, меряет цели, победителя не гасит) и
    список вариантов (dpi.py:1148-1152 `strategies()`);
  * стабильный порядок перебора — Synfronia сортирует стратегии натурально
    (dpi.py:1143-1145 `_natkey`, ALT2 раньше ALT10); здесь порядок задан
    вручную конечным кортежем `ROUTES` и не зависит от содержимого диска;
  * итог каждой дороги пишется СРАЗУ, обрыв прогона не стирает уже
    проверенное (dpi.py:1365-1378 `_tick`);
  * сводка «сколько вышло / сколько не вышло» (dpi.py:1235-1243 `tally`);
  * кэш победителя по контексту и его возраст (dpi.py:1159-1205
    `_cache_key`/`read_cache`/`write_cache`, 1212 `TESTS_TTL`) — здесь ключ
    площадка: дороги у площадок свои, общий кэш на все площадки врал бы по
    той же причине;
  * сначала повторяем сохранённую выигравшую дорогу, полный перебор — после
    неё (dpi.py:1576-1584 `auto()`: кэш -> `scan()`);
  * «маршрут уже открыт — ничего не добавляем» (dpi.py:1560-1563) — здесь
    первая дорога = обычный запрос, остальные включаются только по
    фактическому отказу площадки;
  * кандидаты формата base + HLS-вариант с логом попытки (Synfronia
    downloader.py:793-803 `_format_candidates`, 837-851) — дорога `format_hls`;
  * кодировщик GPU -> CPU при неудаче (downloader.py:253-258) — дорога
    `quality_down` (селектор ниже по высоте);
  * источник с зеркалом и конечным числом повторов (downloader.py:109-172
    `FFMPEG_SOURCES`/`FFMPEG_ATTEMPTS`) — дорога `mirror` (другой домен
    площадки из реестра);
  * проверка остановки МЕЖДУ попытками (downloader.py:838-844) — `stop`
    проверяется перед каждой дорогой;
  * `finish` возвращает снятое состояние обратно (dpi.py:1525-1541) — временные
    файлы прогона убираются в `cleanup`.

Этот модуль НЕ обходит DPI и НЕ меняет сеть пользователя: никаких прокси,
никаких winws/zapret, никакой правки маршрутизации. Дороги — про сам запрос к
площадке: куки, имитация TLS-отпечатка, зеркало домена, селектор формата,
токен подтверждения человека. В журнале и подсказках площадки называются
нейтрально («площадка»), без имени.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import google_auth, platforms, settings as settings_mod, settings_schema
from .paths import bypass_cache_path, platform_tokens_path
from .util import clean_text, now_iso

# Страховка: сколько дорог прогоняем за один запрос (настройка
# bypass_max_attempts). Конечное число — чтобы отказ не уводил в бесконечный
# повтор (той же страховкой держатся MAX_CHUNKS в sources и FFMPEG_ATTEMPTS
# в Synfronia, downloader.py:113).
MAX_ATTEMPTS = 4

# Возраст выигравшей дороги (перенос TESTS_TTL из Synfronia, dpi.py:1212):
# площадка и анти-бот меняются, поэтому кэш не вечный.
CACHE_TTL = 24 * 3600

# Срок токена подтверждения: PO-токен и visitor-info протухают — честнее
# просить обновить, чем молча повторять мёртвым токеном.
TOKEN_TTL = 30 * 24 * 3600

# Cookie-имена, которые площадка ставит после подтверждения человека
# (возраст/согласие/visitor). Имена — механика площадки, в UI не показываются.
CONFIRM_COOKIES = {
    "youtube": ("VISITOR_INFO1_LIVE", "SOCS", "SOCSAY", "POT"),
    "pornhub": ("kwk_age_verified", "hit_notice", "age_verified"),
    "vk": (),
}

# Формат PO-токена для yt-dlp: "CLIENT.CONTEXT+TOKEN"
# (yt_dlp/extractor/youtube/_video.py:2714-2742 `_get_config_po_token`).
PO_CLIENT = "web"
PO_CONTEXT = "gvs"

# Порядок имитации TLS-отпечатка для дороги, когда настройка стоит «Выкл»
# (дорога включает её по отказу, выбор пользователя не перебиваем).
IMPERSONATE_ORDER = ("chrome", "firefox", "edge")


def _flag(settings: dict, key: str, default: bool = True) -> bool:
    """Флаг настройки тем же правилом, что карточка (строки «нет»/«выкл»)."""
    return settings_schema._flag((settings or {}).get(key), default)


def attempts_limit(settings: dict) -> int:
    """Сколько дорог прогоняем: 1, если цепочка выключена."""
    if not _flag(settings, "bypass_enabled", True):
        return 1
    spec = settings_schema.field("bypass_max_attempts")
    if spec is None:
        return MAX_ATTEMPTS
    return int(settings_schema.coerce("bypass_max_attempts",
                                      (settings or {}).get("bypass_max_attempts")))


# --------------------------------------------------------------------- #
#  Симптомы отказа (общий разбор причины - sources.classify_failure)
# --------------------------------------------------------------------- #

_BLOCK_RE = re.compile(r"http error (?:40[1-9]|410|429)|forbidden|gone|"
                       r"permission|access denied|blocked", re.IGNORECASE)
_BOT_RE = re.compile(r"not a bot|bot check|sign in|confirm your age|"
                     r"age[- ]?restrict|captcha|verification required",
                     re.IGNORECASE)
_NAME_RE = re.compile(r"invalid argument|errno 22|filename too long|"
                      r"unable to open for writing", re.IGNORECASE)
_FORMAT_RE = re.compile(r"requested format|no formats|format is not|"
                        r"m3u8|merge output|unable to download video",
                        re.IGNORECASE)


def _looks_blocked(text: str) -> bool:
    return bool(_BLOCK_RE.search(text or ""))


def _looks_bot(text: str, kind: str | None) -> bool:
    return kind == "bot" or bool(_BOT_RE.search(text or ""))


def _looks_name(text: str) -> bool:
    return bool(_NAME_RE.search(text or ""))


def _looks_format(text: str) -> bool:
    return bool(_FORMAT_RE.search(text or ""))


def _is_file_case(ctx: dict) -> bool:
    return ctx.get("purpose") == "file"


# --------------------------------------------------------------------- #
#  Токены подтверждения человека (профиль, DPAPI)
# --------------------------------------------------------------------- #

def capability(platform_id) -> str | None:
    """Что площадка требует для «я человек»: "po" (visitor/PO-токен),
    "age" (подтверждение возраста) или None (не требует). Поле реестра."""
    return platforms.token_capability(platform_id)


def token_path() -> Path:
    """Файл токенов подтверждения в профиле (зашифрован DPAPI)."""
    return platform_tokens_path()


def _token_store() -> dict:
    """Прочитать файл токенов; битый файл или чужая учётка -> пусто
    (вызывающий видит «токена нет» и идёт обычным путём, не молча)."""
    path = token_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(google_auth.decrypt(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(store: dict) -> dict:
    try:
        token_path().write_bytes(google_auth.encrypt(
            json.dumps(store, ensure_ascii=False).encode("utf-8")))
    except OSError as exc:
        return {"ok": False, "error": f"файл токенов не записан: {clean_text(exc)}"}
    return {"ok": True}


def save_token(platform_id, *, cookies: list[str] | None = None,
               po_token: str | None = None, visitor: str | None = None) -> dict:
    """Сохранить подтверждение в профиле (аддитивно к существующей записи).

    cookies - строки Netscape (только подтверждающие cookie площадки),
    po_token - строка PO-токена, visitor - visitor-info. Шифрование то же,
    что у куки аккаунта и ключа участника (google_auth.encrypt): без учётки
    Windows содержимое бесполезно.
    """
    platform_id = str(platform_id or "")
    if not platform_id:
        return {"ok": False, "error": "площадка не названа"}
    store = _token_store()
    record = dict(store.get(platform_id) or {})
    if cookies:
        record["cookies"] = [str(line) for line in cookies]
    if po_token:
        record["po_token"] = str(po_token)
    if visitor:
        record["visitor_data"] = str(visitor)
    if not record:
        return {"ok": False, "error": "нечего сохранять"}
    record["capability"] = capability(platform_id)
    record["saved_at"] = now_iso()
    store[platform_id] = record
    written = _write_store(store)
    if not written.get("ok"):
        return written
    return {"ok": True, "platform": platform_id, "saved_at": record["saved_at"]}


def forget_token(platform_id) -> dict:
    """Убрать подтверждение площадки из профиля (куки аккаунта не трогаются)."""
    store = _token_store()
    platform_id = str(platform_id or "")
    if platform_id not in store:
        return {"ok": True, "removed": False}
    store.pop(platform_id, None)
    written = _write_store(store)
    if not written.get("ok"):
        return written
    return {"ok": True, "removed": True}


def token_record(platform_id) -> dict | None:
    """Запись токена площадки, если она есть и не протухла."""
    record = _token_store().get(str(platform_id or ""))
    if not isinstance(record, dict):
        return None
    stamp = _epoch(record.get("saved_at"))
    if stamp and time.time() - stamp > TOKEN_TTL:
        return None
    return record


def _epoch(iso) -> float | None:
    """ISO-строка -> epoch; None, если разбирать нечего (протухание не гадаем)."""
    try:
        return datetime.fromisoformat(str(iso or "")).timestamp()
    except ValueError:
        return None


def capture_token(platform_id, source) -> dict:
    """Собрать подтверждение из куки-файла пользователя (cookies.txt).

    source - путь к файлу в формате Netscape или сам текст. Берём ТОЛЬКО
    подтверждающие cookie площадки (возраст/согласие/visitor) и visitor-info:
    прочие куки в профиль не кладём.
    """
    text = str(source or "")
    if "\t" not in text and "\n" not in text:
        try:
            text = Path(os.path.expandvars(os.path.expanduser(text))).read_text(
                encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "error": f"файл кук не прочитан: {clean_text(exc)}"}
    names = CONFIRM_COOKIES.get(str(platform_id or ""), ())
    kept = _confirm_cookie_lines(text.splitlines(), names)
    visitor = _visitor_from_cookies(kept)
    if not kept and not visitor:
        return {"ok": False, "error": "в файле кук нет подтверждения площадки"}
    return save_token(platform_id, cookies=kept, visitor=visitor)


def _confirm_cookie_lines(lines: list[str], names: tuple) -> list[str]:
    """Строки Netscape только с подтверждающими именами (комментарии мимо).

    Формат Netscape: домен, флаг, путь, защищённость, срок, Имя, Значение -
    имя cookie в 5-м поле, значение в 6-м (yt-dlp читает его тем же
    порядком полей).
    """
    lowered = {str(name).lower() for name in names}
    out: list[str] = []
    for line in lines:
        if line.strip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5].lower() in lowered:
            out.append(line.rstrip())
    return out


def _visitor_from_cookies(lines: list[str]) -> str | None:
    """VISITOR_INFO1_LIVE - это и есть visitor-info YouTube."""
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] == "VISITOR_INFO1_LIVE":
            return parts[6] or None
    return None


def cookiefile_from_record(record: dict, *, platform: str | None = None) -> str | None:
    """Временный cookies.txt из сохранённых подтверждающих куки площадки.

    Файл живёт ровно одну дорогу: `cleanup(ctx)` убирает его после прогона.
    Подтверждающие имена фильтруются снова - запись в профиле может быть
    старше, чем список имён.
    """
    names = CONFIRM_COOKIES.get(str(platform or record.get("platform") or ""), ())
    lines = _confirm_cookie_lines(
        [str(line) for line in (record.get("cookies") or [])], names)
    if not lines:
        return None
    try:
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             suffix=".txt", delete=False)
    except OSError:
        return None
    try:
        handle.write("# Netscape HTTP Cookie File\n")
        for line in lines:
            handle.write(line + "\n")
    finally:
        handle.close()
    return handle.name


def po_overlay(token: str) -> dict | None:
    """extractor_args yt-dlp с PO-токеном: "CLIENT.CONTEXT+TOKEN".

    Формат читает сам yt-dlp (_video.py:2714-2742); строку с «+» уважаем как
    есть, слишком короткий токен - None (дорога не про мусор).
    """
    value = str(token or "").strip()
    if not value:
        return None
    if "+" not in value:
        value = f"{PO_CLIENT}.{PO_CONTEXT}+{value}"
    if len(value.split("+", 1)[1]) < 8:
        return None
    return {"extractor_args": {"youtube": {"po_token": [value]}}}


def visitor_overlay(visitor: str) -> dict | None:
    """extractor_args с visitor-info (yt_dlp/extractor/youtube/_base.py:925)."""
    value = str(visitor or "").strip()
    if not value:
        return None
    return {"extractor_args": {"youtube": {"visitor_data": [value]}}}


def merge_overlay(opts: dict, overlay: dict) -> dict:
    """Слить overlay в опции: extractor_args площадки не затираем.

    yt-dlp ждёт в extractor_args списки по ключам, поэтому значения
    объединяются (токен и visitor - один и тот же обработчик).
    """
    merged = dict(opts)
    for key, value in overlay.items():
        if key != "extractor_args":
            merged[key] = value
            continue
        args = dict(merged.get("extractor_args") or {})
        for ie_key, ie_value in (value or {}).items():
            ie = dict(args.get(ie_key) or {})
            for name, items in (ie_value or {}).items():
                ie[name] = list(ie.get(name) or []) + list(items)
            args[ie_key] = ie
        merged["extractor_args"] = args
    return merged


# --------------------------------------------------------------------- #
#  Зеркало площадки (домены реестра; id и домены не переименовываются)
# --------------------------------------------------------------------- #

def _host(url: str) -> str:
    return (urlparse(str(url or "")).netloc or "").lower().split(":")[0]


def mirror_hosts(ctx: dict) -> list[str]:
    """Домены площадки, кроме того, что уже стоит в ссылке.

    Зеркало имеет смысл только когда ссылка действительно на домен площадки:
    чужий URL (например тестовый example.com) переписывать нельзя - это был бы
    запрос совсем к другому сайту.
    """
    item = platforms.by_id(ctx.get("platform"))
    if not item:
        return []
    host = _host(ctx.get("url") or "")
    domains = item.get("domains") or ()
    if not any(host == domain or host.endswith("." + domain) for domain in domains):
        return []
    return [domain for domain in domains
            if host != domain and not host.endswith("." + domain)]


def swap_host(url: str, domain: str) -> str:
    """Сменить домен ссылки, сохранив поддомен (www./rt./мобильный) и порт."""
    parsed = urlparse(str(url or ""))
    netloc = parsed.netloc or ""
    host = netloc.lower().split(":")[0]
    port = netloc.split(":", 1)[1] if ":" in netloc else ""
    prefix = ""
    for known in _known_domains():
        if host == known:
            break
        if host.endswith("." + known):
            prefix = host[: -len(known)]
            break
    new = prefix + str(domain)
    return urlunparse(parsed._replace(netloc=f"{new}:{port}" if port else new))


def _known_domains() -> tuple:
    """Все домены реестра (для выделения поддомена ссылки)."""
    seen: list[str] = []
    for item in platforms.PLATFORMS:
        for domain in item.get("domains") or ():
            if domain not in seen:
                seen.append(domain)
    return tuple(seen)


# --------------------------------------------------------------------- #
#  Дороги: предикат применимости + наложение опций
# --------------------------------------------------------------------- #

def _plain(ctx, kind, text) -> dict:
    """Первая дорога = обычный запрос (куки и имитация из настроек)."""
    return {}


def _cookies_browser(ctx, kind, text) -> dict | None:
    """Следующий браузер рабочего порядка; повтор с теми же куками - None.

    Брауер выталкивается только когда оверлей действительно returned: раньше
    name.pop(0) шёл до проверки «куки уже подключены» / «этот браузер уже
    в opts», и из рабочего порядка терялся браузер, который мог пригодиться
    на следующей дороге.
    """
    browsers = ctx.get("browsers") or []
    if not browsers:
        return None
    name = browsers[0]
    if ctx.get("has_cookies") or ctx["opts"].get("cookiesfrombrowser") == (name,):
        return None                       # повтор бессмыслен - порядок не трогаем
    browsers.pop(0)
    ctx["browser_used"] = name
    return {"cookiesfrombrowser": (name,)}


def _cookies_file(ctx, kind, text) -> dict | None:
    path = str(ctx.get("cookies_file") or "").strip()
    if not path or ctx["opts"].get("cookiefile"):
        return None                       # куки уже подключены - повтор бессмыслен
    expanded = os.path.expandvars(os.path.expanduser(path))
    ctx["cookies_file_used"] = expanded
    return {"cookiefile": expanded}


def _impersonate(ctx, kind, text) -> dict | None:
    """Имитация TLS-отпечатка: следующая доступная цель (или первая)."""
    probe = ctx.get("target_of")
    if not probe:
        return None
    current = ctx["opts"].get("impersonate")
    current_name = str(getattr(current, "client", "") or "")
    for name in IMPERSONATE_ORDER:
        target = probe({"impersonate": name})
        if target is None:
            continue
        if current is not None and (current == target or current_name == name):
            continue
        ctx["impersonate_used"] = name
        return {"impersonate": target}
    return None


def _mirror(ctx, kind, text) -> dict | None:
    """Зеркало площадки: следующий домен реестра (чужой URL не трогаем)."""
    hosts = mirror_hosts(ctx)
    if not hosts:
        return None
    domain = hosts[0]
    ctx["url"] = swap_host(ctx["url"], domain)
    ctx["mirror_domain"] = domain
    return {}


def _names_ascii(ctx, kind, text) -> dict | None:
    """Повтор с ascii-именами: только если они ещё не включены."""
    if ctx["opts"].get("restrictfilenames"):
        return None
    return {"restrictfilenames": True}


def _bot_allowed(ctx, kind, text) -> bool:
    """Бот-дороги: настройка включена, отказ - «нужен человек», площадка
    умеет принимать подтверждение."""
    if not _flag(ctx.get("settings") or {}, "bot_token_enabled", True):
        return False
    if not _looks_bot(text, kind):
        return False
    return capability(ctx.get("platform")) in ("po", "age")


def _applies_bot(ctx, kind, text) -> bool:
    return _bot_allowed(ctx, kind, text)


def _visitor(ctx, kind, text) -> dict | None:
    if not _bot_allowed(ctx, kind, text):
        return None
    record = token_record(ctx.get("platform")) or {}
    overlay = visitor_overlay(record.get("visitor_data"))
    if overlay is None:
        return None
    if (ctx["opts"].get("extractor_args") or {}).get("youtube", {}).get("visitor_data"):
        return None          # повтор с тем же visitor-info бессмыслен
    return overlay


def _po_token(ctx, kind, text) -> dict | None:
    if not _bot_allowed(ctx, kind, text):
        return None
    platform = str(ctx.get("platform") or "")
    record = token_record(platform) or {}
    token = record.get("po_token") or str(
        (ctx.get("settings") or {}).get("bot_token") or "").strip()
    overlay = po_overlay(token)
    if overlay is None:
        return None
    if (ctx["opts"].get("extractor_args") or {}).get("youtube", {}).get("po_token"):
        return None          # повтор с тем же токеном бессмыслен
    if not record.get("po_token"):
        # Токен вписан в настройку - сохраняем его в профиль (encrypted),
        # чтобы следующая выкачка шла с ним без участия человека. Открытая
        # копия в settings.json после этого не нужна: без неё токен не
        # лежит в файле и не попадает в снимок окна (см. settings.SECRET_KEYS).
        save_token(platform, po_token=token)
        settings_mod.clear_secret("bot_token")
    return overlay


def _confirm_cookie(ctx, kind, text) -> dict | None:
    if not _bot_allowed(ctx, kind, text):
        return None
    if ctx["opts"].get("cookiefile"):
        return None          # куки уже подключены (аккаунт или файл юзера)
    platform = str(ctx.get("platform") or "")
    record = token_record(platform) or {}
    path = cookiefile_from_record(record, platform=platform)
    if not path:
        return None
    ctx["temp"].append(path)
    ctx["confirm_used"] = True
    return {"cookiefile": path}


def _format_hls(ctx, kind, text) -> dict | None:
    """HLS-вариант селектора формата (перенос _format_candidates, Synfronia
    downloader.py:793-803)."""
    limit = ctx.get("height_limit")
    if ctx.get("hls_used"):
        return None
    ctx["hls_used"] = True
    if limit:
        return {"format": f"bv*[height<={limit}][protocol^=m3u8]+ba/"
                          f"bv*[height<={limit}]+ba/b[height<={limit}]"}
    return {"format": "bv*[protocol^=m3u8]+ba/bv*+ba/b"}


def _quality_down(ctx, kind, text) -> dict | None:
    """Селектор ниже по высоте: запрошенного качества на площадке нет."""
    ladder = ctx.get("ladder") or []
    if not ladder:
        return None
    limit = ladder.pop(0)
    ctx["quality_used"] = limit
    if ctx.get("no_ffmpeg"):
        return {"format": f"b[height<={limit}]/b"}
    return {"format": f"bv*[height<={limit}]+ba/b[height<={limit}]"}


def _applies_any(ctx, kind, text) -> bool:
    return True


def _applies_blocked(ctx, kind, text) -> bool:
    """Куки/имитация/зеркало: 403/410/429 ИЛИ бот-требование (бот - тоже
    отказ площадки, лечится куками браузера)."""
    return _looks_blocked(text) or _looks_bot(text, kind)


def _applies_name(ctx, kind, text) -> bool:
    return _is_file_case(ctx) and _looks_name(text)


def _applies_format(ctx, kind, text) -> bool:
    return _is_file_case(ctx) and _looks_format(text)


def _applies_mirror(ctx, kind, text) -> bool:
    return _applies_blocked(ctx, kind, text) and not ctx.get("mirror_domain")


ROUTES: tuple[dict, ...] = (
    {"id": "plain", "label": "обычный запрос",
     "why": "куки и имитация из настроек, селектор качества по умолчанию",
     "applies": _applies_any, "overlay": _plain},
    {"id": "cookies_browser", "label": "куки из браузера",
     "why": "площадка отдаёт страницу только «своему» браузеру (403/410)",
     "applies": _applies_blocked, "overlay": _cookies_browser,
     "note": lambda ctx: ctx.get("browser_used")},
    {"id": "cookies_file", "label": "куки из файла",
     "why": "экспортированный cookies.txt читается там, где браузерные куки "
            "зашифрованы",
     "applies": _applies_blocked, "overlay": _cookies_file},
    {"id": "impersonate", "label": "имитация браузера (TLS-отпечаток)",
     "why": "анти-бот смотрит на отпечаток TLS: с имитацией площадка отвечает "
            "как браузеру",
     "applies": _applies_blocked, "overlay": _impersonate,
     "note": lambda ctx: ctx.get("impersonate_used")},
    {"id": "mirror", "label": "зеркало площадки",
     "why": "тот же контент на другом домене реестра",
     "applies": _applies_mirror, "overlay": _mirror},
    {"id": "visitor", "label": "visitor-info из профиля",
     "why": "площадка ждёт подтверждение анонимной сессии («не бот»)",
     "applies": _applies_bot, "overlay": _visitor},
    {"id": "po_token", "label": "токен подтверждения (PO)",
     "why": "площадка ждёт токен содержимого вместо «подтвердите, что вы не бот»",
     "applies": _applies_bot, "overlay": _po_token},
    {"id": "confirm_cookie", "label": "куки подтверждения из профиля",
     "why": "подтверждение возраста/согласия, снятое с площадки ранее",
     "applies": _applies_bot, "overlay": _confirm_cookie},
    {"id": "names_ascii", "label": "имена файлов только ascii",
     "why": "площадка дала имя, которое Windows не принимает",
     "applies": _applies_name, "overlay": _names_ascii},
    {"id": "format_hls", "label": "HLS-вариант формата",
     "why": "селектор с HLS-протоколом вместо общего (потоки m3u8)",
     "applies": _applies_format, "overlay": _format_hls},
    {"id": "quality_down", "label": "качество ниже",
     "why": "запрошенного качества на площадке нет - берём следующий уровень",
     "applies": _applies_format, "overlay": _quality_down,
     "note": lambda ctx: ctx.get("quality_used")},
)

ROUTE_IDS = {route["id"]: route for route in ROUTES}


# --------------------------------------------------------------------- #
#  Кэш выигравшей дороги (по площадке, с возрастом)
# --------------------------------------------------------------------- #

# Кэш читается потоком очереди (победа дороги) и потоком окна («попробовать
# другую дорогу»). Без блокировки это read-modify-write на одном файле:
# один поток прочитал, другой записал, первый пишет результат своей read -
# чужая площадка теряется. Заодно запись атомарная (tmp + replace): файл не
# может быть пойман посреди записи.
_CACHE_LOCK = threading.Lock()


def _read_cache() -> dict:
    try:
        data = json.loads(bypass_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict) -> bool:
    path = bypass_cache_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()               # профиль забит: мусор не оставляем
        except OSError:
            pass
        return False                   # кэш не критичен - загрузка не зависит от него


def read_winning(platform_id) -> str | None:
    """Дорога, которая последней выкачала это у этой площадки (или None)."""
    with _CACHE_LOCK:
        data = _read_cache()
    entry = data.get(str(platform_id or ""))
    if not isinstance(entry, dict):
        return None
    stamp = float(entry.get("checked") or 0)
    if stamp and time.time() - stamp > CACHE_TTL:
        return None
    route = str(entry.get("route") or "")
    return route if route in ROUTE_IDS else None


def remember_winning(platform_id, route_id: str) -> None:
    """Запомнить выигравшую дорогу площадки («обычный запрос» не кэшируется)."""
    if route_id == "plain":
        return
    with _CACHE_LOCK:
        data = _read_cache()
        data[str(platform_id or "")] = {"route": str(route_id),
                                        "checked": int(time.time())}
        _write_cache(data)


def forget_winning(platform_id) -> dict:
    """Забыть выигравшую дорогу — «попробовать другую дорогу» (attempt_chain)."""
    platform_id = str(platform_id or "")
    with _CACHE_LOCK:
        data = _read_cache()
        existed = platform_id in data
        data.pop(platform_id, None)
        if not _write_cache(data):
            return {"ok": False, "cleared": False}
    return {"ok": True, "cleared": existed}


# --------------------------------------------------------------------- #
#  Контекст прогона и перебор дорог
# --------------------------------------------------------------------- #

def make_context(settings: dict, base_opts: dict, url: str, *,
                 platform: str, purpose: str = "file", has_cookies: bool = False,
                 browsers: list[str] | None = None, cookies_file: str = "",
                 target_of=None, height_limit: int | None = None,
                 ladder: list[int] | None = None, no_ffmpeg: bool = False,
                 attempt_chain: bool = False) -> dict:
    """Словарь прогона: опции, ссылка и то, чем можно пробовать."""
    return {
        "settings": settings or {},
        "opts": dict(base_opts or {}),
        "url": str(url or ""),
        "platform": str(platform or ""),
        "purpose": purpose,
        "has_cookies": bool(has_cookies),
        "browsers": list(browsers or []),
        "cookies_file": str(cookies_file or ""),
        "target_of": target_of,
        "height_limit": height_limit,
        "ladder": list(ladder or []),
        "no_ffmpeg": bool(no_ffmpeg),
        "attempt_chain": bool(attempt_chain),
        "temp": [],
        "journal": [],
    }


def browser_order(settings: dict) -> list[str]:
    """Браузеры для куки-дороги (битые ABE - мимо). Порядок конечный."""
    choices = getattr(google_auth, "BROWSER_CHOICES", ()) or ()
    broken = getattr(google_auth, "BROKEN_BROWSERS", frozenset()) or frozenset()
    chosen = str((settings or {}).get("browser_cookies") or "none").strip().lower()
    if chosen in choices and chosen != "none":
        order = [chosen]
    else:
        order = [name for name in ("firefox", "chrome", "brave") if name in choices]
    return [name for name in order if name not in broken]


def run_chain(attempt, ctx: dict, *, classify, humanize=None, log=None,
              stop=None, retryable=None) -> dict:
    """Прогнать дороги по порядку: первый успех или последняя честная причина.

    `attempt(opts, url)` - один запрос к площадке (бросает исключение при
    отказе); `classify(text)` - причина (sources.classify_failure),
    `humanize(exc, kind)` - подсказка человеческим языком по последней причине;
    `retryable(exc)` - «этим стоит пробовать другие дороги» (обычно
    DownloadError): прочий исключение не превращаем в отказ площадки.

    Итог каждой дороги пишется сразу (перенос Synfronia dpi.py:1365-1378),
    поэтому обрыв прогона не стирает уже проверенное. Возвращает
    {"ok", "info", "error", "kind", "routes", "tally", "winner", "cancelled",
    "hint"}: kind сохраняется из последней настоящей причины отказа.
    """
    limit = attempts_limit(ctx.get("settings") or {})
    enabled = limit > 1
    routes = list(ROUTES)
    kind: str | None = None
    text = ""
    info = None
    winner = None
    last_exc: Exception | None = None
    hints: list[str] = []
    attempted = 0
    cache_checked = False

    index = 0
    while index < len(routes) and attempted < limit:
        if stop is not None and stop.is_set():
            break                                  # остановка между дорогами
        route = routes[index]
        index += 1
        if not route["applies"](ctx, kind, text):
            continue
        overlay = route["overlay"](ctx, kind, text)
        if overlay is None:
            continue                               # пробовать нечем - дорога мимо
        ctx["opts"] = merge_overlay(ctx["opts"], overlay)
        attempted += 1
        label = route["label"]
        note = route.get("note")
        extra = note(ctx) if note else None
        if extra:
            label = f"{label}: {extra}"
        try:
            info = attempt(ctx["opts"], ctx["url"])
        except Exception as exc:  # noqa: BLE001 - отказ площадки = итог дороги
            if stop is not None and stop.is_set():
                raise                              # пользователь остановил не дорогу
            if retryable is not None and not retryable(exc):
                raise                              # не отказ площадки - без повторов
            text = clean_text(str(exc))
            kind = classify(text) if classify else "network"
            last_exc = exc
            ctx["journal"].append({"route": route["id"], "label": label,
                                   "ok": False, "kind": kind, "detail": text[:200]})
            if log:
                log(f"дорога [{attempted}/{limit}] «{label}» — не вышло "
                    f"({kind}): {text[:200]}")
            if enabled and not cache_checked:
                # Перенос dpi.py:1576-1584: сохранённая выигравшая дорога
                # пробуется следующей, а полный перебор - после неё.
                cache_checked = True
                cached = (None if ctx.get("attempt_chain")
                          else read_winning(ctx.get("platform")))
                if cached and cached != route["id"] and cached in ROUTE_IDS:
                    position = next((i for i, item in enumerate(routes)
                                     if item["id"] == cached), None)
                    if position is not None and position > index:
                        if log:
                            log(f"дорога «{ROUTE_IDS[cached]['label']}» уже "
                                f"выигрывала для этой площадки — пробуем раньше")
                        routes.insert(index, routes.pop(position))
            continue
        ctx["journal"].append({"route": route["id"], "label": label,
                               "ok": True, "kind": None, "detail": ""})
        if log:
            log(f"дорога [{attempted}/{limit}] «{label}» — вышло")
        winner = route["id"]
        remember_winning(ctx.get("platform"), route["id"])
        break

    if not enabled:
        hints.append("цепочка дорог выключена: настройка «Дороги выкачки» "
                     "в разделе «Загрузка»")
    elif info is None and attempted >= limit:
        hints.append(f"дороги выкачки кончились ({attempted} из {limit}): "
                     "повтор с другой дорогой — «Повторить упавшие»")
    tally = {"ok": 1 if info is not None else 0,
             "fail": sum(1 for entry in ctx["journal"] if not entry.get("ok")),
             "of": attempted}
    if log and attempted > 1:
        # Сводка прогона (перенос log "test done: full/partial/none из
        # Synfronia dpi.py:1401-1403): итог виден целиком, а не по строкам дорог.
        log(summary_line(tally))
    error = None
    if info is None:
        error = (humanize(last_exc, kind) if (humanize and last_exc is not None)
                 else (text[:300] or "площадка не ответила"))
        if hints:
            error = f"{error} ({'; '.join(hints)})"
    return {
        "ok": info is not None, "info": info, "error": error, "kind": kind,
        "routes": list(ctx["journal"]),
        "tally": tally,
        "winner": winner,
        "cancelled": stop is not None and stop.is_set(),
        "hint": "; ".join(hints),
    }


def cleanup(ctx: dict) -> None:
    """Убрать временные файлы прогона (куки подтверждения).

    Перенос идеи Synfronia `finish` (dpi.py:1525-1541): снятое возвращено
    обратно, временное не остаётся в профиле.
    """
    for path in ctx.get("temp") or []:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def summary_line(tally: dict) -> str:
    """Сводка прогона (перенос `tally` из Synfronia dpi.py:1235-1243)."""
    out = tally or {}
    return (f"дороги выкачки: вышло {int(out.get('ok') or 0)}, "
            f"не вышло {int(out.get('fail') or 0)} из {int(out.get('of') or 0)}")
