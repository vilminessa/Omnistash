"""Загрузка видео: обёртка над yt-dlp для очереди.

Что здесь решается за кадром:
  * ffmpeg ищется по цепочке: PATH -> `bin/` рядом с программой ->
    `%LOCALAPPDATA%\\Omnistash\\bin` -> `%LOCALAPPDATA%\\Synfronia\\bin`
    (тот же инструмент уже скачан соседним проектом, второй раз не тащим);
    без ffmpeg берём готовый (несклейный) поток и говорим об этом честно;
  * качество из настроек - это ограничение по высоте, а не «лучшее из
    доступного»: «Высокое» не должно тянуть 4К ради 1080p экрана;
  * отмена - исключение DownloadCancelled прямо из хука прогресса:
    yt-dlp останавливается, оставляет `.part` и докачка потом продолжится;
  * итоговый путь читается из requested_downloads[].filepath - поле
    info["filepath"] возвращается пустым, если не было постпроцессоров.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import yt_dlp

from .indexer import SIDECAR_SUFFIX, build_sidecar, file_hash
from .paths import base_dir

# Ограничение по высоте: качество -> максимальная высота (None = без предела).
HEIGHT_LIMIT = {"best": None, "high": 1080, "mid": 720, "low": 480}
SUB_LANGS = {"none": [], "ru": ["ru.*"], "en": ["en.*"], "all": ["all"]}


class DownloadCancelled(Exception):
    """Пользователь остановил очередь: не ошибка, файл остаётся к докачке."""


def find_ffmpeg() -> str | None:
    """Путь к ffmpeg: PATH, потом папка программы, потом профиль, потом сосед."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    # base_dir() в заморозке - папка самой exe: по __file__ здесь был бы
    # временный _MEIPASS, который живёт ровно один запуск.
    profile = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        base_dir() / "bin" / "ffmpeg.exe",
        profile / "Omnistash" / "bin" / "ffmpeg.exe",
        profile / "Synfronia" / "bin" / "ffmpeg.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def format_selector(settings: dict) -> str:
    """Селектор формата под качество и наличие ffmpeg."""
    limit = HEIGHT_LIMIT.get(str(settings.get("quality") or "high"), 1080)
    has_ffmpeg = bool(find_ffmpeg())
    if has_ffmpeg:
        if limit is None:
            return "bv*+ba/b"
        return f"bv*[height<={limit}]+ba/b[height<={limit}]/b"
    # Без ffmpeg склейка видео+аудио невозможна: берём готовый файл,
    # даже если он ниже запрошенного качества.
    if limit is None:
        return "b"
    return f"b[height<={limit}]/b"


def build_opts(settings: dict, dest_dir: str | Path, *, stop: threading.Event,
               on_progress=None, overwrite: bool = False) -> dict:
    """Опции yt-dlp под текущие настройки.

    overwrite=True - принудительная перезапись: так качается файл, который
    проверка целостности признала битым (иначе yt-dlp счёл бы его уже
    скачанным и пропустил).
    """
    settings = settings or {}
    template = str(settings.get("output_template")
                   or "%(title)s [%(id)s].%(ext)s")
    # Шаблон целиком кладётся в подпапку назначения: он сам может содержать
    # папки («%(channel)s/%(upload_date)s - …»), их создаст yt-dlp.
    outtmpl = os.path.join(str(dest_dir), template)

    def hook(delta: dict) -> None:
        if stop is not None and stop.is_set():
            # Бросок из хука - единственный надёжный способ остановить yt-dlp
            # на середине: он всплывает как DownloadCancelled.
            raise DownloadCancelled("остановлено пользователем")
        if on_progress is None:
            return
        status = delta.get("status")
        if status == "downloading":
            total = delta.get("total_bytes") or delta.get("total_bytes_estimate") or 0
            done = delta.get("downloaded_bytes") or 0
            on_progress({
                "stage": "файл",
                "percent": int(done * 100 / total) if total else 0,
                "downloaded": done,
                "total": total,
                "speed": delta.get("speed"),
                "eta": delta.get("eta"),
            })
        elif status == "finished":
            on_progress({"stage": "готово", "percent": 100,
                         "downloaded": delta.get("total_bytes") or 0,
                         "total": delta.get("total_bytes") or 0,
                         "speed": None, "eta": None})

    opts = {
        "outtmpl": outtmpl,
        "format": format_selector(settings),
        "noplaylist": True,          # очередь качает по одному, без сюрпризов
        "continuedl": True,          # .part -> докачка, а не заново
        "retries": max(int(settings.get("retries") or 0), 1),
        "fragment_retries": 3,
        "socket_timeout": 25,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,          # свой прогресс через хук, не спам в консоль
        "windowsfilenames": True,
        "ignoreerrors": False,
        "progress_hooks": [hook],
    }
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
        opts["merge_output_format"] = "mp4"
    if overwrite:
        opts["force_overwrites"] = True

    langs = SUB_LANGS.get(str(settings.get("subtitles") or "none"), [])
    if langs:
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = langs
        if ffmpeg:
            opts["embedsubtitles"] = True   # субтитры в файл, а не рядом

    if settings.get("save_thumb", True):
        opts["writethumbnail"] = True

    return opts


def _paths_after_download(info: dict) -> list[tuple[str, str]]:
    """Что реально лежит на диске: [(путь, kind)].

    info["filepath"] бывает None - надёжнее requested_downloads[].filepath.
    Для субтитров и обложки дополнительно проверяем существование: при
    встроенных в файл субтитрах yt-dlp оставляет о них запись, но сам файл
    убирает.
    """
    found: list[tuple[str, str]] = []

    for item in info.get("requested_downloads") or []:
        path = item.get("filepath")
        if path:
            found.append((path, "video"))

    video = next((p for p, kind in found if kind == "video"), None)

    subs = info.get("requested_subtitles") or {}
    if isinstance(subs, dict):
        for item in subs.values():
            path = item.get("filepath") if isinstance(item, dict) else None
            if path and os.path.exists(path):
                found.append((path, "subtitle"))

    # Обложка лежит рядом с видео с тем же stem: ищем по расширению.
    if video and info.get("thumbnail"):
        stem = str(Path(video).with_suffix(""))
        parent = Path(video).parent
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".avif"):
            candidate = Path(stem + ext)
            if candidate.is_file() and candidate != Path(video):
                found.append((str(candidate), "thumbnail"))
                break

    unique, seen = [], set()
    for path, kind in found:
        if (path, kind) in seen:
            continue
        seen.add((path, kind))
        unique.append((path, kind))
    return unique


def download(video: dict, settings: dict, *, stop: threading.Event,
             on_progress=None, dest: str | None = None,
             overwrite: bool = False) -> dict:
    """Скачать одно видео и подготовить всё для записи в индекс.

    video - строка videos (нужны key/title/webpage_url/remote_id);
    dest - хранилище, которое выбрал человек (null здесь быть не должно:
    очередь обязана разрешить цель ДО вызова); на случай прямых вызовов
    остаётся fallback в settings.dest_dir;
    overwrite=True - перезаписать существующий файл: так качается файл,
    который проверка целостности признала битым (иначе yt-dlp счёл бы его
    уже скачанным и пропустил).
    возвращает {"cancelled": bool, "files": [(путь, kind)], "info": {...},
    "error": str|None}. Ничего в БД не пишет - это делает очередь.
    """
    dest_dir = Path(dest or settings.get("dest_dir") or ".").expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = video.get("webpage_url") or (
        "https://www.youtube.com/watch?v=" + str(video.get("remote_id") or ""))

    opts = build_opts(settings, dest_dir, stop=stop, on_progress=on_progress,
                      overwrite=overwrite)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        return {"cancelled": True, "files": [], "info": {}, "error": None}
    except yt_dlp.utils.DownloadError as exc:
        return {"cancelled": False, "files": [], "info": {},
                "error": _human_error(exc)}

    if not isinstance(info, dict):
        return {"cancelled": False, "files": [], "info": {},
                "error": "площадка не вернула описание видео"}

    files = _paths_after_download(info)
    if on_progress:
        on_progress({"stage": "индексация", "percent": 100, "downloaded": 0,
                     "total": 0, "speed": None, "eta": None})

    # Хеш считаем независимо от сайдкара: он нужен индексу для дедупликации
    # и для опознания переезжающих файлов.
    video_files = [p for p, kind in files if kind == "video"]
    digest = None
    if video_files and settings.get("compute_hash", True):
        try:
            digest = file_hash(video_files[0])
        except OSError:
            digest = None

    # Sidecar рядом с видео: полные метаданные + хеш файла.
    if video_files and settings.get("keep_sidecar", True):
        main = Path(video_files[0])
        # "video.mp4" -> "video.post.json" (тот же конвейер у сканера).
        sidecar = main.with_suffix(SIDECAR_SUFFIX)
        try:
            sidecar.write_text(build_sidecar(info, main, digest), encoding="utf-8")
            files.append((str(sidecar), "sidecar"))
        except OSError:
            pass  # диск забит/нет прав: библиотека и без сайдкара работает

    return {"cancelled": False, "files": files, "info": info, "error": None,
            "hash": digest}


def _human_error(exc: Exception) -> str:
    """Причина сбоя человеческим языком (короткая, без стека)."""
    text = str(exc)
    low = text.lower()
    if "ffmpeg" in low:
        return "нужен ffmpeg для склейки потоков - установите его или снизьте качество"
    if "video unavailable" in low or "private video" in low:
        return "видео недоступно (удалено или приватное)"
    if "sign in" in low or "confirm your age" in low:
        return "площадка требует вход в аккаунт"
    if "requested format is not available" in low:
        return "формат недоступен - попробуйте другое качество"
    if "unable to download" in low or "http error 4" in low:
        return "площадка не отдала файл (временный сбой?)"
    return text.strip()[:300]
