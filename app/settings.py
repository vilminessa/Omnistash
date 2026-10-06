"""Настройки: чтение/запись settings.json, переносы старых ключей.

Схема (тип, границы, подписи) - в settings_schema; этот модуль отвечает
только за файл: пути, слияние с дефолтами, приведение типов и атомарную
запись (tmp -> replace, чтобы ползущее сохранение не порвало файл).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import settings_schema
from .paths import settings_path

# Переносы старых ключей: (старый, новый, значение по старому ключу).
# Новый ключ не должен быть уже задан явно - тогда перенос уважает выбор
# пользователя, а не затирает его.
MIGRATIONS: tuple = ()


def load() -> dict:
    """Дефолты из схемы + пользовательские значения поверх.

    Файл перезаписывается, только если это нужно: его нет, он битый, в нём
    не хватает новых ключей или значение не совпадает с типом схемы.
    Обычное чтение файл не трогает.
    """
    settings = settings_schema.defaults()
    try:
        raw = settings_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        save(settings)
        return settings

    changed = False
    for old, new, mapper in MIGRATIONS:
        if old in data and new not in data:
            data[new] = mapper(data[old])
            changed = True

    for key in list(settings):
        if key not in data:
            # Новый ключ появился в обновлении - дописываем его в файл.
            changed = True
            continue
        fixed = settings_schema.coerce(key, data[key])
        if fixed != data[key]:
            changed = True
        settings[key] = fixed

    if changed:
        save(settings)
    return settings


def save(settings: dict) -> None:
    """Записать настройки, оставив в файле только ключи, известные схеме."""
    clean = {}
    for key, default in settings_schema.defaults().items():
        if key in settings:
            clean[key] = settings_schema.coerce(key, settings[key])
        else:
            clean[key] = default
    path = settings_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def set_value(key: str, value) -> dict:
    """Привести значение к типу поля, сохранить и вернуть свежие настройки.

    Неизвестный ключ молча отбрасывается (схема - единственный источник
    правды, «случайные» ключи в файле не живут).
    """
    settings = load()
    if settings_schema.field(key) is None:
        return settings
    settings[key] = settings_schema.coerce(key, value)
    save(settings)
    return settings


def reload_if_changed(cache: dict) -> dict:
    """Перечитать файл, если он менялся снаружи (mtime-кэш).

    Нужно poll(): карточка настроек и окно обязаны видеть изменения,
    сделанные не через API (например, правку файла руками).
    """
    path = settings_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    if cache.get("_mtime") != stamp:
        cache.clear()
        cache.update(load())
        cache["_mtime"] = stamp
    return cache


def as_public(settings: dict) -> dict:
    """Настройки для окна: без служебного _mtime."""
    return {k: v for k, v in settings.items() if not k.startswith("_")}


def path_of(settings: dict, key: str) -> Path:
    """Значение-путь как Path с раскрытием переменных окружения."""
    value = str(settings.get(key) or "")
    return Path(os.path.expandvars(os.path.expanduser(value)))
