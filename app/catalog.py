"""Каталог площадок - браузер похожий на оригинальную страницу.

Только просмотр и метаданные. Никакого скачивания, только чтение.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import yt_dlp

from . import metadata
from . import platforms
from . import sources
from .util import to_int, to_text


class CatalogError(Exception):
    """Ошибка загрузки каталога."""


# TTL в секундах
CACHE_TTL = {
    "trending": 600,
    "home": 600,
    "search": 300,
}


def _cache_dir() -> str:
    """Директория кэша каталога в data/ (не во временных)."""
    from . import profile
    base = profile.data_dir()
    d = os.path.join(base, "cache", "catalog")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(platform: str, mode: str, query: str, page: int) -> str:
    raw = f"{platform}|{mode}|{query or ''}|{page}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(platform: str, mode: str, query: str, page: int) -> dict | None:
    try:
        path = os.path.join(_cache_dir(), _cache_key(platform, mode, query, page) + ".json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        ts = float(data.get("_ts") or 0)
        ttl = CACHE_TTL.get(mode, 600)
        if time.time() - ts > ttl:
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _cache_set(platform: str, mode: str, query: str, page: int, data: dict) -> None:
    try:
        path = os.path.join(_cache_dir(), _cache_key(platform, mode, query, page) + ".json")
        payload = dict(data)
        payload["_ts"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        pass


def _normalize_item(raw: dict, platform: str) -> dict | None:
    """Нормализация записи yt-dlp в CatalogItem."""
    if not isinstance(raw, dict):
        return None
    video_id = to_text(raw.get("id"))
    if not video_id:
        return None
    title = to_text(raw.get("title")) or ""
    url = to_text(raw.get("url")) or to_text(raw.get("webpage_url")) or ""
    # Для youtube короткие id - строим канонический URL
    if platform == "youtube" and len(video_id) == 11 and not url:
        url = f"https://www.youtube.com/watch?v={video_id}"
    # Для pornhub viewkey
    if platform == "pornhub" and not url and video_id:
        url = metadata.video_url(platform, video_id) or ""
    if platform == "vk" and not url and video_id:
        url = metadata.video_url(platform, video_id) or ""
    # Канонизация PH зеркал
    if platform == "pornhub" and url:
        # Приводим к www.pornhub.com для единообразия
        try:
            from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
            p = urlparse(url)
            if p.netloc.endswith("pornhub.org") or p.netloc.endswith("pornhub.net"):
                q = parse_qs(p.query)
                p2 = p._replace(netloc="www.pornhub.com")
                url = urlunparse(p2)
        except Exception:
            pass
    thumb = to_text(raw.get("thumbnail"))
    if not thumb and isinstance(raw.get("thumbnails"), list) and raw["thumbnails"]:
        tlist = raw["thumbnails"]
        try:
            thumb = to_text(tlist[-1].get("url")) if isinstance(tlist[-1], dict) else None
        except Exception:
            thumb = None
    duration = to_int(raw.get("duration"))
    views = to_int(raw.get("view_count"))
    author = to_text(raw.get("uploader")) or to_text(raw.get("channel")) or to_text(raw.get("artist"))
    author_id = to_text(raw.get("channel_id")) or to_text(raw.get("uploader_id"))
    return {
        "url": url,
        "video_id": video_id,
        "platform": platform,
        "title": title,
        "thumb": thumb,
        "duration_s": duration,
        "views": views,
        "author": author,
        "author_id": author_id,
        "added": False,
    }


def _fetch_youtube(mode: str, query: str, page: int, settings: dict | None = None) -> list[dict]:
    """YouTube: trending/home/search через yt-dlp flat-extract."""
    settings = settings or {}
    items_out: list[dict] = []
    if mode == "search" and query:
        url = f"ytsearch50:{query}"
    elif mode in ("trending", "home"):
        # trending больше похож на оригинал и стабильнее без авторизации
        url = "https://www.youtube.com/feed/trending"
    else:
        url = "https://www.youtube.com/feed/trending"
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
    }
    browser_opt = None
    try:
        from . import google_auth
        browser_opt = google_auth.browser_cookie_option(settings)
    except Exception:
        browser_opt = None
    if browser_opt:
        opts["cookiesfrombrowser"] = browser_opt
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        text = str(exc)
        if "timed out" in text or "Connection" in text or "403" in text or "410" in text:
            raise CatalogError("Площадка недоступна из вашей сети") from exc
        raise CatalogError(f"Не удалось загрузить каталог: {exc}") from exc
    except Exception as exc:
        raise CatalogError(f"Не удалось загрузить каталог: {exc}") from exc
    entries = []
    if isinstance(info, dict):
        if isinstance(info.get("entries"), list):
            entries = info["entries"]
        elif info.get("id") and info.get("title"):
            entries = [info]
    # Пагинация упрощённая для MVP: yt-dlp flat даёт первые ~50
    # Для простоты берём все доступные на первой загрузке
    for raw in entries:
        item = _normalize_item(raw, "youtube")
        if item:
            items_out.append(item)
    return items_out


def _fetch_pornhub(mode: str, query: str, page: int, settings: dict | None = None) -> list[dict]:
    """PornHub: trending/home через yt-dlp flat-extract."""
    settings = settings or {}
    items_out: list[dict] = []
    if mode == "search" and query:
        url = f"https://www.pornhub.com/video/search?search={query}"
    elif mode in ("trending", "home"):
        url = "https://www.pornhub.com/video"
    else:
        url = "https://www.pornhub.com/video"
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
    }
    browser_opt = None
    try:
        from . import google_auth
        browser_opt = google_auth.browser_cookie_option(settings)
    except Exception:
        browser_opt = None
    if browser_opt:
        opts["cookiesfrombrowser"] = browser_opt
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        text = str(exc)
        if "timed out" in text or "Connection" in text or "403" in text or "410" in text or "reset" in text.lower():
            raise CatalogError("Площадка недоступна из вашей сети") from exc
        raise CatalogError(f"Не удалось загрузить каталог: {exc}") from exc
    except Exception as exc:
        raise CatalogError(f"Не удалось загрузить каталог: {exc}") from exc
    entries = []
    if isinstance(info, dict):
        if isinstance(info.get("entries"), list):
            entries = info["entries"]
        elif info.get("id") and info.get("title"):
            entries = [info]
    for raw in entries:
        item = _normalize_item(raw, "pornhub")
        if item and item.get("url"):
            items_out.append(item)
    return items_out


def _fetch_vk(mode: str, query: str, page: int, settings: dict | None = None) -> list[dict]:
    """VK: best-effort через yt-dlp."""
    settings = settings or {}
    items_out: list[dict] = []
    if mode == "search" and query:
        url = f"vksearch:{query}"
    else:
        raise CatalogError("Каталог временно недоступен")
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise CatalogError("Каталог временно недоступен") from exc
    entries = []
    if isinstance(info, dict) and isinstance(info.get("entries"), list):
        entries = info["entries"]
    for raw in entries:
        item = _normalize_item(raw, "vk")
        if item and item.get("url"):
            items_out.append(item)
    return items_out


FETCHERS = {
    "youtube": _fetch_youtube,
    "pornhub": _fetch_pornhub,
    "vk": _fetch_vk,
}


def fetch(platform: str, mode: str = "trending", query: str = "", page: int = 1,
         settings: dict | None = None, force: bool = False) -> dict:
    """Получить каталог площадки."""
    platform = str(platform or "").lower()
    mode = str(mode or "trending").lower()
    if mode == "home":
        mode = "trending"
    query = str(query or "").strip()
    page = max(int(page or 1), 1)
    if mode == "search" and not query:
        mode = "trending"
        query = ""
    settings = settings or {}
    if not force:
        cached = _cache_get(platform, mode, query, page)
        if cached:
            out = dict(cached)
            out.pop("_ts", None)
            out["source"] = "cache"
            return out
    fetcher = FETCHERS.get(platform)
    if not fetcher:
        raise CatalogError("Каталог для этой площадки не поддерживается")
    try:
        items = fetcher(mode, query, page, settings)
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError(str(exc)) from exc
    result = {
        "ok": True,
        "items": items,
        "has_more": False,
        "source": "site",
        "platform": platform,
        "mode": mode,
        "query": query,
    }
    _cache_set(platform, mode, query, page, result)
    return result


def clear_cache(platform: str | None = None) -> int:
    """Очистить кэш каталога."""
    count = 0
    try:
        d = _cache_dir()
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            if platform:
                continue  # упрощённо не фильтруем по platform
            try:
                os.remove(os.path.join(d, fn))
                count += 1
            except OSError:
                pass
    except OSError:
        pass
    return count
