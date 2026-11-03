"""HLS на лету для клиента (docs/SERVER-CLIENT.md §6, шаг S3).

Проба `ffprobe` решает, можно ли отдать дорожку браузеру без ре-encode:
H.264 + AAC перекладываются в MPEG-TS копированием (`ffmpeg -f hls -c copy`),
HEVC/VP9/AV1 - нет. Отказ называет кодек, а ре-encode включается только явной
настройкой `serve_hls_transcode` (CPU и качество): ничего не «подгоняется на
глаз» и не молчит.

Кеш лежит в `profile/hls_cache/<video_id>/` (плейлист `index.m3u8` и сегменты
`seg<N>.ts` - их пишет ffmpeg, мы не сочиняем свой формат) и сбрасывается,
когда изменилась сигнатура файла (размер, mtime, sha256) или когда строку
перепакуют, перенесут, перепривяжут или убрали из индекса (хук в app/gui.py).

Раздача: те же чанки под `pacing.RateLimiter` и право `watch`, что у `/media`.
Зрители для HLS считаются сессиями (видео + адрес, живёт TTL): сегменты
короткие, и считать каждый сегмент «новым зрителем» было бы враньём про лимит.

Быстрого ответа без ffmpeg нет: без ffprobe или ffmpeg сервер честно говорит
«инструмент не установлен» (установка - кнопка в окне), а не «не получено».
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

from .. import i18n
from ..paths import hls_cache_dir

SEGMENT_SECONDS = 4           # тот же размер сегмента, что в §6
PROBE_TIMEOUT = 20.0
BUILD_TIMEOUT = 600.0         # длинная дорожка: 10 минут на перекладку
WAIT_FOR_BUILD = 30.0         # второй зритель ждёт, пока первый строит кеш

# Кодексы, которые MPEG-TS переносит копированием (без ре-encode).
COPY_VIDEO = {"h264"}
COPY_AUDIO = {"aac"}
# Что просим при ре-encode (только когда настройка включена и энкодер есть).
TRANSCODE_VIDEO = "libx264"
TRANSCODE_AUDIO = "aac"

# Имена, которые пишет ffmpeg: плейлист и сегменты. Чужое имя - отказ:
# маршрут читает только кеш этого видео.
PLAYLIST_NAME = "index.m3u8"
SEGMENT = re.compile(r"seg\d+\.ts")
MIME = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}


class HlsError(Exception):
    """HLS не построился: причина названа (нет ffmpeg, кодек, битый файл)."""


def _run(cmd: list[str], timeout: float, cwd: str | None = None):
    """Один процесс ffmpeg/ffprobe (тесты подменяют эту функцию без процесса)."""
    return subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ffprobe_path() -> str | None:
    """Где лежит ffprobe (None - не установлен: отказ, а не «не получено»)."""
    from ..audio import find_ffprobe
    return find_ffprobe()


def ffmpeg_path() -> str | None:
    """Где лежит ffmpeg (None - не установлен: установка по кнопке в окне)."""
    from ..downloader import find_ffmpeg
    return find_ffmpeg()


def transcoders(ffmpeg: str | None) -> list[str]:
    """Ключи кодировщиков этой сборки ffmpeg (пусто - без ffmpeg)."""
    if not ffmpeg:
        return []
    from ..downloader import available_transcoders
    return available_transcoders(ffmpeg)


def probe(path: str) -> dict | None:
    """Кодексы дорожки: {video, audio, container}. None - читать нечем.

    Никаких примерных значений: вызывающий отказывает с причиной.
    """
    exe = ffprobe_path()
    if not exe:
        return None
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostats",
           "-show_entries", "stream=codec_type,codec_name",
           "-of", "json", path]
    try:
        proc = _run(cmd, PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads((proc.stdout or b"").decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    video = None
    audio = None
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        name = str(stream.get("codec_name") or "").lower()
        if kind == "video" and video is None:
            video = name
        elif kind == "audio" and audio is None:
            audio = name
    if not video and not audio:
        return None
    return {"video": video, "audio": audio,
            "container": os.path.splitext(path)[1].lower()}


def decide(kinds: dict, transcode_allowed: bool, encoders: list[str]) -> tuple[str, str]:
    """("copy" | "transcode", причина отказа). Причина называет кодек.

    Ре-encode включается только явной настройкой: он жрёт CPU и качество, и
    без энкодера в сборке ffmpeg отказывается честно.
    """
    video = kinds.get("video")
    audio = kinds.get("audio")
    if video in COPY_VIDEO and (audio is None or audio in COPY_AUDIO):
        return "copy", ""
    if not transcode_allowed:
        names = "/".join(x for x in (video, audio) if x) or "кодексы не прочитаны"
        return "", i18n.t("http.hls_codec", "ru") + f": {names}"
    if TRANSCODE_VIDEO not in encoders:
        return "", i18n.t("http.hls_encoder", "ru") + f": {TRANSCODE_VIDEO}"
    return "transcode", ""


def signature(row) -> str:
    """Сигнатура файла: размер, mtime, sha256 (если известен)."""
    hash_value = (row["hash"] or "").removeprefix("sha256:")
    return f"{int(row['size'] or 0)}|{int(float(row['mtime'] or 0))}|{hash_value}"


def cache_dir(video_id: int) -> str:
    """Папка кеша одного видео (создаётся лениво)."""
    path = hls_cache_dir() / str(int(video_id))
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def read_meta(video_id: int) -> dict | None:
    """Что лежит в кеше: сигнатура файла и режим перекладки."""
    try:
        with open(os.path.join(cache_dir(video_id), "meta.json"),
                  encoding="utf-8") as handle:
            return json.loads(handle.read())
    except (OSError, json.JSONDecodeError):
        return None


def write_meta(video_id: int, sig: str, mode: str, kinds: dict) -> None:
    """Что кеш построил для этой сигнатуры файла (перечитывается при запросе)."""
    with open(os.path.join(cache_dir(video_id), "meta.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"signature": sig, "mode": mode, "kinds": kinds,
                   "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}, handle)


def invalidate(video_id: int, reason: str = "", log=None) -> bool:
    """Сбросить кеш видео (repack, migrate, rebind, удаление строки).

    False - папки не было (не выдумываем «сбросили», чего не было).
    """
    path = hls_cache_dir() / str(int(video_id))
    if not path.is_dir():
        return False
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        if log:
            log(f"HLS-кеш {video_id} не сброшен ({reason or 'измена файла'}): {exc}")
        return False
    if log and reason:
        log(f"HLS-кеш {video_id} сброшен: {reason}")
    return True


def _ffmpeg_cmd(mode: str, source: str) -> list[str]:
    cmd = ["-hide_banner", "-loglevel", "error", "-nostats", "-i", source,
           "-f", "hls", "-hls_time", str(SEGMENT_SECONDS),
           "-hls_playlist_type", "vod", "-hls_segment_filename", "seg%d.ts"]
    if mode == "copy":
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", TRANSCODE_VIDEO, "-c:a", TRANSCODE_AUDIO]
    cmd.append(PLAYLIST_NAME)
    return cmd


def _playlist_ready(video_id: int, sig: str) -> bool:
    """Кеш годен: meta по той же сигнатуре файла и плейлист на диске."""
    meta = read_meta(video_id)
    if not meta or meta.get("signature") != sig:
        return False
    return os.path.exists(os.path.join(cache_dir(video_id), PLAYLIST_NAME))


class HlsBuilder:
    """Строит кеш HLS под замком на видео: второй зритель ждёт, а не дублирует работу."""

    def __init__(self, log=None):
        self._locks: dict[int, threading.Lock] = {}
        self._global = threading.Lock()
        self._log = log or (lambda message: None)

    def _lock_for(self, video_id: int) -> threading.Lock:
        with self._global:
            lock = self._locks.get(video_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[video_id] = lock
            return lock

    def ready(self, api, video_id: int, row: dict, settings: dict) -> tuple[str, str]:
        """(имя плейлиста, ошибка). Ошибка называет причину честно.

        Кеш переиспользуется, пока сигнатура файла та же; файл, пропавший с
        диска, - это «пропал», а не «не скачан».
        """
        sig = signature(row)
        if _playlist_ready(video_id, sig):
            return PLAYLIST_NAME, ""
        lock = self._lock_for(video_id)
        if not lock.acquire(timeout=WAIT_FOR_BUILD):
            return "", i18n.t("http.hls_busy", "ru")
        try:
            # Проверка внутри замка: пока второй зритель ждал очереди, кеш
            # уже мог быть построен - перекладывать его заново было бы
            # двойная работа и ложный «rebuild».
            if _playlist_ready(video_id, sig):
                return PLAYLIST_NAME, ""
            self._build(api, video_id, row, settings)
        finally:
            lock.release()
        if not _playlist_ready(video_id, sig):
            # ffmpeg отказал или плейлист не написан: «не построен», не пусто.
            return "", i18n.t("http.hls_failed", "ru")
        return PLAYLIST_NAME, ""

    def _build(self, api, video_id: int, row: dict, settings: dict) -> None:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise HlsError(i18n.t("http.hls_no_ffmpeg", "ru"))
        kinds = probe(row["path"])
        if kinds is None:
            raise HlsError(i18n.t("http.hls_probe_failed", "ru") +
                           " (ffprobe не прочитал файл)")
        mode, refusal = decide(kinds, bool(settings.get("serve_hls_transcode")),
                               transcoders(ffmpeg))
        if not mode:
            raise HlsError(refusal)
        directory = cache_dir(video_id)
        # Кеш этого файла перекладывается заново: старые сегменты не должны
        # остаться рядом с новым плейлистом (плеер читает список файлов).
        for name in os.listdir(directory):
            if name == "meta.json":
                continue
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
        try:
            proc = _run([ffmpeg] + _ffmpeg_cmd(mode, row["path"]), BUILD_TIMEOUT,
                        cwd=directory)
        except subprocess.TimeoutExpired as exc:
            raise HlsError(i18n.t("http.hls_failed", "ru") +
                           f" (ffmpeg не успел за {BUILD_TIMEOUT:.0f} с)") from exc
        except OSError as exc:
            raise HlsError(i18n.t("http.hls_failed", "ru") + f": {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-200:]
            raise HlsError(i18n.t("http.hls_failed", "ru") + f": {tail}")
        if not os.path.exists(os.path.join(directory, PLAYLIST_NAME)):
            raise HlsError(i18n.t("http.hls_failed", "ru") + " (плейлист не написан)")
        write_meta(video_id, signature(row), mode, kinds)
        self._log(f"HLS: {video_id} перекладка {mode} "
                  f"({kinds.get('video')}/{kinds.get('audio')})")


def serve(handler, video_id: str, rest: str) -> None:
    """GET/HEAD /hls/<video_id>/<имя>: плейлист и сегменты из кеша.

    Имя читается только из кеша этого видео: путь кеша строится по id, и
    «../» к нему не приклеивается. Место под `serve_watchers_max` занимает
    сессия плеера (видео + адрес), а не каждый сегмент.
    """
    try:
        key = int(video_id)
    except (TypeError, ValueError):
        handler.respond_problem(404, "нет видео", "missing", target=video_id,
                                route="hls")
        return
    if not SEGMENT.fullmatch(rest) and rest != PLAYLIST_NAME:
        handler.respond_problem(404, handler.text("http.hls_segment") + f": {rest}",
                                "missing", target=rest, route="hls")
        return
    row, reason = _media_row(handler.api.db.conn, key)
    if row is None:
        handler.respond_problem(404, reason or handler.text("http.file_missing"),
                                "missing", target=video_id, route="hls")
        return
    try:
        name, error = handler.hls_builder.ready(handler.api, key, row,
                                                handler.settings())
    except HlsError as exc:
        # Причина из пробы или из ffmpeg: название кодека, «нет ffmpeg»,
        # «не успел» - не 500 без текста и не молчание.
        handler.respond_problem(400, str(exc), "hls", target=video_id, route="hls")
        return
    if error:
        handler.respond_problem(400, error, "hls", target=video_id, route="hls")
        return
    # Сессия плеера занимает место только когда байты реально раздаются:
    # отказ по кодексу или «нет ffmpeg» не должен съедать слот зрителя.
    if not handler.viewers.session(["hls", key, handler.addr()]):
        handler.respond_problem(429, handler.text("http.rate") +
                                f" - максимум {handler.viewers.limit} зрителей",
                                "rate", target=video_id, route="hls")
        return
    path = os.path.join(cache_dir(key), rest)
    try:
        size = os.path.getsize(path)
    except OSError:
        handler.respond_problem(404, handler.text("http.hls_segment"), "missing",
                                target=rest, route="hls")
        return
    handler.send_file(path, 0, size,
                      MIME.get(os.path.splitext(rest)[1].lower(),
                               "application/octet-stream"),
                      {"Cache-Control": "no-store", "Accept-Ranges": "bytes"},
                      route="hls", scope="watch", target=f"{video_id}/{rest}")


def _media_row(conn, video_id: int) -> tuple[dict | None, str]:
    """Файл дорожки строки (video kind) + честная причина пропажи."""
    row = conn.execute(
        """SELECT id, path, size, hash, mtime, missing FROM files
            WHERE video_id=? AND kind='video' AND missing=0
            ORDER BY id LIMIT 1""", (video_id,)).fetchone()
    if not row:
        known = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=? AND kind='video'",
            (video_id,)).fetchone()["n"]
        return None, ("файл пропал с диска - переиндексируйте"
                      if known else "видео не скачано")
    row = dict(row)
    if row["missing"] or not os.path.exists(row["path"]):
        return None, "файл пропал с диска - переиндексируйте"
    return row, ""
