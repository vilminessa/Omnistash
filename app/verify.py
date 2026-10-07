"""Проверка целостности: сверка файлов с хешем, который лежит в индексе.

Зачем: sha256 считается при загрузке и при скане - но сам по себе он ничего
не доказывает, пока с ним ничего не сверяют. Проверка отвечает на один
вопрос: «то, что лежит на диске, всё ещё то, что я индексировал?».

Результат - три разные категории, и путать их нельзя:
  * битый (хеш не совпал) - можно перекачать;
  * без хеша - проверять нечем, мы его просто заполняем;
  * нет файла - это работа скана (он пометит пропажу), здесь не подменим.
"""

from __future__ import annotations

import os

from .indexer import file_hash
from .repack import selection_rows


def _chunked(seq, size: int = 400):
    for start in range(0, len(seq), size):
        yield seq[start:start + size]


def verify(conn, selection: dict, *, progress=None, stop=None) -> dict:
    """Сверить файлы выбранных строк с их хешами в базе."""
    videos = selection_rows(conn, selection)
    if not videos:
        return {"error": "Нечего проверять: не выбрано ни одного видео"}
    ids = [int(v["id"]) for v in videos]

    files = []
    for chunk in _chunked(ids):
        marks = ",".join("?" * len(chunk))
        files.extend(dict(row) for row in conn.execute(
            f"""SELECT f.id, f.path, f.hash, f.video_id, v.title
                  FROM files f JOIN videos v ON v.id = f.video_id
                 WHERE f.kind='video' AND f.missing=0
                   AND f.video_id IN ({marks})""", chunk))
    if not files:
        return {"error": "У выбранных строк нет файлов на диске"}

    broken: list[dict] = []
    missing: list[dict] = []
    filled = 0
    checked = 0
    total = len(files)

    for row in files:
        if stop is not None and stop.is_set():
            break
        if not os.path.exists(row["path"]):
            missing.append({"video_id": row["video_id"], "path": row["path"],
                            "title": row["title"]})
            if progress:
                progress(checked, total, row["path"])
            continue
        try:
            digest = file_hash(row["path"])
        except OSError as exc:
            missing.append({"video_id": row["video_id"], "path": row["path"],
                            "title": row["title"], "error": str(exc)})
            continue
        if not row["hash"]:
            # Эталона не было: заполняем, чтобы следующая проверка уже
            # могла что-то утверждать.
            conn.execute("UPDATE files SET hash=? WHERE id=?", (digest, row["id"]))
            filled += 1
            checked += 1
            if progress:
                progress(checked, total, row["path"])
            continue
        checked += 1
        if digest != row["hash"]:
            broken.append({"video_id": row["video_id"], "file_id": row["id"],
                           "path": row["path"], "title": row["title"],
                           "expected": row["hash"], "actual": digest})
        if progress:
            progress(checked, total, row["path"])

    return {"checked": checked, "total": total, "filled": filled,
            "broken": broken, "broken_total": len(broken),
            "missing": missing, "missing_total": len(missing),
            "stopped": bool(stop is not None and stop.is_set())}
