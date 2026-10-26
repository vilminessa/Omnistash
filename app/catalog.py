"""Каталог площадок: просмотр в духе оригинальной страницы сайта.

Только чтение и метаданные: ни скачивания, ни записей в БД. Добавление
видео идёт обычным флоу (gui.add_start), здесь лишь карточки со ссылками.

Правила модуля:
  * YouTube - yt-dlp в плоском режиме (extract_flat): один запрос на
    страницу карточек, без download;
  * PornHub - карточки читаем с самой страницы площадки (HTML): yt-dlp
    разбирает только видео PH, списки страниц ему не знакомы; разбор
    ограничивается границами video-wrapper и не выдумывает записей;
  * VK - каталога нет (списки площадок не разбираются): вкладка остаётся
    «Библиотекой», переключатель не показывается;
  * кэш в профиле (catalog_cache/) с TTL: тренды 10 мин, поиск 5 мин;
    «Обновить» (force) игнорирует кэш;
  * пагинация: youtube - диапазоном записей (playlist_items), PH -
    страницей ?page=N; has_more решается по длине;
  * ссылки канонизированы (PH-зеркала сводим к www.pornhub.com), карточки
    дедуплицируются по video_id;
  * отказ площадки классифицирован по kind: blocked (недоступна из сети),
    empty (площадка ответила, но список пуст), unsupported (не умеем) -
    UI по kind решает, что предлагать (VPN/повтор/mesh).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from urllib.parse import urlparse, urlunparse

import yt_dlp

from . import metadata
from .paths import profile_dir
from .util import QuietLogger, clean_text, iso_date, to_int, to_text

PAGE_SIZE = 30          # карточек на страницу каталога
CACHE_TTL = {"trending": 600, "home": 600, "search": 300, "channels": 600}


class CatalogError(Exception):
    """Каталог не загружен. kind: blocked | empty | unsupported."""

    def __init__(self, message: str, kind: str = "empty") -> None:
        super().__init__(message)
        self.kind = kind


def cache_dir() -> str:
    """Кэш карточек каталога в профиле (не в системных временных)."""
    path = os.path.join(str(profile_dir()), "catalog_cache")
    os.makedirs(path, exist_ok=True)
    return path


def _cache_file(platform: str, mode: str, query: str, page: int,
                scope: str = "") -> str:
    raw = f"{mode}|{query or ''}|{page}|{scope or ''}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return os.path.join(cache_dir(), f"{platform}.{digest}.json")


def _cache_get(platform: str, mode: str, query: str, page: int,
               scope: str = "") -> dict | None:
    try:
        with open(_cache_file(platform, mode, query, page, scope), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if time.time() - float(data.get("_ts") or 0) > CACHE_TTL.get(mode, 600):
        return None
    return data


def _cache_set(platform: str, mode: str, query: str, page: int,
               data: dict, scope: str = "") -> None:
    payload = dict(data)
    payload["_ts"] = time.time()
    try:
        with open(_cache_file(platform, mode, query, page, scope), "w",
                  encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError:
        pass                      # кэш - не барьер для показа карточек


def clear_cache(platform: str | None = None) -> int:
    """Удалить кэш карточек (все площадки или одну)."""
    removed = 0
    prefix = f"{platform}." if platform else ""
    try:
        for name in os.listdir(cache_dir()):
            if not name.endswith(".json"):
                continue
            if prefix and not name.startswith(prefix):
                continue
            try:
                os.remove(os.path.join(cache_dir(), name))
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def _canonical(url: str, platform: str, remote_id: str) -> str:
    """Ссылка карточки: каноническая и разборная нашим classify_url."""
    text = to_text(url) or ""
    if platform == "pornhub":
        # Зеркала (rt./pornhub.org/.net) сводим к www.pornhub.com: тот же
        # viewkey, но единый ключ строки в библиотеке.
        host = urlparse(text).netloc.lower()
        if host.endswith(("pornhub.org", "pornhub.net")):
            text = urlunparse(urlparse(text)._replace(netloc="www.pornhub.com"))
        if not text and remote_id:
            text = metadata.video_url("pornhub", remote_id) or ""
    elif platform == "vk":
        if not text and remote_id:
            text = metadata.video_url("vk", remote_id) or ""
    elif platform == "youtube":
        if not text and remote_id:
            text = metadata.video_url("youtube", remote_id) or ""
    return text


def _thumbnail(raw: dict) -> str | None:
    thumb = to_text(raw.get("thumbnail"))
    if not thumb and isinstance(raw.get("thumbnails"), list):
        for item in raw["thumbnails"]:        # последняя - самая крупная
            if isinstance(item, dict) and to_text(item.get("url")):
                thumb = to_text(item.get("url"))
    return thumb


def _normalize(raw, platform: str) -> dict | None:
    """Запись источника (yt-dlp или карточка страницы) -> общий формат."""
    if not isinstance(raw, dict):
        return None                 # приватное/удалённое: дырка в списке
    remote_id = to_text(raw.get("id"))
    if not remote_id:
        return None
    tags = raw.get("tags")
    people = raw.get("people")
    return {
        "url": _canonical(to_text(raw.get("url")) or to_text(raw.get("webpage_url")),
                          platform, remote_id),
        "video_id": remote_id,
        "platform": platform,
        "title": to_text(raw.get("title")) or remote_id,
        "thumb": _thumbnail(raw),
        "duration_s": to_int(raw.get("duration")),
        "views": to_int(raw.get("view_count")),
        # Дата выхода (RSS канала даёт точную; flat-списки страниц - нет).
        "uploaded_at": iso_date(raw.get("upload_date")
                                or raw.get("release_date")),
        "author": (to_text(raw.get("channel")) or to_text(raw.get("uploader"))
                   or to_text(raw.get("artist"))),
        "author_id": to_text(raw.get("channel_id")) or to_text(raw.get("uploader_id")),
        # Теги площадки со страницы карточек (PH): фильтр каталога по тегу.
        "tags": [str(tag)[:40] for tag in tags][:6]
                if isinstance(tags, list) else [],
        # Исполнители карточки (PH): чипсы фильтра - клик делает поиск по имени.
        "people": [str(person)[:40] for person in people][:4]
                  if isinstance(people, list) else [],
        "added": False,             # обогащается в gui по ключу строки
    }


def _clean(text) -> str:
    """Текст без ANSI-кодов (см. util.clean_text): журнал и диалоги
    должны быть читаемыми, логгер yt-dlp добавляет коды цвета."""
    return clean_text(text)


def _blocked(exc: BaseException) -> bool:
    """Сетевой отказ (блокировка/таймаут) против «площадка ответила пусто»."""
    text = _clean(exc).lower()
    return ("timed out" in text or "connection" in text or "reset" in text
            or " refused" in text or "403" in text or "410" in text
            or "429" in text or "network" in text)


def _supported(url: str) -> bool:
    """Есть ли у yt-dlp экстрактор для этого адреса страницы.

    ignoreerrors=True в плоском режиме молча съедает «Unsupported URL»,
    поэтому страницу без листинга отличаем здесь: иначе каталог площадки,
    для которой yt-dlp не умеет списки, выглядел бы как «пусто».
    """
    try:
        from yt_dlp.extractor import gen_extractors
    except Exception:  # noqa: BLE001
        return True
    for extractor in gen_extractors():
        try:
            if extractor.suitable(url) and extractor.IE_NAME != "generic":
                return True
        except Exception:  # noqa: BLE001 - кривой экстрактор не решает
            continue
    return False


def _probe(url: str, settings: dict | None) -> str:
    """Лёгкая проверка доступности страницы: ok | blocked | error.

    Площадка отвечает 403/410 при блокировке из сети пользователя; в
    плоском режиме с ignoreerrors это выглядит как «пустой список»,
    поэтому статус решаем отдельным коротким запросом.
    """
    import urllib.request
    timeout = max(int((settings or {}).get("socket_timeout") or 0), 10)
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; Omnistash)"})
    try:
        with urllib.request.urlopen(request, timeout=min(timeout, 15)) as got:
            status = got.status
    except Exception as exc:  # noqa: BLE001 - код ответа тоже отказ
        text = _clean(exc)
        if _blocked(exc) or "403" in text or "410" in text or "429" in text:
            return "blocked"
        return "error"
    if status in (403, 410, 429, 503):
        return "blocked"
    return "ok"


def _opts(settings: dict | None) -> dict:
    """Плоский режим без download: карточки одним запросом, без файлов."""
    settings = settings or {}
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,       # битая запись в списке не валит страницу
        "extract_flat": "in_playlist",
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
    }
    try:
        from . import google_auth
        browser_opt = google_auth.browser_cookie_option(settings)
        if browser_opt:
            opts["cookiesfrombrowser"] = browser_opt
    except Exception:  # noqa: BLE001 - куки не должны валить каталог
        pass
    return opts


def _youtube(opts: dict, start: int, end: int, log=None):
    """YoutubeDL с молчаливым логгером и диапазоном записей страницы.

    quiet=True не глушит ERROR логгера: логгер молчит отдельно, иначе
    консоль и журнал забиваются строками «ERROR: [youtube:tab] …».
    """
    opts = dict(opts)
    opts["playlist_items"] = f"{start}-{end}"
    opts["logger"] = QuietLogger(log)     # молчит в консоли, ошибки - в журнал
    return yt_dlp.YoutubeDL(opts)


def _entries(info) -> list:
    if not isinstance(info, dict):
        return []
    entries = info.get("entries")
    if isinstance(entries, list):
        return [item for item in entries if item]
    if info.get("id") and info.get("title"):
        return [info]               # одна карточка вместо списка
    return []


def _extract_candidates(urls: list[str], opts: dict, start: int, end: int,
                        settings: dict | None, log=None) -> tuple[list, bool]:
    """Первый успешный ответ: пробой нескольких адресов одной площадки.

    Страница выдаётся диапазоном записей (playlist_items): YouTube
    догружает continuation сам, поэтому страницы 2..N дают новые id.
    Пустой список различаем честно: unsupported (yt-dlp не умеет такие
    страницы), blocked (403/410 на блокировку из сети пользователя) и
    «пусто на этой странице». Логгер молчит (см. _youtube), отказ -
    только в журнале приложения.
    """
    known = [url for url in urls if _supported(url)]
    if not known:
        raise CatalogError(
            "Каталог этой площадки yt-dlp не разбирает (списки страниц не "
            "поддерживаются) - добавляйте контент ссылкой", kind="unsupported")
    for url in known:
        try:
            with _youtube(opts, start, end, log) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            if _blocked(exc):
                message = _clean(exc)
                if log:
                    log(f"Каталог: {url} -> {message}")
                raise CatalogError(
                    "Площадка не ответила (недоступна из вашей сети - "
                    "поможет VPN/прокси или запрос участникам mesh)",
                    kind="blocked") from exc
            if log:
                log(f"Каталог: {url} -> {_clean(exc)} (пробуем следующий адрес)")
            continue                # не сетевой отказ: пробуем следующий адрес
        except Exception as exc:  # noqa: BLE001 - extractor-глюк не фатален
            if _blocked(exc):
                raise CatalogError("Площадка не ответила", kind="blocked") from exc
            continue
        entries = _entries(info)
        if entries:
            # Полный диапазон = страница ещё есть; меньше запроса = конец.
            return entries, len(entries) >= PAGE_SIZE
    if _probe(known[0], settings) == "blocked":
        raise CatalogError(
            "Площадка недоступна из вашей сети (403/410 на страницу) - "
            "поможет VPN/прокси; если контент есть у участников mesh, его "
            "можно запросить из карточки или по ссылке", kind="blocked")
    return [], False


def _fetch_youtube(mode: str, query: str, page: int,
                   settings: dict | None, log=None,
                   channels: list[dict] | None = None) -> tuple[list, bool]:
    if mode == "channels":
        if not channels:
            raise CatalogError(
                "Нет каналов: добавьте плейлист или канал ссылкой — каталог "
                "«новое от каналов» соберётся по ним", kind="empty")
        return _fetch_youtube_channels(channels, settings, log)
    opts = _opts(settings)
    start = (page - 1) * PAGE_SIZE + 1
    end = page * PAGE_SIZE
    if mode == "channel" and query:
        # Страница загрузок канала: newest-first, диапазоном записей.
        urls = [query if query.startswith("http")
                else f"https://www.youtube.com/channel/{query}/videos"]
    elif mode == "search" and query:
        urls = [f"https://www.youtube.com/results?search_query={query}"]
    else:
        # feed/trending мёртв (редирект на главную) - тренды через
        # страницу результатов по «trending».
        urls = ["https://www.youtube.com/results?search_query=trending",
                "https://www.youtube.com/feed/topics-music"]
    entries, full = _extract_candidates(urls, opts, start, end, settings, log)
    if mode == "channel":
        # Страница загрузок канала конечна: короткая или пустая страница
        # означает конец истории.
        return entries, full
    # Тренды и поиск открыты: YouTube не отдаёт «конец списка» - конец
    # решает UI (пустая или полностью дублированная страница).
    return entries, True


def _http_get(url: str, settings: dict | None) -> str:
    """HTML страницы площадки (только чтение, без download).

    yt-dlp не умеет списки страниц PornHub (он разбирает сами видео),
    поэтому карточки читаем с страницы напрямую: тот же адрес, что видит
    человек в браузере. Сетевой отказ и 403/410 трактуем как блокировку.
    """
    import urllib.request
    timeout = max(int((settings or {}).get("socket_timeout") or 0), 10)
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                      "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=min(timeout, 20)) as got:
            return got.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 - честный отказ в kind
        text = _clean(exc)
        raise CatalogError(
            "Площадка не ответила (недоступна из вашей сети - поможет "
            "VPN/прокси или запрос участникам mesh)",
            kind="blocked" if (_blocked(exc) or "403" in text or "410" in text
                               or "429" in text) else "empty") from exc


_PH_ANCHOR_RE = re.compile(
    r'<a\s[^>]*href="/view_video\.php\?viewkey=([A-Za-z0-9]+)"[^>]*'
    r'title="([^"]{2,200})"', re.S)
_PH_IMG_RE = re.compile(r'<img[^>]*?(?:src|data-src)="([^"]+)"', re.S)
_PH_TAG_RE = re.compile(r'<a\s[^>]*href="/video/tag/([^"?/]+)"', re.S)
_PH_PERSON_RE = re.compile(
    r'<a\s[^>]*href="/(?:pornstar|model|channels|users)/([^"?/]+)"[^>]*>'
    r'([^<]{2,40})<', re.S)


_PH_BLOCK_RE = re.compile(r'class="phimage"')


def _parse_ph_cards(html: str) -> list[dict]:
    """Карточки со страницы PornHub.

    Актуальная разметка площадки (2026): карточка начинается с обёртки
    phimage, внутри - ссылка «href=view_video.php?viewkey=… title=Название»,
    <img> превью, исполнительница/исполнитель (usernameWrap) и теги, если
    страница их отдаёт. Границы карточки берём по обёрткам phimage: окно
    фиксированной длины не ловит блок исполнителя ( он в нескольких
    килобайтах после заголовка из-за отступов разметки).
    Длительность и просмотры в HTML страницы не лежат (их подгружает JS) -
    карточка честно показывается без них. Дубли одной ссылки схлопываются
    по viewkey.
    """
    import html as html_mod
    marks = [match.start() for match in _PH_BLOCK_RE.finditer(html)]
    out: list[dict] = []
    seen: set[str] = set()
    for index, start in enumerate(marks):
        end = marks[index + 1] if index + 1 < len(marks) else len(html)
        segment = html[start:end]
        title = _PH_ANCHOR_RE.search(segment)
        if not title:
            continue
        remote_id = title.group(1)
        if remote_id in seen:
            continue
        seen.add(remote_id)
        thumb = _PH_IMG_RE.search(segment)
        tags: list[str] = []
        for tag in _PH_TAG_RE.finditer(segment):
            slug = to_text(tag.group(1))
            if slug and slug not in tags:
                tags.append(slug)
        people: list[str] = []
        for person in _PH_PERSON_RE.finditer(segment):
            name = to_text(html_mod.unescape(person.group(2)))
            if name and name not in people:
                people.append(name)
        out.append({
            "id": remote_id,
            "title": to_text(html_mod.unescape(title.group(2))) or remote_id,
            "url": "https://www.pornhub.com/view_video.php?viewkey=" + remote_id,
            "duration": None,
            "thumbnail": to_text(thumb.group(1)) if thumb else None,
            "view_count": None,
            "tags": tags[:6],
            "people": people[:4],
        })
    return out


def _fetch_pornhub(mode: str, query: str, page: int,
                   settings: dict | None, log=None,
                   channels: list[dict] | None = None) -> tuple[list, bool]:
    """Карточки страницы PH. Страница площадки - параметром ?page=N.

    Площадка отдаёт ~44 карточки на страницу; has_more решаем по длине
    (не по нашему PAGE_SIZE), иначе «Загрузить ещё» зависало бы на
    первой странице.
    """
    if mode == "tag" and query:
        url = (f"https://www.pornhub.com/video/tag/{query}"
               + (f"?page={page}" if page > 1 else ""))
    elif mode == "search" and query:
        url = (f"https://www.pornhub.com/video/search?search={query}"
               + (f"&page={page}" if page > 1 else ""))
    else:
        url = ("https://www.pornhub.com/video"
               + (f"?page={page}" if page > 1 else ""))
    html = _http_get(url, settings)
    cards = _parse_ph_cards(html)
    if log:
        log(f"Каталог PornHub: {len(cards)} карточек со страницы {url}")
    return cards, len(cards) >= PAGE_SIZE


FETCHERS = {
    "youtube": _fetch_youtube,
    "pornhub": _fetch_pornhub,
}


# ---------------------------- RSS каналов ---------------------------- #
# YouTube отдаёт ленту канала (последние ~15 загрузок) с точной датой
# публикации - единственный дешёвый способ получить «свежее от каналов»
# с сортировкой по дате: flat-список страницы канала дат не несёт.

_RSS_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_RSS_ID_RE = re.compile(r"<yt:videoId>([^<]+)")
_RSS_TITLE_RE = re.compile(r"<title>([^<]+)")
_RSS_PUB_RE = re.compile(r"<published>([^<]+)")
_RSS_THUMB_RE = re.compile(r'<media:thumbnail url="([^"]+)"')
_RSS_VIEWS_RE = re.compile(r'<media:statistics views="([^"]+)"')
_RSS_UPDATED_RE = re.compile(r"<updated>([^<]+)")

CHANNELS_MAX = 12         # каналов вMerge одной страницы каталога


def _rss_feed_url(channel: dict) -> str | None:
    """Адрес ленты канала: по UC-id или по ссылке канала."""
    remote_id = to_text(channel.get("remote_id")) or ""
    if remote_id.startswith("UC"):
        return ("https://www.youtube.com/feeds/videos.xml"
                f"?channel_id={remote_id}")
    url = to_text(channel.get("url")) or ""
    match = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", url)
    if match:
        return ("https://www.youtube.com/feeds/videos.xml"
                f"?channel_id={match.group(1)}")
    return None


def _parse_rss(xml: str, channel_title: str | None) -> list[dict]:
    """Лента канала -> записи карточек (id, title, дата, превью, просмотры)."""
    out: list[dict] = []
    for chunk in _RSS_ENTRY_RE.finditer(xml):
        entry = chunk.group(1)
        video_id = _RSS_ID_RE.search(entry)
        if not video_id:
            continue
        title = _RSS_TITLE_RE.search(entry)
        published = _RSS_PUB_RE.search(entry) or _RSS_UPDATED_RE.search(entry)
        thumb = _RSS_THUMB_RE.search(entry)
        views = _RSS_VIEWS_RE.search(entry)
        out.append({
            "id": video_id.group(1),
            "title": to_text(title.group(1)) if title else video_id.group(1),
            "url": f"https://www.youtube.com/watch?v={video_id.group(1)}",
            "upload_date": published.group(1) if published else None,
            "thumbnail": to_text(thumb.group(1)) if thumb else None,
            "view_count": to_int(views.group(1)) if views else None,
            "channel": channel_title,
        })
    return out


def _fetch_youtube_channels(channels: list[dict], settings: dict | None,
                            log=None) -> tuple[list, bool]:
    """Последние загрузки каналов человека (RSS), сортировка по дате.

    Один GET на канал (дешеве, чем разворачивать страницы каналов),
    датировано - поэтому сортировка «новые» работает честно. Лента
    отдаёт ~15 последних загрузок: глубокая история канала - режим
    «канал» (страница загрузок), где дат нет.
    """
    records: list[dict] = []
    failed = 0
    for channel in channels[:CHANNELS_MAX]:
        feed = _rss_feed_url(channel)
        if not feed:
            continue
        try:
            xml = _http_get(feed, settings)
        except CatalogError as exc:
            if getattr(exc, "kind", "") == "blocked":
                raise
            failed += 1
            continue
        records += _parse_rss(xml, to_text(channel.get("title")))
    if log:
        log(f"Каталог каналов: {len(records)} записей с "
            f"{min(len(channels), CHANNELS_MAX)} каналов"
            + (f", {failed} недоступно" if failed else ""))
    # Новые сверху; без даты - в конец (не выдумываем порядок).
    records.sort(key=lambda item: str(item.get("upload_date") or ""),
                 reverse=True)
    return records, False


def _args(platform: str, mode: str, query: str, page) -> tuple[str, str, str, int]:
    """Нормализация запроса каталога (один порядок для fetch и peek)."""
    platform = str(platform or "").lower()
    mode = str(mode or "trending").lower()
    if mode == "home":
        mode = "trending"
    query = str(query or "").strip()
    page = max(to_int(page, 1) or 1, 1)
    if mode == "search" and not query:
        mode = "trending"
    return platform, mode, query, page


def peek(platform: str, mode: str = "trending", query: str = "",
         page: int = 1, channels: list[dict] | None = None) -> dict | None:
    """Кэш карточек без обращения к сети.

    Повторное открытие вкладки отдаётся мгновенно (и без воркера): кэш
    свежий - сетевой запрос не нужен вообще.
    """
    platform, mode, query, page = _args(platform, mode, query, page)
    cached = _cache_get(platform, mode, query, page, _scope(channels))
    if not cached:
        return None
    cached.pop("_ts", None)
    cached["source"] = "cache"
    return cached


def _scope(channels: list[dict] | None) -> str:
    """Кэш-ключ режима «каналы»: список каналов (их id) часть запроса."""
    if not channels:
        return ""
    return ",".join(str(item.get("remote_id") or item.get("url") or "")
                    for item in channels[:CHANNELS_MAX])


def fetch(platform: str, mode: str = "trending", query: str = "", page: int = 1,
          settings: dict | None = None, force: bool = False, log=None,
          channels: list[dict] | None = None) -> dict:
    """Страница карточек площадки: метаданные без скачивания."""
    platform, mode, query, page = _args(platform, mode, query, page)
    scope = _scope(channels)
    if not force:
        cached = _cache_get(platform, mode, query, page, scope)
        if cached:
            cached.pop("_ts", None)
            cached["source"] = "cache"
            return cached
    fetcher = FETCHERS.get(platform)
    if not fetcher:
        raise CatalogError("Каталог этой площадки не поддерживается",
                           kind="unsupported")
    raw, has_more = fetcher(mode, query, page, settings, log,
                            channels=channels)
    # Дедуп по video_id: разные адреса одной площадки дают одни и те же
    # записи, а «Загрузить ещё» не должен плодить копии.
    items: list = []
    seen: set[str] = set()
    for entry in raw:
        item = _normalize(entry, platform)
        if not item:
            continue
        if item["video_id"] in seen:
            continue
        seen.add(item["video_id"])
        items.append(item)
    if not items:
        if page > 1:
            # Пустая (или дублированная) следующая страница = конец списка,
            # а не ошибка: интерфейс должен остановиться, не показывать
            # «повторить» на пустом месте.
            result = {"ok": True, "items": [], "has_more": False,
                      "source": "site", "platform": platform, "mode": mode,
                      "query": query, "page": page, "end": True}
            _cache_set(platform, mode, query, page, result, scope)
            return result
        raise CatalogError(
            "Площадка ответила, но карточек на этой странице нет "
            "(попробуйте поиск или «Обновить»)", kind="empty")
    result = {"ok": True, "items": items, "has_more": has_more,
              "source": "site", "platform": platform, "mode": mode,
              "query": query, "page": page}
    _cache_set(platform, mode, query, page, result, scope)
    return result
