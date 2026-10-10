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
from .metadata import flat_entry, normalize_playlist

CHUNK = 100        # записей на запрос
MAX_CHUNKS = 500   # защита от зацикливания (50 000 записей)
SUPPORTED = ("playlist", "channel")


class FetchError(Exception):
    """Площадка не ответила или вернула то, что мы не просили."""


class Aborted(Exception):
    """Пользователь нажал «Отмена» между чанками - БД не тронута."""


def _opts(settings: dict | None) -> dict:
    """Опции yt-dlp для плоского извлечения: метаданные, без файлов."""
    settings = settings or {}
    return {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,      # приватная запись не должна валить весь список
        "socket_timeout": 25,
        "retries": max(int(settings.get("retries") or 0), 1),
        # Обход/прокси и cookies подключатся вместе с Bypass из Synfronia (M4).
    }


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
    kind = classify(url).get("kind")
    if kind not in SUPPORTED:
        raise FetchError(
            "Поддерживаются ссылки на плейлист и на канал; "
            "одиночное видео появится вместе с загрузчиком.")

    with _account_cookiefile(account_id) as cookiefile:
        return _snapshot(url, kind, settings=settings,
                         on_progress=on_progress, stop=stop,
                         cookiefile=cookiefile)


def _snapshot(url: str, kind: str, *, settings: dict | None,
              on_progress, stop, cookiefile) -> dict:
    delay = max(int((settings or {}).get("delay_ms") or 0), 0) / 1000.0

    # Первый чанк приносит и метаданные плейлиста, и первые записи.
    head = _extract(url, f"1-{CHUNK}", settings, cookiefile)
    if not isinstance(head, dict) or head.get("_type") not in ("playlist", None):
        raise FetchError("Ссылка не оказалась плейлистом или каналом")

    playlist_id = str(head.get("id") or "")
    playlist = normalize_playlist(
        head, kind=("uploads" if kind == "channel"
                    else metadata.playlist_kind(playlist_id)))
    total = int(head.get("playlist_count") or 0)

    playlist_channel = playlist.get("channel")
    entries = _collect(head.get("entries"), playlist_channel, start=1)
    got = len(entries)
    if on_progress:
        on_progress(got, total)

    # Последующие чанки идут по канонической ссылке: параметр si и прочий
    # мусор из адреса пользователя только мешает пагинации.
    request_url = url
    if metadata.classify_url(url)["playlist_id"]:
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
        chunk = _extract(request_url, f"{start}-{start + CHUNK - 1}", settings,
                         cookiefile)
        chunk_entries = _collect(chunk.get("entries") if isinstance(chunk, dict) else None,
                                 playlist_channel, start=start)
        chunks += 1
        if not chunk_entries:
            break
        entries.extend(chunk_entries)
        got = len(entries)
        if on_progress:
            on_progress(got, total)
        # Меньше, чем просили, - конец списка. Ровно CHUNK может значить и
        # конец, и просто границу: тогда решит следующий (пустой) запрос.
        if len(chunk_entries) < CHUNK:
            break
        start = got + 1

    if not entries:
        raise FetchError("Плейлист пуст или все записи недоступны")

    return {"playlist": playlist, "entries": entries,
            "total": total or len(entries), "url": request_url}


def _collect(raw_entries, playlist_channel: dict | None, start: int) -> list[dict]:
    """Записи из yt-dlp -> нормализованный вид, с наследованием автора.

    У канала записи приходят БЕЗ channel/channel_id (yt-dlp не повторяет
    автора для каждого видео загрузок) - без наследования такие видео
    оказывались бы «без автора» и не собирались в дерево «Каналы».
    """
    out: list[dict] = []
    seen: set[str] = set()
    position = start
    for raw in (raw_entries or []):
        item = flat_entry(raw, position)
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
