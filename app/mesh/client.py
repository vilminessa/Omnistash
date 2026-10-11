"""Mesh-клиент: манифесты участников и загрузка файла с резюме.

Порядок (см. verify.review): адресаты -> подписанные манифесты ->
выбор хеша -> загрузка в .part с дозапиской -> своя проверка. Клиент
НЕ принимает решений за человека: он собирает факты, фильтрует
стоп-лист и подписи, а барьеры - у verify.review.

Сетевые сбои адресатов не фатальны: недоступный участник просто
выпадает из выборки (у нас же могли не ответить все).
"""

from __future__ import annotations

import json
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import manifest as manifest_mod

CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 60.0          # большие файлы читаем чанками долго
CHUNK = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
USER_AGENT = "OmnistashMesh/1"


class MeshCancelled(Exception):
    """Остановлено пользователем (stop-событие очереди/окна)."""


def _open(url: str, timeout: float = CONNECT_TIMEOUT, headers=None):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout)


def collect_manifests(remote_id: str, addrs: list[str]) -> list[tuple]:
    """Манифесты всех адресатов: [(addr, manifest)], сбой - молча мимо.

    Подписи здесь НЕ проверяются - это делает verify.review, чтобы
    недействительные заявления просто не участвовали в счётчиках.
    """
    def one(addr: str):
        with _open(f"http://{addr}/manifest/{remote_id}",
                   timeout=CONNECT_TIMEOUT) as response:
            raw = response.read(MAX_MANIFEST_BYTES)
        return addr, json.loads(raw.decode("utf-8"))

    out: list[tuple] = []
    if not addrs:
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(addrs))) as pool:
        for future in [pool.submit(one, addr) for addr in addrs]:
            try:
                out.append(future.result())
            except Exception:  # noqa: BLE001 - участник недоступен/соврал
                continue
    return out


def pick_digest(pairs: list[tuple], banned=frozenset()) -> str | None:
    """Хеш для загрузки: мажоритарный среди подписанных незабаненных."""
    counts: Counter = Counter()
    for _addr, item in pairs:
        if manifest_mod.verify(item) is not None:
            continue
        digest = str(item.get("sha256") or "")
        if not digest or digest in banned:
            continue
        counts[digest] += 1
    return counts.most_common(1)[0][0] if counts else None


def fetch(remote_id: str, addr: str, part_path, *, progress=None,
          stop=None) -> dict:
    """Загрузка (или дозагрузка) в part_path. Возвращает
    {resumed_from, written, total} - хеш считает вызывающая сторона.
    """
    import os
    existing = 0
    if os.path.exists(part_path):
        existing = os.path.getsize(part_path)
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    mode = "wb"
    try:
        with _open(f"http://{addr}/file/{remote_id}",
                   timeout=CONNECT_TIMEOUT, headers=headers) as response:
            status = getattr(response, "status", 200)
            if existing and status != 206:
                # Участник проигнорировал Range: начинаем заново.
                existing = 0
                mode = "wb"
            else:
                mode = "ab" if existing else "wb"
            declared = response.headers.get("Content-Length")
            total = existing + int(declared) if declared else None
            written = 0
            with open(part_path, mode) as out:
                while True:
                    if stop is not None and stop.is_set():
                        raise MeshCancelled("остановлено пользователем")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(existing + written, total)
    except MeshCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - причину показывает вызывающий
        raise RuntimeError(f"перенос с {addr} не удался: {exc}") from exc
    return {"resumed_from": existing, "written": written}


def sniffer_ext(path) -> str:
    """Расширение файла по сигнатуре содержимого (не по чужим словам).

    Имя мы формируем САМИ (шаблон с [id]), поэтому стороннее расширение
    не используется - оно лишь подсказка; а подделать «внутренности»
    всё равно не даёт проверка хеша.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return "mp4"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML: matroska/webm - разводим по DocType внутри (первые КБ).
        try:
            with open(path, "rb") as handle:
                blob = handle.read(4096)
            return "webm" if b"webm" in blob else "mkv"
        except OSError:
            return "mkv"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return "mp4"   # YouTube по умолчанию отдаёт mp4
