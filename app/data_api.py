"""Метаданные видео через YouTube Data API v3 (G2), с откатом на обычный скрейп.

Чем Data API отличается от скрейпа: это официальный JSON (videos.list),
не разбор страницы и не анти-бот; квота — 1 unit на запрос по API-ключу и
2 units по OAuth-токену. Поэтому Data API — вторичный путь: если ключ/токен
не настроены, квота исчерпана или отказ — честный откат на существующий
скрейп (app/sources.py, yt-dlp), kind отказа сохраняется и виден в журнале.

RSS-ленты канала (app/sources.py) остаются основной опорой списка канала:
здесь Data API не подменяет списки каналов (channels.list дороже по квоте,
а RSS даёт более стабильный список) — Data API используется только для
методов карточок одиночного видео.

Журнал: при отказе пишется строка «Data API не доступна, использован скрейп»
без ANSI-кодов (clean_text).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import metadata, oauth, sources
from .sources import FetchError
from .util import clean_text, to_text

API_BASE = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT = 15.0
# Для карточок нужны: snippet (название/автор/дата/обложка/категория/теги),
# contentDetails (длительность), statistics (просмотры/лайки).
PARTS = "snippet,contentDetails,statistics"

# ISO-8601 длительность: "PT1M38S", "PT2H0M5S", "PT0M30.5S".
_DURATION_RE = re.compile(
    r"^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$")


def iso_duration(value) -> float | None:
    """ISO-8601 duration из contentDetails -> секунды (float) или None."""
    text = to_text(value)
    if not text:
        return None
    match = _DURATION_RE.match(text)
    if not match:
        return None
    hours, minutes, seconds = (match.groups())
    total = 0.0
    if hours:
        total += float(hours) * 3600
    if minutes:
        total += float(minutes) * 60
    if seconds:
        total += float(seconds)
    return total


def credentials(settings: dict) -> dict:
    """Чем Data API пользуется сейчас: сначала ключ (дешевле по квоте),
    иначе OAuth-вход (refresh в профиле). Пустой dict — не настроено."""
    if not bool(settings.get("data_api_enabled")):
        return {}
    key = to_text(settings.get("data_api_key"))
    if key:
        return {"api_key": key}
    tokens = oauth.load_tokens()
    if tokens and not tokens.get("broken") and tokens.get("refresh_token"):
        return {"oauth": True}
    return {}


def ready(settings: dict) -> bool:
    return bool(credentials(settings))


def status(settings: dict) -> dict:
    """Состояние Data API для UI: ключ/токен — только флаги, значения молчат."""
    cfg = credentials(settings)
    return {"enabled": bool(settings.get("data_api_enabled")),
            "key_set": bool(to_text(settings.get("data_api_key"))),
            "oauth_ready": bool(cfg.get("oauth")),
            "ready": bool(cfg)}


def _api_http_error(exc: urllib.error.HTTPError) -> FetchError:
    """HTTP-ошибка Data API -> FetchError с kind (квота отдельно от блокировок)."""
    message = ""
    reason = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            errors = error.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason") or "")
    except Exception:  # noqa: BLE001 - тело могло быть не-JSON
        message = clean_text(exc)
    lowered = (reason + " " + message).lower()
    if "quota" in lowered:
        return FetchError(f"Квота Data API исчерпана (reason {reason or '?'}): "
                          "метаданные возьмём из обычного скрейпа — лимит на "
                          "день или суточный лимит", kind="quota")
    if exc.code == 429:
        return FetchError("Data API режет поток (HTTP 429): метаданные возьмём "
                          "из скрейпа", kind="blocked")
    if exc.code in (401, 403):
        return FetchError(f"Data API не приняла credentials (HTTP {exc.code}): "
                          f"{message or clean_text(exc)} — метаданные из скрейпа",
                          kind="blocked")
    if exc.code == 400:
        return FetchError(f"Data API отвергла запрос (HTTP 400): "
                          f"{message or clean_text(exc)}", kind="unsupported")
    return FetchError(f"Data API не ответила (HTTP {exc.code}): "
                      f"{message or clean_text(exc)}", kind="network")


def _open(request: urllib.request.Request) -> dict:
    """GET на endpoint API; JSON-ответ. HTTP-ошибка -> FetchError с kind."""
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:       # subclass OSError — ветка раньше
        raise _api_http_error(exc) from exc
    except OSError as exc:
        raise FetchError(f"Data API не ответила: {clean_text(exc)}",
                         kind="blocked") from exc


def _get(path: str, params: dict, settings: dict) -> dict:
    """Один GET к endpoint v3 с credentials (ключ или OAuth access-токен)."""
    cfg = credentials(settings)
    if not cfg:
        raise FetchError("Data API не настроен: нет ни ключа (настройка "
                         "«Ключ Data API»), ни OAuth входа",
                         kind="unsupported")
    params = dict(params)
    headers = {"Accept": "application/json"}
    if cfg.get("api_key"):
        params["key"] = cfg["api_key"]
    elif cfg.get("oauth"):
        token = oauth.access_token(settings)
        if not token:
            raise FetchError("Data API: OAuth вход не выполнен — настроьте "
                             "ключ или вход", kind="unsupported")
        headers["Authorization"] = f"Bearer {token}"
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    return _open(urllib.request.Request(url, headers=headers))


def _to_info(item: dict) -> dict:
    """Запись videos.list -> info-dict в форме yt-dlp: поверх него
    metadata.normalize_video проходит те же правила нормализации, поэтому
    метки (длительность, дата, просмотры) одинаковы у скрейпа и Data API.
    """
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    details = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}
    thumbnails = snippet.get("thumbnails") if isinstance(snippet.get("thumbnails"), dict) else {}
    thumb = None
    for name in ("maxres", "high", "medium", "default"):
        entry = thumbnails.get(name)
        if isinstance(entry, dict) and entry.get("url"):
            thumb = str(entry["url"])
            break
    published = snippet.get("publishedAt")
    video_id = to_text(item.get("id")) or ""
    return {
        "id": video_id,
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "channel_id": snippet.get("channelId"),
        "channel": snippet.get("channelTitle"),
        "uploader": snippet.get("channelTitle"),
        "category": snippet.get("category"),
        "tags": snippet.get("tags"),
        # publishedAt — ISO с временем: iso_date берёт дату из начала строки.
        "release_date": published,
        "upload_date": published,
        "duration": iso_duration(details.get("duration")),
        "view_count": statistics.get("viewCount"),
        "like_count": statistics.get("likeCount"),
        "thumbnail": thumb,
        "webpage_url": metadata.video_url(metadata.PLATFORM, video_id),
    }


def video_meta(video_id: str, settings: dict) -> dict:
    """Карточка одиночного видео из videos.list (нормализация metadata.py)."""
    payload = _get("videos", {"part": PARTS, "id": video_id, "maxResults": "1"},
                   settings)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not items:
        raise FetchError(f"Data API не нашла видео (id {video_id}): возможно, "
                         "удалено или приватное", kind="unavailable")
    info = _to_info(items[0])
    normalized = metadata.normalize_video(info, origin="data-api")
    if not normalized.get("remote_id"):
        raise FetchError("Data API вернула описание без id видео", kind="empty")
    return normalized


def meta(url: str, settings: dict, *, log=None) -> dict:
    """Метаданные карточки одиночного видео: Data API (если включён и настроен),
    иначе/после отказа — обычный скрейп (sources.fetch_video). Ничего не пишет:
    ответ {source, video, api_error, api_kind}.
    """
    classified = metadata.classify_url(url)
    if (classified.get("platform") != metadata.PLATFORM
            or classified.get("kind") != "video" or not classified.get("video_id")):
        raise FetchError("Data API работает по ссылке только одиночного видео "
                         "YouTube (watch?v=...)", kind="unsupported")
    api_error: str | None = None
    api_kind: str | None = None
    if bool(settings.get("data_api_enabled")):
        try:
            if not ready(settings):
                raise FetchError("Data API не настроен: нет ни ключа, ни OAuth "
                                 "входа", kind="unsupported")
            return {"ok": True, "source": "data-api",
                    "video": video_meta(classified["video_id"], settings),
                    "api_error": None, "api_kind": None}
        except (FetchError, oauth.FlowError) as exc:
            kind = getattr(exc, "kind", "network")
            api_error, api_kind = clean_text(exc), kind
            if log:
                log(f"Data API не доступна, использован скрейп ({api_kind}): "
                    f"{api_error}")
    info = sources.fetch_video(classified.get("url") or url, settings=settings,
                               log=log)
    return {"ok": True, "source": "scrape",
            "video": metadata.normalize_video(info, origin="yt-dlp"),
            "api_error": api_error, "api_kind": api_kind}
