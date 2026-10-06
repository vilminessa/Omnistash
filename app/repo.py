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
import sqlite3
from collections import Counter

from .db import STATUSES, SYNC_MODES
from .metadata import PLATFORM, local_key, video_key
from .util import now_iso

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
                 category=coalesce(?, category),
                 webpage_url=coalesce(?, webpage_url),
                 thumb_url=coalesce(?, thumb_url),
                 raw_json=coalesce(?, raw_json),
                 channel_id=coalesce(?, channel_id),
                 updated_at=?
               WHERE id=?""",
            (data.get("title"), data.get("description"), data.get("uploaded_at"),
             data.get("duration_s"), data.get("view_count"), data.get("category"),
             data.get("webpage_url"), data.get("thumb_url"), raw,
             channel_id, now, vid),
        )
        return vid, False

    cur = conn.execute(
        """INSERT INTO videos (key, platform, remote_id, channel_id, title,
                               description, uploaded_at, duration_s, view_count,
                               category, webpage_url, thumb_url, raw_json, origin,
                               status, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, platform, remote_id, channel_id, data.get("title"),
         data.get("description"), data.get("uploaded_at"), data.get("duration_s"),
         data.get("view_count"), data.get("category"), data.get("webpage_url"),
         data.get("thumb_url"), data.get("raw_json"), data.get("origin") or "yt-dlp",
         "known", now, now),
    )
    return int(cur.lastrowid), True


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
                digest: str | None = None, format_json: str | None = None) -> None:
    """Файл на диске -> строка files (сразу с флагом «существует»)."""
    conn.execute(
        """INSERT INTO files (video_id, kind, path, size, hash, mtime, missing)
           VALUES (?,?,?,?,?,?,0)
           ON CONFLICT (path) DO UPDATE SET
               video_id=excluded.video_id, kind=excluded.kind,
               size=excluded.size, hash=coalesce(excluded.hash, files.hash),
               mtime=excluded.mtime, missing=0""",
        (video_id, kind, path, size, digest, mtime))
    if kind == "video":
        conn.execute(
            """UPDATE videos SET status='downloaded', downloaded_at=coalesce(downloaded_at, ?)
               WHERE id=? AND status IN ('known','queued','downloading','failed','missing')""",
            (now_iso(), video_id))


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


def enqueue(conn: sqlite3.Connection, video_ids) -> int:
    """Поставить в очередь: только то, что ещё не скачано и не качается."""
    changed = 0
    now = now_iso()
    for chunk in _chunks([int(v) for v in video_ids]):
        marks = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"""UPDATE videos SET status='queued', updated_at=?
                WHERE id IN ({marks})
                  AND status NOT IN ('downloaded','downloading','unavailable')""",
            [now, *chunk])
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

    # 1. Что уже есть в индексе: видео одним запросом, каналы - вторым.
    remote_ids = [e["remote_id"] for e in entries if e.get("remote_id")]
    known_ids: set[str] = set()
    for chunk in _chunks(remote_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, key FROM videos WHERE key IN ({marks})",
            [video_key(PLATFORM, r) for r in chunk]).fetchall()
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
            [PLATFORM, *chunk]).fetchall()
        known_channels.update(r["remote_id"] for r in rows)

    # 2. Плейлист и его текущие связи.
    row = conn.execute(
        "SELECT id, sync_mode FROM playlists WHERE platform=? AND remote_id=?",
        (PLATFORM, playlist.get("remote_id"))).fetchone()
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
        key = video_key(PLATFORM, remote_id)
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
                on_stage=None) -> dict:
    """Принять план: плейлист -> авторы -> видео -> связи. Одна транзакция.

    on_stage(stage, state, current, total) вызывается по ходу - отсюда UI
    берёт чек-лист стадий. Исключение (в том числе отмена) откатывает всё
    до последней строки: частичных состояний не остаётся.
    """
    def stage(name, state, current=0, total=0):
        if on_stage:
            on_stage(name, state, current, total)

    stages = ("playlist", "channels", "videos", "links")
    for name in stages:
        stage(name, "wait")
    stats = dict(plan["counts"])

    # Явный BEGIN/COMMIT: соединения работают в autocommit (иначе запись
    # воркера висела бы незакоммиченной и держала лок файла базы).
    conn.execute("BEGIN")
    try:
        # --- стадия 1: плейлист -------------------------------------------
        stage("playlist", "active", 0, 1)
        playlist_id = ensure_playlist(conn, snapshot["playlist"])
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
            data = _entry_to_video(entry)
            vid, _created = upsert_video(conn, data, full=False)
            if vid:
                ids_by_key[item["key"]] = vid
            if index % 50 == 0 or index == len(todo):
                stage("videos", "active" if index < len(todo) else "done",
                      index, len(todo))

        # --- стадия 4: связи ----------------------------------------------
        stage("links", "active", 0, len(entries))
        gone = now_iso()
        for index, entry in enumerate(entries, start=1):
            key = video_key(PLATFORM, entry["remote_id"])
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
        current = {video_key(PLATFORM, e["remote_id"]) for e in entries}
        rows = conn.execute(
            """SELECT pi.id, v.key FROM playlist_items pi
                 JOIN videos v ON v.id = pi.video_id
                WHERE pi.playlist_id=? AND pi.removed_at IS NULL""",
            (playlist_id,)).fetchall()
        removed = 0
        for row in rows:
            if row["key"] not in current:
                conn.execute("UPDATE playlist_items SET removed_at=? WHERE id=?",
                             (gone, row["id"]))
                removed += 1
        stats["removed"] = removed
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


def _entry_to_video(entry: dict) -> dict:
    """Плоская запись плейлиста -> аргумент upsert_video (без raw_json)."""
    return {
        "platform": PLATFORM,
        "remote_id": entry.get("remote_id"),
        "key": video_key(PLATFORM, entry.get("remote_id")),
        "title": entry.get("title"),
        "description": None,
        "uploaded_at": entry.get("uploaded_at"),
        "duration_s": entry.get("duration_s"),
        "view_count": entry.get("view_count"),
        "category": None,
        "webpage_url": entry.get("webpage_url"),
        "thumb_url": None,
        "raw_json": None,          # тонкий: богатый raw_json не затираем
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


def list_videos(conn: sqlite3.Connection, *, scope: dict | None = None,
                status: str | None = None, query: str = "",
                offset: int = 0, limit: int = 200) -> dict:
    """Строки таблицы библиотеки: (total, rows) для виртуализированного списка.

    scope: {"type": "pool"|"channel"|"playlist", "id": int}.
    Поиск - по FTS5 (название, описание, локальные теги), сортировка - по
    выбранной колонке, страница - offset/limit.
    """
    scope = scope or {"type": "pool"}
    joins, where, args = [], [], []
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
    query = (query or "").strip()
    if query:
        fts = fts_query(query)
        if fts:
            where.append("v.id IN (SELECT rowid FROM videos_fts WHERE videos_fts MATCH ?)")
            args.append(fts)

    join_sql = " ".join(joins)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) n FROM videos v {join_sql} {where_sql}", args).fetchone()["n"]

    order = "v.uploaded_at DESC NULLS LAST, v.id DESC"
    rows = [dict(row) for row in conn.execute(
        f"""SELECT v.id, v.key, v.title, v.status, v.duration_s, v.uploaded_at,
                   v.origin, c.title AS channel,
                   (SELECT SUM(size) FROM files f
                     WHERE f.video_id=v.id AND f.kind='video' AND f.missing=0) AS size
              FROM videos v
              LEFT JOIN channels c ON c.id = v.channel_id
              {join_sql} {where_sql}
             ORDER BY {order}
             LIMIT ? OFFSET ?""",
        [*args, int(limit), int(offset)])]
    for row in rows:
        row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
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
        "SELECT kind, path, size, hash, mtime, missing FROM files "
        "WHERE video_id=? ORDER BY kind", (video_id,))]
    data["playlists"] = [dict(p) for p in conn.execute(
        """SELECT p.id, p.title, p.kind, pi.position, pi.removed_at
             FROM playlist_items pi JOIN playlists p ON p.id=pi.playlist_id
            WHERE pi.video_id=? ORDER BY p.title""", (video_id,))]
    try:
        data["raw"] = json.loads(data.get("raw_json") or "{}")
    except json.JSONDecodeError:
        data["raw"] = {}
    return data


def queue_rows(conn: sqlite3.Connection) -> list[dict]:
    """Очередь загрузки: ожидающие, качающиеся и упавшие - в порядке постановки."""
    rows = [dict(row) for row in conn.execute(
        """SELECT id, key, title, status, updated_at
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
    """Источники для вкладки «Синхронизация»: счётчики по каждому плейлисту."""
    rows = conn.execute(
        """SELECT p.id, p.title, p.kind, p.sync_mode, p.url, p.last_synced_at,
                  p.item_count, p.remote_id,
                  SUM(CASE WHEN pi.removed_at IS NULL THEN 1 ELSE 0 END) total,
                  SUM(CASE WHEN pi.removed_at IS NULL AND v.status='downloaded'
                           THEN 1 ELSE 0 END) downloaded,
                  SUM(CASE WHEN pi.removed_at IS NULL AND v.status IN ('known','queued')
                           THEN 1 ELSE 0 END) pending
             FROM playlists p
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


def enqueue_playlist(conn: sqlite3.Connection, playlist_id: int) -> int:
    """Поставить в очередь всё ожидающее в плейлисте (режим «Полная»)."""
    rows = conn.execute(
        """SELECT v.id FROM playlist_items pi JOIN videos v ON v.id=pi.video_id
            WHERE pi.playlist_id=? AND pi.removed_at IS NULL
              AND v.status IN ('known','failed')""",
        (playlist_id,)).fetchall()
    return enqueue(conn, [row["id"] for row in rows])


def status_breakdown(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """[(статус, сколько)] для панели фильтров (всегда полный список)."""
    counts = Counter({s: 0 for s in STATUSES})
    for row in conn.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status"):
        counts[row["status"]] = row["n"]
    return [(s, counts[s]) for s in STATUSES]


def video_id_by_key(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute("SELECT id FROM videos WHERE key=?", (key,)).fetchone()
    return int(row["id"]) if row else None


def insert_local_video(conn: sqlite3.Connection, *, title: str, path: str,
                       size: int | None, mtime: float | None,
                       digest: str | None) -> int:
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
    record_file(conn, vid, path, "video", size=size, mtime=mtime, digest=digest)
    return vid
