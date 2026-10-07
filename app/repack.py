"""Переупаковка: привести уже собранные файлы к текущему шаблону имени.

Чем это отличается от переноса (migrate.py):
  * переупаковка - ПЕРЕИМЕНОВАНИЕ ВНУТРИ того же хранилища (та же папка,
    другие имя/подпапки) - файл никуда не едет между дисками;
  * перенос - перевод в ДРУГОЕ хранилище, с копированием и проверкой.

Здесь, как и везде, действует правило «не гадать»: если метаданных не
хватает, чтобы построить путь, файл остаётся как есть и попадает в отчёт
с причиной, а не получает `NA/NA - ...` в имени.

Путь строит сам yt-dlp (`prepare_filename`) - шаблон у нас его синтаксис,
и переизобретать его сопряжения нельзя.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yt_dlp

from . import indexer
from .util import SIDECAR_SUFFIX

DISPLAY_LIMIT = 100     # сколько «было -> станет» отдаём в превью
MOVE_LIMIT = 50000      # защита от «переупакуй всё разом» без разбора
_FIELDS_RE = re.compile(r"%\((\w+)[^)]*\)")


def required_fields(template: str) -> set[str]:
    """Какие ключа шаблона нужны (кроме ext - его подставляем сами)."""
    return {key for key in _FIELDS_RE.findall(template) if key != "ext"}


def _info_for(video: dict, ext: str) -> dict:
    """Метаданные для построения пути: raw_json + то, что есть в колонках."""
    try:
        info = json.loads(video.get("raw_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        info = {}
    if not isinstance(info, dict):
        info = {}
    info = dict(info)
    info["id"] = video.get("remote_id") or info.get("id") or ""
    info["title"] = video.get("title") or info.get("title") or "без названия"
    info["ext"] = ext
    if video.get("uploaded_at") and not info.get("upload_date"):
        info["upload_date"] = str(video["uploaded_at"]).replace("-", "")
    if video.get("channel") and not info.get("channel"):
        info["channel"] = video["channel"]
    return info


def missing_fields(template: str, info: dict) -> list[str]:
    """Чего не хватает шаблону: список пуст == путь можно построить."""
    missing = []
    for key in required_fields(template):
        value = info.get(key)
        if value in (None, "", "NA"):
            missing.append(key)
    return sorted(missing)


def _selection(conn, selection: dict) -> list[dict]:
    """Строки, которые подлежат переупаковке: явные id или область."""
    ids = [int(v) for v in (selection or {}).get("ids") or []]
    if ids:
        rows = []
        marks = ",".join("?" * len(ids))
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows.extend(dict(r) for r in conn.execute(
                f"""SELECT v.id, v.key, v.platform, v.remote_id, v.title,
                           v.uploaded_at, v.raw_json,
                           c.title AS channel, s.path AS storage_path,
                           s.id AS storage_id, s.label AS storage_label
                      FROM videos v
                      LEFT JOIN channels c ON c.id = v.channel_id
                      LEFT JOIN files f ON f.video_id = v.id AND f.kind='video'
                      LEFT JOIN storages s ON s.id = f.storage_id
                     WHERE v.id IN ({",".join("?" * len(chunk))})
                     ORDER BY v.id""", chunk))
        return _dedupe_videos(rows)

    scope = (selection or {}).get("scope") or {"type": "pool"}
    joins = "LEFT JOIN playlist_items pi ON pi.video_id = v.id AND pi.removed_at IS NULL"
    where, args = [], []
    if scope.get("type") == "channel":
        where.append("v.channel_id = ?")
        args.append(scope.get("id"))
    elif scope.get("type") == "playlist":
        where.append("pi.playlist_id = ?")
        args.append(scope.get("id"))
    else:
        where.append("f.id IS NOT NULL")   # «Пул»: только то, что лежит на диске
    sql = f"""SELECT v.id, v.key, v.platform, v.remote_id, v.title,
                     v.uploaded_at, v.raw_json,
                     c.title AS channel, s.path AS storage_path,
                     s.id AS storage_id, s.label AS storage_label
                FROM videos v
                {joins}
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN files f ON f.video_id = v.id AND f.kind='video'
                LEFT JOIN storages s ON s.id = f.storage_id
               WHERE {' AND '.join(where)}
               ORDER BY v.id"""
    return _dedupe_videos(dict(r) for r in conn.execute(sql, args))


def _dedupe_videos(rows) -> list[dict]:
    """Видео с двумя копиями попадают в выборку дважды (JOIN files)."""
    seen, out = set(), []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(row)
    return out


def _aux_files(conn, video_id: int, old_stem: str) -> list[dict]:
    """Сайдкар/субтитры/обложка: их имена строятся от стема видео."""
    out = []
    for row in conn.execute(
            """SELECT id, kind, path FROM files
                WHERE video_id=? AND kind<>'video' AND missing=0""",
            (video_id,)):
        name = os.path.basename(row["path"])
        if not name.startswith(old_stem):
            continue          # чужой стем: к этому видео не относится
        out.append(dict(row))
    return out


def plan_repack(conn, selection: dict, template: str) -> dict:
    """Превью переупаковки: что переименуется, что конфликтует, что нельзя."""
    if not str(template or "").strip():
        return {"error": "Шаблон имени пустой"}
    if "%" not in str(template):
        return {"error": "Похоже, шаблон без полей (нет ни одного %(…)s)"}

    videos = _selection(conn, selection)
    if not videos:
        return {"error": "Нечего переупаковывать: не выбрано ни одного файла"}
    if len(videos) > MOVE_LIMIT:
        return {"error": f"Слишком много файлов за раз (лимит {MOVE_LIMIT})"}

    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                            "ignoreerrors": True})
    items: list[dict] = []
    rename_display: list[dict] = []
    conflicts: list[dict] = []
    no_meta: list[dict] = []
    unchanged = 0
    taken: set[str] = set()          # цели внутри этого же плана
    total_bytes = 0

    for video in videos:
        storage_path = video.get("storage_path")
        if not storage_path:
            no_meta.append({"title": video.get("title"),
                            "reason": "файл не привязан к хранилищу"})
            continue
        if video.get("platform") != "youtube":
            no_meta.append({"title": video.get("title"),
                            "reason": "нет метаданных площадки (локальный файл)"})
            continue

        files = [dict(r) for r in conn.execute(
            """SELECT id, kind, path, size FROM files
                WHERE video_id=? AND missing=0 ORDER BY kind='video' DESC""",
            (video["id"],))]
        video_file = next((f for f in files if f["kind"] == "video"), None)
        if not video_file:
            no_meta.append({"title": video.get("title"),
                            "reason": "файл не найден на диске"})
            continue

        ext = Path(video_file["path"]).suffix.lstrip(".")
        info = _info_for(video, ext)
        absent = missing_fields(template, info)
        if absent:
            no_meta.append({"title": video.get("title"),
                            "reason": "нет полей: " + ", ".join(absent)})
            continue

        target = ydl.prepare_filename(
            info, outtmpl=os.path.join(storage_path, template))
        if not target:
            no_meta.append({"title": video.get("title"),
                            "reason": "шаблон не собрался"})
            continue
        target = os.path.normpath(target)
        root = os.path.normpath(storage_path)
        if not (target == root or target.startswith(root + os.sep)):
            # Шаблон с абсолютным путём увёл бы файл из хранилища.
            no_meta.append({"title": video.get("title"),
                            "reason": "шаблон выводит за пределы хранилища"})
            continue

        if os.path.normcase(target) == os.path.normcase(video_file["path"]):
            unchanged += 1
            continue
        key = os.path.normcase(target)
        if key in taken:
            conflicts.append({"path": video_file["path"], "target": target,
                              "title": video.get("title")})
            continue
        if os.path.exists(target):
            conflicts.append({"path": video_file["path"], "target": target,
                              "title": video.get("title")})
            continue
        taken.add(key)

        new_stem = Path(target).stem
        old_stem = Path(video_file["path"]).stem
        aux = []
        for row in _aux_files(conn, video["id"], old_stem):
            name = os.path.basename(row["path"])
            suffix = name[len(old_stem):]
            if row["kind"] == "video":
                continue
            aux.append({"id": row["id"], "kind": row["kind"],
                        "old_path": row["path"],
                        "new_path": os.path.join(os.path.dirname(target),
                                                 new_stem + suffix)})

        items.append({"video_id": video["id"], "title": video.get("title"),
                      "storage_id": video["storage_id"],
                      "storage_path": storage_path,
                      "old_path": video_file["path"], "new_path": target,
                      "old_rel": _rel(storage_path, video_file["path"]),
                      "new_rel": _rel(storage_path, target),
                      "file_id": video_file["id"],
                      "size": video_file.get("size") or 0,
                      "aux": aux})
        total_bytes += video_file.get("size") or 0
        if len(rename_display) < DISPLAY_LIMIT:
            rename_display.append({"video_id": video["id"],
                                   "title": video.get("title"),
                                   "from": video_file["path"],
                                   "to": target})

    return {"template": template, "count": len(items),
            "rename": rename_display, "rename_total": len(items),
            "unchanged": unchanged, "conflicts": conflicts,
            "conflict_total": len(conflicts),
            "no_meta": no_meta, "no_meta_total": len(no_meta),
            "bytes": total_bytes, "selected": len(videos), "items": items}


def _rel(storage_path: str, path: str) -> str:
    """Путь относительно хранилища (канон файлов в индексе)."""
    prefix = os.path.normpath(storage_path).rstrip("\\/") + os.sep
    normalized = os.path.normpath(path)
    if normalized.lower().startswith(prefix.lower()):
        return normalized[len(prefix):]
    return os.path.basename(normalized)


class RepackCancelled(Exception):
    """Переупаковка прервана: уже переименованное остаётся, строки целы."""


def _move(src: str, dst: str) -> None:
    """Переименование с запасным путём: на разных томах replace не работает."""
    try:
        os.replace(src, dst)
    except OSError:
        import shutil
        shutil.move(src, dst)


def apply_repack(conn, plan: dict, *, stop=None, progress=None) -> dict:
    """Выполнить план переупаковки.

    Порядок на одно видео: файл -> рядом лежащее -> строки в базе ->
    путь внутри сайдкара. Отказ на любом шаге откатывает ЭТО видео
    обратно: индекс не должен указывать на несуществующий путь (а если
    откат не удался - следующий скан всё равно найдёт файл по хешу).
    """
    items = plan.get("items") or []
    total_files = sum(1 + len(item.get("aux") or []) for item in items)
    done_files = 0
    renamed = 0
    errors: list[str] = []
    old_dirs: set[str] = set()
    roots: set[str] = set()

    for item in items:
        if stop is not None and stop.is_set():
            raise RepackCancelled()
        moved: list[tuple[str, str]] = []
        try:
            os.makedirs(os.path.dirname(item["new_path"]) or ".", exist_ok=True)
            _move(item["old_path"], item["new_path"])
            moved.append((item["old_path"], item["new_path"]))
            done_files += 1
            if progress:
                progress(done_files, total_files, item["new_path"])

            for aux in item.get("aux") or []:
                os.makedirs(os.path.dirname(aux["new_path"]) or ".",
                            exist_ok=True)
                _move(aux["old_path"], aux["new_path"])
                moved.append((aux["old_path"], aux["new_path"]))
                done_files += 1
                if progress:
                    progress(done_files, total_files, aux["new_path"])

            conn.execute("UPDATE files SET path=?, rel_path=? WHERE id=?",
                         (item["new_path"], item["new_rel"], item["file_id"]))
            for aux in item.get("aux") or []:
                conn.execute("UPDATE files SET path=?, rel_path=? WHERE id=?",
                             (aux["new_path"],
                              _rel(item["storage_path"], aux["new_path"]),
                              aux["id"]))

            # В сайдкаре лежит путь до видео - иначе он станет врать.
            sidecar = next((new for _old, new in moved
                            if new.lower().endswith(SIDECAR_SUFFIX)), None)
            if sidecar:
                indexer.rewrite_sidecar(item["new_path"], sidecar)

            old_dirs.add(os.path.dirname(item["old_path"]))
            # Пустые папки чистим только ВНУТРИ хранилища: сам корень
            # трогать нельзя - это само хранилище, а не его подпапка.
            roots.add(os.path.normpath(item["storage_path"]))
            renamed += 1
        except (OSError, Exception) as exc:  # noqa: BLE001
            # Откат именно этого видео: старые пути возвращаем.
            for old, new in reversed(moved):
                try:
                    _move(new, old)
                except OSError:
                    errors.append(f"не удалось вернуть {new}: {exc}")
            errors.append(f"{item.get('title') or item['old_path']}: {exc}")
            if progress:
                progress(done_files, total_files, item.get("title") or "")

    # Пустые папки, которые освободились (самые глубокие первыми), - но
    # не выше корня хранилища.
    pruned = 0
    for directory in sorted(old_dirs, key=lambda p: len(p), reverse=True):
        current = directory
        while current and os.path.normpath(current) not in roots:
            try:
                os.rmdir(current)
                pruned += 1
            except OSError:
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    return {"renamed": renamed, "files": done_files, "errors": errors,
            "dirs_removed": pruned, "skipped_conflicts": len(plan.get("conflicts") or []),
            "skipped_no_meta": len(plan.get("no_meta") or []),
            "total": len(items)}


def safe_display(items: list[dict], limit: int = DISPLAY_LIMIT) -> list[dict]:
    """Список для окна: без служебных полей и с ограничением."""
    return [{"video_id": i["video_id"], "title": i.get("title"),
             "from": i["old_path"], "to": i["new_path"]}
            for i in items[:limit]]
