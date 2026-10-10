"""Агрегат метаданных папки: один .omnistash.json на все видео в ней.

Режим «всё в видео» (настройка «Метаданные рядом с видео») не пишет
построчные post.json: сведения всех видео папки лежат в одном файле
рядом с ними. На каждое видео на диске остаётся один mp4 (обложка,
субтитры и метаданные вшиты внутрь), на папку - один json.

Запись повторяет payload пофайлового сайдкара (indexer.build_sidecar),
с двумя отличиями, продиктованными жизнью папки:
  * ключ - remote_id: файл опознаётся по ID в имени, а не по пути;
  * «path» - только имя файла: переезд папки имя не меняет, поэтому
    записи не нужны переписывания при каждом перемещении каталога.

Файл НЕ регистрируется в таблице files: строка на каждое видео из-за
общего файла путала бы индекс (conflict по пути перетирал бы video_id).
Из-за этого переезды и переименования переносят записи сами (relocate),
а потерянное скан дописывает из базы (self-heal в режиме embed).

Записи сливается read-modify-write c атомарной заменой (tmp -> replace):
читатели (скан) видят либо старый, либо новый файл, никогда - полфайла.
Писатели у нас два (очередь и скан), и встречаются они редко; худший
исход гонки - потерянная запись, которую следующий скан дописывает.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .metadata import PLATFORM, slim_info
from .util import now_iso

FILENAME = ".omnistash.json"


def path_for(folder) -> str:
    """Путь агрегата папки (один на каталог с видео)."""
    return os.path.join(str(folder), FILENAME)


def _cache_key(folder) -> str:
    return os.path.normcase(os.path.abspath(str(folder)))


def _read_file(folder) -> dict:
    """Сырой json агрегата; битый/отсутствующий - пусто.

    Терпимость к битому файлу важна: одна полная запись не должна
    убивать чтение остальных (скан перечитает и, при необходимости,
    перезапишет файл целиком).
    """
    try:
        data = json.loads(Path(path_for(folder)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("videos"), dict):
        return {}
    return data


def read(folder, cache: dict | None = None) -> dict:
    """Записи папки: {remote_id: record}.

    cache - кэш скана (папка -> записи): в обходке файл читается один раз
    на папку, а не на каждый встреченный файл.
    """
    key = _cache_key(folder)
    if cache is not None and key in cache:
        return cache[key]
    videos = _read_file(folder).get("videos") or {}
    if cache is not None:
        cache[key] = videos
    return videos


def find(folder, remote_id, cache: dict | None = None) -> dict | None:
    """Запись одного видео или None (нет папки/файла/записи)."""
    return read(folder, cache).get(str(remote_id))


def _merged(folder, cache: dict | None) -> dict:
    """База для правки: диск + кэш.

    Писатель не должен терять ни внешние правки файла (другой поток успел
    дописать), ни то, что уже видел в кэше (файл могли удалить между
    чтением и записью).
    """
    videos = dict(_read_file(folder).get("videos") or {})
    if cache is not None:
        videos.update(cache.get(_cache_key(folder)) or {})
    return videos


def _write(folder, data: dict, cache: dict | None = None) -> None:
    """Атомарная запись файла (tmp -> replace). OSError наружу."""
    path = path_for(folder)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    os.replace(tmp, path)
    if cache is not None:
        cache[_cache_key(folder)] = data.get("videos") or {}


def write_record(folder, remote_id, record: dict,
                 cache: dict | None = None) -> None:
    """Добавить/обновить запись, остальные записи папки сохранить.

    Незнакомые ключи файла (чужие правки) тоже не трогаем: json остаётся
    чужим там, где мы не главные.
    """
    data = _read_file(folder)
    videos = _merged(folder, cache)
    videos[str(remote_id)] = record
    data.update({"omnistash": 1, "version": 1, "updated_at": now_iso(),
                 "videos": videos})
    _write(folder, data, cache)


def drop_record(folder, remote_id, cache: dict | None = None) -> None:
    """Убрать запись; последняя запись - убрать и сам файл."""
    videos = _merged(folder, cache)
    remote_id = str(remote_id)
    if remote_id not in videos:
        return
    videos.pop(remote_id, None)
    if not videos:
        try:
            os.remove(path_for(folder))
        except OSError:
            pass  # не удалился - не беда, скан перезапишет
        if cache is not None:
            cache[_cache_key(folder)] = {}
        return
    data = _read_file(folder)
    data.update({"omnistash": 1, "version": 1, "updated_at": now_iso(),
                 "videos": videos})
    _write(folder, data, cache)


def relocate(old_folder, remote_id, new_folder, new_name: str) -> None:
    """Запись едет вслед за видео (переезд папки или переименование).

    Сначала пишется запись в новую папку, потом удаляется из старой:
    сохранность данных важнее чистоты - сбой на середине оставит
    безобидный дубль в старой папке, а не потерю метаданных.
    """
    record = find(old_folder, remote_id)
    if record is None:
        return                      # файлов режима/записи не было - нечего двигать
    record = dict(record)
    record["path"] = new_name
    if _cache_key(old_folder) == _cache_key(new_folder):
        write_record(old_folder, remote_id, record)
        return
    write_record(new_folder, remote_id, record)
    drop_record(old_folder, remote_id)


def build_record(info: dict, path, digest: str | None = None) -> dict:
    """Payload записи из info-dict качалки: тот же состав, что у post.json."""
    return {
        "omnistash": 1,
        "platform": PLATFORM,
        "remote_id": str(info.get("id") or ""),
        # Имя, а не путь: папку переехали - имя файла не изменилось.
        "path": os.path.basename(str(path)),
        "size": os.path.getsize(path) if os.path.exists(path) else None,
        "hash": digest,
        "created_at": now_iso(),
        # slim_info: полезное целиком, списки форматов - сводкой.
        "info": json.loads(slim_info(info) or "{}"),
    }
