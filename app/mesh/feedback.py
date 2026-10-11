"""Стоп-лист негодных хешей: отзыв «несовпадение» после просмотра.

Жизненный цикл (решение пользователя): скачали из mesh -> просмотрели
файла -> «несовпадение» -> хеш уходит сюда -> при следующих поисках
манифесты с этим хешом клиент молча игнорирует, а ключ-носитель
запоминается (peer в строке) - это локальная репутация без сервера.

Хранение в БД (а не в настройках): стоп-лист живёт рядом с индексом,
переживает перенос базы и опрашивается при каждом обходе манифестов.
"""

from __future__ import annotations

from ..util import now_iso


def _norm(digest: str) -> str:
    """Привести к виду files.hash («sha256:<hex>»)."""
    text = str(digest or "").strip().lower()
    if not text:
        return ""
    if not text.startswith("sha256:"):
        text = "sha256:" + text
    return text


def ban(conn, digest: str, peer: str = "", note: str = "") -> bool:
    """Поставить хеш в стоп-лист. Идемпотентно: False - уже стоял."""
    digest = _norm(digest)
    if not digest or digest == "sha256:":
        raise ValueError("пустой хеш")
    cur = conn.execute(
        "INSERT INTO mesh_bans (hash, peer, note, created_at) "
        "VALUES (?,?,?,?) ON CONFLICT(hash) DO NOTHING",
        (digest, str(peer or ""), str(note or ""), now_iso()))
    return bool(cur.rowcount)


def unban(conn, digest: str) -> bool:
    """Снять хеш («всё же совпало»). True - хеш был в списке."""
    cur = conn.execute("DELETE FROM mesh_bans WHERE hash=?", (_norm(digest),))
    return bool(cur.rowcount)


def banned_set(conn) -> set[str]:
    """Все забаненные хеши - для фильтрации манифестов на лету."""
    return {row["hash"] for row in
            conn.execute("SELECT hash FROM mesh_bans")}


def list_bans(conn) -> list[dict]:
    """Список для UI: что забанено, кем принесено и когда."""
    return [dict(row) for row in conn.execute(
        "SELECT hash, peer, note, created_at FROM mesh_bans "
        "ORDER BY created_at DESC")]


def peer_offenses(conn, peer: str) -> int:
    """Сколько раз этот ключ приносил негодные хеши (информация, не барьер).

    Блокирует только стоп-лист - по конкретному хешу, это решение человека.
    Счётчик нужен, чтобы узел с историей был виден ДО обмена (в review)
    и в карточке: «этот ключ уже приносил несовпадения: N».
    """
    if not peer:
        return 0
    row = conn.execute("SELECT COUNT(*) AS n FROM mesh_bans WHERE peer=?",
                       (str(peer),)).fetchone()
    return int(row["n"] or 0)
