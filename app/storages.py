"""Хранилища: где живут файлы библиотеки, их состояния и операции над ними.

Хранилище - это папка, которую программа сканирует и в которую кладёт новые
файлы. Раньше это была просто строка в настройках; теперь - строка в базе,
потому что от неё зависят:

  * канон пути файлов: `files.rel_path` от корня хранилища, поэтому переезд
    диска (E:\\ -> F:\\) это ОДИН UPDATE, а не переписывание тысяч путей;
  * состояние «файл пропал» - только если хранилище ДОСТУПНО и сканировалось;
  * отвязка - осознанное «забыть папку» с последующим восстановлением;
  * выбор, куда качать: он всегда ручной (настройка канала/плейлиста или
    выбор в панели выделения), фоновый воркер сам ничего не решает.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from pathlib import Path

from .util import now_iso, sanitize_name

MARKER = ".omnistash-root"     # файл-маркер корня: uuid внутри папки
PROBE_TIMEOUT = 2.5            # сек: сетевой путь, который висит, считаем недоступным


# --------------------------------------------------------------------------- #
#  Пути
# --------------------------------------------------------------------------- #

def normalize_path(path) -> str:
    """Абсолютный путь без завершающего разделителя, с переменными окружения.

    Сравнение путей везде регистронезависимое (Windows), но хранить будем
    как есть - так путь остаётся читаемым для человека.
    """
    text = os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"')))
    if not text:
        return ""
    normalized = os.path.normpath(text)
    # Срезаем завершающий слэш, кроме корня диска ("D:\").
    if len(normalized) > 3:
        normalized = normalized.rstrip("\\/")
    return normalized


def label_for(path: str) -> str:
    """Метка по умолчанию - имя папки (для корня диска - сам путь)."""
    name = os.path.basename(normalize_path(path))
    return sanitize_name(name) if name else normalize_path(path)


def split_path(storages: list[dict], path: str) -> tuple[dict | None, str]:
    """(хранилище, относительный путь) по самому длинному совпадению префикса.

    Единственное правило привязки в программе: если папки вложены друг в
    друга, выигрывает самая длинная. Сравнение регистронезависимое.
    """
    needle = normalize_path(path)
    if not needle:
        return None, ""
    low = needle.lower()
    best_root = ""
    best_storage = None
    best_rel = ""
    for storage in storages:
        root = normalize_path(storage.get("path"))
        if not root:
            continue
        low_root = root.lower()
        if low == low_root:
            rel = ""
        elif low.startswith(low_root + os.sep):
            rel = needle[len(root) + 1:]
        else:
            continue
        if len(low_root) > len(best_root):
            best_root, best_storage, best_rel = low_root, storage, rel
    if best_storage is None:
        return None, ""
    return best_storage, best_rel


def join_path(storage_path: str, rel_path: str) -> str:
    """Абсолютный путь из хранилища + относительного."""
    return os.path.join(normalize_path(storage_path), rel_path) if rel_path \
        else normalize_path(storage_path)


# --------------------------------------------------------------------------- #
#  Маркер корня
# --------------------------------------------------------------------------- #

def read_marker(path: str) -> str | None:
    """uuid из `.omnistash-root`, если папка уже была хранилищем."""
    try:
        data = json.loads((Path(normalize_path(path)) / MARKER)
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    key = data.get("root_key") if isinstance(data, dict) else None
    return str(key) if key else None


def write_marker(path: str, root_key: str | None = None) -> str:
    """Положить/обновить маркер: он и есть «я эту папку знаю»."""
    root_key = root_key or f"rt_{uuid.uuid4().hex[:16]}"
    target = Path(normalize_path(path)) / MARKER
    target.write_text(json.dumps({"omnistash": 1, "root_key": root_key},
                                 ensure_ascii=False), encoding="utf-8")
    return root_key


# --------------------------------------------------------------------------- #
#  Доступность
# --------------------------------------------------------------------------- #

def probe(path: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """Проверить папку, НЕ блокируя вызывающий поток дольше timeout.

    os.path.isdir на отвалившемся сетевом шаре может висеть минутами - мы
    считаем такой путь недоступным, а не вешаем окно.
    """
    result = {"ok": False, "free": None, "total": None, "timeout": False}

    def work() -> None:
        try:
            ok = os.path.isdir(path)
        except OSError:
            ok = False
        if ok:
            try:
                free, total, _used = shutil.disk_usage(path)
                result.update(free=free, total=total)
            except OSError:
                pass
        result["ok"] = ok

    worker = threading.Thread(target=work, name="omnistash-probe", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        result["timeout"] = True
    return result


def refresh_availability(conn, storage_ids: list[str] | None = None) -> dict:
    """Перепроверить доступность хранилищ и обновить состояние в базе.

    Каждый путь проверяется в своём потоке с таймаутом и параллельно -
    пять сетевых корней не должны складываться в пять таймаутов подряд.
    """
    if storage_ids is None:
        rows = conn.execute(
            "SELECT id, path FROM storages WHERE status='active' AND enabled=1"
        ).fetchall()
    else:
        marks = ",".join("?" * len(storage_ids))
        rows = conn.execute(
            f"""SELECT id, path FROM storages
                WHERE id IN ({marks}) AND status='active' AND enabled=1""",
            list(storage_ids)).fetchall()
    if not rows:
        return {"checked": 0, "available": 0, "lost": 0}

    results: dict[str, dict] = {}
    threads = []
    for row in rows:
        worker = threading.Thread(
            target=lambda r=row: results.__setitem__(r["id"], probe(r["path"])),
            name="omnistash-probe", daemon=True)
        worker.start()
        threads.append(worker)
    for worker in threads:
        worker.join(PROBE_TIMEOUT + 1)

    now = now_iso()
    available = lost = 0
    for row in rows:
        state = results.get(row["id"], {"ok": False, "free": None, "total": None})
        if state["ok"]:
            available += 1
            conn.execute(
                """UPDATE storages SET available=1, missing_since=NULL,
                       last_seen_at=?, free_bytes=?, total_bytes=?, updated_at=?
                   WHERE id=?""",
                (now, state["free"], state["total"], now, row["id"]))
        else:
            lost += 1
            row_state = conn.execute(
                "SELECT available, missing_since FROM storages WHERE id=?",
                (row["id"],)).fetchone()
            missing_since = row_state["missing_since"] if row_state else None
            if not row_state or row_state["available"]:
                missing_since = now      # носитель только что пропал
            conn.execute(
                """UPDATE storages SET available=0, missing_since=?,
                       updated_at=? WHERE id=?""",
                (missing_since, now, row["id"]))
    return {"checked": len(rows), "available": available, "lost": lost}


# --------------------------------------------------------------------------- #
#  CRUD
# --------------------------------------------------------------------------- #

def add(conn, path: str, *, label: str | None = None, kind: str = "local",
        enabled: bool = True, recursive: bool = True,
        write_marker_flag: bool = True) -> dict:
    """Зарегистрировать хранилище. Возвращает строку или словарь-подсказку.

    Если папка уже встречалась раньше (маркер), молча плодить дубль нельзя:
    возвращаем {"hint": ..., "storage": ...} - что делать, решает вызывающий.
    """
    normalized = normalize_path(path)
    if not normalized:
        return {"error": "Путь пустой"}
    if not os.path.isdir(normalized):
        return {"error": f"Папка не найдена: {normalized}"}

    existing = find_by_path(conn, normalized)
    if existing:
        # Отвязанная папка по тому же пути - не дубль, а «вернуть?».
        return {"hint": "detached" if existing["status"] != "active" else "already",
                "storage": existing}

    marker_key = read_marker(normalized)
    if marker_key:
        known = get_by_root_key(conn, marker_key)
        if known:
            return {"hint": "known_root", "storage": known,
                    "root_key": marker_key}

    storage_id = f"st_{uuid.uuid4().hex[:16]}"
    root_key = None
    if write_marker_flag:
        try:
            root_key = write_marker(normalized, marker_key)
        except OSError:
            root_key = None      # нет прав на запись - работаем и без маркера

    now = now_iso()
    conn.execute(
        """INSERT INTO storages (id, path, label, kind, status, enabled,
                                 recursive, root_key, last_seen_at, created_at,
                                 updated_at)
           VALUES (?,?,?,?,'active',?,?,?,?,?,?)""",
        (storage_id, normalized, label or label_for(normalized), kind,
         1 if enabled else 0, 1 if recursive else 0, root_key, now, now, now))
    return get(conn, storage_id)


def get(conn, storage_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM storages WHERE id=?", (storage_id,)).fetchone()
    return dict(row) if row else None


def find_by_path(conn, path: str) -> dict | None:
    needle = normalize_path(path).lower()
    for row in conn.execute("SELECT * FROM storages"):
        if normalize_path(row["path"]).lower() == needle:
            return dict(row)
    return None


def get_by_root_key(conn, root_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM storages WHERE root_key=?",
                       (root_key,)).fetchone()
    return dict(row) if row else None


def all_storages(conn, include_detached: bool = False) -> list[dict]:
    """Хранилища для настроек: активные, и - если нужно - отвязанные."""
    query = "SELECT * FROM storages"
    if not include_detached:
        query += " WHERE status='active'"
    query += " ORDER BY status, label COLLATE NOCASE"
    return [dict(row) for row in conn.execute(query)]


def set_path(conn, storage_id: str, new_path: str) -> dict:
    """Переезд корня: смена пути без переписывания относительных путей.

    Абсолютный `files.path` - кэш, его пересчитываем одним UPDATE; канон
    (storage_id, rel_path) не меняется вовсе, поэтому коллизий между
    хранилищами не бывает. Выполняется одной транзакцией.
    """
    normalized = normalize_path(new_path)
    if not normalized or not os.path.isdir(normalized):
        return {"error": f"Папка не найдена: {new_path}"}
    storage = get(conn, storage_id)
    if not storage:
        return {"error": "Хранилище не найдено"}
    other = find_by_path(conn, normalized)
    if other and other["id"] != storage_id:
        return {"error": f"Путь уже занят хранилищем «{other['label']}»"}

    rows = conn.execute(
        "SELECT rel_path FROM files WHERE storage_id=?",
        (storage_id,)).fetchall()
    present = 0
    for row in rows:
        if row["rel_path"] and os.path.exists(join_path(normalized, row["rel_path"])):
            present += 1

    old = normalize_path(storage["path"])
    conn.commit()
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE storages SET path=?, updated_at=? WHERE id=?",
                     (normalized, now_iso(), storage_id))
        # Один UPDATE вместо тысяч: путь = корень + относительный.
        conn.execute(
            """UPDATE files SET path = ?
                   || CASE WHEN rel_path IS NULL OR rel_path='' THEN ''
                           ELSE ? || rel_path END
               WHERE storage_id=?""",
            (normalized, os.sep, storage_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "files": len(rows), "present": present,
            "old_path": old, "new_path": normalized}


def detach(conn, storage_id: str, keep_trace: bool = True) -> dict:
    """Забыть папку: файловые записи исчезают, знание о видео - нет.

    Правила:
      * файлы, которых нет больше нигде, у видео статус 'detached'
        («папку отвязали», не «файл пропал») - синк их не перекачивает;
      * локальные видео (без площадочной личности) удаляются целиком:
        без файла о них нечего знать;
      * след папки (метка, путь, root_key) по желанию остаётся - он и есть
        то, что позволяет потом сказать «это та самая папка, вернуть?».
    """
    storage = get(conn, storage_id)
    if not storage:
        return {"error": "Хранилище не найдено"}

    conn.commit()
    conn.execute("BEGIN")
    try:
        files_removed = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE storage_id=?",
            (storage_id,)).fetchone()["n"]

        # Видео, у которых этот файл - единственный (или единственный живой).
        victims = [row["video_id"] for row in conn.execute(
            "SELECT DISTINCT video_id FROM files WHERE storage_id=?",
            (storage_id,))]

        local_deleted = detached_count = kept_elsewhere = 0
        for video_id in victims:
            others = conn.execute(
                "SELECT COUNT(*) n FROM files WHERE video_id=? "
                "AND storage_id<>?", (video_id, storage_id)).fetchone()["n"]
            video = conn.execute(
                "SELECT platform, status FROM videos WHERE id=?",
                (video_id,)).fetchone()
            if not video:
                continue
            if others:
                kept_elsewhere += 1          # копия в другом месте - не трогаем
                continue
            if video["platform"] == "local":
                conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
                local_deleted += 1
            else:
                conn.execute(
                    """UPDATE videos SET status='detached', detached_from=?,
                           updated_at=? WHERE id=?""",
                    (storage["label"], now_iso(), video_id))
                detached_count += 1

        conn.execute("DELETE FROM files WHERE storage_id=?", (storage_id,))

        if keep_trace:
            conn.execute(
                """UPDATE storages SET status='detached', detached_at=?,
                       available=0, updated_at=? WHERE id=?""",
                (now_iso(), now_iso(), storage_id))
        else:
            conn.execute("DELETE FROM storages WHERE id=?", (storage_id,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return {"ok": True, "files_removed": files_removed,
            "detached": detached_count, "local_deleted": local_deleted,
            "kept_elsewhere": kept_elsewhere, "label": storage["label"],
            "kept_trace": bool(keep_trace)}


def preview_detach(conn, storage_id: str) -> dict:
    """Сколько что будет забыто - для диалога подтверждения."""
    storage = get(conn, storage_id)
    if not storage:
        return {"error": "Хранилище не найдено"}
    files = conn.execute(
        "SELECT COUNT(*) n FROM files WHERE storage_id=?",
        (storage_id,)).fetchone()["n"]
    victims = [row["video_id"] for row in conn.execute(
        "SELECT DISTINCT video_id FROM files WHERE storage_id=?",
        (storage_id,))]
    local_only = detached = kept = 0
    for video_id in victims:
        others = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=? AND storage_id<>?",
            (video_id, storage_id)).fetchone()["n"]
        if others:
            kept += 1
            continue
        platform = conn.execute(
            "SELECT platform FROM videos WHERE id=?", (video_id,)).fetchone()
        if platform and platform["platform"] == "local":
            local_only += 1
        else:
            detached += 1
    return {"label": storage["label"], "path": storage["path"],
            "files": files, "detached": detached, "local_deleted": local_only,
            "kept_elsewhere": kept, "status": storage["status"]}


def forget(conn, storage_id: str) -> dict:
    """Убрать след отвязанной папки совсем (после detach)."""
    storage = get(conn, storage_id)
    if not storage:
        return {"error": "Хранилище не найдено"}
    if storage["status"] != "detached":
        return {"error": "Сначала отвяжите хранилище"}
    left = conn.execute(
        "SELECT COUNT(*) n FROM files WHERE storage_id=?",
        (storage_id,)).fetchone()["n"]
    if left:
        return {"error": "В хранилище ещё есть файловые записи"}
    conn.execute("DELETE FROM storages WHERE id=?", (storage_id,))
    return {"ok": True, "label": storage["label"]}


def restore(conn, storage_id: str) -> dict:
    """Вернуть отвязанную папку: строку назад, файлы вернёт скан."""
    storage = get(conn, storage_id)
    if not storage:
        return {"error": "Хранилище не найдено"}
    if storage["status"] != "detached":
        return {"error": "Хранилище не отвязано"}
    conn.execute(
        """UPDATE storages SET status='active', detached_at=NULL, enabled=1,
               updated_at=? WHERE id=?""",
        (now_iso(), storage_id))
    return {"ok": True, "storage": get(conn, storage_id)}


def set_enabled(conn, storage_id: str, enabled: bool) -> dict:
    """Временно выключить сканирование (строки файлов при этом целы)."""
    cur = conn.execute(
        "UPDATE storages SET enabled=?, updated_at=? WHERE id=? AND status='active'",
        (1 if enabled else 0, now_iso(), storage_id))
    if not cur.rowcount:
        return {"error": "Хранилище не найдено или отвязано"}
    return {"ok": True}


def bootstrap(db, raw: dict) -> dict:
    """Однократно перенести старые настройки в таблицу хранилищ.

    `raw` - НЕФИЛЬТРОВАННЫЙ settings.json: ключи library_roots/dest_dir уже
    убраны из схемы, и прочитать их можно только до первого save(). Поэтому
    вызывается сразу после открытия базы, до load().
    """
    conn = db.conn
    roots = raw.get("library_roots") or []
    dest = raw.get("dest_dir")
    if not roots and not dest:
        return {"created": 0}
    if conn.execute("SELECT COUNT(*) n FROM storages").fetchone()["n"]:
        return {"created": 0, "skipped": True}   # уже перенесено/добавлено вручную

    created = 0
    default_id = None
    missing: list[str] = []

    def take(result, make_default=True, path=None):
        nonlocal created, default_id
        if isinstance(result, dict) and result.get("error"):
            # Папка могла не существовать (dest_dir создавался лениво при
            # первой загрузке): проговариваем, а не молчим.
            if path:
                missing.append(path)
            return None
        if isinstance(result, dict) and result.get("id"):
            created += 1
            if make_default and default_id is None:
                default_id = result["id"]
            return result
        if isinstance(result, dict) and result.get("storage"):
            storage = result["storage"]
            if make_default and default_id is None:
                default_id = storage.get("id")
        return None

    if dest:
        label = "Загрузки" if os.path.basename(
            os.path.normpath(str(dest))).lower() == "downloads" else None
        take(add(conn, dest, label=label), path=str(dest))

    for root in roots:
        if not isinstance(root, dict) or not root.get("path"):
            continue
        take(add(conn, root["path"],
                 enabled=bool(root.get("enabled", True)),
                 recursive=bool(root.get("recursive", True))),
             path=str(root["path"]))

    adopted = adopt_files(conn)
    return {"created": created, "default": default_id, "missing": missing,
            **adopted}


# --------------------------------------------------------------------------- #
#  Привязка файлов
# --------------------------------------------------------------------------- #

def adopt_files(conn) -> dict:
    """Проставить storage_id/rel_path тем файлам, где их ещё нет.

    Вызывается после импорта корней из старых настроек и вообще везде, где
    файл может лежать вне известного хранилища (это дыра, которую закрывает
    миграция: раньше dest_dir вообще не был связан со списком корней).
    """
    storages = all_storages(conn, include_detached=True)
    rows = conn.execute(
        "SELECT id, path FROM files WHERE storage_id IS NULL").fetchall()
    adopted = orphans = 0
    for row in rows:
        storage, rel = split_path(storages, row["path"])
        if storage:
            conn.execute(
                "UPDATE files SET storage_id=?, rel_path=? WHERE id=?",
                (storage["id"], rel, row["id"]))
            adopted += 1
        else:
            orphans += 1
    return {"adopted": adopted, "orphans": orphans}


def ensure_for_file(conn, path: str) -> tuple[dict | None, str]:
    """Хранилище для нового файла (при загрузке): точный префикс или None."""
    return split_path(all_storages(conn, include_detached=False), path)


# --------------------------------------------------------------------------- #
#  Выбор хранилища для загрузки (всегда осознанный)
# --------------------------------------------------------------------------- #

def resolve_target(conn, settings: dict, *, video_id: int | None = None,
                   playlist_id: int | None = None,
                   channel_id: int | None = None) -> dict | None:
    """Куда качать: плейлист -> канал -> глобальное -> None (спросить).

    Никаких эвристик «по свободному месту»: выбор всегда делает человек,
    программа лишь подставляет его прошлый выбор по умолчанию.
    """
    def by_id(storage_id) -> dict | None:
        if not storage_id:
            return None
        row = conn.execute(
            "SELECT * FROM storages WHERE id=? AND status='active' AND enabled=1",
            (storage_id,)).fetchone()
        return dict(row) if row else None

    if playlist_id:
        row = conn.execute("SELECT storage_id FROM playlists WHERE id=?",
                           (playlist_id,)).fetchone()
        storage = by_id(row["storage_id"]) if row else None
        if storage:
            return storage
    if channel_id:
        row = conn.execute("SELECT storage_id FROM channels WHERE id=?",
                           (channel_id,)).fetchone()
        storage = by_id(row["storage_id"]) if row else None
        if storage:
            return storage
    if video_id is not None and not channel_id and not playlist_id:
        row = conn.execute(
            """SELECT c.storage_id AS channel_storage, p.storage_id AS playlist_storage
                 FROM videos v
                 LEFT JOIN channels c ON c.id = v.channel_id
                 LEFT JOIN playlist_items pi ON pi.video_id = v.id
                 LEFT JOIN playlists p ON p.id = pi.playlist_id
                WHERE v.id=? ORDER BY pi.id LIMIT 1""", (video_id,)).fetchone()
        if row:
            storage = by_id(row["playlist_storage"]) or by_id(row["channel_storage"])
            if storage:
                return storage
    return by_id(settings.get("default_storage_id"))
