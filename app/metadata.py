"""Нормализация метаданных: сырой info-dict площадки -> строки таблиц.

Правила, которые держит этот модуль:
  * raw_json - источник истины, но «хвостов» в нём быть не должно:
    списки форматов и субтитров (десятки килобайт на видео) заменяются
    сводками, иначе библиотека из 10 000 видео раздула бы файл базы;
  * тонкие колонки - только то, что участвует в индексе, сортировке
    и поиске: они пишутся здесь один раз и не пересчитываются;
  * неизвестное -> None, а не "" и не 0: пустая дата и отсутствие даты
    в запросах различимы.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

from .util import iso_date, norm_title, to_int, to_text

PLATFORM = "youtube"

# Ключевые слова, из-за которых info-dict раздувается на десятки килобайт.
# Их ценность для индекса нулевая: форматы меняются от запроса к запросу.
_HEAVY_KEYS = ("formats", "requested_formats", "automatic_captions",
               "subtitles", "thumbnails", "comment_count")

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def video_key(platform: str, remote_id: str) -> str:
    """Стабильный ключ записи: «youtube:dQw4w9WgXcQ»."""
    return f"{platform}:{remote_id}"


def local_key(path: str) -> str:
    """Ключ для файла, у которого нет площадочного ID.

    Хеш пути (не содержимого): один и тот же файл в одной и той же папке
    всегда одна запись, а после переезда в другую папку скан найдёт его
    по хешу содержимого и перепривяжет, не плодя дубль.
    """
    digest = hashlib.sha1(path.encode("utf-8", "surrogatepass")).hexdigest()
    return f"local:{digest}"


def slim_info(info: dict) -> str:
    """info-dict для raw_json: полезное целиком, громоздкое - сводкой."""
    if not isinstance(info, dict):
        return ""
    slim = {}
    for key, value in info.items():
        if key in _HEAVY_KEYS:
            continue
        if key == "entries" and isinstance(value, (list, tuple)):
            # Записи плейлиста живут в playlist_items, а не в JSON.
            slim["entries_count"] = len(value)
            continue
        slim[key] = value
    if isinstance(info.get("subtitles"), dict):
        slim["subtitles_available"] = sorted(info["subtitles"])
    if isinstance(info.get("automatic_captions"), dict):
        slim["captions_available"] = sorted(info["automatic_captions"])
    if isinstance(info.get("thumbnails"), list) and info["thumbnails"]:
        slim["thumbnail_last"] = info["thumbnails"][-1].get("url")
    if isinstance(info.get("formats"), list):
        slim["formats_count"] = len(info["formats"])
    try:
        return json.dumps(slim, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


def channel_ref(info: dict, platform: str = PLATFORM) -> dict | None:
    """Автор из info-dict: предпочитаем канонический UC-id, иначе заголовок.

    youtube даёт channel_id (UC...), но в плоских записях плейлиста его
    может не быть - остаётся title. Ключ из заголовка помечен префиксом
    «title:», чтобы реальный id, найденный позже, не сошёлся с ним сам:
    repo умеет переносить такие записи на настоящий канал.
    """
    if not isinstance(info, dict):
        return None
    channel_id = to_text(info.get("channel_id"))
    if channel_id and _CHANNEL_ID_RE.match(channel_id):
        return {"platform": platform, "remote_id": channel_id,
                "title": to_text(info.get("channel")) or to_text(info.get("uploader")),
                "handle": to_text(info.get("uploader")) if str(info.get("uploader") or "").startswith("@") else None,
                "url": _channel_url(channel_id), "fallback": False}
    uploader_id = to_text(info.get("uploader_id"))
    if uploader_id and _CHANNEL_ID_RE.match(uploader_id):
        return {"platform": platform, "remote_id": uploader_id,
                "title": to_text(info.get("channel")) or to_text(info.get("uploader")),
                "handle": None, "url": _channel_url(uploader_id), "fallback": False}
    title = to_text(info.get("channel")) or to_text(info.get("uploader"))
    if not title:
        return None
    digest = hashlib.sha1(norm_title(title).encode("utf-8")).hexdigest()[:16]
    return {"platform": platform, "remote_id": f"title:{digest}", "title": title,
            "handle": None, "url": None, "fallback": True}


def _channel_url(remote_id: str) -> str | None:
    return f"https://www.youtube.com/channel/{remote_id}" if remote_id.startswith("UC") else None


def normalize_video(info: dict, platform: str = PLATFORM, origin: str = "yt-dlp") -> dict:
    """Строка таблицы videos (+ вложенный «channel» для repo.upsert_video)."""
    info = info if isinstance(info, dict) else {}
    remote_id = to_text(info.get("id")) or ""
    thumb = to_text(info.get("thumbnail"))
    if not thumb and isinstance(info.get("thumbnails"), list) and info["thumbnails"]:
        thumb = to_text(info["thumbnails"][-1].get("url"))
    description = to_text(info.get("description"))
    return {
        "platform": platform,
        "remote_id": remote_id,
        "key": video_key(platform, remote_id) if remote_id else None,
        "title": to_text(info.get("title")),
        "description": description,
        "uploaded_at": iso_date(info.get("upload_date") or info.get("release_date")),
        "duration_s": to_int(info.get("duration")),
        "view_count": to_int(info.get("view_count")),
        "category": to_text(info.get("category")),
        "webpage_url": to_text(info.get("webpage_url")) or to_text(info.get("url")),
        "thumb_url": thumb,
        "raw_json": slim_info(info),
        "origin": origin,
        "channel": channel_ref(info, platform),
    }


def normalize_playlist(info: dict, kind: str = "remote", platform: str = PLATFORM) -> dict:
    """Строка таблицы playlists."""
    info = info if isinstance(info, dict) else {}
    remote_id = to_text(info.get("id")) or ""
    return {
        "platform": platform,
        "remote_id": remote_id,
        "title": to_text(info.get("title")),
        "description": to_text(info.get("description")),
        "kind": kind,
        "url": to_text(info.get("webpage_url")) or to_text(info.get("url")),
        "item_count": to_int(info.get("playlist_count") or info.get("n_entries")),
        "raw_json": slim_info(info),
        "channel": channel_ref(info, platform),
    }


def flat_entry(entry, position: int, platform: str = PLATFORM) -> dict:
    """Запись плейлиста из extract_flat: только то, что нужно для diff.

    entry может быть None (приватное/удалённое видео в списке) - тогда
    получаем «дырку» с флагом unavailable: она попадёт в счётчик
    «пропущено» диалога и не создаст строку в БД.
    """
    if not isinstance(entry, dict):
        return {"position": position, "remote_id": None, "title": None,
                "unavailable": True, "channel": None}
    remote_id = to_text(entry.get("id"))
    return {
        "position": position,
        "remote_id": remote_id,
        "title": to_text(entry.get("title")),
        "duration_s": to_int(entry.get("duration")),
        "uploaded_at": iso_date(entry.get("upload_date") or entry.get("release_date")),
        "view_count": to_int(entry.get("view_count")),
        "unavailable": not bool(remote_id),
        "webpage_url": to_text(entry.get("url")) or (
            f"https://www.youtube.com/watch?v={remote_id}" if remote_id else None),
        "channel": channel_ref(entry, platform),
    }


def playlist_kind(playlist_id: str) -> str:
    """RD.../RDMM... - микс, который YouTube пересобирает каждый день."""
    return "mix" if str(playlist_id or "").startswith("RD") else "remote"


def classify_url(url: str) -> dict:
    """Что перед нами: playlist / channel / video / unknown.

    Порядок важен: `watch?v=ID&list=PL...` - это плейлист (именно так
    пользователь и вставляет ссылки), одиночное видео - только когда
    параметра list нет.
    """
    text = to_text(url) or ""
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    result = {"kind": "unknown", "url": text, "video_id": None,
              "playlist_id": None, "channel_id": None, "handle": None}

    playlist_id = (query.get("list") or [None])[0]
    if playlist_id and _PLAYLIST_ID_RE.match(str(playlist_id)):
        result["playlist_id"] = str(playlist_id)

    video_id = (query.get("v") or [None])[0]
    if not video_id and parsed.netloc.endswith("youtu.be"):
        video_id = parsed.path.lstrip("/").split("/")[0] or None
    if not video_id:
        match = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", text)
        video_id = match.group(1) if match else None
    if video_id and _VIDEO_ID_RE.match(video_id):
        result["video_id"] = video_id

    # /channel/UC... и /@handle - канал; список UL... (загруженные) - тоже канал
    match = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", text)
    if match:
        result["channel_id"] = match.group(1)
    match = re.search(r"/@([A-Za-z0-9._-]+)", text)
    if match:
        result["handle"] = match.group(1)
    if not result["handle"] and re.search(r"/(?:user|c)/([^/?#]+)", text):
        result["handle"] = re.search(r"/(?:user|c)/([^/?#]+)", text).group(1)

    if result["playlist_id"]:
        # Загрузки канала - это playlist UU...: подпись канала сохраняем.
        if result["channel_id"] or result["handle"]:
            result["kind"] = "uploads" if str(result["playlist_id"]).startswith("UU") else "playlist"
        else:
            result["kind"] = "playlist"
    elif result["channel_id"] or result["handle"]:
        result["kind"] = "channel"
    elif result["video_id"]:
        result["kind"] = "video"
    return result
