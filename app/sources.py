"""Индексация источников: URL -> снапшот плейлиста или канала.

Здесь только чтение сети - ни одной записи в БД. Ровно так и устроен флоу
добавления: сначала снять снапшот, затем посчитать план (repo.plan_diff),
показать диалог со счётчиками и лишь после подтверждения писать в базу.

Почему чанки: yt-dlp при extract_flat не отдаёт поштучного прогресса
(progress_hooks молчат), поэтому список тяжёлых плейлистов собирается
запросами по CHUNK записей - это и честная полоса прогресса, и возможность
остановиться между чанками, не убивая получасовую загрузку.
"""

from __future__ import annotations

import contextlib
import time

import yt_dlp

from . import google_auth
from . import metadata
from .metadata import PLATFORM, flat_entry, normalize_playlist
from .util import to_text

CHUNK = 100        # записей на запрос (быстрый плоский синк)
DEEP_CHUNK = 25    # записей на запрос в полном режиме: каждый чанк в десятки
                   # раз дороже (полный info на каждое видео) - и прогресс
                   # должен дышать чаще
MAX_CHUNKS = 500   # защита от зацикливания (50 000 записей)
SUPPORTED = ("playlist", "channel", "video")


class FetchError(Exception):
    """Площадка не ответила или вернула то, что мы не просили."""


class Aborted(Exception):
    """Пользователь нажал «Отмена» между чанками - БД не тронута."""


def _opts(settings: dict | None) -> dict:
    """Опции yt-dlp для извлечения списка: метаданные, без файлов.

    Глубина синка (настройка sync_depth):
      "full" (по умолчанию) - каждое видео извлекается полностью: лайки,
        комменты, дата публикации, описание, обложка. Медленно (запрос
        на видео) и ближе к анти-боту - зато база богаче;
      "fast" - плоский список: длительность/просмотры/обложка одним
        запросом на чанк (старое поведение).
    """
    settings = settings or {}
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,      # приватная запись не должна валить весь список
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
        # Обход/прокси и cookies подключатся вместе с Bypass из Synfronia (M4).
    }
    if str(settings.get("sync_depth") or "full") == "fast":
        opts["extract_flat"] = "in_playlist"
    return opts


def _extract(request_url: str, items: str, settings: dict | None = None,
             cookiefile=None) -> dict | None:
    """Один запрос к площадке: плоский список записей указанным диапазоном.

    Отдельная функция (а не замыкание), чтобы тесты могли подменить сеть
    и проверить сборку чанков без интернета. cookiefile - куки аккаунта
    источника на время запроса (если привязаны).
    """
    opts = _opts(settings)
    opts["playlist_items"] = items
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)
    else:
        browser_opt = google_auth.browser_cookie_option(settings or {})
        if browser_opt:
            # Режим «куки из браузера»: копий нет, читает сам yt-dlp.
            opts["cookiesfrombrowser"] = browser_opt
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(request_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise FetchError(f"Площадка не ответила: {exc}") from exc


def fetch_video(url: str, *, settings: dict | None = None,
                stop=None) -> dict:
    """Полные метаданные ОДНОГО видео (без файлов).

    Этим же конвейером пользуется кнопка «Обновить метаданные»: полный
    запрос несёт то, чего нет в плоском списке плейлиста - лайки,
    комменты, дату публикации, описание и обложку. Один запрос на видео,
    поэтому stop проверяется между видео (в воркере), а не здесь.
    """
    settings = settings or {}
    opts = {
        "noplaylist": True,           # одиночное видео, даже если в ссылке list
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
    }
    browser_opt = google_auth.browser_cookie_option(settings)
    if browser_opt:
        opts["cookiesfrombrowser"] = browser_opt
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        text = str(exc)
        hint = ""
        if ("Connection aborted" in text or "timed out" in text
                or "410" in text or "403" in text):
            # Площадка может быть недоступна из сети пользователя
            # (блокировка региона/провайдера) - честная подсказка вместо
            # молчаливого таймаута; лечится средствами доступа пользователя
            # (VPN/прокси) или участием mesh (relay - этап 3).
            hint = (" (площадка недоступна из вашей сети - возможно, "
                    "региональная блокировка: поможет VPN/прокси, либо "
                    "запрос участникам mesh)")
        raise FetchError(f"Площадка не ответила: {exc}{hint}") from exc
    if not isinstance(info, dict) or not info.get("id"):
        raise FetchError("площадка не вернула описание видео")
    return info


def classify(url: str) -> dict:
    """Что за ссылка: плейлист, канал или не поддерживаемое."""
    info = metadata.classify_url(url)
    if info["kind"] == "channel":
        # Канал - это тот же плейлист загрузок: yt-dlp сам развернёт @handle.
        info["kind"] = "channel"
    return info


@contextlib.contextmanager
def _account_cookiefile(account_id):
    """Куки аккаунта источника на время снапшота (см. google_auth).

    Копия на диске живёт ровно столько, сколько идёт запрос.
    """
    path = None
    if account_id:
        try:
            path = google_auth.temporary_cookiefile(account_id)
        except OSError:
            path = None
    try:
        yield path
    finally:
        google_auth.release_temp(path)


def fetch_snapshot(url: str, *, settings: dict | None = None,
                   on_progress=None, stop=None,
                   account_id: str | None = None) -> dict:
    """Снять снапшот источника: метаданные + все записи. Ничего не пишет.

    on_progress(got, total) - сколько записей уже получено (total может быть
        0, пока площадка не назвала размер списка);
    stop - threading.Event: проверяется между чанками, при срабатывании
        бросает Aborted (частично собранный снапшот выбрасывается);
    account_id - аккаунт Google источника: «подтвердите, что не бот»
        проходится от имени привязанной учётки.
    """
    info = classify(url)
    kind = info.get("kind")
    platform = str(info.get("platform") or PLATFORM)
    if kind not in SUPPORTED:
        raise FetchError(
            "Поддерживаются ссылки на плейлист, канал и одиночное видео")
    if kind == "video":
        return _snapshot_video(url, platform=platform, settings=settings,
                               on_progress=on_progress, stop=stop,
                               account_id=account_id)

    with _account_cookiefile(account_id) as cookiefile:
        return _snapshot(url, kind, platform=platform, settings=settings,
                         on_progress=on_progress, stop=stop,
                         cookiefile=cookiefile)


def _snapshot_video(url: str, *, platform: str, settings: dict | None,
                    on_progress, stop, account_id: str | None) -> dict:
    """Одиночное видео: снапшот ИЗ ОДНОЙ записи (тот же формат).

    Формат не меняется (playlist + entries) - весь дальнейший конвейер
    (plan_diff/commit_plan/синки) работает как для плейлиста, просто
    план содержит одно видео. Запрос - полный (fetch_video): карточка
    получает лайки/дату/описание сразу, а не после «Обновить
    метаданные». noplaylist гарантирует, что &list= в ссылке не
    развернёт весь плейлист.
    """
    info = fetch_video(url, settings=settings, stop=stop)
    video_id = to_text(info.get("id") or "")
    if not video_id:
        raise FetchError("площадка не вернула описание видео")
    playlist = normalize_playlist(info, kind="video", platform=platform)
    entry = flat_entry(info, position=1, platform=platform)
    if on_progress:
        on_progress(1, 1)               # фиксированная форма: 1 из 1
    return {"playlist": playlist, "entries": [entry],
            "total": 1, "url": url}


def _snapshot(url: str, kind: str, *, platform: str,
              settings: dict | None,
              on_progress, stop, cookiefile) -> dict:
    delay = max(int((settings or {}).get("delay_ms") or 0), 0) / 1000.0
    # Размер чанка зависит от глубины: полный режим берёт по 25 записей
    # (каждая - отдельный тяжёлый запрос), быстрый - по 100.
    chunk_size = (CHUNK if str((settings or {}).get("sync_depth") or "full")
                  == "fast" else DEEP_CHUNK)

    # Первый чанк приносит и метаданные плейлиста, и первые записи.
    head = _extract(url, f"1-{chunk_size}", settings, cookiefile)
    if not isinstance(head, dict) or head.get("_type") not in ("playlist", None):
        raise FetchError("Ссылка не оказалась плейлистом или каналом")

    playlist_id = str(head.get("id") or "")
    playlist = normalize_playlist(
        head, platform=platform,
        kind=("uploads" if kind == "channel"
              else ("remote" if platform != PLATFORM
                    else metadata.playlist_kind(playlist_id))))
    total = int(head.get("playlist_count") or 0)

    playlist_channel = playlist.get("channel")
    entries = _collect(head.get("entries"), playlist_channel, start=1,
                       platform=platform)
    got = len(entries)
    if on_progress:
        on_progress(got, total)

    # Последующие чанки идут по канонической ссылке: параметр si и прочий
    # мусор из адреса пользователя только мешает пагинации (канонизация
    # youtube-специфична: у других площадок своя механика пагинации).
    request_url = url
    if platform == PLATFORM and metadata.classify_url(url)["playlist_id"]:
        request_url = ("https://www.youtube.com/playlist?list="
                       + metadata.classify_url(url)["playlist_id"])

    start = got + 1
    chunks = 0
    while True:
        if stop is not None and stop.is_set():
            raise Aborted()
        if total and got >= total:
            break
        if chunks >= MAX_CHUNKS:
            break
        if delay:
            time.sleep(delay)
        chunk = _extract(request_url, f"{start}-{start + chunk_size - 1}",
                         settings, cookiefile)
        chunk_entries = _collect(chunk.get("entries") if isinstance(chunk, dict) else None,
                                 playlist_channel, start=start,
                                 platform=platform)
        chunks += 1
        if not chunk_entries:
            break
        entries.extend(chunk_entries)
        got = len(entries)
        if on_progress:
            on_progress(got, total)
        # Меньше, чем просили, - конец списка. Ровно chunk_size может значить
        # и конец, и просто границу: тогда решит следующий (пустой) запрос.
        if len(chunk_entries) < chunk_size:
            break
        start = got + 1

    if not entries:
        raise FetchError("Плейлист пуст или все записи недоступны")

    return {"playlist": playlist, "entries": entries,
            "total": total or len(entries), "url": request_url}


def _collect(raw_entries, playlist_channel: dict | None, start: int,
             platform: str = PLATFORM) -> list[dict]:
    """Записи из yt-dlp -> нормализованный вид, с наследованием автора.

    У канала записи приходят БЕЗ channel/channel_id (yt-dlp не повторяет
    автора для каждого видео загрузок) - без наследования такие видео
    оказывались бы «без автора» и не собирались в дерево «Каналы».
    """
    out: list[dict] = []
    seen: set[str] = set()
    position = start
    for raw in (raw_entries or []):
        item = flat_entry(raw, position, platform=platform)
        position += 1
        remote_id = item.get("remote_id")
        if remote_id:
            if remote_id in seen:
                continue        # дубль в списке: позиции сдвигаем, копию не берём
            seen.add(remote_id)
        elif item.get("unavailable"):
            # Приватное/удалённое остаётся «дыркой»: попадёт в счётчик
            # «пропущено» диалога и не создаст строку в БД.
            pass
        if not item.get("channel") and playlist_channel:
            item["channel"] = playlist_channel
        out.append(item)
    return out
