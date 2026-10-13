"""Подписанные каталоги участников: «что у меня есть» (B3, синхронизация).

Каталог - снимок скачанного индекса узла: список строк (remote_id, имя,
длительность, дата синка, версия строки, хеш файла). Подписан ключом
узла тем же способом, что манифесты, поэтому у зеркала есть проверяемый
автор, а чужой каталог нельзя подделать по пути.

Зачем синхронизация индексу, если восстановление принимает только СВОИ
метаданные: каталог отвечает на вопрос «где это лежит сейчас» - после
утраты файла, «переезда» или чистой установки второй машине видно, что
файл жив у участника (и с каким хешом), а восстановление идёт привычным
путём mesh.fetch со сверкой по своей строке.

Мерж (repo.catalog_apply) - LWW по версии строки (videos.updated_at) в
рамках одного origin: у каждого узла своя шкала времени, сравнивать
времена РАЗНЫХ узлов нельзя и не нужно. Снимок авторитетен для своего
origin: строк, которых в нём больше нет, в зеркале не остаётся.
"""

from __future__ import annotations

import json
import time

from . import identity

ENTRY_KEYS = ("remote_id", "title", "duration_s", "synced_at",
              "updated_at", "sha256")
MAX_ENTRIES = 5000            # больше - уже не каталог, а выгрузка базы


def _canonical(snapshot: dict) -> bytes:
    """Байты подписи: детерминированный JSON снапшота (без pubkey/sig)."""
    return json.dumps(
        {"generated_at": snapshot.get("generated_at"),
         "entries": snapshot.get("entries")},
        ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def build(entries: list[dict], now: int | None = None) -> dict:
    """Снапшот собственного каталога, подписанный нашим ключом."""
    clean = []
    for item in (entries or [])[:MAX_ENTRIES]:
        clean.append({
            "remote_id": str(item.get("remote_id") or ""),
            "title": str(item.get("title") or ""),
            "duration_s": (int(item["duration_s"])
                           if isinstance(item.get("duration_s"), int)
                           else None),
            "synced_at": str(item.get("synced_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
            "sha256": str(item.get("sha256") or ""),
        })
    clean.sort(key=lambda entry: entry["remote_id"])
    snapshot = {"generated_at": str(now if now is not None else time.time()),
                "entries": clean}
    snapshot["pubkey"] = identity.get_identity()[1].hex()
    snapshot["sig"] = identity.sign(_canonical(snapshot)).hex()
    return snapshot


def verify(snapshot) -> tuple[list[dict] | None, str | None]:
    """(чистые записи, None) или (None, причина отказа).

    Проверяется подпись ЧУЖОГО ключа; отдельно вызывающая сторона
    сверяет pubkey с ключом, открывшим TLS-канал (как в манифестах).
    """
    if not isinstance(snapshot, dict):
        return None, "каталог не объект"
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        return None, "в каталоге нет списка записей"
    if len(entries) > MAX_ENTRIES:
        return None, f"слишком много записей ({len(entries)})"
    try:
        pub = bytes.fromhex(str(snapshot.get("pubkey") or ""))
        sig = bytes.fromhex(str(snapshot.get("sig") or ""))
    except ValueError:
        return None, "ключ или подпись не hex"
    if len(pub) != 32:
        return None, "недопустимая длина ключа"
    if not identity.verify(pub, _canonical(snapshot), sig):
        return None, "подпись каталога не сходится"

    clean: list[dict] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            return None, "запись каталога не объект"
        remote_id = str(item.get("remote_id") or "")
        if not remote_id or remote_id in seen:
            return None, "пустой или повторяющийся remote_id"
        seen.add(remote_id)
        clean.append({
            "remote_id": remote_id,
            "title": str(item.get("title") or ""),
            "duration_s": (int(item["duration_s"])
                           if isinstance(item.get("duration_s"), int)
                           else None),
            "synced_at": str(item.get("synced_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
            "sha256": str(item.get("sha256") or ""),
        })
    return clean, None
