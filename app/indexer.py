"""Скан библиотеки: папки -> строки индекса.

Зачем вообще скан, если есть загрузка: пользователь приходит с уже
накопленными файлами (папки Synfronia, NAS, внешние диски), и индекс
должен знать про них сразу. Порядок опознавания одного файла выстроен
от дешёвого к дорогому:

    1. путь уже известен        -> просто освежить размер/время (без чтения)
    2. sidecar post.json        -> полная правда о файле, дороже всего читать
    3. ID площадки в имени      -> множество из 11 символов, сверяется с БД
    4. точное совпадение имени  -> «title» из файла = title из индекса
    5. хеш содержимого          -> файл переезжал: тот же хеш, новый путь
    6. ничего не подошло        -> честный импорт platform='local'

Шаги 2-4 не читают содержимое файла, поэтому опознание большинства
библиотеки укладывается в секунды; хеш считается только там, где без
него определить личность нечем.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import repo, storages as storages_mod
from .metadata import PLATFORM, normalize_video, slim_info, video_key
from .util import norm_title, now_iso, sanitize_name

# Расширения, которые считаем видео (индексируются как kind='video').
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".wmv",
             ".ts", ".mpg", ".mpeg", ".ogv", ".3gp", ".vob"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa", ".sub"}
THUMB_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif"}

SIDECAR_SUFFIX = ".post.json"
# Токен из тех же символов, что и ID площадки (11 штук у YouTube).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{11,}")
# Суффиксы, которыми площадки/парсеры дописывают имя: их надо снять,
# прежде чем сравнивать с названием из индекса.
_TITLE_SUFFIXES = (" - youtube", " (official)", " - официальный видео")

HASH_CHUNK = 1024 * 1024


def file_hash(path: str | Path) -> str:
    """sha256 файла как "sha256:<hex>".

    Префикс - чтобы завтра появился другой алгоритм без миграции: в
    колонке всегда лежит «чем именно» посчитано.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def read_sidecar(path: str | Path) -> dict | None:
    """post.json рядом с файлом: наше собственное описание загрузки.

    Формат терпим к чужим файлам: берём то, что нашли (remote_id сверху
    или внутри info), и не ругаемся на лишние ключи.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    remote_id = data.get("remote_id") or info.get("id")
    if not remote_id:
        return None
    merged = dict(info)
    merged.setdefault("id", remote_id)
    for key in ("title", "channel", "channel_id", "uploader", "uploader_id",
                "upload_date", "duration", "view_count", "category",
                "description", "webpage_url", "thumbnail"):
        if key not in merged and key in data:
            merged[key] = data[key]
    return {"remote_id": str(remote_id), "info": merged,
            "hash": data.get("hash"), "downloaded_at": data.get("downloaded_at")}


def build_sidecar(info: dict, path: str | Path, digest: str | None = None) -> str:
    """post.json для свежескачанного файла (пишет загрузчик)."""
    payload = {
        "omnistash": 1,
        "platform": PLATFORM,
        "remote_id": str(info.get("id") or ""),
        "path": str(path),
        "size": os.path.getsize(path) if os.path.exists(path) else None,
        "hash": digest,
        "created_at": now_iso(),
        # slim_info: полезное целиком, списки форматов - сводкой
        "info": json.loads(slim_info(info) or "{}"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _title_variants(stem: str) -> list[str]:
    """Варианты имени файла для сверки с названием из индекса."""
    base = norm_title(stem)
    variants = [base]
    for suffix in _TITLE_SUFFIXES:
        if base.endswith(suffix):
            variants.append(base[: -len(suffix)].strip())
    # «Название [dQw4w9WgXcQ]»: скобки уже отработал шаг 3, но и без них
    # лишнее в конце мешает - пробуем отрезать хвост в скобках.
    cut = re.sub(r"\s*\[[^\]]{11}\]\s*$", "", base).strip()
    if cut and cut != base:
        variants.append(cut)
    return [v for v in variants if v]


class _Lookups:
    """Индекса памяти, по которым файлы опознаются без чтения содержимого."""

    def __init__(self, conn, compute_hash: bool = True):
        self.by_path: dict[str, dict] = {}
        self.by_hash: dict[str, tuple[int, str]] = {}
        self.ids: set[str] = set()
        self.titles: dict[str, list[int]] = {}
        self.by_key: dict[str, int] = {}
        self.compute_hash = compute_hash
        # Кэш хранилищ на весь прогон: без него record_file на каждом файле
        # читал бы таблицу заново (десятки тысяч лишних запросов).
        self.storages = storages_mod.all_storages(conn, include_detached=True)

        for row in conn.execute(
                """SELECT f.path, f.video_id, f.hash, f.size, f.mtime, f.missing,
                          v.key AS vkey, v.remote_id, v.title, v.platform
                     FROM files f JOIN videos v ON v.id=f.video_id
                    WHERE f.kind='video'"""):
            self.by_path[row["path"]] = {"video_id": row["video_id"],
                                         "hash": row["hash"], "size": row["size"],
                                         "mtime": row["mtime"],
                                         "missing": row["missing"]}
            if row["hash"]:
                self.by_hash[row["hash"]] = (row["video_id"], row["path"])

        for row in conn.execute(
                "SELECT id, key, remote_id, title, platform FROM videos"):
            self.by_key[row["key"]] = row["id"]
            if row["platform"] != "local":
                self.ids.add(row["remote_id"])
                title = norm_title(row["title"])
                if title:
                    # Две записи с одинаковым названием - совпадение больше
                    # не доказательство: такие имена пропускаем.
                    bucket = self.titles.setdefault(title, [])
                    if row["id"] not in bucket:
                        bucket.append(row["id"])


def _walk(root: Path, recursive: bool, with_stat: bool = True):
    """Обход корня: (путь[, размер, mtime]) по файлам, без симлинков-петель.

    with_stat=False - только подсчёт (первый проход для полосы прогресса):
    stat на 100 000 файлов стоит секунды, а общий итог он не меняет.
    """
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # скрытые и служебные папки не трогаем (Thumbs.db, $RECYCLE.BIN)
            dirnames[:] = [d for d in dirnames
                           if not d.startswith((".", "$"))]
            for name in filenames:
                path = os.path.join(dirpath, name)
                if not with_stat:
                    yield path, 0, 0.0
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                yield path, stat.st_size, stat.st_mtime
    else:
        try:
            entries = list(os.scandir(root))
        except OSError:
            return
        for entry in entries:
            if not entry.is_file():
                continue
            if not with_stat:
                yield entry.path, 0, 0.0
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            yield entry.path, stat.st_size, stat.st_mtime


def _attach_aux(conn, video_id: int, media_path: Path, size, mtime, digest,
                storages_list: list[dict] | None = None) -> None:
    """Сайдкар, субтитры и обложка рядом с видео -> строки files того же видео.

    Обход - os.listdir, а не glob: имена вида «Title [dQw4w9WgXcQ].mp4»
    glob читает как символный класс и молча ничего не находит.
    """
    stem = media_path.stem
    parent = media_path.parent
    sidecar = parent / (stem + SIDECAR_SUFFIX)
    if sidecar.exists():
        try:
            stat = sidecar.stat()
        except OSError:
            stat = None
        if stat:
            repo.record_file(conn, video_id, str(sidecar), "sidecar",
                             size=stat.st_size, mtime=stat.st_mtime,
                             storages_list=storages_list)
    try:
        names = os.listdir(parent)
    except OSError:
        return
    prefix = stem + "."
    for name in names:
        if not name.startswith(prefix) or name == media_path.name:
            continue
        candidate = parent / name
        ext = candidate.suffix.lower()
        if ext in SUB_EXT:
            kind = "subtitle"
        elif ext in THUMB_EXT:
            kind = "thumbnail"
        else:
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        repo.record_file(conn, video_id, str(candidate), kind,
                         size=stat.st_size, mtime=stat.st_mtime,
                         storages_list=storages_list)


def scan(roots, db, *, progress=None, stop=None, compute_hash: bool = True) -> dict:
    """Переиндексировать корни. Возвращает отчёт (см. ScanReport-словарь).

    progress(done, total, path) - для полосы прогресса; stop - событие,
        по которому скан вежливо останавливается (кнопка «Отмена»).
    """
    started = time.time()
    report = {"roots": [], "scanned": 0, "added": 0, "bound_sidecar": 0,
              "bound_id": 0, "bound_title": 0, "updated": 0, "unchanged": 0,
              "rebound": 0, "missing": 0, "skipped": 0, "errors": [],
              "stopped": False}

    conn = db.conn
    lookups = _Lookups(conn, compute_hash=compute_hash)
    multi_title = {t for t, ids in lookups.titles.items() if len(ids) > 1}

    active = []
    for root in roots:
        path = str(root.get("path") or "")
        if not root.get("enabled", True) or not path:
            continue
        if not os.path.isdir(path):
            # Недоступный корень НЕ помечает свои файлы пропавшими:
            # отключённый диск - не потеря (см. _mark_missing).
            report["roots"].append({"path": path, "state": "unavailable"})
            continue
        active.append((Path(path), bool(root.get("recursive", True)),
                       root.get("id")))

    # Общий счётчик для полосы: только подсчёт имён, без stat.
    total = 0
    for path, recursive, _sid in active:
        for _file in _walk(path, recursive, with_stat=False):
            total += 1
    if progress:
        progress(0, total, "")

    done = 0
    seen_paths: set[str] = set()

    for root_path, recursive, storage_id in active:
        if stop is not None and stop.is_set():
            report["stopped"] = True
            break
        root_state = {"path": str(root_path), "state": "ok", "files": 0,
                      "missing": 0}
        report["roots"].append(root_state)
        for file_path, size, mtime in _walk(root_path, recursive):
            if stop is not None and stop.is_set():
                report["stopped"] = True
                break
            done += 1
            report["scanned"] += 1
            root_state["files"] += 1
            if progress and (done % 20 == 0 or done == total):
                progress(done, total, file_path)
            seen_paths.add(file_path)

            ext = Path(file_path).suffix.lower()
            if ext not in MEDIA_EXT:
                report["skipped"] += 1
                continue

            try:
                _index_one(conn, lookups, file_path, size, mtime,
                           compute_hash, multi_title, report)
            except OSError as exc:
                report["errors"].append(f"{file_path}: {exc}")
                continue
        if report["stopped"]:
            break
        # Корень пройден: файлы этого корня, которых не было в прогоне,
        # отсутствуют на диске. «Недоступный диск» сюда не попадает -
        # недоступность не должна помечать всю библиотеку потерянной.
        # Записи уже на диске: соединения в autocommit, отдельный коммит
        # прогону не нужен - прогресс виден сразу.
        missing = _mark_missing(conn, root_path, seen_paths,
                                storage_id=storage_id)
        report["missing"] += missing
        root_state["missing"] = missing

    report["duration_s"] = round(time.time() - started, 2)
    return report


def _index_one(conn, lookups: _Lookups, file_path: str, size: int, mtime: float,
               compute_hash: bool, multi_title: set[str], report: dict) -> str:
    """Опознать один файл и записать его в индекс. Возвращает исход."""
    known = lookups.by_path.get(file_path)
    if known is not None and known["size"] == size and known["mtime"] == mtime:
        # Ничего не поменялось: пишем только если файл «пропал» и вернулся -
        # лишний UPDATE на каждом файле писал бы WAL впустую.
        if known.get("missing"):
            conn.execute("UPDATE files SET missing=0 WHERE path=?", (file_path,))
            conn.execute(
                """UPDATE videos SET status='downloaded', updated_at=?
                   WHERE id=? AND status='missing'""",
                (now_iso(), known["video_id"]))
            report["updated"] += 1
        else:
            report["unchanged"] += 1
        return "same"

    media_path = Path(file_path)
    stem = media_path.stem

    # 2. sidecar - полная правда о файле
    sidecar = read_sidecar(media_path.parent / (stem + SIDECAR_SUFFIX))
    if sidecar:
        data = normalize_video(sidecar["info"], origin="sidecar")
        if not data.get("remote_id"):
            data["remote_id"] = sidecar["remote_id"]
            data["key"] = video_key(PLATFORM, sidecar["remote_id"])
        vid, _created = repo.upsert_video(conn, data, full=True)
        if vid:
            _register_media(conn, vid, file_path, size, mtime, None, lookups.storages)
            _attach_aux(conn, vid, media_path, size, mtime, None, lookups.storages)
            report["bound_sidecar"] += 1
            lookups.by_path[file_path] = {"video_id": vid, "hash": sidecar.get("hash"),
                                          "size": size, "mtime": mtime}
            return "sidecar"

    # 3. ID площадки токеном в имени файла
    for token in _TOKEN_RE.findall(stem):
        if token in lookups.ids:
            key = video_key(PLATFORM, token)
            vid = lookups.by_key.get(key)
            if vid:
                _register_media(conn, vid, file_path, size, mtime, None, lookups.storages)
                _attach_aux(conn, vid, media_path, size, mtime, None, lookups.storages)
                report["bound_id"] += 1
                lookups.by_path[file_path] = {"video_id": vid, "hash": None,
                                              "size": size, "mtime": mtime}
                return "id"

    # 4. точное совпадение с названием из индекса
    for variant in _title_variants(stem):
        ids = lookups.titles.get(variant)
        if not ids or variant in multi_title:
            continue
        vid = ids[0]
        _register_media(conn, vid, file_path, size, mtime, None, lookups.storages)
        _attach_aux(conn, vid, media_path, size, mtime, None, lookups.storages)
        report["bound_title"] += 1
        lookups.by_path[file_path] = {"video_id": vid, "hash": None,
                                      "size": size, "mtime": mtime}
        return "title"

    # Путь был известен, но размер/время разошлись: содержимое изменил
    # кто-то другой - привязываем новый файл к той же записи.
    if known is not None:
        vid = known["video_id"]
        _register_media(conn, vid, file_path, size, mtime, None, lookups.storages)
        report["updated"] += 1
        lookups.by_path[file_path] = {"video_id": vid, "hash": known["hash"],
                                      "size": size, "mtime": mtime}
        return "changed"

    # 5. хеш: файл мог переезжать
    digest = None
    if compute_hash:
        try:
            digest = file_hash(file_path)
        except OSError as exc:
            report["errors"].append(f"{file_path}: {exc}")
        if digest and digest in lookups.by_hash:
            vid, old_path = lookups.by_hash[digest]
            # Переезд: строка едет за файлом (та же запись, новый путь и
            # новое хранилище), а не плодит дубль со старым адресом.
            storage, rel = storages_mod.split_path(lookups.storages, file_path)
            conn.execute(
                "UPDATE files SET path=?, storage_id=?, rel_path=?, size=?, "
                "mtime=?, missing=0 WHERE video_id=? AND hash=? AND path=?",
                (file_path, storage["id"] if storage else None,
                 rel if storage else None, size, mtime, vid, digest, old_path))
            if old_path != file_path:
                report["rebound"] += 1
            lookups.by_path[file_path] = {"video_id": vid, "hash": digest,
                                          "size": size, "mtime": mtime}
            _attach_aux(conn, vid, media_path, size, mtime, digest, lookups.storages)
            return "hash"

    # 6. ничего не подошло - честный импорт без площадочной личности
    vid = repo.insert_local_video(conn, title=sanitize_name(stem), path=file_path,
                                   size=size, mtime=mtime, digest=digest,
                                   storages_list=lookups.storages)
    _attach_aux(conn, vid, media_path, size, mtime, digest, lookups.storages)
    if digest:
        lookups.by_hash[digest] = (vid, file_path)
    lookups.by_path[file_path] = {"video_id": vid, "hash": digest,
                                  "size": size, "mtime": mtime}
    report["added"] += 1
    return "local"


def _register_media(conn, video_id: int, file_path: str, size: int,
                    mtime: float, digest: str | None,
                    storages_list: list[dict] | None = None) -> None:
    """Привязать видеофайл к записи (и снять с неё флаг «пропало»).

    Вся логика - в repo.record_file: туда же сводится привязка к хранилищу
    (storage_id/rel_path) и перевод статуса в downloaded.
    """
    repo.record_file(conn, video_id, file_path, "video", size=size,
                     mtime=mtime, digest=digest, storages_list=storages_list)


def _mark_missing(conn, root_path: Path, seen: set[str],
                  storage_id: str | None = None) -> int:
    """Файлы этого корня, не встреченные в прогоне, пометить пропавшими.

    Предпочтительен storage_id (индекс, без LIKE); префикс остаётся
    запасным путём для вызовов, где хранилище неизвестно. Вызывается только
    для доступных и включённых корней - недоступный носитель не должен
    разом пометить тысячи файлов потерянными.
    """
    if storage_id:
        rows = conn.execute(
            """SELECT f.path, f.video_id FROM files f
                WHERE f.kind='video' AND f.missing=0 AND f.storage_id=?""",
            (storage_id,)).fetchall()
    else:
        prefix = str(root_path).rstrip(os.sep) + os.sep
        rows = conn.execute(
            """SELECT f.path, f.video_id FROM files f
                WHERE f.kind='video' AND f.missing=0 AND f.path LIKE ?""",
            (prefix + "%",)).fetchall()
    lost = 0
    for row in rows:
        if row["path"] in seen:
            continue
        conn.execute("UPDATE files SET missing=1 WHERE path=?", (row["path"],))
        # Запись живёт, пока есть хоть один существующий файл этого видео.
        left = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=? AND kind='video' "
            "AND missing=0", (row["video_id"],)).fetchone()["n"]
        if not left:
            conn.execute(
                "UPDATE videos SET status='missing', updated_at=? WHERE id=? "
                "AND status='downloaded'", (now_iso(), row["video_id"]))
        lost += 1
    return lost


def headless_scan() -> int:
    """`python omnistash.py --scan`: переиндексация без окна (для watchdog)."""
    from . import settings
    from .db import Database

    db = Database()
    roots = [row for row in storages_mod.all_storages(db.conn)
             if row.get("enabled", 1)]
    if not roots:
        print("Нет хранилищ: добавьте папку в настройках.")
        return 1
    config = settings.load()

    def progress(done, total, path):
        if done % 200 == 0 or done == total:
            print(f"[{done}/{total}] {path}", flush=True)

    report = scan(roots, db, progress=progress,
                  compute_hash=bool(config.get("compute_hash", True)))
    print(json.dumps({k: v for k, v in report.items() if k != "roots"},
                     ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 2
