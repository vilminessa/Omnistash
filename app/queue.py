"""Очередь загрузки: воркер, который превращает queued в downloaded.

Очередь живёт в таблице videos (а не в памяти), поэтому переживает рестарт
окна: закрыли посреди списка - открыли, нажали «Запустить», и поехали с
того же места. Один поток, по одной ссылке: так проще честно показывать
прогресс и так спокойнее к площадке.
"""

from __future__ import annotations

import os
import threading

from . import downloader, repo, storages
from .util import human_size


class DownloadWorker:
    """Цикл «взять queued -> скачать -> записать в индекс»."""

    def __init__(self, db, settings_provider, *, log=None):
        self.db = db
        self._settings_provider = settings_provider
        self._log = log or (lambda message: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = {"running": False, "done": 0, "failed": 0,
                       "attempted": 0, "current": None, "error": None}
        # Раз за сессию предупреждаем, что без ffmpeg идёт тихая деградация.
        self._ffmpeg_warned = False

    # ------------------------------------------------------------------ #

    @property
    def state(self) -> dict:
        """Снимок для окна: счётчики и текущая закачка."""
        with self._lock:
            snap = dict(self._state)
            snap["current"] = dict(self._state["current"]) if self._state["current"] else None
            return snap

    @property
    def ffmpeg_warned(self) -> bool:
        """Очередь уже жаловалась на отсутствие ffmpeg (раз за сессию).

        Окно превращает это в заметку: раз деградация реально случилась -
        пусть видна, но гаснет после установки, а не висит вечно.
        """
        return self._ffmpeg_warned

    def start(self) -> dict:
        """Запустить цикл (идемпотентно: повторный вызов - no-op)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": True, "already": True}
            self._stop.clear()
            self._state.update(running=True, error=None)
        pending = repo.stats(self.db.conn)["queued"]
        if not pending:
            with self._lock:
                self._state["running"] = False
            return {"ok": False, "error": "Очередь пуста", "queued": 0}
        self._log(f"Очередь запущена: ждут {pending}")
        thread = threading.Thread(target=self._run, name="omnistash-queue",
                                  daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return {"ok": True, "queued": pending}

    def stop(self) -> dict:
        """Остановить: текущая закачка прервётся и вернётся в очередь."""
        with self._lock:
            thread = self._thread
        self._stop.set()
        if thread and thread.is_alive():
            self._log("Очередь останавливается…")
            return {"ok": True, "stopping": True}
        with self._lock:
            self._state["running"] = False
        return {"ok": True, "stopping": False}

    def retry_failed(self) -> dict:
        """Упавшие снова в очередь (остальные статусы не трогаем)."""
        count = repo.retry_failed(self.db.conn)
        if count:
            self._log(f"Повтор: {count} упавших вернулись в очередь")
        return {"ok": True, "retried": count}

    def wait(self, timeout: float | None = None) -> bool:
        """Дождаться конца цикла (для тестов и головного запуска)."""
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            return not thread.is_alive()
        return True

    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        conn = self.db.conn
        settings = dict(self._settings_provider() or {})
        try:
            while not self._stop.is_set():
                row = repo.next_queued(conn)
                if row is None:
                    break
                self._download_one(conn, row, settings)
        except Exception as exc:  # noqa: BLE001 - воркер не должен молча умереть
            self._log(f"Очередь упала: {exc}")
            with self._lock:
                self._state["error"] = str(exc)
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["current"] = None
            self._log("Очередь остановлена")

    def _resolve_target(self, conn, row: dict, settings: dict):
        """Куда качать эту строку: цель из очереди -> резолвер -> ошибка.

        Фон не может спрашивать пользователя, поэтому отсутствие выбора -
        это тоже результат, который видно в списке очереди.
        """
        target_id = row.get("target_storage_id")
        if target_id:
            storage = storages.get(conn, target_id)
            if not storage or storage["status"] != "active":
                return None, (f"хранилище отвязано или удалено "
                              f"({(storage or {}).get('label', target_id)})")
            if not storage.get("enabled", 1):
                return None, f"хранилище отключено: {storage['label']}"
            if not os.path.isdir(storage["path"]):
                return None, f"нет хранилища: {storage['label']} не подключён"
            return storage, None

        storage = storages.resolve_target(
            conn, settings, video_id=row.get("id"),
            channel_id=row.get("channel_id"))
        if storage and os.path.isdir(storage["path"]):
            return storage, None
        if storage:
            return None, f"нет хранилища: {storage['label']} не подключён"
        return None, "не выбрано хранилище - укажите его при постановке в очередь"

    def _download_one(self, conn, row: dict, settings: dict) -> None:
        if not self._ffmpeg_warned and not downloader.find_ffmpeg():
            # Без ffmpeg склейки и субтитров в файл не будет, а перекодировка
            # пропустится молча - пусть в журнале это видно ровно один раз.
            self._ffmpeg_warned = True
            extra = (" (перекодировка из настроек будет пропущена)"
                     if str(settings.get("transcode") or "none") != "none"
                     else "")
            self._log("ffmpeg не найден: склейка видео+аудио, метаданные и "
                      "субтитры в файл, перекодировка - недоступны"
                      f"{extra}; установка кнопкой в настройках, «Загрузка»")
        target, error = self._resolve_target(conn, row, settings)
        if error:
            repo.set_status(conn, [row["id"]], "failed")
            repo.set_last_error(conn, row["id"], error)
            with self._lock:
                self._state["failed"] += 1
            self._log(f"Ошибка: {row['title'] or row['key']} - {error}")
            return

        repo.set_status(conn, [row["id"]], "downloading")
        # Файл, который проверка целостности признала битым, нужно скачать
        # ПОВТОРНО, а не пропустить как уже скачанный. Флаг снимаем до
        # вызова (last_error чистится), поэтому запоминаем заранее.
        overwrite = str(row.get("last_error") or "").startswith(
            repo.CHECKSUM_PREFIX)
        repo.set_last_error(conn, row["id"], None)
        with self._lock:
            self._state["attempted"] += 1
            self._state["current"] = {
                "id": row["id"], "title": row["title"] or row["key"],
                "percent": 0, "stage": "подготовка", "speed": None, "eta": None,
                "storage": target.get("label"),
            }

        def progress(delta: dict) -> None:
            with self._lock:
                current = self._state["current"]
                if not current:
                    return
                current.update(
                    percent=int(delta.get("percent") or 0),
                    stage=str(delta.get("stage") or current["stage"]),
                    speed=delta.get("speed"),
                    eta=delta.get("eta"))

        result = downloader.download(row, settings, stop=self._stop,
                                     on_progress=progress,
                                     dest=target["path"],
                                     overwrite=overwrite)

        if result.get("cancelled"):
            # Стоп - не ошибка: файл остаётся к докачке (.part), статус
            # возвращается в очередь, чтобы продолжить со следующего запуска.
            repo.set_status(conn, [row["id"]], "queued")
            self._log(f"Остановлено: {row['title'] or row['key']}")
            return

        if result.get("error"):
            repo.set_status(conn, [row["id"]], "failed")
            repo.set_last_error(conn, row["id"], result["error"])
            with self._lock:
                self._state["failed"] += 1
            self._log(f"Ошибка: {row['title'] or row['key']} - {result['error']}")
            return

        recorded = 0
        for path, kind in result.get("files") or []:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            digest = result.get("hash") if kind == "video" else None
            repo.record_file(conn, row["id"], path, kind, size=stat.st_size,
                             mtime=stat.st_mtime, digest=digest)
            recorded += 1
        # record_file(kind='video') сам переводит запись в downloaded.
        if not any(kind == "video" for _path, kind in result.get("files") or []):
            repo.set_status(conn, [row["id"]], "failed")
            with self._lock:
                self._state["failed"] += 1
            self._log(f"Файл не найден после загрузки: {row['title'] or row['key']}")
            return

        with self._lock:
            self._state["done"] += 1
            self._state["current"] = None
        size = next((os.path.getsize(path) for path, kind in (result.get("files") or [])
                     if kind == "video"), 0)
        self._log(f"Скачано: {row['title'] or row['key']} ({human_size(size)}, "
                  f"строк в индекс: {recorded})")
