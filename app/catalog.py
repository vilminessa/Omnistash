"""Каталог площадок: просмотр в духе оригинальной страницы сайта.

Только чтение и метаданные: ни скачивания, ни записей в БД. Добавление
видео идёт обычным флоу (gui.add_start), здесь лишь карточки со ссылками.

Правила модуля:
  * источник - yt-dlp в плоском режиме (extract_flat): один запрос на
    страницу карточек, без download; ручной парсинг HTML не делаем;
  * кэш в профиле (catalog_cache/) с TTL: тренды 10 мин, поиск 5 мин;
    «Обновить» (force) игнорирует кэш;
  * пагинация диапазоном записей (playlist_items): страница N - это
    записи [(N-1)*PAGE+1 .. N*PAGE], has_more решается по длине;
  * ссылки канонизированы (PH-зеркала сводим к www.pornhub.com), карточки
    дедуплицируются по video_id;
  * отказ площадки классифицирован по kind: blocked (недоступна из сети), empty
    (площадка ответила, но список пуст), unsupported (не умеем) - UI по
    kind решает, что предлагать (VPN/повтор/mesh).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from urllib.parse import urlparse, urlunparse

import yt_dlp

from . import metadata
from .paths import profile_dir
from .util import to_int, to_text

PAGE_SIZE = 30          # карточек на страницу каталога
CACHE_TTL = {"trending": 600, "home": 600, "search": 300}


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


def _cache_file(platform: str, mode: str, query: str, page: int) -> str:
    raw = f"{mode}|{query or ''}|{page}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return os.path.join(cache_dir(), f"{platform}.{digest}.json")


def _cache_get(platform: str, mode: str, query: str, page: int) -> dict | None:
    try:
        with open(_cache_file(platform, mode, query, page), "r",
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
               data: dict) -> None:
    payload = dict(data)
    payload["_ts"] = time.time()
    try:
        with open(_cache_file(platform, mode, query, page), "w",
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
    """Запись yt-dlp -> карточка каталога (общий формат для всех площадок)."""
    if not isinstance(raw, dict):
        return None                 # приватное/удалённое: дырка в списке
    remote_id = to_text(raw.get("id"))
    if not remote_id:
        return None
    return {
        "url": _canonical(to_text(raw.get("url")) or to_text(raw.get("webpage_url")),
                          platform, remote_id),
        "video_id": remote_id,
        "platform": platform,
        "title": to_text(raw.get("title")) or remote_id,
        "thumb": _thumbnail(raw),
        "duration_s": to_int(raw.get("duration")),
        "views": to_int(raw.get("view_count")),
        "author": (to_text(raw.get("channel")) or to_text(raw.get("uploader"))
                   or to_text(raw.get("artist"))),
        "author_id": to_text(raw.get("channel_id")) or to_text(raw.get("uploader_id")),
        "added": False,             # обогащается в gui по ключу строки
    }


def _blocked(exc: BaseException) -> bool:
    """Сетевой отказ (блокировка/таймаут) против «площадка ответила пусто»."""
    text = str(exc).lower()
    return ("timed out" in text or "connection" in text or "reset" in text
            or " refused" in text or "403" in text or "410" in text
            or "network" in text)


def _opts(settings: dict | None, start: int, end: int) -> dict:
    """Плоский режим без download: карточки одним запросом, диапазон N..M."""
    settings = settings or {}
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,       # битая запись в списке не валит страницу
        "extract_flat": "in_playlist",
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
        "playlist_items": f"{start}-{end}",
    }
    try:
        from . import google_auth
        browser_opt = google_auth.browser_cookie_option(settings)
        if browser_opt:
            opts["cookiesfrombrowser"] = browser_opt
    except Exception:  # noqa: BLE001 - куки не должны валить каталог
        pass
    return opts


def _entries(info) -> list:
    if not isinstance(info, dict):
        return []
    entries = info.get("entries")
    if isinstance(entries, list):
        return [item for item in entries if item]
    if info.get("id") and info.get("title"):
        return [info]               # одна карточка вместо списка
    return []


def _extract_candidates(urls: list[str], opts: dict, log=None) -> list:
    """Первый успешный ответ: пробой нескольких адресов одной площадки.

    feed/trending в плоском режиме отдаёт пусто чаще, чем страница
    результатов - поэтому тренды пробуем и через результаты по «trending».
    """
    for url in urls:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            if _blocked(exc):
                raise CatalogError(
                    "Площадка не ответила (недоступна из вашей сети - "
                    "поможет VPN/прокси или запрос участникам mesh)",
                    kind="blocked") from exc
            continue                # не сетевой отказ: пробуем следующий адрес
        except Exception as exc:  # noqa: BLE001 - extractor-глюк не фатален
            if _blocked(exc):
                raise CatalogError("Площадка не ответила", kind="blocked") from exc
            continue
        entries = _entries(info)
        if entries:
            return entries
    return []


def _fetch_youtube(mode: str, query: str, start: int, end: int,
                   settings: dict | None, log=None) -> list:
    opts = _opts(settings, start, end)
    if mode == "search" and query:
        urls = [f"https://www.youtube.com/results?search_query={query}"]
    else:
        urls = ["https://www.youtube.com/feed/trending",
                "https://www.youtube.com/results?search_query=trending",
                "https://www.youtube.com/feed/topics-music"]
    return _extract_candidates(urls, opts, log)


def _fetch_pornhub(mode: str, query: str, start: int, end: int,
                   settings: dict | None, log=None) -> list:
    opts = _opts(settings, start, end)
    if mode == "search" and query:
        urls = [f"https://www.pornhub.com/video/search?search={query}"]
    else:
        urls = ["https://www.pornhub.com/video",
                "https://www.pornhub.com/videos/most_viewed"]
    return _extract_candidates(urls, opts, log)


def _fetch_vk(mode: str, query: str, start: int, end: int,
              settings: dict | None, log=None) -> list:
    if mode != "search" or not query:
        raise CatalogError("Каталог VK: пока доступен только поиск по запросу",
                           kind="unsupported")
    opts = _opts(settings, start, end)
    return _extract_candidates([f"https://vk.com/search?q[{query}]=q"],
                               opts, log)


FETCHERS = {
    "youtube": _fetch_youtube,
    "pornhub": _fetch_pornhub,
    "vk": _fetch_vk,
}


def fetch(platform: str, mode: str = "trending", query: str = "", page: int = 1,
          settings: dict | None = None, force: bool = False, log=None) -> dict:
    """Страница карточек площадки: метаданные без скачивания."""
    platform = str(platform or "").lower()
    mode = str(mode or "trending").lower()
    if mode == "home":
        mode = "trending"
    query = str(query or "").strip()
    page = max(to_int(page, 1) or 1, 1)
    if mode == "search" and not query:
        mode = "trending"
    if not force:
        cached = _cache_get(platform, mode, query, page)
        if cached:
            cached.pop("_ts", None)
            cached["source"] = "cache"
            return cached
    fetcher = FETCHERS.get(platform)
    if not fetcher:
        raise CatalogError("Каталог этой площадки не поддерживается",
                           kind="unsupported")
    start = (page - 1) * PAGE_SIZE + 1
    end = page * PAGE_SIZE
    raw = fetcher(mode, query, start, end, settings, log)
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
        raise CatalogError(
            "Площадка ответила, но карточек на этой странице нет "
            "(попробуйте поиск или «Обновить»)", kind="empty")
    result = {"ok": True, "items": items,
              "has_more": len(raw) >= PAGE_SIZE, "source": "site",
              "platform": platform, "mode": mode, "query": query,
              "page": page}
    _cache_set(platform, mode, query, page, result)
    return result
