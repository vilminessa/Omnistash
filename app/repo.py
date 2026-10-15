"""Репозиторий: запросы и записи в индекс библиотеки.

Всё, что трогает БД, живёт здесь - остальной код работает с нормальными
словарями и не знает SQL. Два принципа, которые держит модуль:

  * `plan_diff` - только чтение: диалог подтверждения обязан показывать
    честные счётчики, а отмена пользователя не должна оставить следов;
  * `commit_plan` - одна транзакция на все стадии: UI отдаёт прогресс
    по ходу, но падение на любой стадии откатывает всё, и «Повторить»
    начинает с чистого состояния.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from . import storages as storages_mod
from .db import STATUSES, SYNC_MODES
from .metadata import PLATFORM, local_key, video_key
from .util import SIDECAR_SUFFIX, now_iso

# Подписи статусов для таблицы и карточки.
STATUS_LABELS = {
    "known": "в индексе",
    "queued": "в очереди",
    "downloading": "качается",
    "downloaded": "скачано",
    "failed": "ошибка",
    "missing": "файл пропал",
    "unavailable": "удалено на площадке",
}

KIND_LABELS = {"uploads": "загрузки канала", "remote": "плейлист",
               "local": "подборка", "mix": "микс"}

_CHUNK = 400  # меньше лимита SQLite на ?-параметры, с запасом


def _chunks(seq, size: int = _CHUNK):
    for start in range(0, len(seq), size):
        yield seq[start:start + size]


# --------------------------------------------------------------------------- #
#  Запись
# --------------------------------------------------------------------------- #

def upsert_channel(conn: sqlite3.Connection, ref: dict | None) -> int | None:
    """Канал по (platform, remote_id): создать или освежить название.

    ref с fallback-ключом «title:...» - заголовок вместо UC-id: такая
    строка помечена в metadata.channel_ref и позже может быть заменена
    настоящим каналом, когда площадка его наконец отдаст.
    """
    if not isinstance(ref, dict):
        return None
    platform = ref.get("platform") or PLATFORM
    remote_id = ref.get("remote_id")
    if not remote_id:
        return None
    now = now_iso()
    row = conn.execute(
        "SELECT id, title FROM channels WHERE platform=? AND remote_id=?",
        (platform, remote_id),
    ).fetchone()
    if row:
        # Название площадка может сменить (ребрендинг канала) - обновляем,
        # но не затираем существующее пустым.
        title = ref.get("title")
        if title and title != row["title"]:
            conn.execute("UPDATE channels SET title=?, updated_at=? WHERE id=?",
                         (title, now, row["id"]))
        return row["id"]
    cur = conn.execute(
        """INSERT INTO channels (platform, remote_id, title, handle, url,
                                 raw_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (platform, remote_id, ref.get("title") or "", ref.get("handle"),
         ref.get("url"), ref.get("raw_json"), now, now),
    )
    return int(cur.lastrowid)


def upsert_video(conn: sqlite3.Connection, data: dict, *,
                 full: bool = False) -> tuple[int | None, bool]:
    """Видео по key: (id, создана ли строка).

    Существующая запись обновляется только сетевыми полями: title, дата,
    счётчики, raw_json. `status` и `user_*` - не трогаются никогда, это
    локальное состояние и оценка пользователя (см. db.py).

    full=False означает «данные из плоской записи плейлиста»: raw_json там
    тощий, затирая им богатую карточку, мы бы теряли метаданные при каждом
    синке - поэтому он обновляется только при full=True или если пуст.
    """
    remote_id = (data.get("remote_id") or "").strip()
    platform = data.get("platform") or PLATFORM
    if not remote_id:
        return None, False
    key = data.get("key") or video_key(platform, remote_id)
    now = now_iso()
    channel_id = upsert_channel(conn, data.get("channel"))

    row = conn.execute("SELECT id, raw_json FROM videos WHERE key=?", (key,)).fetchone()
    if row:
        vid = int(row["id"])
        new_raw = data.get("raw_json") or None
        old_raw = row["raw_json"] or None
        # Богатый карточку (full) можно перезаписать, но пустым не затирать;
        # тонкий набор из плоской записи плейлиста богатую не трогает,
        # зато заполняет пустую - иначе каждый синк терял бы метаданные.
        raw = (new_raw or old_raw) if full else (old_raw or new_raw)
        conn.execute(
            """UPDATE videos SET
                 title=coalesce(?, title),
                 description=coalesce(?, description),
                 uploaded_at=coalesce(?, uploaded_at),
                 duration_s=coalesce(?, duration_s),
                 view_count=coalesce(?, view_count),
                 like_count=coalesce(?, like_count),
                 comment_count=coalesce(?, comment_count),
                 category=coalesce(?, category),
                 webpage_url=coalesce(?, webpage_url),
                 thumb_url=coalesce(?, thumb_url),
                 raw_json=coalesce(?, raw_json),
                 channel_id=coalesce(?, channel_id),
                 updated_at=?
               WHERE id=?""",
            (data.get("title"), data.get("description"), data.get("uploaded_at"),
             data.get("duration_s"), data.get("view_count"),
             data.get("like_count"), data.get("comment_count"),
             data.get("category"), data.get("webpage_url"),
             data.get("thumb_url"), raw,
             channel_id, now, vid),
        )
        if data.get("platform_tags") is not None:
            set_platform_tags(conn, vid, platform,
                              data.get("platform_tags"))
        return vid, False

    cur = conn.execute(
        """INSERT INTO videos (key, platform, remote_id, channel_id, title,
                               description, uploaded_at, duration_s, view_count,
                               like_count, comment_count, category, webpage_url,
                               thumb_url, raw_json, origin, status,
                               first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, platform, remote_id, channel_id, data.get("title"),
         data.get("description"), data.get("uploaded_at"), data.get("duration_s"),
         data.get("view_count"), data.get("like_count"),
         data.get("comment_count"), data.get("category"),
         data.get("webpage_url"), data.get("thumb_url"), data.get("raw_json"),
         data.get("origin") or "yt-dlp", "known", now, now),
    )
    vid = int(cur.lastrowid)
    if data.get("platform_tags") is not None:
        set_platform_tags(conn, vid, platform, data.get("platform_tags"))
    return vid, True


def set_platform_tags(conn: sqlite3.Connection, video_id: int,
                      platform: str, tags) -> None:
    """Связать видео с тегами ПЛОЩАДКИ (полная замена набора).

    Справочник tags пополняется при необходимости (platform+slug
    уникальны); slug = строчная нормализация (для юникода сохраняем
    буквы, прочее - в дефис) - он же ключ фильтра. Связи заменяются
    целиком: синк площадки - авторитетное состояние тегов строки.
    """
    cleaned: list[tuple[str, str]] = []
    for raw in (tags or [])[:50]:
        name = str(raw or "").strip()[:80]
        if not name:
            continue
        slug = tag_slug(name)
        if slug and (slug, name) not in cleaned:
            cleaned.append((slug, name))
    conn.execute("DELETE FROM video_tags WHERE video_id=?", (int(video_id),))
    for slug, name in cleaned:
        row = conn.execute(
            "SELECT id FROM tags WHERE platform=? AND slug=?",
            (platform, slug)).fetchone()
        if row:
            tag_id = int(row["id"])
        else:
            tag_id = int(conn.execute(
                "INSERT INTO tags (platform, slug, name) VALUES (?,?,?)",
                (platform, slug, name)).lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?,?)",
            (int(video_id), tag_id))


def tag_slug(name) -> str:
    """Ключ тега: строчная нормализация (буквы/цифры, прочее - дефис)."""
    return re.sub(r"[^0-9a-zа-яё]+", "-", str(name or "").lower()).strip("-")


def tags_popular(conn: sqlite3.Connection, platform: str | None = None,
                 limit: int = 40) -> list[dict]:
    """Теги счётчиками для чипсов фильтра: популярные первыми.

    Счёт - связанные строки: пустые теги (никто не носит) не показываем.
    """
    where = "WHERE t.platform=?" if platform else ""
    args = ([platform] if platform else []) + [int(limit)]
    return [dict(row) for row in conn.execute(
        f"""SELECT t.slug, t.name, t.platform,
                   COUNT(vt.video_id) AS n
              FROM tags t
              JOIN video_tags vt ON vt.tag_id = t.id
              {where}
             GROUP BY t.id
             HAVING n > 0
             ORDER BY n DESC, t.name COLLATE NOCASE
             LIMIT ?""", args)]


def ensure_playlist(conn: sqlite3.Connection, data: dict) -> int:
    """Плейлист (источник) по (platform, remote_id): создать или освежить.

    sync_mode в снапшоте может отсутствовать (обычный ресинк не спрашивает
    про режим): тогда при создании берём дефолт, а у существующего плейлиста
    режим НЕ сбивается - его выбирает пользователь, а не площадка.
    """
    platform = data.get("platform") or PLATFORM
    remote_id = (data.get("remote_id") or "").strip()
    if not remote_id:
        raise ValueError("playlist: нет remote_id")
    sync_mode = data.get("sync_mode")
    if sync_mode is not None and sync_mode not in SYNC_MODES:
        raise ValueError(f"playlist: неизвестный режим {sync_mode!r}")
    now = now_iso()
    channel_id = upsert_channel(conn, data.get("channel"))
    row = conn.execute(
        "SELECT id FROM playlists WHERE platform=? AND remote_id=?",
        (platform, remote_id)).fetchone()
    if row:
        conn.execute(
            """UPDATE playlists SET title=coalesce(?, title),
                 description=coalesce(?, description),
                 kind=coalesce(?, kind), url=coalesce(?, url),
                 item_count=coalesce(?, item_count),
                 raw_json=coalesce(?, raw_json),
                 channel_id=coalesce(?, channel_id),
                 sync_mode=coalesce(?, sync_mode),
                 last_synced_at=?, updated_at=?
               WHERE id=?""",
            (data.get("title"), data.get("description"), data.get("kind"),
             data.get("url"), data.get("item_count"), data.get("raw_json"),
             channel_id, sync_mode, now, now, row["id"]))
        return int(row["id"])
    cur = conn.execute(
        """INSERT INTO playlists (platform, remote_id, title, description, kind,
                                  url, item_count, sync_mode, raw_json,
                                  channel_id, created_at, updated_at, last_synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (platform, remote_id, data.get("title"), data.get("description"),
         data.get("kind") or "remote", data.get("url"), data.get("item_count"),
         sync_mode or "partial", data.get("raw_json"), channel_id,
         now, now, now),
    )
    return int(cur.lastrowid)


def link_item(conn: sqlite3.Connection, playlist_id: int, video_id: int,
              position: int, title: str | None = None,
              removed_at: str | None = None) -> None:
    """Связь «видео в плейлисте»: позиция и флаг присутствия в снапшоте.

    ON CONFLICT нужен для идемпотентности: повторный синк и повторный
    запуск после сбоя не создают дублей связей.
    """
    conn.execute(
        """INSERT INTO playlist_items (playlist_id, video_id, position,
                                       added_at, removed_at, snapshot_title)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT (playlist_id, video_id) DO UPDATE SET
               position=excluded.position,
               removed_at=excluded.removed_at,
               snapshot_title=coalesce(excluded.snapshot_title, snapshot_title)""",
        (playlist_id, video_id, position, now_iso(), removed_at, title))


def record_file(conn: sqlite3.Connection, video_id: int, path: str, kind: str,
                size: int | None = None, mtime: float | None = None,
                digest: str | None = None, format_json: str | None = None,
                storages_list: list[dict] | None = None) -> None:
    """Файл на диске -> строка files (сразу с флагом «существует»).

    Здесь же проставляется каноническая привязка: storage_id + rel_path.
    Скан передаёт свой кэш хранилищ (storages_list), чтобы на десятках тысяч
    файлов не вычитывать таблицу заново; без кэша - один запрос на вызов.

    status='detached' в списке восстанавливаемых - не случайно: вернувшаяся
    из отвязанной папки копия снова делает видео «скачанным».
    """
    if storages_list is None:
        storages_list = storages_mod.all_storages(conn)
    storage, rel = storages_mod.split_path(storages_list, path)
    conn.execute(
        """INSERT INTO files (video_id, kind, path, size, hash, mtime, missing,
                              storage_id, rel_path, format_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (path) DO UPDATE SET
               video_id=excluded.video_id, kind=excluded.kind,
               size=excluded.size, hash=coalesce(excluded.hash, files.hash),
               mtime=excluded.mtime, missing=0,
               storage_id=excluded.storage_id,
               rel_path=excluded.rel_path,
               format_json=coalesce(excluded.format_json, files.format_json)""",
        (video_id, kind, path, size, digest, mtime, 0,
         storage["id"] if storage else None, rel if storage else None,
         format_json))
    if kind == "video":
        conn.execute(
            """UPDATE videos SET status='downloaded', downloaded_at=coalesce(downloaded_at, ?)
               WHERE id=? AND status IN ('known','queued','downloading',
                                         'failed','missing','detached')""",
            (now_iso(), video_id))


def set_last_error(conn: sqlite3.Connection, video_id: int,
                   message: str | None) -> None:
    """Причина падения - в самой строке, а не только в журнале.

    Очередь показывает её списком: «нет хранилища: Внешний 4 ТБ не
    подключён» читается понятнее, чем общий «ошибка загрузки».
    """
    text = (str(message).strip()[:500] if message else None) or None
    conn.execute("UPDATE videos SET last_error=?, updated_at=? WHERE id=?",
                 (text, now_iso(), video_id))


def set_status(conn: sqlite3.Connection, video_ids, status: str) -> int:
    """Массовая смена статуса (очередь, ошибки, пропажа файла)."""
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус {status!r}")
    changed = 0
    now = now_iso()
    for chunk in _chunks([int(v) for v in video_ids]):
        marks = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE videos SET status=?, updated_at=? WHERE id IN ({marks})",
            [status, now, *chunk])
        changed += cur.rowcount
    return changed


def enqueue(conn: sqlite3.Connection, video_ids,
            storage_id: str | None = None) -> int:
    """Поставить в очередь: только то, что ещё не скачано и не качается.

    storage_id - куда качать. Цель записывается в строку, потому что фоновый
    воркер не может спрашивать пользователя: выбор делается в момент
    постановки (панель выделения, настройка канала, глобальное хранилище).
    None - цель остаётся прежней, а на старте загрузки её достроит
    storages.resolve_target.
    """
    changed = 0
    now = now_iso()
    for chunk in _chunks([int(v) for v in video_ids]):
        marks = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"""UPDATE videos SET status='queued', updated_at=?,
                       target_storage_id=coalesce(?, target_storage_id)
                WHERE id IN ({marks})
                  AND status NOT IN ('downloaded','downloading','unavailable')""",
            [now, storage_id, *chunk])
        changed += cur.rowcount
    return changed


# --------------------------------------------------------------------------- #
#  План добавления (только чтение)
# --------------------------------------------------------------------------- #

def plan_diff(conn: sqlite3.Connection, snapshot: dict) -> dict:
    """Что изменится в БД, если принять снапшот плейлиста.

    Ничего не пишет: результат - аргумент диалога «Точно добавить?» и
    одновременно инструкция для commit_plan. Счётчики здесь честные,
    поэтому отмена пользователя бесплатна.
    """
    entries = [e for e in (snapshot.get("entries") or []) if isinstance(e, dict)]
    playlist = dict(snapshot.get("playlist") or {})
    # Платформа снапшота (PH1): youtube - дефолт старых снапшотов.
    platform = str(playlist.get("platform") or PLATFORM)

    # 1. Что уже есть в индексе: видео одним запросом, каналы - вторым.
    remote_ids = [e["remote_id"] for e in entries if e.get("remote_id")]
    known_ids: set[str] = set()
    for chunk in _chunks(remote_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, key FROM videos WHERE key IN ({marks})",
            [video_key(platform, r) for r in chunk]).fetchall()
        known_ids.update(r["key"] for r in rows)

    channel_refs: dict[str, dict] = {}
    for entry in entries:
        ref = entry.get("channel")
        if isinstance(ref, dict) and ref.get("remote_id"):
            channel_refs.setdefault(ref["remote_id"], ref)
    known_channels: set[str] = set()
    for chunk in _chunks(list(channel_refs)):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT remote_id FROM channels WHERE platform=? AND remote_id IN ({marks})",
            [platform, *chunk]).fetchall()
        known_channels.update(r["remote_id"] for r in rows)

    # 2. Плейлист и его текущие связи.
    row = conn.execute(
        "SELECT id, sync_mode FROM playlists WHERE platform=? AND remote_id=?",
        (platform, playlist.get("remote_id"))).fetchone()
    playlist_row = dict(row) if row else None
    linked: set[str] = set()
    if playlist_row:
        rows = conn.execute(
            """SELECT v.key FROM playlist_items pi
                 JOIN videos v ON v.id = pi.video_id
                WHERE pi.playlist_id=? AND pi.removed_at IS NULL""",
            (playlist_row["id"],)).fetchall()
        linked.update(r["key"] for r in rows)

    # 3. Разбор записей: дубли внутри снапшота и «дырки» приватного/удалённого.
    seen: set[str] = set()
    new_videos, known_videos, skipped, dupes = [], [], 0, 0
    for entry in entries:
        remote_id = entry.get("remote_id")
        if entry.get("unavailable") or not remote_id:
            skipped += 1
            continue
        key = video_key(platform, remote_id)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        if key in known_ids:
            known_videos.append({"entry": entry, "key": key,
                                 "linked": key in linked})
        else:
            new_videos.append({"entry": entry, "key": key})

    links_to_create = sum(1 for v in known_videos if not v["linked"]) + len(new_videos)
    plan = {
        "playlist": {
            "remote_id": playlist.get("remote_id"),
            "title": playlist.get("title"),
            "kind": playlist.get("kind"),
            "url": playlist.get("url"),
            "exists": playlist_row is not None,
            "id": playlist_row["id"] if playlist_row else None,
            "sync_mode": (playlist_row or {}).get("sync_mode")
                         or playlist.get("sync_mode") or "partial",
        },
        "counts": {
            "playlist": 1,
            "new_channels": len([r for r in channel_refs if r not in known_channels]),
            "known_channels": len([r for r in channel_refs if r in known_channels]),
            "new_videos": len(new_videos),
            "known_videos": len(known_videos),
            "links_to_create": links_to_create,
            "links_existing": len([v for v in known_videos if v["linked"]]),
            "skipped": skipped,
            "dupes": dupes,
        },
        "new_videos": new_videos,
        "known_videos": known_videos,
        "new_channel_refs": [r for k, r in channel_refs.items() if k not in known_channels],
        "is_mix": playlist.get("kind") == "mix",
        "entries_total": len(entries),
    }
    return plan


def commit_plan(conn: sqlite3.Connection, snapshot: dict, plan: dict,
                on_stage=None, storage_id: str | None = None,
                account_id: str | None = None) -> dict:
    """Принять план: плейлист -> авторы -> видео -> связи. Одна транзакция.

    on_stage(stage, state, current, total) вызывается по ходу - отсюда UI
    берёт чек-лист стадий. Исключение (в том числе отмена) откатывает всё
    до последней строки: частичных состояний не остаётся.

    storage_id - куда качать этот источник (выбор пользователя в диалоге
    добавления), account_id - чьи куки использовать при синке и загрузке
    (привязка аккаунта к источнику). Оба пишутся на плейлист В ТОЙ ЖЕ
    транзакции, что и само добавление: отмена не должна оставить за собой
    ни выбора папки, ни привязки аккаунта.
    """
    def stage(name, state, current=0, total=0):
        if on_stage:
            on_stage(name, state, current, total)

    stages = ("playlist", "channels", "videos", "links")
    for name in stages:
        stage(name, "wait")
    stats = dict(plan["counts"])
    platform = str(snapshot["playlist"].get("platform") or PLATFORM)

    # Явный BEGIN/COMMIT: соединения работают в autocommit (иначе запись
    # воркера висела бы незакоммиченной и держала лок файла базы).
    conn.execute("BEGIN")
    try:
        # --- стадия 1: плейлист -------------------------------------------
        stage("playlist", "active", 0, 1)
        playlist_id = ensure_playlist(conn, snapshot["playlist"])
        if storage_id:
            # «Куда качать этот источник»: resolve_target в очереди читает
            # playlists.storage_id первым (см. storages.resolve_target).
            conn.execute(
                "UPDATE playlists SET storage_id=?, updated_at=? WHERE id=?",
                (storage_id, now_iso(), playlist_id))
        if account_id:
            # «Чьи куки у этого источника»: синк и качалка читают аккаунт
            # через цепочку playlist_items -> playlists.account_id.
            conn.execute(
                "UPDATE playlists SET account_id=?, updated_at=? WHERE id=?",
                (account_id, now_iso(), playlist_id))
        stage("playlist", "done", 1, 1)

        # --- стадия 2: авторы ---------------------------------------------
        refs = list({r["remote_id"]: r for r in plan["new_channel_refs"]}.values())
        # заодно освежаем названия уже известных каналов
        for entry in (snapshot.get("entries") or []):
            ref = entry.get("channel") if isinstance(entry, dict) else None
            if isinstance(ref, dict) and ref.get("remote_id"):
                refs.append(ref)
        refs = list({r["remote_id"]: r for r in refs}.values())
        stage("channels", "active", 0, len(refs))
        for index, ref in enumerate(refs, start=1):
            upsert_channel(conn, ref)
            stage("channels", "active" if index < len(refs) else "done", index, len(refs))

        # --- стадия 3: видео ----------------------------------------------
        entries = [e for e in (snapshot.get("entries") or []) if isinstance(e, dict)
                   and e.get("remote_id") and not e.get("unavailable")]
        todo = [{"entry": e["entry"], "key": e["key"]} for e in plan["new_videos"]]
        todo += [{"entry": e["entry"], "key": e["key"]} for e in plan["known_videos"]]
        stage("videos", "active", 0, len(todo))
        ids_by_key: dict[str, int] = {}
        for index, item in enumerate(todo, start=1):
            entry = item["entry"]
            data = _entry_to_video(entry, platform)
            vid, _created = upsert_video(conn, data, full=False)
            if vid:
                ids_by_key[item["key"]] = vid
            if index % 50 == 0 or index == len(todo):
                stage("videos", "active" if index < len(todo) else "done",
                      index, len(todo))

        # «Когда данные приходили с площадки» - отдельно от updated_at:
        # тот меняется и от скачивания, и от правок, а synced_at честно
        # отвечает только на вопрос «когда синк освежил эту строку».
        synced_at = now_iso()
        synced_ids = list(ids_by_key.values())
        for start in range(0, len(synced_ids), 400):
            chunk = synced_ids[start:start + 400]
            conn.execute(
                f"UPDATE videos SET synced_at=? "
                f"WHERE id IN ({','.join('?' * len(chunk))})",
                [synced_at, *chunk])
        # Строка, помеченная «удалено на площадке», снова отдалась площадкой
        # (синк её освежил) - метка снимается: скачанное остаётся
        # «скачанным», остальное снова «в индексе». Ничего не перекачиваем.
        if synced_ids:
            marks = ",".join("?" * len(synced_ids))
            conn.execute(
                f"""UPDATE videos SET status = CASE WHEN EXISTS(
                        SELECT 1 FROM files f WHERE f.video_id = videos.id
                             AND f.kind='video' AND f.missing=0)
                    THEN 'downloaded' ELSE 'known' END
                  WHERE status='unavailable' AND id IN ({marks})""",
                synced_ids)

        # --- стадия 4: связи ----------------------------------------------
        stage("links", "active", 0, len(entries))
        gone = now_iso()
        for index, entry in enumerate(entries, start=1):
            key = video_key(platform, entry["remote_id"])
            vid = ids_by_key.get(key)
            if vid is None:
                row = conn.execute("SELECT id FROM videos WHERE key=?", (key,)).fetchone()
                vid = int(row["id"]) if row else None
            if vid:
                link_item(conn, playlist_id, vid, entry.get("position") or index,
                          entry.get("title"), None)
            if index % 50 == 0 or index == len(entries):
                stage("links", "active" if index < len(entries) else "done",
                      index, len(entries))

        # Позиции, которых больше нет в снапшоте: помечаем убывшими.
        # Файлы и записи видео не трогаем - исчезнувшее из плейлиста
        # не значит «удалить у меня».
        #
        # Дырки (недоступные без входа: возрастное, приватное - в полном
        # режиме такие реальны) приходят БЕЗ remote_id: отличить «этот
        # ролик выпал» от «его просто не смогли прочитать» нельзя, а
        # гадать нельзя тем более - поэтому при наличии дырок сверку
        # пропускаем (removed=0), а не помечаем ложных убывших.
        raw_entries = [e for e in (snapshot.get("entries") or [])
                       if isinstance(e, dict)]
        holes = sum(1 for e in raw_entries
                    if e.get("unavailable") or not e.get("remote_id"))
        removed = 0
        if not holes:
            current = {video_key(platform, e["remote_id"]) for e in entries}
            rows = conn.execute(
                """SELECT pi.id, v.key FROM playlist_items pi
                     JOIN videos v ON v.id = pi.video_id
                    WHERE pi.playlist_id=? AND pi.removed_at IS NULL""",
                (playlist_id,)).fetchall()
            for row in rows:
                if row["key"] not in current:
                    conn.execute(
                        "UPDATE playlist_items SET removed_at=? WHERE id=?",
                        (gone, row["id"]))
                    removed += 1
        stats["removed"] = removed
        stats["holes"] = holes
        stats["playlist_id"] = playlist_id
        stats["total"] = stats["new_videos"] + stats["known_videos"]
        # Итоги стадий - для финального чек-листа: каждый «done» должен
        # приходить со своими числами, иначе последний вызов обнулил бы
        # счётчики, которые окно показывает как готовые.
        totals = {"playlist": 1, "channels": len(refs), "videos": len(todo),
                  "links": len(entries)}
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    for name in stages:
        stage(name, "done", totals[name], totals[name])
    return stats


def _entry_to_video(entry: dict, platform: str = PLATFORM) -> dict:
    """Запись снапшота -> аргумент upsert_video.

    raw_json здесь НЕ None: upsert с full=False сохраняет старую богатую
    карточку приоритетнее (old_raw or new_raw), а той, где raw_json ещё
    не было, - дописывает приходящий. Быстрый синк кладёт тонкую запись,
    полный - полную.
    """
    return {
        "platform": platform,
        "remote_id": entry.get("remote_id"),
        "key": video_key(platform, entry.get("remote_id")),
        "platform_tags": entry.get("platform_tags"),
        "title": entry.get("title"),
        "description": entry.get("description"),
        "uploaded_at": entry.get("uploaded_at"),
        "duration_s": entry.get("duration_s"),
        "view_count": entry.get("view_count"),
        "like_count": entry.get("like_count"),
        "comment_count": entry.get("comment_count"),
        "category": entry.get("category"),
        "webpage_url": entry.get("webpage_url"),
        "thumb_url": entry.get("thumb"),
        "raw_json": entry.get("raw"),
        "origin": "yt-dlp",
        "channel": entry.get("channel"),
    }


def start_run(conn: sqlite3.Connection, kind: str, source_id: int | None = None) -> int:
    """Открыть запись журнала запусков."""
    cur = conn.execute("INSERT INTO runs (kind, source_id, started_at) VALUES (?,?,?)",
                       (kind, source_id, now_iso()))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, stats: dict) -> None:
    """Закрыть запуск итогами (тот JSON, что показывает журнал)."""
    conn.execute("UPDATE runs SET finished_at=?, stats_json=? WHERE id=?",
                 (now_iso(), json.dumps(stats, ensure_ascii=False), run_id))


# --------------------------------------------------------------------------- #
#  Чтение для интерфейса
# --------------------------------------------------------------------------- #

def fts_query(text: str) -> str:
    """Пользовательский запрос -> безопасный для FTS5.

    Каждое слово в кавычки: иначе символы вроде * или : в названии
    превращались бы в синтаксис FTS и валяли бы поиск.
    """
    words = [w.replace('"', '""') for w in str(text or "").split() if w]
    return " ".join(f'"{w}"' for w in words)


def stats(conn: sqlite3.Connection) -> dict:
    """Счётчики для шапки и вкладок одним проходом."""
    by_status = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status, COUNT(*) n FROM videos GROUP BY status")}
    total = sum(by_status.values())
    playlists = conn.execute("SELECT COUNT(*) n FROM playlists").fetchone()["n"]
    channels = conn.execute(
        "SELECT COUNT(*) n FROM channels c WHERE EXISTS "
        "(SELECT 1 FROM videos v WHERE v.channel_id=c.id)").fetchone()["n"]
    files = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes FROM files "
        "WHERE kind='video' AND missing=0").fetchone()
    return {
        "total": total,
        "by_status": by_status,
        "downloaded": by_status.get("downloaded", 0),
        "queued": by_status.get("queued", 0),
        "playlists": playlists,
        "channels": channels,
        "files": files["n"],
        "bytes": files["bytes"],
    }


def tree(conn: sqlite3.Connection) -> dict:
    """Дерево «Пул / Каналы / Плейлисты» - виртуальные узлы над videos."""
    pool = stats(conn)
    channels = [dict(row) for row in conn.execute(
        """SELECT c.id, c.title, c.remote_id,
                  COUNT(v.id) total,
                  SUM(CASE WHEN v.status='downloaded' THEN 1 ELSE 0 END) downloaded
             FROM channels c
             JOIN videos v ON v.channel_id = c.id
            GROUP BY c.id
            ORDER BY total DESC, c.title COLLATE NOCASE
            LIMIT 500""")]
    playlists = [dict(row) for row in conn.execute(
        """SELECT p.id, p.title, p.kind, p.sync_mode, p.last_synced_at,
                  p.item_count, p.remote_id,
                  SUM(CASE WHEN pi.removed_at IS NULL THEN 1 ELSE 0 END) total,
                  SUM(CASE WHEN pi.removed_at IS NULL AND v.status='downloaded'
                           THEN 1 ELSE 0 END) downloaded
             FROM playlists p
             LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
             LEFT JOIN videos v ON v.id = pi.video_id
            GROUP BY p.id
            ORDER BY p.title COLLATE NOCASE""")]
    return {"pool": pool, "channels": channels, "playlists": playlists}


# Причина «файл битый»: очередь узнаёт по ней, что файл нужно скачать
# заново с перезаписью, а не пропустить как уже скачанный.
CHECKSUM_PREFIX = "контрольная сумма"


def normalize_tags(text) -> str | None:
    """«рок, живой,, 4К» -> «рок, живой, 4К».

    Теги идут в FTS5 (ищутся поиском), поэтому: без пустых, без дублей
    без учёта регистра, с ограничением на длину и количество - иначе
    одна строка могла бы раздуть индекс.
    """
    if text is None:
        return None
    parts: list[str] = []
    seen: set[str] = set()
    for chunk in str(text).replace("\n", ",").split(","):
        tag = chunk.strip()[:48]
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        parts.append(tag)
        if len(parts) >= 40:
            break
    return ", ".join(parts) or None


def save_fields(conn, ids, fields: dict) -> int:
    """Записать ЛОКАЛЬНЫЕ поля: теги, рейтинг, заметка, «просмотрено».

    Whitelist обязателен: значения приходят из окна, а статус, хранилище
    и метаданные площадки менять оттуда нельзя - это состояние диска и
    источника, а не оценка пользователя. updated_at не трогаем: очередь
    сортуется по нему, и правка тега не должна менять порядок загрузки.
    """
    allowed: dict = {}
    if "user_tags" in fields:
        allowed["user_tags"] = normalize_tags(fields.get("user_tags"))
    if "user_rating" in fields:
        try:
            allowed["user_rating"] = max(0, min(5, int(fields.get("user_rating") or 0)))
        except (TypeError, ValueError):
            allowed["user_rating"] = 0
    if "notes" in fields:
        allowed["notes"] = str(fields.get("notes") or "").strip()[:2000] or None
    if "watched" in fields:
        allowed["watched_at"] = now_iso() if fields.get("watched") else None
    if not allowed or not ids:
        return 0

    columns = ", ".join(f"{key}=?" for key in allowed)
    marks = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"UPDATE videos SET {columns} WHERE id IN ({marks})",
        [*allowed.values(), *[int(i) for i in ids]])
    return int(cursor.rowcount)


# Сортировка списка: ключ из запроса окна -> выражение SQL. Только эти
# ключи умеет list_videos (белый список против SQL-инъекций); "size" -
# alias вычисляемого подзапроса, он же разрешён SQLite в ORDER BY.
ORDER_COLS: dict[str, str] = {
    "status": "v.status",
    # nlow() - юникод-casefold (зарегистрирован в db.Database.conn):
    # COLLATE NOCASE в SQLite знает только ASCII.
    "title": "nlow(v.title)",
    "channel": "nlow(c.title)",
    "duration": "v.duration_s",
    "date": "v.uploaded_at",
    "size": "size",
    "views": "v.view_count",
    "likes": "v.like_count",
    "comments": "v.comment_count",
    "sync": "v.synced_at",
    # Сортировка ПО ТЕГУ (PH3): алфавит первого тега строки; у строк
    # без тегов - NULL (последними в обе стороны).
    "tag": ("(SELECT MIN(t.name) FROM video_tags vt JOIN tags t "
            "ON t.id=vt.tag_id WHERE vt.video_id=v.id)"),
}


def list_videos(conn: sqlite3.Connection, *, scope: dict | None = None,
                status: str | None = None, query: str = "",
                rating_min: int | None = None, order: dict | None = None,
                tag: str = "", platform: str = "",
                offset: int = 0, limit: int = 200) -> dict:
    """Строки таблицы библиотеки: (total, rows) для виртуализированного списка.

    scope: {"type": "pool"|"channel"|"playlist", "id": int}.
    Поиск - по FTS5 (название, описание, локальные теги), фильтры - по
    статусу, рейтингу, ТЕГУ ПЛОЩАДКИ (slug из video_tags) и ПЛОЩАДКЕ
    (platform: вкладка 18+ показывает только pornhub), страница -
    offset/limit.
    order: {"col": <ключ из ORDER_COLS>, "dir": "asc"|"desc"} - сортировка
    по белому списку: ключ из запроса окна попадает в SQL только через
    этот словарь, произвольный текст отвергается молча (дефолтная дата).
    """
    scope = scope or {"type": "pool"}
    joins, where, args = [], [], []
    if platform:
        where.append("v.platform=?")
        args.append(str(platform))
    if scope.get("type") == "channel":
        where.append("v.channel_id=?")
        args.append(scope.get("id"))
    elif scope.get("type") == "playlist":
        joins.append("JOIN playlist_items pi ON pi.video_id=v.id "
                     "AND pi.playlist_id=? AND pi.removed_at IS NULL")
        args.append(scope.get("id"))
    if status:
        where.append("v.status=?")
        args.append(status)
    if rating_min:
        where.append("COALESCE(v.user_rating, 0) >= ?")
        args.append(int(rating_min))
    tag = tag_slug(tag) if tag else ""
    if tag:
        # Тег площадки: подзапрос по связям (slug нормализуется тем же
        # правилом, что писал тег).
        where.append("v.id IN (SELECT vt.video_id FROM video_tags vt "
                     "JOIN tags t ON t.id=vt.tag_id WHERE t.slug=?)")
        args.append(tag)
    query = (query or "").strip()
    if query:
        fts = fts_query(query)
        if fts:
            # Поиск задевает и ТЕГИ ПЛОЩАДКИ (PH4): справочник маленький,
            # LIKE по нему дёшев, а FTS-таблицу пересобирать ради колонки
            # нельзя (внешний content + триггеры - только полная реконструкция).
            # Функционально строка поиска ищет по названию/описанию/локальным
            # тегам (FTS) и по жанрам площадки (связи video_tags).
            where.append("(v.id IN (SELECT rowid FROM videos_fts "
                         "WHERE videos_fts MATCH ?) "
                         "OR v.id IN (SELECT vt.video_id FROM video_tags vt "
                         "JOIN tags t ON t.id=vt.tag_id "
                         "WHERE t.name LIKE ? OR t.slug LIKE ?))")
            needle = "%" + query.lower() + "%"
            args.extend([fts, needle, needle])

    join_sql = " ".join(joins)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) n FROM videos v {join_sql} {where_sql}", args).fetchone()["n"]

    # Сортировка по белому списку; NULL - последним в обе стороны, чтобы
    # пустые даты/счётчики не занимали первую строку по возрастанию.
    order_sql = "v.uploaded_at DESC NULLS LAST, v.id DESC"
    direction = "DESC" if str((order or {}).get("dir") or "").lower() == "desc" else "ASC"
    col = str((order or {}).get("col") or "")
    if col in ORDER_COLS:
        order_sql = f"{ORDER_COLS[col]} {direction} NULLS LAST, v.id DESC"
    rows = [dict(row) for row in conn.execute(
        f"""SELECT v.id, v.key, v.title, v.status, v.duration_s, v.uploaded_at,
                   v.origin, v.user_rating, v.user_tags, v.watched_at,
                   v.view_count, v.like_count, v.comment_count, v.synced_at,
                   c.title AS channel,
                   (SELECT SUM(size) FROM files f
                     WHERE f.video_id=v.id AND f.kind='video' AND f.missing=0) AS size
              FROM videos v
              LEFT JOIN channels c ON c.id = v.channel_id
              {join_sql} {where_sql}
             ORDER BY {order_sql}
             LIMIT ? OFFSET ?""",
        [*args, int(limit), int(offset)])]
    for row in rows:
        row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])

    # Теги страницы одним запросом (PH4: колонка сетки «Теги»).
    ids = [row["id"] for row in rows]
    tags_by_video: dict[int, list] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for tag_row in conn.execute(
                f"""SELECT vt.video_id, t.name FROM video_tags vt
                      JOIN tags t ON t.id=vt.tag_id
                     WHERE vt.video_id IN ({marks})
                  ORDER BY t.name COLLATE NOCASE""", ids):
            tags_by_video.setdefault(int(tag_row["video_id"]),
                                     []).append(tag_row["name"])
    for row in rows:
        row["platform_tags"] = tags_by_video.get(row["id"], [])

    # Есть ли локальная обложка - одним запросом на страницу: коррелированный
    # подзапрос на каждую строку стоил бы столько же, сколько вся выборка,
    # а плитке нужен только флаг (саму картинку отдаёт get_thumbs).
    thumbs: set[int] = set()
    ids = [row["id"] for row in rows]
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        thumbs.update(
            r["video_id"] for r in conn.execute(
                f"""SELECT DISTINCT video_id FROM files
                     WHERE kind='thumbnail' AND missing=0
                       AND video_id IN ({marks})""", chunk))
    for row in rows:
        row["has_thumb"] = row["id"] in thumbs
    return {"total": int(total), "rows": rows}


def video_detail(conn: sqlite3.Connection, video_id: int) -> dict | None:
    """Карточка видео: колонки + файлы + плейлисты + разобранный raw_json."""
    row = conn.execute(
        """SELECT v.*, c.title AS channel, c.remote_id AS channel_remote_id
             FROM videos v LEFT JOIN channels c ON c.id=v.channel_id
            WHERE v.id=?""", (video_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["status_label"] = STATUS_LABELS.get(data["status"], data["status"])
    data["files"] = [dict(f) for f in conn.execute(
        "SELECT kind, path, size, hash, mtime, missing, format_json "
        "FROM files WHERE video_id=? ORDER BY kind", (video_id,))]
    data["playlists"] = [dict(p) for p in conn.execute(
        """SELECT p.id, p.title, p.kind, pi.position, pi.removed_at
             FROM playlist_items pi JOIN playlists p ON p.id=pi.playlist_id
            WHERE pi.video_id=? ORDER BY p.title""", (video_id,))]
    try:
        data["raw"] = json.loads(data.get("raw_json") or "{}")
    except json.JSONDecodeError:
        data["raw"] = {}
    return data


def mesh_file(conn: sqlite3.Connection, video_id: int) -> dict | None:
    """Файл-копия, пришедшая из mesh (провенанс source=mesh в format_json).

    Даёт карточке ровно то, что ей нужно увидеть: путь, хеш и подпись
    происхождения. Скачанный с площадки файл возвращает None - отзыв
    «несовпадение» относится только к mesh-копиям.
    """
    for row in conn.execute(
            "SELECT path, hash, missing, format_json FROM files "
            "WHERE video_id=? AND kind='video'", (video_id,)):
        try:
            meta = json.loads(row["format_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(meta, dict) and meta.get("source") == "mesh":
            return {"path": row["path"], "hash": row["hash"],
                    "missing": int(row["missing"] or 0), "meta": meta}
    return None


# --------------------------------------------------------------------------- #
#  Mesh: хранилище доверия устройствам (B0)
# --------------------------------------------------------------------------- #

TRUST_STATUSES = ("tofu", "verified", "revoked")


def _trust_key(pubkey) -> str:
    """Валидация ключа: 64 символа hex - иначе в таблицу не пишем."""
    key = str(pubkey or "").strip().lower()
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("ключ устройства - 64 символа hex "
                         "(отпечаток берите в карточке участника)")
    return key


def _trust_row(row) -> dict:
    data = dict(row)
    data["status"] = ("revoked" if data.get("revoked_at")
                      else "verified" if data.get("verified_at")
                      else "tofu")
    return data


def truststore_list(conn: sqlite3.Connection) -> list[dict]:
    """Все устройства: подтверждённые и живые первыми, отозванные - в конце.

    Пустой label не вылезает вверх - сортируем названия по nlow (юникод-
    регистронезависимо, как и весь остальной список).
    """
    rows = [dict(row) for row in conn.execute(
        "SELECT pubkey, label, verified_at, verified_via, revoked_at, "
        "created_at, last_addr FROM truststore "
        "ORDER BY revoked_at IS NOT NULL, label = '', nlow(label), pubkey")]
    return [_trust_row(row) for row in rows]


def truststore_get(conn: sqlite3.Connection, pubkey) -> dict | None:
    row = conn.execute("SELECT * FROM truststore WHERE pubkey=?",
                       (_trust_key(pubkey),)).fetchone()
    return _trust_row(row) if row else None


def truststore_add(conn: sqlite3.Connection, pubkey, *, label: str = "",
                   addr: str = "", via: str = "tofu") -> dict:
    """Запомнить устройство (первый контакт); свежий адрес - всегда.

    INSERT OR IGNORE: повторный контакт НЕ переписывает решения человека -
    подтверждение и отзыв живут до ручного изменения.
    """
    key = _trust_key(pubkey)
    if via not in ("tofu", "manual"):
        raise ValueError(f"неизвестный способ проверки: {via!r}")
    conn.execute(
        "INSERT OR IGNORE INTO truststore (pubkey, label, verified_via, "
        "created_at, last_addr) VALUES (?,?,?,?,?)",
        (key, str(label or ""), via, now_iso(), str(addr or "")))
    if addr:
        conn.execute("UPDATE truststore SET last_addr=? WHERE pubkey=?",
                     (str(addr), key))
    device = truststore_get(conn, key)
    assert device is not None            # строку только что завели
    return device


def truststore_verify(conn: sqlite3.Connection, pubkey,
                      label: str | None = None,
                      via: str = "manual") -> dict:
    """Человек подтвердил устройство - ключ становится verified.

    via: 'manual' (сверка отпечатка глазами) или 'sas' (сверка 12 слов,
    этап 1.1) - способ хранится, чтобы понимать, как подтвердили.
    """
    key = _trust_key(pubkey)
    if via not in ("manual", "sas"):
        raise ValueError(f"неизвестный способ подтверждения: {via!r}")
    if not conn.execute("SELECT 1 FROM truststore WHERE pubkey=?",
                        (key,)).fetchone():
        truststore_add(conn, key)
    if label is None:
        conn.execute("UPDATE truststore SET verified_at=?, "
                     "verified_via=?, revoked_at=NULL WHERE pubkey=?",
                     (now_iso(), via, key))
    else:
        conn.execute("UPDATE truststore SET verified_at=?, "
                     "verified_via=?, revoked_at=NULL, label=? "
                     "WHERE pubkey=?",
                     (now_iso(), via, str(label or "").strip()[:64], key))
    return truststore_get(conn, key)


def truststore_set_label(conn: sqlite3.Connection, pubkey,
                         label: str) -> dict:
    key = _trust_key(pubkey)
    if not conn.execute("SELECT 1 FROM truststore WHERE pubkey=?",
                        (key,)).fetchone():
        truststore_add(conn, key)
    conn.execute("UPDATE truststore SET label=? WHERE pubkey=?",
                 (str(label or "").strip()[:64], key))
    return truststore_get(conn, key)


def truststore_revoke(conn: sqlite3.Connection, pubkey) -> dict:
    """Отозвать доверие: манифесты и файлы этого ключа больше не принимаем."""
    key = _trust_key(pubkey)
    if not conn.execute("SELECT 1 FROM truststore WHERE pubkey=?",
                        (key,)).fetchone():
        truststore_add(conn, key)
    conn.execute("UPDATE truststore SET revoked_at=? WHERE pubkey=?",
                 (now_iso(), key))
    return truststore_get(conn, key)


def truststore_restore(conn: sqlite3.Connection, pubkey) -> dict:
    """Снять отзыв: устройство возвращается в TOFU (сверку повторит человек)."""
    key = _trust_key(pubkey)
    conn.execute("UPDATE truststore SET revoked_at=NULL, "
                 "verified_at=NULL, verified_via='tofu' WHERE pubkey=?",
                 (key,))
    device = truststore_get(conn, key)
    assert device is not None
    return device


def truststore_forget(conn: sqlite3.Connection, pubkey) -> bool:
    """Забыть устройство совсем (строка уходит; TOFU вернётся при новом
    контакте). Возвращает True, если строка была."""
    cursor = conn.execute("DELETE FROM truststore WHERE pubkey=?",
                          (_trust_key(pubkey),))
    return bool(cursor.rowcount)


def truststore_revoked(conn: sqlite3.Connection) -> set:
    """Множество отозванных ключей - горячий путь барьера в mesh.fetch."""
    return {row["pubkey"] for row in
            conn.execute("SELECT pubkey FROM truststore "
                         "WHERE revoked_at IS NOT NULL")}


def truststore_verified(conn: sqlite3.Connection) -> set:
    """Подтверждённые и не отозванные ключи - круг «друзей» (strict, 1.2).

    Только этот круг участвует в strict-режиме: манифесты/файлы от
    неподтверждённых устройств отбрасываются (см. mesh.fetch.restore).
    """
    return {row["pubkey"] for row in
            conn.execute("SELECT pubkey FROM truststore "
                         "WHERE verified_at IS NOT NULL "
                         "AND revoked_at IS NULL")}


def truststore_import_pins(conn: sqlite3.Connection, pins: dict) -> int:
    """Перенос TOFU-пинов «адрес -> ключ» в устройства (идемпотентно).

    mesh_pins.json живёт как транспортный кэш (адрес может меняться), но
    человеку нужен список устройств - завести его из уже запомненных
    пинов один раз и не трогать дальше: повторный импорт ничего не
    дублирует и не переписывает подтверждения/отзывы (INSERT OR IGNORE).
    """
    imported = 0
    for addr, key in dict(pins or {}).items():
        try:
            key = _trust_key(key)
        except ValueError:
            continue                        # мусор в файле не должен валить
        cursor = conn.execute(
            "INSERT OR IGNORE INTO truststore (pubkey, verified_via, "
            "created_at, last_addr) VALUES (?, 'tofu', ?, ?)",
            (key, now_iso(), str(addr or "")))
        imported += int(cursor.rowcount or 0)
    return imported


# --------------------------------------------------------------------------- #
#  Mesh: зеркало каталогов участников (B3, синхронизация индекса)
# --------------------------------------------------------------------------- #

def catalog_apply(conn: sqlite3.Connection, origin: str,
                  entries: list, addr: str = "") -> dict:
    """Применить подписанный снапшот участника: LWW + чистка ушедших.

    origin - ключ автора (его подпись уже проверена в mesh.client).
    Версия строки - updated_at САМОГО узла: чужие шкалы времени не
    сравниваются (иначе перевод часов одного ПК «побеждал» бы записи
    другого). Снимок авторитетен для своего origin: строк, которых в нём
    больше нет, в зеркале не остаётся - отдельные тамбстоуны не нужны.
    """
    origin = _trust_key(origin)
    now = now_iso()
    added = updated = unchanged = 0
    seen: set[str] = set()
    for entry in entries or []:
        remote_id = str(entry.get("remote_id") or "")
        if not remote_id or remote_id in seen:
            continue
        seen.add(remote_id)
        duration = entry.get("duration_s")
        title = str(entry.get("title") or "")[:500]
        row = conn.execute(
            "SELECT updated_at FROM mesh_catalog WHERE origin=? "
            "AND remote_id=?", (origin, remote_id)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO mesh_catalog (origin, remote_id, title, "
                "duration_s, sha256, synced_at, updated_at, addr, "
                "fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (origin, remote_id, title,
                 int(duration) if isinstance(duration, int) else None,
                 str(entry.get("sha256") or ""),
                 str(entry.get("synced_at") or ""),
                 str(entry.get("updated_at") or ""), str(addr or ""), now))
            added += 1
        elif str(entry.get("updated_at") or "") > str(row["updated_at"] or ""):
            conn.execute(
                "UPDATE mesh_catalog SET title=?, duration_s=?, sha256=?, "
                "synced_at=?, updated_at=?, addr=?, fetched_at=? "
                "WHERE origin=? AND remote_id=?",
                (title,
                 int(duration) if isinstance(duration, int) else None,
                 str(entry.get("sha256") or ""),
                 str(entry.get("synced_at") or ""),
                 str(entry.get("updated_at") or ""), str(addr or ""), now,
                 origin, remote_id))
            updated += 1
        else:
            # его же более старая версия - не качаем назад, но факт
            # «видели недавно» обновляем
            unchanged += 1
            conn.execute(
                "UPDATE mesh_catalog SET addr=?, fetched_at=? "
                "WHERE origin=? AND remote_id=?",
                (str(addr or ""), now, origin, remote_id))
    if seen:
        marks = ",".join("?" * len(seen))
        cursor = conn.execute(
            f"DELETE FROM mesh_catalog WHERE origin=? "
            f"AND remote_id NOT IN ({marks})", (origin, *sorted(seen)))
    else:
        cursor = conn.execute("DELETE FROM mesh_catalog WHERE origin=?",
                              (origin,))
    return {"origin": origin, "added": added, "updated": updated,
            "unchanged": unchanged, "removed": int(cursor.rowcount or 0)}


def catalog_list(conn: sqlite3.Connection, query: str = "",
                 limit: int = 200) -> list[dict]:
    """Зеркало для UI: автор (устройство/статус) и признак «есть у меня».

    Поиск - подстрока без регистра через nlow (кириллица: встроенный
    LIKE NOCASE её не понимает).
    """
    needle = str(query or "").strip()
    rows = conn.execute(
        """SELECT c.origin, c.remote_id, c.title, c.duration_s, c.sha256,
                  c.synced_at, c.updated_at, c.addr, c.fetched_at,
                  t.label, t.verified_at, t.revoked_at,
                  (SELECT COUNT(*) FROM videos v
                    WHERE v.remote_id = c.remote_id
                      AND v.platform <> 'local') AS own
             FROM mesh_catalog c
             LEFT JOIN truststore t ON t.pubkey = c.origin
            WHERE ? = '' OR instr(nlow(c.title), nlow(?)) > 0
               OR instr(nlow(c.remote_id), nlow(?)) > 0
            ORDER BY c.fetched_at DESC, c.title
            LIMIT ?""",
        (needle, needle, needle, int(limit)))
    return [dict(row) for row in rows]


def catalog_own(conn: sqlite3.Connection, remote_id: str) -> dict | None:
    """Наша строка по remote_id - восстановление идёт только по ней."""
    row = conn.execute(
        "SELECT id, title, status FROM videos WHERE remote_id=? "
        "AND platform <> 'local' ORDER BY id LIMIT 1",
        (str(remote_id),)).fetchone()
    return dict(row) if row else None


def catalog_origins(conn: sqlite3.Connection) -> list[dict]:
    """Откуда есть зеркала: ключ, число строк, устройство и его статус."""
    return [dict(row) for row in conn.execute(
        "SELECT c.origin, COUNT(*) AS entries, "
        "MAX(c.fetched_at) AS fetched_at, "
        "t.label, t.verified_at, t.revoked_at "
        "FROM mesh_catalog c LEFT JOIN truststore t ON t.pubkey = c.origin "
        "GROUP BY c.origin ORDER BY MAX(c.fetched_at) DESC")]


def catalog_clear(conn: sqlite3.Connection, origin: str) -> int:
    """Забыть зеркало участника; возвращает число удалённых строк."""
    cursor = conn.execute("DELETE FROM mesh_catalog WHERE origin=?",
                          (_trust_key(origin),))
    return int(cursor.rowcount or 0)


# --------------------------------------------------------------------------- #
#  1.3/1.4: адресная книга и здоровье устройств
# --------------------------------------------------------------------------- #

PEER_ADDR_KEEP = 20           # сколько адресов помним на устройство


def peer_addr_add(conn: sqlite3.Connection, pubkey, addr: str,
                  source: str = "probe") -> None:
    """Запомнить адрес устройства (источник + время); история растёт.

    Лимит PEER_ADDR_KEEP на устройство: старые адреса вытесняются -
    книга про «где_device_был недавно», не архив.
    """
    key = _trust_key(pubkey)
    addr = str(addr or "").strip()
    if not addr:
        return
    conn.execute(
        "INSERT INTO peer_addrs (pubkey, addr, source, last_seen) "
        "VALUES (?,?,?,?) ON CONFLICT(pubkey, addr) DO UPDATE SET "
        "last_seen=excluded.last_seen, source=excluded.source",
        (key, addr, str(source or "probe"), now_iso()))
    conn.execute(
        "DELETE FROM peer_addrs WHERE pubkey=? AND addr NOT IN "
        "(SELECT addr FROM peer_addrs WHERE pubkey=? "
        " ORDER BY last_seen DESC LIMIT ?)", (key, key, PEER_ADDR_KEEP))


def peer_addr_list(conn: sqlite3.Connection, pubkey,
                   limit: int = 5) -> list[dict]:
    """Свежие адреса устройства (последний первым) - кандидаты редиала."""
    return [dict(row) for row in conn.execute(
        "SELECT addr, source, last_seen FROM peer_addrs WHERE pubkey=? "
        "ORDER BY last_seen DESC, rowid DESC LIMIT ?",
        (_trust_key(pubkey), int(limit)))]


def peer_health_set(conn: sqlite3.Connection, pubkey, status: str,
                    detail: str = "") -> dict:
    """Сохранить статус тихого probe (alive|dead) и вернуть старый.

    Возвращает {"was": ...} - вызывающий логирует СМЕНУ статуса (не
    каждый проб:5 мин * N устройств = спам в журнал).
    """
    key = _trust_key(pubkey)
    row = conn.execute("SELECT status FROM peer_health WHERE pubkey=?",
                       (key,)).fetchone()
    conn.execute(
        "INSERT INTO peer_health (pubkey, status, detail, checked_at) "
        "VALUES (?,?,?,?) ON CONFLICT(pubkey) DO UPDATE SET "
        "status=excluded.status, detail=excluded.detail, "
        "checked_at=excluded.checked_at",
        (key, str(status), str(detail or ""), now_iso()))
    return {"was": row["status"] if row else None}


def peer_health(conn: sqlite3.Connection) -> dict:
    """Снимок здоровья: {pubkey: {status, detail, checked_at}}."""
    return {row["pubkey"]: {"status": row["status"],
                            "detail": row["detail"],
                            "checked_at": row["checked_at"]}
            for row in conn.execute(
                "SELECT pubkey, status, detail, checked_at FROM peer_health")}


def queue_rows(conn: sqlite3.Connection) -> list[dict]:
    """Очередь загрузки: ожидающие, качающиеся и упавшие - в порядке постановки.

    Возвращает и last_error: причина падения должна быть видна в списке,
    а не только в журнале - иначе «ошибка» ничего не объясняет.
    """
    rows = [dict(row) for row in conn.execute(
        """SELECT id, key, title, status, updated_at, last_error
             FROM videos
            WHERE status IN ('queued','downloading','failed')
            ORDER BY CASE status WHEN 'downloading' THEN 0 WHEN 'queued' THEN 1
                                 ELSE 2 END,
                     updated_at""")]
    for row in rows:
        row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
    return rows


def runs(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    """Журнал запусков (последние N)."""
    out = []
    for row in conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)):
        item = dict(row)
        try:
            item["stats"] = json.loads(item.get("stats_json") or "{}")
        except json.JSONDecodeError:
            item["stats"] = {}
        item.pop("stats_json", None)
        out.append(item)
    return out


def sources(conn: sqlite3.Connection) -> list[dict]:
    """Источники для вкладки «Синхронизация»: счётчики по каждому плейлисту.

    Плюс назначение (storage_id/storage_label): что выбрано «куда качать»,
    чтобы было видно, куда уйдёт режим «Полная». Пустое - честное
    «по умолчанию» (resolve_target допьёт глобальное хранилище).
    """
    rows = conn.execute(
        """SELECT p.id, p.title, p.kind, p.sync_mode, p.url, p.last_synced_at,
                  p.item_count, p.remote_id, p.storage_id, p.account_id,
                  s.label AS storage_label,
                  SUM(CASE WHEN pi.removed_at IS NULL THEN 1 ELSE 0 END) total,
                  SUM(CASE WHEN pi.removed_at IS NULL AND v.status='downloaded'
                           THEN 1 ELSE 0 END) downloaded,
                  SUM(CASE WHEN pi.removed_at IS NULL AND v.status IN ('known','queued')
                           THEN 1 ELSE 0 END) pending
             FROM playlists p
             LEFT JOIN storages s ON s.id = p.storage_id
             LEFT JOIN playlist_items pi ON pi.playlist_id=p.id
             LEFT JOIN videos v ON v.id=pi.video_id
            GROUP BY p.id
            ORDER BY p.last_synced_at DESC NULLS LAST""").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["kind_label"] = KIND_LABELS.get(item["kind"], item["kind"])
        out.append(item)
    return out


def find_duplicates(conn, limit: int = 200) -> list[dict]:
    """Группы видео, у которых на диске больше одной живой копии.

    «Дубль» - производное состояние (у видео 2+ файлов kind='video'),
    поэтому отдельной колонки не нужно: вопрос решается на месте - какую
    копию оставить.
    """
    groups = []
    rows = conn.execute(
        """SELECT v.id AS video_id, v.key, v.title,
                  COUNT(*) AS copies, SUM(COALESCE(f.size,0)) AS bytes
             FROM videos v JOIN files f ON f.video_id = v.id
            WHERE f.kind='video' AND f.missing=0
            GROUP BY v.id HAVING COUNT(*) > 1
            ORDER BY bytes DESC LIMIT ?""", (int(limit),)).fetchall()
    for row in rows:
        files = [dict(item) for item in conn.execute(
            """SELECT f.id, f.path, f.size, f.mtime, s.label AS storage,
                      s.available, s.status AS storage_status
                 FROM files f LEFT JOIN storages s ON s.id = f.storage_id
                WHERE f.video_id=? AND f.kind='video' AND f.missing=0
                ORDER BY f.id""", (row["video_id"],))]
        groups.append({"video_id": row["video_id"], "key": row["key"],
                       "title": row["title"], "copies": row["copies"],
                       "bytes": row["bytes"], "files": files})
    return groups


def _aux_stem(path: str) -> str:
    """Стем файла с учётом составного суффикса сайдкара.

    `Path('x.post.json').with_suffix('')` даёт 'x.post' - а сайдкар лежит
    как 'x.post.json' рядом с 'x.mp4'. Сравнивать стемы надо по-настоящему,
    иначе сайдкар уцелевает и становится сиротой после удаления копии.
    """
    target = Path(path)
    name = target.name
    if name.lower().endswith(SIDECAR_SUFFIX):
        name = name[:-len(SIDECAR_SUFFIX)]
    else:
        name = target.stem
    return str(target.parent / name).lower()


def delete_file_cascade(conn, file_id: int) -> dict:
    """Удалить файл с диска вместе с его строкой (и сайдкаром рядом).

    Порядок намеренный: сначала диск, потом базу. Если файл снять не
    удалось (занят, нет прав) - строка остаётся, и индекс не рассказывает
    неправду о том, чего на диске больше нет.
    """
    row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row:
        return {"error": "Файл не найден"}
    path = row["path"]

    siblings = []
    if row["kind"] == "video":
        # Сайдкар/субтитры/обложка лежат рядом с тем же stem: после
        # удаления копии они становятся сиротами.
        stem = _aux_stem(path)
        siblings = [dict(s) for s in conn.execute(
            """SELECT id, path FROM files
                WHERE video_id=? AND kind<>'video' AND missing=0""",
            (row["video_id"],))
            if _aux_stem(s["path"]) == stem]

    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        return {"error": f"Не удалось удалить {path}: {exc}"}

    removed = [path]
    with conn:
        for sibling in siblings:
            try:
                if os.path.exists(sibling["path"]):
                    os.remove(sibling["path"])
                removed.append(sibling["path"])
            except OSError:
                continue  # файл занят - строку оставляем, её поймает скан
            conn.execute("DELETE FROM files WHERE id=?", (sibling["id"],))
        conn.execute("DELETE FROM files WHERE id=?", (file_id,))
        # Если это была последняя живая копия - честно отмечаем пропажу.
        left = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=? AND kind='video' "
            "AND missing=0", (row["video_id"],)).fetchone()["n"]
        if not left:
            conn.execute(
                "UPDATE videos SET status='missing', updated_at=? WHERE id=?",
                (now_iso(), row["video_id"]))
    return {"ok": True, "removed": removed, "video_id": row["video_id"]}


def resolve_duplicates(conn, video_id: int, keep_file_id: int) -> dict:
    """Оставить одну копию: удалить с диска все остальные файлы видео.

    Работает и для «возможного переезда»: там оставляем новый файл, а
    строку старого (в недоступном хранилище) убираем - если старая копия
    однажды вернётся, скан найдёт её снова и снова предложит выбор.
    """
    keep = conn.execute(
        "SELECT * FROM files WHERE id=? AND video_id=? AND kind='video'",
        (keep_file_id, video_id)).fetchone()
    if not keep:
        return {"error": "Не выбран файл, который оставить"}

    others = conn.execute(
        "SELECT id, path FROM files WHERE video_id=? AND kind='video' "
        "AND id<>? AND missing=0", (video_id, keep_file_id)).fetchall()
    errors = []
    removed = []
    for other in others:
        result = delete_file_cascade(conn, other["id"])
        if result.get("error"):
            errors.append(result["error"])
        else:
            removed.extend(result.get("removed") or [])
    title = conn.execute("SELECT title FROM videos WHERE id=?",
                         (video_id,)).fetchone()
    return {"ok": not errors, "removed": removed, "errors": errors,
            "kept": keep["path"],
            "title": title["title"] if title else None}


def rebind_file(conn, file_id: int, video_id: int) -> dict:
    """Привязать файл к другой записи вручную (остаток, который не опознался).

    Локальное видео, оставшееся без файлов, удаляется: знать о нём больше
    нечего.
    """
    file_row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not file_row:
        return {"error": "Файл не найден"}
    target = conn.execute("SELECT id, title FROM videos WHERE id=?",
                          (video_id,)).fetchone()
    if not target:
        return {"error": "Видео не найдено"}
    old_video = file_row["video_id"]
    if old_video == video_id:
        return {"error": "Файл уже привязан к этому видео"}

    with conn:
        conn.execute("UPDATE files SET video_id=? WHERE id=?", (video_id, file_id))
        if file_row["kind"] == "video":
            conn.execute(
                """UPDATE videos SET status='downloaded', updated_at=?
                   WHERE id=? AND status IN ('known','queued','downloading',
                                             'failed','missing','detached')""",
                (now_iso(), video_id))
        left = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=?", (old_video,)).fetchone()["n"]
        if not left:
            old = conn.execute("SELECT platform FROM videos WHERE id=?",
                               (old_video,)).fetchone()
            if old and old["platform"] == "local":
                conn.execute("DELETE FROM videos WHERE id=?", (old_video,))
    return {"ok": True, "title": target["title"]}


def next_queued(conn: sqlite3.Connection) -> dict | None:
    """Следующее к скачиванию - в порядке постановки в очередь.

    Возвращает только то, что нужно загрузчику, чтобы строка не тащила
    за собой мегабайты raw_json в каждый воркер.
    """
    row = conn.execute(
        """SELECT id, key, platform, remote_id, title, webpage_url, status,
                  target_storage_id, channel_id, last_error
             FROM videos WHERE status='queued'
            ORDER BY updated_at, id LIMIT 1""").fetchone()
    return dict(row) if row else None


def retry_failed(conn: sqlite3.Connection) -> int:
    """Упавшие -> в очередь (strayed-статусы не трогаем)."""
    cur = conn.execute(
        "UPDATE videos SET status='queued', updated_at=? WHERE status='failed'",
        (now_iso(),))
    return int(cur.rowcount)


def playlist_pending(conn: sqlite3.Connection, playlist_id: int,
                     limit: int = 300) -> tuple[int, list[dict]]:
    """Плейлист -> (сколько ждёт загрузки, первые N строк для пикера).

    Пикер (режим «Частичная») выбирает контент руками, поэтому ему нужны
    названия и длительность - по ним и принято выбирать.
    """
    where = ("""pi.playlist_id=? AND pi.removed_at IS NULL
                AND v.status IN ('known','failed')""")
    total = conn.execute(
        f"""SELECT COUNT(*) n FROM playlist_items pi
              JOIN videos v ON v.id=pi.video_id WHERE {where}""",
        (playlist_id,)).fetchone()["n"]
    rows = [dict(row) for row in conn.execute(
        f"""SELECT v.id, v.key, v.title, v.duration_s, v.uploaded_at, v.status
              FROM playlist_items pi JOIN videos v ON v.id=pi.video_id
             WHERE {where} ORDER BY pi.position LIMIT ?""",
        (playlist_id, int(limit)))]
    return int(total), rows


def enqueue_playlist(conn: sqlite3.Connection, playlist_id: int,
                     storage_id: str | None = None) -> int:
    """Поставить в очередь всё ожидающее в плейлисте (режим «Полная»)."""
    rows = conn.execute(
        """SELECT v.id FROM playlist_items pi JOIN videos v ON v.id=pi.video_id
            WHERE pi.playlist_id=? AND pi.removed_at IS NULL
              AND v.status IN ('known','failed')""",
        (playlist_id,)).fetchall()
    return enqueue(conn, [row["id"] for row in rows], storage_id=storage_id)


def status_breakdown(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """[(статус, сколько)] для панели фильтров (всегда полный список)."""
    counts = Counter({s: 0 for s in STATUSES})
    for row in conn.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status"):
        counts[row["status"]] = row["n"]
    return [(s, counts[s]) for s in STATUSES]


def video_id_by_key(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute("SELECT id FROM videos WHERE key=?", (key,)).fetchone()
    return int(row["id"]) if row else None


def video_account_id(conn: sqlite3.Connection, video_id: int) -> str | None:
    """Аккаунт Google видео - через его плейлист (как resolve_target).

    У видео может быть несколько плейлистов - берём первый привязанный
    аккаунт по порядку связей (честнее, чем «любой»).
    """
    row = conn.execute(
        """SELECT p.account_id FROM playlist_items pi
             JOIN playlists p ON p.id = pi.playlist_id
            WHERE pi.video_id=? AND p.account_id IS NOT NULL
            ORDER BY pi.id LIMIT 1""", (int(video_id),)).fetchone()
    return row["account_id"] if row else None


def insert_local_video(conn: sqlite3.Connection, *, title: str, path: str,
                       size: int | None, mtime: float | None,
                       digest: str | None,
                       storages_list: list[dict] | None = None) -> int:
    """Файл, у которого не нашлось площадочного ID: строка platform='local'.

    Такие записи - честный «импорт из проводника»: они участвуют в
    библиотеке, но не синхронизируются (место занято, автор неизвестен).
    Хеш содержимого - единственная связь, по которой скан later
    перепривяжет файл, если он той же самый встал под новый путь.
    """
    key = local_key(path)
    now = now_iso()
    row = conn.execute("SELECT id FROM videos WHERE key=?", (key,)).fetchone()
    if row:
        vid = int(row["id"])
    else:
        cur = conn.execute(
            """INSERT INTO videos (key, platform, remote_id, title, uploaded_at,
                                   origin, status, first_seen_at, updated_at,
                                   webpage_url)
               VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
            (key, "local", key.split(":", 1)[1], title, None,
             "path", "downloaded", now, now))
        vid = int(cur.lastrowid)
    record_file(conn, vid, path, "video", size=size, mtime=mtime, digest=digest,
                storages_list=storages_list)
    return vid
