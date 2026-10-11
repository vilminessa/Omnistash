"""Подписанные манифесты: «у меня есть файл remote_id с хешом H».

Манифест - единственный факт, который mesh принимает от узла: сам файл
мы проверяем сами (sha256 + структура + своя строка в базе). Заявление
подписано ключом узла (Ed25519), поэтому:
  * подделать чужой адрес/хеш по пути нельзя;
  * утверждение привязано к ключу - ключ можно запомнить (репутация,
    стоп-лист) и опознать при следующих поисках.

Канонизация подписи: JSON с сортированными ключами и без пробелов -
порядок полей в объекте не влияет на подпись.
"""

from __future__ import annotations

import json
import time

from . import identity

PAYLOAD_KEYS = ("remote_id", "sha256", "size", "synced_at", "ts")


def _canonical(payload: dict) -> bytes:
    """Байты подписи: только payload-ключи, детерминированный порядок."""
    return json.dumps(
        {key: payload[key] for key in PAYLOAD_KEYS},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def build(remote_id: str, digest: str, size: int, synced_at: str | None = None,
          now: int | None = None) -> dict:
    """Манифест для собственного файла.

    synced_at - дата последней синхронизации ЭТОЙ строки с площадкой
    (videos.synced_at): получатель фильтрует/сортирует узлов по свежести.
    """
    payload = {
        "remote_id": str(remote_id),
        "sha256": str(digest),
        "size": int(size or 0),
        "synced_at": str(synced_at or ""),
        "ts": int(now if now is not None else time.time()),
    }
    pub = identity.get_identity()[1]
    sig = identity.sign(_canonical(payload))
    return {**payload, "pubkey": pub.hex(), "sig": sig.hex()}


def verify(manifest: dict) -> str | None:
    """None - подпись верна; иначе - причина отказа.

    Проверяется подпись ЧУЖОГО ключа; отдельно смотрите banned-фильтр
    по хешу (feedback.banned_set) и консенсус (verify.review).
    """
    if not isinstance(manifest, dict):
        return "манифест не объект"
    missing = [key for key in PAYLOAD_KEYS if key not in manifest]
    if missing:
        return f"нет полей: {', '.join(missing)}"
    try:
        pub = bytes.fromhex(str(manifest.get("pubkey") or ""))
        sig = bytes.fromhex(str(manifest.get("sig") or ""))
    except ValueError:
        return "ключ или подпись не hex"
    if len(pub) != 32:
        return "недопустимая длина ключа"
    payload = {key: manifest[key] for key in PAYLOAD_KEYS}
    if not identity.verify(pub, _canonical(payload), sig):
        return "подпись не сходится"
    return None


def claims(manifests: list) -> dict:
    """Сводка предложений: {sha256: [synced_at узлов]} - что и сколько
    узлов подтверждают (информация для решения, не барьер)."""
    out: dict[str, list] = {}
    for item in manifests or []:
        digest = str((item or {}).get("sha256") or "")
        if not digest:
            continue
        out.setdefault(digest, []).append(str(item.get("synced_at") or ""))
    return out
