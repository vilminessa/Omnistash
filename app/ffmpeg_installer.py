"""Докачка ffmpeg по согласию пользователя (и почему мы его не вшиваем).

ffmpeg распространяется по GPL, этот проект - под PolyForm Noncommercial:
мы не можем перераспространять ffmpeg, поэтому он НЕ входит ни в исходники,
ни в exe. Вместо этого пользователь сам ставит его кнопкой в настройках,
а мы честно говорим, что именно качаем и откуда.

Источники - те же, что в Synfronia: gyan.dev (release essentials) основным,
зеркало BtbN на GitHub (win64 gpl). URL «живые» (release/latest), поэтому
заранее пинить sha256 нельзя; вместо пиннинга установка атомарная:
распаковка в staging -> пробный запуск `ffmpeg -version` -> и только потом
подмена файла. Не запустился или не скачался - на месте остаётся прежняя
версия (или пусто), битого exe не бывает.

Куда: %LOCALAPPDATA%\\Omnistash\\bin - профиль, всегда на запись (папка
рядом с exe может быть в read-only месте), переживает переезд exe.
Проверяет путь и downloader.find_ffmpeg(), он же смотрит PATH и готовую
копию соседнего Synfronia.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from .paths import profile_dir

# Основной источник и зеркало: у gyan.dev бывают вспышки 503 на CDN,
# поэтому есть запасной путь с теми же ffmpeg.exe/ffprobe.exe внутри.
SOURCES: tuple[tuple[str, str], ...] = (
    ("gyan.dev",
     "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("github.com/BtbN",
     "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
     "ffmpeg-master-latest-win64-gpl.zip"),
)
ATTEMPTS = 4          # попыток на один источник
RETRY_DELAY = 2.0     # пауза перед повтором, растёт: 2, 4, 8 с
CHUNK = 1 << 16
USER_AGENT = "Omnistash (auto-installer)"
# Из архива берём ровно эти файлы: zip большой, а нам нужно два exe.
WANTED = ("ffmpeg.exe", "ffprobe.exe")


class InstallCancelled(Exception):
    """Пользователь нажал «Стоп»: не ошибка, просто отмена."""


def install_dir() -> Path:
    """%LOCALAPPDATA%\\Omnistash\\bin - куда ставится ffmpeg."""
    return profile_dir() / "bin"


def installed_ffmpeg() -> Path | None:
    """Путь к уже установленному нами ffmpeg (None - не ставили)."""
    path = install_dir() / "ffmpeg.exe"
    return path if path.is_file() else None


# Сетевой шов: тесты подменяют _open, не трогая сеть.
def _open(request, timeout=120):
    return urllib.request.urlopen(request, timeout=timeout)


def _smoke(exe: Path) -> None:
    """Пробный запуск ДО подмены: свежий бинарник должен запускаться.

    Это и защита от битого/недокачанного архива, и защита от того, что
    подменили рабочий exe на нерабочий.
    """
    try:
        proc = subprocess.run([str(exe), "-version"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffmpeg не запустился: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise RuntimeError(
            f"ffmpeg -version вернул {proc.returncode}: {tail or 'без вывода'}")


def _download(url: str, zip_path: Path, progress, stop) -> None:
    """Скачать zip целиком; прогресс 0..100 обнуляется на каждой попытке."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _open(request, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        received = 0
        with open(zip_path, "wb") as handle:
            while True:
                if stop is not None and stop.is_set():
                    raise InstallCancelled("остановлено пользователем")
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if progress and total:
                    progress("download", received / total * 100.0)


def _extract(zip_path: Path, staging: Path) -> dict[str, Path]:
    """Достать нужные exe из архива (пути вложенные: .../bin/ffmpeg.exe).

    Цель строим из basename сами - чужие пути из архива не используем
    (защита от zip-пути вида ../../что-нибудь).
    """
    staging.mkdir(parents=True, exist_ok=True)
    got: dict[str, Path] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            base = Path(name).name
            if base not in WANTED or base in got:
                continue
            target = staging / base
            with archive.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            got[base] = target
    return got


def install(*, progress=None, log=None, stop=None, probe=None) -> Path:
    """Скачать и поставить ffmpeg в install_dir(). Возвращает путь к exe.

    progress(phase, pct) - фазы: download / extract / verify / done;
    log(msg)             - строки в журнал окна;
    stop                 - threading.Event: установлен -> InstallCancelled;
    probe(exe)           - пробный запуск (в тестах подменяется, потому что
                           подделать запускаемый .exe в фикстуре нельзя).

    Идемпотентно: уже стоит - сразу возвращаем путь, к сеть не ходим.
    """
    dest = install_dir()
    exe = dest / "ffmpeg.exe"
    if exe.is_file():
        return exe

    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="ffmpeg-", dir=dest))
    zip_path = staging / "ffmpeg.zip"
    probe = probe or _smoke
    last_error: Exception | None = None
    try:
        for source, url in SOURCES:
            for attempt in range(1, ATTEMPTS + 1):
                if stop is not None and stop.is_set():
                    raise InstallCancelled("остановлено пользователем")
                if log:
                    log(f"Скачиваю ffmpeg: {source} "
                        f"(попытка {attempt} из {ATTEMPTS})…")
                if progress:
                    progress("download", 0.0)   # обнуление на каждой попытке
                try:
                    _download(url, zip_path, progress, stop)
                    if progress:
                        progress("extract", 0.0)
                    got = _extract(zip_path, staging)
                    if "ffmpeg.exe" not in got:
                        raise RuntimeError("в архиве нет ffmpeg.exe")
                    zip_path.unlink(missing_ok=True)
                    if progress:
                        progress("verify", 0.0)
                    probe(got["ffmpeg.exe"])
                    # Проба прошла - подменяем. os.replace атомарен (staging
                    # и цель на одном томе), а ffmpeg.exe меняем последним:
                    # installed_ffmpeg() смотрит именно на него, и сбой на
                    # середине не должен выглядеть как «установлено».
                    for base in ("ffprobe.exe", "ffmpeg.exe"):
                        if base in got:
                            os.replace(got[base], dest / base)
                    if log:
                        log(f"ffmpeg установлен: {exe}")
                    if progress:
                        progress("done", 100.0)
                    return exe
                except InstallCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - ошибка источника
                    last_error = exc
                    zip_path.unlink(missing_ok=True)
                    if log:
                        log(f"ffmpeg: {source}, попытка {attempt} "
                            f"не удалась: {exc}")
                    if attempt < ATTEMPTS:
                        delay = RETRY_DELAY * (2 ** (attempt - 1))
                        if stop is not None:
                            if stop.wait(delay):
                                raise InstallCancelled(
                                    "остановлено пользователем") from exc
                        else:
                            time.sleep(delay)
        raise RuntimeError("не удалось скачать ffmpeg ни с одного источника: "
                           f"{last_error}")
    finally:
        # staging убираем всегда: внутри - zip и полуфабрикаты.
        shutil.rmtree(staging, ignore_errors=True)
