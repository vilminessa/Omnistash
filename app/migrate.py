"""Перенос файлов между хранилищами: копия -> проверка -> удаление оригинала.

Почему не «просто переместить»: перенос - это единственная операция, где
программа сама уничтожает данные. Поэтому порядок жёсткий:

    1. копия в цель (с прогрессом, с возможностью остановиться);
    2. проверка: sha256 источника и цели совпали;
    3. только теперь строка в базе переключается на новый путь;
    4. оригинал удаляется последним.

На любом шаге отказ означает «ничего не потеряно»: цель подчищается,
строка остаётся у источника, а уже перенесённое при повторе просто
пропускается (строка уже указывает на цель) - отсюда и возобновление.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .indexer import file_hash

CHUNK = 1024 * 1024          # копирование кусками: так виден прогресс
MOVE_LIMIT = 5000            # защита от «перенеси мне всю библиотеку разом»


class MigrateCancelled(Exception):
    """Пользователь прервал перенос: уже скопированное остаётся на месте."""


def _rel_of(row: dict) -> str:
    """Относительный путь файла в его хранилище.

    Канон - rel_path; у старых строк (до миграции) его нет, тогда
    восстанавливаем от префикса хранилища, а в самом худшем случае
    берём имя файла: структура сохранится по возможности.
    """
    rel = row.get("rel_path")
    if rel:
        return rel
    storage_path = row.get("storage_path")
    if storage_path:
        prefix = storage_path.rstrip("\\/") + os.sep
        if row["path"].lower().startswith(prefix.lower()):
            return row["path"][len(prefix):]
    return os.path.basename(row["path"])


def collect_files(conn, video_ids) -> list[dict]:
    """Все файлы выбранных видео: само видео и всё, что лежит рядом."""
    ids = [int(v) for v in video_ids]
    if not ids:
        return []
    rows = []
    marks = ",".join("?" * len(ids))
    for chunk_start in range(0, len(ids), 400):
        chunk = ids[chunk_start:chunk_start + 400]
        rows.extend(dict(r) for r in conn.execute(
            f"""SELECT f.id, f.video_id, f.kind, f.path, f.size, f.mtime,
                       f.hash, f.storage_id, f.rel_path,
                       s.path AS storage_path, s.label AS storage_label
                  FROM files f LEFT JOIN storages s ON s.id = f.storage_id
                 WHERE f.video_id IN ({",".join("?" * len(chunk))})
                   AND f.missing = 0
                 ORDER BY CASE f.kind WHEN 'video' THEN 0 ELSE 1 END, f.id""",
            chunk))
    return rows


def plan_move(conn, video_ids, target: dict) -> dict:
    """Превью переноса: что пойдёт, что конфликтует, чего уже нет."""
    rows = collect_files(conn, video_ids)
    if not rows:
        return {"error": "Не выбраны файлы для переноса"}
    target_path = target["path"].rstrip("\\/")

    items: list[dict] = []
    conflicts: list[dict] = []
    already: list[dict] = []
    missing: list[dict] = []
    same_storage = 0
    total_bytes = 0

    for row in rows:
        if row.get("storage_id") == target["id"]:
            same_storage += 1
            continue
        if not os.path.exists(row["path"]):
            missing.append({"path": row["path"]})
            continue
        # Размер берём с диска: в БД он мог устареть, а сравнение в цели
        # идёт именно по фактическому размеру.
        try:
            size = os.path.getsize(row["path"])
        except OSError:
            size = row.get("size") or 0
        rel = _rel_of(row)
        destination = os.path.join(target_path, rel)
        if os.path.exists(destination):
            # То же имя в цели: либо уже переносили, либо это чужой файл.
            try:
                same_size = os.path.getsize(destination) == size
            except OSError:
                same_size = False
            entry = {"path": row["path"], "target": destination,
                     "size": size}
            (already if same_size else conflicts).append(entry)
            continue
        items.append({"file_id": row["id"], "video_id": row["video_id"],
                      "kind": row["kind"], "src": row["path"],
                      "dst": destination, "rel": rel, "size": size,
                      "hash": row.get("hash")})
        total_bytes += size

    if len(items) > MOVE_LIMIT:
        return {"error": f"Слишком много файлов за раз (лимит {MOVE_LIMIT})"}

    return {"files": items, "bytes": total_bytes, "count": len(items),
            "conflicts": conflicts, "already": already, "missing": missing,
            "same_storage": same_storage,
            "target": {"id": target["id"], "path": target_path,
                       "label": target["label"]}}


def _copy_with_progress(src: str, dst: str, stop, progress) -> str:
    """Копия кусками + sha256 ИСТОЧНИКА по ходу дела.

    Прерывание оставляет хвост только в ЦЕЛИ (источник цел всегда), а хеш
    источника считается попутно - отдельное чтение файла не нужно.
    Возвращает "sha256:<hex>".
    """
    import hashlib

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    temp = dst + ".part"
    source_digest = hashlib.sha256()
    copied = 0
    with open(src, "rb") as source, open(temp, "wb") as target:
        while True:
            if stop is not None and stop.is_set():
                raise MigrateCancelled()
            chunk = source.read(CHUNK)
            if not chunk:
                break
            source_digest.update(chunk)
            target.write(chunk)
            copied += len(chunk)
            if progress:
                progress(copied)
    os.replace(temp, dst)
    return "sha256:" + source_digest.hexdigest()


def _rewrite_sidecar(video_path: str, sidecar_path: str) -> None:
    """Внутри post.json лежит путь к видео - поправляем после переноса."""
    if not video_path:
        return
    try:
        data = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(data, dict) or data.get("path") == video_path:
        return
    data["path"] = video_path
    try:
        Path(sidecar_path).write_text(json.dumps(data, ensure_ascii=False),
                                      encoding="utf-8")
    except OSError:
        pass  # не смог переписать - не беда: скан пересоберёт сайдкар


def move_files(conn, plan: dict, target: dict, *, stop=None, progress=None) -> dict:
    """Выполнить перенос по плану. Возвращает итог (см. docstring модуля)."""
    items = plan.get("files") or []
    total_bytes = plan.get("bytes") or 0
    # Новые пути видео - чтобы сайдкар мог записать путь своего видео.
    new_video_path = {item["video_id"]: item["dst"] for item in items
                      if item["kind"] == "video"}

    done_files = 0
    done_bytes = 0
    errors: list[str] = []
    total_done = 0

    for item in items:
        if stop is not None and stop.is_set():
            raise MigrateCancelled()

        def progress_chunk(copied: int, item=item) -> None:
            if progress:
                progress(done_bytes + copied, total_bytes, done_files,
                         len(items), item["src"])

        try:
            source_digest = _copy_with_progress(item["src"], item["dst"], stop,
                                                progress_chunk)
            # Сверяемся с тем, что реально ЛЕЖИТ в цели (читаем обратно):
            # значение hash из БД может быть устаревшим, если содержимое
            # файла менялось после индексации.
            target_digest = file_hash(item["dst"])
        except MigrateCancelled:
            # Недокопированная цель мусор: убираем, строка всё ещё у источника.
            _silent_remove(item["dst"] + ".part")
            _silent_remove(item["dst"])
            raise
        except OSError as exc:
            _silent_remove(item["dst"] + ".part")
            _silent_remove(item["dst"])
            errors.append(f"{item['src']}: {exc}")
            continue

        if source_digest and target_digest != source_digest:
            # Проверка не сошлась: цель удаляем, источник не трогаем.
            _silent_remove(item["dst"])
            errors.append(f"{item['src']}: контрольная сумма не совпала "
                          "после копирования")
            continue

        stat = os.stat(item["dst"])
        try:
            conn.execute(
                """UPDATE files SET path=?, storage_id=?, rel_path=?, size=?,
                       mtime=?, hash=?, missing=0 WHERE id=?""",
                (item["dst"], target["id"], item["rel"], stat.st_size,
                 stat.st_mtime, target_digest, item["file_id"]))
        except Exception as exc:  # noqa: BLE001 - строка не обновилась
            _silent_remove(item["dst"])
            errors.append(f"{item['src']}: не удалось обновить запись - {exc}")
            continue

        if item["kind"] == "sidecar":
            moved_video = new_video_path.get(item["video_id"])
            if moved_video:
                _rewrite_sidecar(moved_video, item["dst"])

        try:
            os.remove(item["src"])
        except OSError as exc:
            # Копия уже в цели и в базе: источник остался - это честный
            # дубль, его подберёт сверка, а не потерянные данные.
            errors.append(f"перенесено, но источник не удалился ({item['src']}): {exc}")

        done_files += 1
        done_bytes += item["size"] or 0
        total_done += 1
        if progress:
            progress(done_bytes, total_bytes, done_files, len(items), item["src"])

    return {"done": done_files, "total": len(items),
            "bytes": done_bytes, "errors": errors,
            "skipped_conflicts": len(plan.get("conflicts") or []),
            "already": len(plan.get("already") or []),
            "missing": len(plan.get("missing") or []),
            "target": plan.get("target")}


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def new_video_paths_for_sidecars(plan: dict) -> dict[int, str]:
    """video_id -> новый путь видео (для правки сайдкаров)."""
    return {item["video_id"]: item["dst"] for item in plan.get("files") or []
            if item["kind"] == "video"}
