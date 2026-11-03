"""Байтовые маршруты клиента: обложки, байты медиа, субтитры (§3, шаг S2).

Тот же приём раздачи, что mesh-сервер (app/mesh/server.py): чанки под
`pacing.RateLimiter`, `Range` с 206 и честным 416, отказ соединения не валит
сервер. Чем отличается: права решает токен со скоупами, и пути дисков наружу
не уходят - адрес строится по id строки из индекса (`/thumb/<video_id>`,
`/media/<file_id>`, `/subs/<file_id>`), а не по пути.

Обложка под правом `read` (она часть карточки, а не воспроизведение), байты и
субтитры - под `watch`. HLS добавляется в S3 (`/hls/<video_id>/...`).

Честность: «обложка не найдена», «файл пропал с диска», «субтитры не найдены»,
«сервер ограничил отдачу» - как есть. Ни пустой картинки, ни пустого трека, ни
выдуманных 206 для битого Range.

Функции принимают обработчик (`_Handler` из app/http/server.py): он даёт
раздачу (`write`, `send_file`), отказ (`respond_problem`) и журнал.
"""

from __future__ import annotations

import os
import re
import threading
import time

CHUNK = 256 * 1024
# Обложка и субтитры - файлы на порядок меньше дорожки; лимит нужен, чтобы
# битый или чужой файл не отдавался гигабайтами.
THUMB_MAX_BYTES = 16 * 1024 * 1024
SUBS_MAX_BYTES = 8 * 1024 * 1024

# Типы, которые браузер играет напрямую (HLS появится в S3).
MEDIA_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
}
THUMB_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".avif": "image/avif"}

RANGE = re.compile(r"(\d*)-(\d*)")


class Viewers:
    """Зрители сервера: живые раздачи (`/media`) и сессии плеера (`/hls`).

    Один лимит на всё (`serve_watchers_max`): раздача держит место до конца
    (`hold`/`release`), сессия плеера живёт TTL активности - сегменты HLS
    короткие, и считать каждый сегмент «новым зрителем» было бы враньём про
    лимит, а считать только плейлист - наоборот, «зритель ушёл» при живом
    плеере. Сессии одного и того же (видео, адрес) не плодят места.
    """

    def __init__(self, limit: int = 0, ttl: float = 300.0):
        self.limit = int(limit or 0)      # 0 = без лимита
        self.ttl = float(ttl)
        self._held: set = set()
        self._sessions: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def hold(self, key) -> bool:
        """Место на время раздачи (ключ ["media", file_id])."""
        with self._lock:
            self._prune(time.time())
            if self.limit and self._total() >= self.limit:
                return False
            self._held.add(tuple(key))
            return True

    def release(self, key) -> None:
        with self._lock:
            self._held.discard(tuple(key))

    def session(self, key, now: float | None = None) -> bool:
        """Место плеера: тот же (видео, адрес) продлевает, чужой - занимает."""
        stamp = now if now is not None else time.time()
        with self._lock:
            self._prune(stamp)
            pair = tuple(key)
            if pair in self._sessions:
                self._sessions[pair] = stamp
                return True
            if self.limit and self._total() >= self.limit:
                return False
            self._sessions[pair] = stamp
            return True

    def drop_video(self, video_id: int) -> None:
        """Снять места этого видео (строка удалена из индекса, кеш сброшен)."""
        key_id = int(video_id)
        with self._lock:
            for pair in [p for p in self._sessions if p[1] == key_id]:
                del self._sessions[pair]
            self._held = {p for p in self._held if p[1] != key_id}

    def count(self) -> int:
        with self._lock:
            return self._total()

    def _total(self) -> int:
        return len(self._held) + len(self._sessions)

    def _prune(self, now: float) -> None:
        for pair in [p for p, stamp in self._sessions.items() if now - stamp > self.ttl]:
            del self._sessions[pair]


def etag_for(row) -> str:
    """ETag файла: известный sha256, иначе размер и mtime.

    Обложка без хеша (compute_hash выключен) всё равно получает стабильный
    ключ - иначе клиент не смог бы подтвердить «не изменилось» (304).
    """
    hash_value = (row["hash"] or "").removeprefix("sha256:")
    if hash_value:
        return f'"{hash_value[:32]}"'
    return f'"{int(row["size"] or 0)}-{int(row["mtime"] or 0)}"'


def mime_for(path: str, table: dict, fallback: str) -> str:
    return table.get(os.path.splitext(path)[1].lower(), fallback)


def parse_range(header, size: int):
    """Range: bytes=start-end, start-, -end. None = заголовок не читается.

    Многоразовый Range и «bytes=0-0-0» не разбираем молча: игнорировать чужой
    запрос - это отдать не то, что просили.
    """
    if not header:
        return None
    text = str(header).strip()
    if not text.startswith("bytes=") or "," in text:
        return None
    match = RANGE.fullmatch(text[len("bytes="):])
    if not match:
        return None
    start_text, end_text = match.group(1), match.group(2)
    if not start_text and not end_text:
        return None
    if size <= 0:
        return None
    if start_text and end_text:
        start, end = int(start_text), int(end_text)
        if start > end or start >= size:
            return None
        return start, min(end, size - 1)
    if start_text:
        start = int(start_text)
        if start >= size:
            return None
        return start, size - 1
    tail = int(end_text)
    if tail <= 0:
        return None
    return max(0, size - tail), size - 1


# ------------------------------------------------------------------ #
#  Записи индекса (путь наружу не уходит - только id)
# ------------------------------------------------------------------ #

def thumb_row(conn, video_id) -> tuple[dict | None, str]:
    """Файл обложки строки (None + честная причина)."""
    try:
        key = int(video_id)
    except (TypeError, ValueError):
        return None, "нет видео"
    if key <= 0:
        return None, "нет видео"
    row = conn.execute(
        """SELECT id, path, size, hash, mtime FROM files
            WHERE video_id=? AND kind='thumbnail' AND missing=0
            ORDER BY id LIMIT 1""", (key,)).fetchone()
    if not row:
        known = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=? AND kind='thumbnail'",
            (key,)).fetchone()["n"]
        return None, ("обложка пропала с диска - переиндексируйте"
                      if known else "обложка не найдена")
    if not os.path.exists(row["path"]):
        return None, "обложка пропала с диска - переиндексируйте"
    return dict(row), ""


def file_row(conn, file_id, kinds: tuple) -> tuple[dict | None, str]:
    """Файл по id (video/subtitle). Причина называет состояние, не молчит."""
    try:
        key = int(file_id)
    except (TypeError, ValueError):
        return None, "нет файла"
    marks = ",".join("?" * len(kinds))
    row = conn.execute(
        f"""SELECT id, video_id, kind, path, size, hash, mtime, missing
              FROM files WHERE id=? AND kind IN ({marks})""",
        (key, *kinds)).fetchone()
    if not row:
        return None, "файл не найден в индексе"
    row = dict(row)
    if row["missing"]:
        return None, "файл пропал с диска - переиндексируйте"
    if not os.path.exists(row["path"]):
        # Индекс говорит «файл есть», а диска нет: это пропал, а не «не скачан».
        return None, "файл пропал с диска - переиндексируйте"
    return row, ""


def srt_to_vtt(raw: str) -> str:
    """SRT -> WebVTT: браузер читает только VTT, конвертация на лету.

    Метка времени с «,000» переходит на формат VTT, номер cue и пустые строки
    убираются; текст cue остаётся как есть - мы не «правим» субтитры.
    """
    out = ["WEBVTT", ""]
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").lstrip("\ufeff"))
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing = None
        text: list[str] = []
        for line in lines:
            if "-->" in line:
                timing = line.replace(",", ".")
            elif timing is not None:
                text.append(line)
        if timing is None:
            continue          # блок без метки времени - не cue
        out.append(timing)
        out.extend(text)
        out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ #
#  Раздача
# ------------------------------------------------------------------ #

def send_thumb(handler, video_id: str) -> None:
    """GET /thumb/<video_id>: обложка с ETag и 304 вместо base64 в JSON."""
    row, reason = thumb_row(handler.api.db.conn, video_id)
    if row is None:
        # Причина из индекса честнее общей подписи: «пропала» и «не найдена»
        # - разные факты (тот же договор, что подписи Api в окне).
        handler.respond_problem(404, reason or handler.text("http.thumb_missing"),
                                "missing", target=video_id, note=reason, route="thumb")
        return
    tag = etag_for(row)
    ctype = mime_for(row["path"], THUMB_MIME, "application/octet-stream")
    extra = {"ETag": tag, "Cache-Control": "public, max-age=3600",
             "Accept-Ranges": "bytes"}
    if handler.headers.get("If-None-Match") == tag:
        handler.write(304, b"", ctype, extra, route="thumb", scope="read",
                      target=video_id)
        return
    try:
        size = os.path.getsize(row["path"])
    except OSError as exc:
        handler.respond_problem(404, f"обложка не читается: {exc}",
                                "missing", target=video_id, note=str(exc),
                                route="thumb")
        return
    if THUMB_MAX_BYTES and size > THUMB_MAX_BYTES:
        handler.respond_problem(413, handler.text("http.too_large") +
                                f" ({size} байт, лимит {THUMB_MAX_BYTES})",
                                "too_large", target=video_id, note="обложка",
                                route="thumb")
        return
    handler.send_file(row["path"], 0, size, ctype, extra,
                      route="thumb", scope="read", target=video_id)


def send_media(handler, file_id: str) -> None:
    """GET /media/<file_id>: байты дорожки с Range (206/416) и лимитом скорости.

    Зрители считаются здесь: сверх `serve_watchers_max` - честный 429, полоса
    на клиенте не выдумывается.
    """
    row, reason = file_row(handler.api.db.conn, file_id, ("video",))
    if row is None:
        handler.respond_problem(404, reason or handler.text("http.file_missing"),
                                "missing", target=file_id, note=reason, route="media")
        return
    if not handler.viewers.hold(["media", row["video_id"], file_id]):
        handler.respond_problem(429, handler.text("http.rate") +
                                f" - максимум {handler.viewers.limit}",
                                "rate", target=file_id, note="зрители", route="media")
        return
    try:
        size = os.path.getsize(row["path"])
        header = handler.headers.get("Range")
        parts = parse_range(header, size)
        if header and parts is None:
            # Битый Range назван причиной: «отдали целиком» было бы вранье.
            handler.respond_problem(416, handler.text("http.range") + f": {header}",
                                    "range", target=file_id, note="Range",
                                    route="media")
            return
        start, end = parts if parts else (0, size - 1)
        extra = {"Accept-Ranges": "bytes", "ETag": etag_for(row)}
        if parts:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        handler.send_file(row["path"], start, max(0, end - start + 1),
                          mime_for(row["path"], MEDIA_MIME, "application/octet-stream"),
                          extra, route="media", scope="watch", target=file_id,
                          status=206 if parts else 200)
    finally:
        handler.viewers.release(["media", row["video_id"], file_id])


def send_subs(handler, file_id: str) -> None:
    """GET /subs/<file_id>: WebVTT (sidecar .vtt или .srt - конвертация на лету).

    Встроенные дорожки из контейнера - S3 (ffmpeg -f webvtt в кеш): здесь
    отказ называет причину, а не пустой трек.
    """
    row, reason = file_row(handler.api.db.conn, file_id, ("subtitle",))
    if row is None:
        handler.respond_problem(404, reason or handler.text("http.subs_missing"),
                                "missing", target=file_id, note=reason, route="subs")
        return
    extension = os.path.splitext(row["path"])[1].lower()
    if extension not in (".vtt", ".srt"):
        handler.respond_problem(415, handler.text("http.subs_format") + f": {extension}",
                                "subs_format", target=file_id, note="не VTT/SRT",
                                route="subs")
        return
    try:
        with open(row["path"], "rb") as handle:
            raw = handle.read(SUBS_MAX_BYTES + 1)
    except OSError as exc:
        handler.respond_problem(404, f"субтитры не читаются: {exc}",
                                "missing", target=file_id, note=str(exc), route="subs")
        return
    if len(raw) > SUBS_MAX_BYTES:
        handler.respond_problem(413, handler.text("http.too_large") +
                                f" ({SUBS_MAX_BYTES} байт)", "too_large",
                                target=file_id, note="субтитры", route="subs")
        return
    body = srt_to_vtt(raw.decode("utf-8", errors="replace")).encode("utf-8") \
        if extension == ".srt" else raw
    handler.write(200, body, "text/vtt", {"Cache-Control": "no-store"},
                  route="subs", scope="watch", target=file_id)
