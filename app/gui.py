"""Главное окно: мост pywebview <-> индекс библиотеки.

Паттерн унаследован от Synfronia: окно не считает ничего само, оно раз в
полсекунды спрашивает poll(since) и рисует снимок. Здесь живут:

  * состояние воркера скана (полоса прогресса, кнопка «Отмена»);
  * кольцевой журнал строк - отдаётся дельтой с last-курсора;
  * настройки: чтение с проверкой mtime (правки файла снаружи видны сразу);
  * «тяжёлые» агрегаты (stats/tree/sources) - не чаще раза в секунду,
    чтобы GROUP BY по большой библиотеке не жрал половину ядра на опросе.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback

from . import __version__, indexer, repo, settings, settings_schema, sources
from .db import SYNC_MODES, Database
from .queue import DownloadWorker

MAX_LOG = 2000
HEAVY_TTL = 1.0  # секунд между пересчётом агрегатов


def _log_line(text: str) -> str:
    return f"[{time.strftime('%H:%M:%S')}] {text}"


class Api:
    """Методы, которые вызывает JavaScript (js_api)."""

    def __init__(self) -> None:
        self.db = Database()
        self._lock = threading.RLock()
        self._logs: list[str] = []
        self._status = "Готов."
        self._busy = False
        self._scan: dict | None = None
        self._stop_scan = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._settings_cache: dict = {}
        self._settings_rev = 1
        self._heavy: dict | None = None
        self._heavy_at = 0.0
        self._window = None
        # Добавление источника (фазы A-D): публичное состояние в self._add,
        # снапшот и план - отдельно, они большие и в JSON не должны.
        self._add: dict = {"phase": "idle", "mode": "partial", "url": "",
                           "fetch": None, "plan": None,
                           "stages": self._stages(), "result": None,
                           "error": None}
        self._add_snapshot: dict | None = None
        self._add_plan: dict | None = None
        self._add_stop = threading.Event()   # отмена фазы A (сеть)
        self._add_cancel = threading.Event()  # отмена фазы C (транзакция)
        self._add_thread: threading.Thread | None = None
        # Синхронизация источников: свой воркер и свой стоп, чтобы не
        # мешать ни добавлению, ни переиндексации.
        self._sync: dict = {"running": False, "index": 0, "total": 0,
                            "current": None, "fetch": None, "stage": None,
                            "results": [], "new_ids": [], "new_total": 0,
                            "queued": 0, "error": None}
        self._sync_stop = threading.Event()
        self._sync_thread: threading.Thread | None = None
        # Качалка: очередь живёт в таблице videos, воркер - фоновый поток.
        self.dl = DownloadWorker(self.db, self._current_settings, log=self._log)
        settings.reload_if_changed(self._settings_cache)
        self._log("Индекс открыт: " + str(self.db.path))

    # ------------------------------------------------------------------ #
    #  Служебное
    # ------------------------------------------------------------------ #

    def bind_window(self, window) -> None:
        """Окно нужно для системных диалогов (выбор папки)."""
        self._window = window

    def _log(self, text: str) -> None:
        with self._lock:
            self._logs.append(_log_line(text))
            if len(self._logs) > MAX_LOG:
                del self._logs[: len(self._logs) - MAX_LOG]

    def _set_status(self, text: str) -> None:
        with self._lock:
            self._status = text

    def _current_settings(self) -> dict:
        with self._lock:
            return settings.reload_if_changed(self._settings_cache)

    def _heavy_snapshot(self) -> dict:
        """Агрегаты для окна, но не чаще HEAVY_TTL секунд."""
        now = time.time()
        with self._lock:
            if self._heavy is None or now - self._heavy_at >= HEAVY_TTL:
                conn = self.db.conn
                self._heavy = {
                    "stats": repo.stats(conn),
                    "tree": repo.tree(conn),
                    "sources": repo.sources(conn),
                    "runs": repo.runs(conn),
                    "queue": repo.queue_rows(conn),
                }
                self._heavy_at = now
            return self._heavy

    # ------------------------------------------------------------------ #
    #  API для окна
    # ------------------------------------------------------------------ #

    def get_initial(self) -> dict:
        config = self._current_settings()
        return {
            "version": __version__,
            "settings": settings.as_public(config),
            "schema": settings_schema.schema(),
            "settings_rev": self._settings_rev,
            "db_path": str(self.db.path),
            "profile": str(self.db.path.parent),
        }

    def poll(self, since: int = 0) -> dict:
        """Снимок состояния: журнал дельтой, агрегаты, ход скана."""
        try:
            since = int(since or 0)
        except (TypeError, ValueError):
            since = 0
        with self._lock:
            logs = self._logs[since:] if since <= len(self._logs) else list(self._logs)
            cursor = len(self._logs)
            status = self._status
            busy = self._busy
            scan = dict(self._scan) if self._scan else None
            rev = self._settings_rev
            add_flow = dict(self._add)
            add_flow["stages"] = [dict(s) for s in self._add.get("stages", [])]
            dl_state = self.dl.state
            sync_state = dict(self._sync)
            sync_state["results"] = [dict(r) for r in self._sync.get("results", [])]
            sync_state["new_ids"] = list(self._sync.get("new_ids") or [])[:200]
        config = settings.as_public(self._current_settings())
        heavy = self._heavy_snapshot()
        return {
            "log_cursor": cursor,
            "logs": logs,
            "status": status,
            "busy": busy,
            "scan": scan,
            "add_flow": add_flow,
            "dl": dl_state,
            "sync": sync_state,
            "settings": config,
            "settings_rev": rev,
            **heavy,
        }

    def list_videos(self, request=None) -> dict:
        """Страница таблицы библиотеки (виртуализация на стороне окна)."""
        request = request or {}
        scope = request.get("scope") or {"type": "pool"}
        try:
            return repo.list_videos(
                self.db.conn,
                scope=scope,
                status=request.get("status") or None,
                query=request.get("query") or "",
                offset=int(request.get("offset") or 0),
                limit=min(int(request.get("limit") or 200), 500),
            )
        except Exception as exc:  # noqa: BLE001 - окно должно увидеть ошибку
            self._log(f"Ошибка списка: {exc}")
            return {"total": 0, "rows": [], "error": str(exc)}

    def get_video(self, video_id) -> dict | None:
        """Карточка видео: колонки + файлы + плейлисты + raw_json."""
        try:
            return repo.video_detail(self.db.conn, int(video_id))
        except (TypeError, ValueError):
            return None

    def save_setting(self, pair=None) -> dict:
        """Значение из карточки настроек: {key, value} -> свежие настройки."""
        if not isinstance(pair, dict):
            return settings.as_public(self._current_settings())
        key = str(pair.get("key") or "")
        try:
            settings.set_value(key, pair.get("value"))
        except Exception as exc:  # noqa: BLE001
            self._log(f"Настройка {key} не сохранена: {exc}")
            return {"error": str(exc)}
        with self._lock:
            self._settings_rev += 1
            self._settings_cache.clear()
        settings.reload_if_changed(self._settings_cache)
        if key == "library_roots":
            self._log("Корни библиотеки обновлены")
        else:
            self._log(f"Настройка {key} = {pair.get('value')!r}")
        self._heavy_at = 0.0  # дерево/счётчики могли измениться
        return {"settings": settings.as_public(self._current_settings()),
                "settings_rev": self._settings_rev}

    def pick_folder(self):
        """Системный диалог выбора папки (из карточки настроек)."""
        if self._window is None:
            return None
        try:
            import webview
            result = webview.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Диалог выбора папки не открылся: {exc}")
            return None
        if not result:
            return None
        picked = result[0] if isinstance(result, (list, tuple)) else result
        return str(picked)

    def open_path(self, path) -> None:
        """Открыть папку в проводнике (кнопка в карточке, M3)."""
        if not path:
            return
        try:
            import os
            os.startfile(str(path))  # noqa: S606 - намеренный вызов проводника
        except OSError as exc:
            self._log(f"Не открылось {path}: {exc}")

    # ------------------------------------------------------------------ #
    #  Переиндексация
    # ------------------------------------------------------------------ #

    def start_scan(self) -> dict:
        """Запустить скан всех корней в фоне (кнопка «Переиндексировать»)."""
        config = self._current_settings()
        roots = [r for r in (config.get("library_roots") or [])
                 if r.get("enabled", True) and r.get("path")]
        if not roots:
            return {"error": "Сначала добавьте папки библиотеки в настройках"}
        with self._lock:
            if self._scan and self._scan.get("running"):
                return {"error": "Переиндексация уже идёт"}
            self._stop_scan.clear()
            self._scan = {"running": True, "done": 0, "total": 0, "path": ""}
            self._busy = True
            self._status = "Переиндексация…"
        self._log(f"Переиндексация: {len(roots)} корн.(-ей)")
        thread = threading.Thread(target=self._scan_worker,
                                  args=(roots, bool(config.get("compute_hash", True))),
                                  name="omnistash-scan", daemon=True)
        self._scan_thread = thread
        thread.start()
        return {"ok": True}

    def stop_scan(self) -> dict:
        """Попросить скан остановиться: закончит текущий файл и выйдет."""
        if self._scan_thread and self._scan_thread.is_alive():
            self._stop_scan.set()
            self._log("Переиндексация остановлена пользователем")
            return {"ok": True}
        return {"ok": False}

    def _scan_worker(self, roots: list[dict], compute_hash: bool) -> None:
        run_id = repo.start_run(self.db.conn, "scan")
        started = time.time()

        def progress(done: int, total: int, path: str) -> None:
            with self._lock:
                self._scan = {"running": True, "done": done, "total": total,
                              "path": path}
                self._status = f"Переиндексация {done}/{total}"

        try:
            report = indexer.scan(roots, self.db, progress=progress,
                                  stop=self._stop_scan, compute_hash=compute_hash)
        except Exception as exc:  # noqa: BLE001 - фон не должен молча умереть
            self._log("Переиндексация упала: " + str(exc))
            self._log(traceback.format_exc(limit=3))
            with self._lock:
                self._scan = {"running": False, "summary": f"Ошибка: {exc}"}
                self._busy = False
                self._status = "Ошибка переиндексации"
            try:
                repo.finish_run(self.db.conn, run_id, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                pass
            return

        stats = {k: v for k, v in report.items()
                 if k not in ("roots", "errors", "stopped")}
        stats["duration_s"] = report["duration_s"]
        repo.finish_run(self.db.conn, run_id, stats)
        for line in report["roots"]:
            if line.get("state") == "unavailable":
                self._log(f"Корень недоступен: {line['path']}")
        for err in report["errors"][:5]:
            self._log("Скан: " + err)
        if len(report["errors"]) > 5:
            self._log(f"…и ещё ошибок: {len(report['errors']) - 5}")

        parts = [f"скан {report['scanned']} файлов"]
        if report["bound_sidecar"] or report["bound_id"] or report["bound_title"]:
            parts.append("опознано "
                         f"{report['bound_sidecar'] + report['bound_id'] + report['bound_title']}")
        if report["added"]:
            parts.append(f"новых {report['added']}")
        if report["rebound"]:
            parts.append(f"переехало {report['rebound']}")
        if report["missing"]:
            parts.append(f"пропало {report['missing']}")
        if report["errors"]:
            parts.append(f"ошибок {len(report['errors'])}")
        if report["stopped"]:
            summary = "Остановлено: " + ", ".join(parts)
        else:
            summary = "Готово: " + ", ".join(parts)

        with self._lock:
            self._scan = {"running": False, "done": report["scanned"],
                          "total": report["scanned"], "summary": summary}
            self._busy = False
            self._status = "Готово" if not report["stopped"] else "Остановлено"
            self._heavy_at = 0.0
        self._log(summary + f" ({report['duration_s']} c)")

    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  Добавление источника: A индексация -> B диалог -> C создание -> D итог
    # ------------------------------------------------------------------ #

    # Стадии фазы C: порядок продикован внешними ключами (у видео есть
    # channel_id, у связи есть video_id), поэтому авторы раньше видео.
    STAGES = (("playlist", "Плейлист"), ("channels", "Авторы"),
              ("videos", "Видео"), ("links", "Связи"))
    PICKER_LIMIT = 300  # сколько строк отдаём в пикер ручного выбора

    def _stages(self, state: str = "wait") -> list[dict]:
        return [{"id": sid, "title": title, "state": state,
                 "current": 0, "total": 0} for sid, title in self.STAGES]

    def add_start(self, request=None) -> dict:
        """Фаза A: снять снапшот с площадки и посчитать план (БД не трогаем)."""
        request = request or {}
        url = str(request.get("url") or "").strip()
        if not url:
            return {"error": "Вставьте ссылку на плейлист или канал"}
        config = self._current_settings()
        mode = request.get("mode") or config.get("default_sync_mode") or "partial"
        if mode not in SYNC_MODES:
            mode = "partial"
        try:
            kind = sources.classify(url)["kind"]
        except Exception as exc:  # noqa: BLE001 - кривая ссылка не должна ронять окно
            return {"error": f"Ссылка не разобрана: {exc}"}
        if kind not in ("playlist", "channel"):
            return {"error": "Поддерживаются ссылки на плейлист и на канал; "
                             "одиночное видео появится вместе с загрузчиком"}

        with self._lock:
            if self._add.get("phase") in ("fetching", "committing"):
                return {"error": "Добавление уже идёт"}
            self._add = {"phase": "fetching", "mode": mode, "url": url,
                         "fetch": {"got": 0, "total": 0}, "plan": None,
                         "stages": self._stages(), "result": None, "error": None}
            self._add_snapshot = None
            self._add_plan = None
            self._add_stop.clear()
            self._add_cancel.clear()
            self._busy = True
            self._status = "Индексация источника…"
        self._log(f"Индексация: {url}")
        thread = threading.Thread(target=self._add_fetch_worker,
                                  args=(url, config),
                                  name="omnistash-add", daemon=True)
        self._add_thread = thread
        thread.start()
        return {"ok": True}

    def add_confirm(self, request=None) -> dict:
        """Фаза C: пользователь подтвердил план - пишем в БД одной транзакцией."""
        request = request or {}
        with self._lock:
            if self._add.get("phase") != "confirm":
                return {"error": "План не готов - нажмите «Индексировать»"}
            if request.get("mode") in SYNC_MODES:
                self._add["mode"] = request["mode"]
            self._add["phase"] = "committing"
            self._add["stages"] = self._stages()
            self._add["error"] = None
            self._add_cancel.clear()
            self._busy = True
            self._status = "Создание записей…"
        thread = threading.Thread(target=self._add_commit_worker, daemon=True)
        self._add_thread = thread
        thread.start()
        return {"ok": True}

    def add_close(self) -> dict:
        """Отмена/закрытие по текущей фазе.

        Фаза A - сетевая: поднимаем флаг, воркер уйдёт между чанками.
        Фаза C - транзакционная: флаг роняет исключение внутри стадии,
        with conn откатывает всё до последней строки.
        """
        with self._lock:
            phase = self._add.get("phase", "idle")
        if phase == "fetching":
            self._add_stop.set()
            self._log("Индексация остановлена")
            return {"ok": True, "phase": phase}
        if phase == "committing":
            self._add_cancel.set()
            self._log("Создание прервано пользователем")
            return {"ok": True, "phase": phase}
        with self._lock:
            self._add = {"phase": "idle", "mode": self._add.get("mode", "partial"),
                         "url": "", "fetch": None, "plan": None,
                         "stages": self._stages(), "result": None, "error": None}
            self._add_snapshot = None
            self._add_plan = None
            self._busy = False
            self._status = "Готов."
        return {"ok": True, "phase": "idle"}

    def enqueue(self, request=None) -> dict:
        """«Загрузить выбранные» из пикера: id строк -> статус queued."""
        ids = (request or {}).get("ids") or []
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return {"error": "Некорректный список"}
        if not ids:
            return {"queued": 0}
        queued = repo.enqueue(self.db.conn, ids)
        with self._lock:
            self._heavy_at = 0.0
        self._log(f"В очередь поставлено: {queued}")
        if queued:
            # Пользователь явно попросил качать - стартуем сразу.
            self.dl.start()
        return {"queued": queued}

    def _add_fetch_worker(self, url: str, config: dict) -> None:
        def progress(got: int, total: int) -> None:
            with self._lock:
                self._add["fetch"] = {"got": got, "total": total}
                self._status = (f"Получаем записи {got}/{total}" if total
                                else f"Получаем записи {got}")

        try:
            snapshot = sources.fetch_snapshot(url, settings=config,
                                              on_progress=progress,
                                              stop=self._add_stop)
        except sources.Aborted:
            self._finish_fetch(None, None, error=None, aborted=True)
            return
        except sources.FetchError as exc:
            self._finish_fetch(None, None, error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - сбой площадки не должен молчать
            self._log("Индексация упала: " + str(exc))
            self._finish_fetch(None, None, error=f"Не удалось получить источник: {exc}")
            return

        plan = repo.plan_diff(self.db.conn, snapshot)
        self._finish_fetch(snapshot, plan)

    def _finish_fetch(self, snapshot, plan, *, error=None, aborted=False) -> None:
        # Пользователь мог нажать «Отмена», пока запрос ещё шёл: маленький
        # плейлист успевает прийти целиком, но снимать план в этом случае
        # уже поздно - он просил остановиться.
        if aborted or self._add_stop.is_set():
            aborted, error, snapshot, plan = True, None, None, None
        with self._lock:
            if aborted:
                self._add.update(phase="idle", fetch=None, plan=None, error=None)
                self._status = "Готов."
            elif error:
                self._add.update(phase="error", fetch=None, plan=None, error=error)
                self._status = "Ошибка"
            else:
                counts = plan["counts"]
                self._add.update(
                    phase="confirm", fetch=None, error=None,
                    plan={
                        "counts": counts,
                        "is_mix": plan["is_mix"],
                        "exists": plan["playlist"]["exists"],
                        "sync_mode": plan["playlist"]["sync_mode"],
                        "url": snapshot["url"],
                        "entries_total": plan["entries_total"],
                        "title": snapshot["playlist"].get("title"),
                        "kind": snapshot["playlist"].get("kind"),
                        "channel": (snapshot["playlist"].get("channel") or {}).get("title"),
                        "item_count": snapshot["playlist"].get("item_count"),
                    })
                self._add_snapshot = snapshot
                self._add_plan = plan
                self._status = "Проверьте план добавления"
            self._busy = False
        if error:
            self._log("Индексация: " + error)
        elif aborted:
            return
        else:
            counts = plan["counts"]
            self._log("План: новых {} · уже есть {} · пропущено {}".format(
                counts["new_videos"], counts["known_videos"], counts["skipped"]))

    def _add_commit_worker(self) -> None:
        snapshot = self._add_snapshot
        plan = self._add_plan
        with self._lock:
            mode = self._add.get("mode", "partial")
        if snapshot is None or plan is None:
            with self._lock:
                self._add.update(phase="error",
                                 error="План потерян - повторите индексацию")
                self._busy = False
            return

        conn = self.db.conn

        def on_stage(name, state, current, total):
            # Отмена внутри транзакции: исключение уходит в with conn,
            # который откатывает все стадии разом.
            if self._add_cancel.is_set():
                raise sources.Aborted()
            with self._lock:
                for stage in self._add.get("stages", []):
                    if stage["id"] == name:
                        stage.update(state=state, current=current, total=total)

        run_id = repo.start_run(conn, "add")
        try:
            stats = repo.commit_plan(conn, snapshot, plan, on_stage=on_stage)
        except sources.Aborted:
            repo.finish_run(conn, run_id, {"cancelled": True})
            with self._lock:
                self._add.update(phase="idle", plan=None, error=None, result=None)
                self._busy = False
                self._status = "Отменено - ничего не изменено"
            self._log("Добавление отменено: транзакция откатена")
            return
        except Exception as exc:  # noqa: BLE001 - окно должно увидеть причину
            repo.finish_run(conn, run_id, {"error": str(exc)})
            self._log("Создание упало: " + str(exc))
            with self._lock:
                self._add.update(phase="error",
                                 error=f"Не удалось записать: {exc}")
                self._busy = False
                self._status = "Ошибка"
            return

        playlist_id = stats.get("playlist_id")
        queued = 0
        picker, picker_total = [], 0
        if mode == "full" and playlist_id:
            # «Полная»: всё ожидающее в этом источнике встаёт в очередь.
            queued = repo.enqueue_playlist(conn, playlist_id)
        elif mode == "partial" and playlist_id:
            # «Частичная»: вместо очереди - пикер, контент выбирают руками.
            # «Ручная» вообще ничего не готовит: источник просто занесён.
            picker_total, picker = repo.playlist_pending(conn, playlist_id,
                                                         limit=self.PICKER_LIMIT)
        repo.finish_run(conn, run_id, {"mode": mode, "queued": queued, **stats})
        if queued:
            # Режим «Полная»: очередь начинает качать сама.
            self.dl.start()

        with self._lock:
            for stage in self._add.get("stages", []):
                stage.update(state="done", current=stage["total"], total=stage["total"])
            self._add.update(
                phase="done", error=None,
                result={"stats": stats, "mode": mode, "queued": queued,
                        "picker": picker, "picker_total": picker_total,
                        "title": (snapshot["playlist"].get("title")
                                  or snapshot["playlist"].get("remote_id")),
                        "url": snapshot.get("url")})
            self._busy = False
            self._heavy_at = 0.0
            self._status = "Готово"
        self._log("Добавлено: плейлист {}, новых {}, связей {}{}".format(
            1, stats["new_videos"], stats["links_to_create"],
            f", в очередь {queued}" if queued else ""))

    # ------------------------------------------------------------------ #
    #  Очередь загрузки
    # ------------------------------------------------------------------ #

    def queue_start(self) -> dict:
        """Запустить качалку: берёт queued по порядку, ставит downloaded."""
        with self._lock:
            self._heavy_at = 0.0
        return self.dl.start()

    def queue_stop(self) -> dict:
        """Остановить: текущая закачка вернётся в очередь, файл останется."""
        result = self.dl.stop()
        with self._lock:
            self._heavy_at = 0.0
        return result

    def queue_retry(self) -> dict:
        """Упавшие снова в очередь (и сразу запускаем, если стояли)."""
        result = self.dl.retry_failed()
        with self._lock:
            self._heavy_at = 0.0
        if result.get("retried"):
            self.dl.start()
        return result

    # ------------------------------------------------------------------ #
    #  Синхронизация источников: переснять каждый источник и применить diff
    # ------------------------------------------------------------------ #

    def sync_start(self) -> dict:
        """Фаза A+C для всех источников сразу: снапшот -> diff -> запись.

        Режим каждого источника решает, что делать с новым: «Полная»
        ставит в очередь сразу, «Частичная»/«Ручная» показывают счётчики и
        дают кнопку «поставить новые в очередь» (качалка всё равно ждёт).
        """
        with self._lock:
            if self._sync.get("running"):
                return {"error": "Синхронизация уже идёт"}
            if self._add.get("phase") in ("fetching", "committing"):
                return {"error": "Сначала закончите добавление источника"}
            if self._scan and self._scan.get("running"):
                return {"error": "Дождитесь переиндексации"}
            sources_list = [s for s in repo.sources(self.db.conn)
                            if s.get("url") and s.get("kind") in
                            ("uploads", "remote", "mix")]
            if not sources_list:
                return {"error": "Нет источников для синхронизации"}
            config = dict(self._current_settings())
            self._sync_stop.clear()
            self._sync = {"running": True, "index": 0,
                          "total": len(sources_list), "current": None,
                          "fetch": None, "stage": None, "results": [],
                          "new_ids": [], "new_total": 0, "queued": 0,
                          "error": None}
            self._busy = True
            self._status = f"Синхронизация 0/{len(sources_list)}"
        self._log(f"Синхронизация: источников {len(sources_list)}")
        thread = threading.Thread(target=self._sync_worker,
                                  args=(sources_list, config),
                                  name="omnistash-sync", daemon=True)
        self._sync_thread = thread
        thread.start()
        return {"ok": True, "sources": len(sources_list)}

    def sync_stop(self) -> dict:
        """Остановить между источниками и между чанками (БД не пострадает)."""
        if self._sync.get("running"):
            self._sync_stop.set()
            self._log("Синхронизация остановлена пользователем")
            return {"ok": True}
        return {"ok": False}

    def sync_queue_new(self) -> dict:
        """«Поставить новые в очередь» после синка в частичном/ручном режиме."""
        with self._lock:
            ids = list(self._sync.get("new_ids") or [])
        if not ids:
            return {"queued": 0, "error": "Новых для загрузки нет"}
        result = self.enqueue({"ids": ids})
        return result

    def _sync_worker(self, sources_list: list[dict], config: dict) -> None:
        conn = self.db.conn
        stop = self._sync_stop
        new_ids: list[int] = []
        queued_total = 0
        try:
            for index, source in enumerate(sources_list, start=1):
                if stop.is_set():
                    break
                title = source.get("title") or source.get("remote_id") or "?"
                with self._lock:
                    self._sync.update(
                        index=index, current=title, fetch={"got": 0, "total": 0},
                        stage=None)
                    self._status = (f"Синхронизация {index}/{len(sources_list)}"
                                    f" · {title}")

                entry = {"title": title, "kind": source.get("kind_label"),
                         "mode": source.get("sync_mode"), "new": 0,
                         "known": 0, "removed": 0, "queued": 0, "error": None}
                try:
                    snapshot = sources.fetch_snapshot(
                        source["url"], settings=config, stop=stop,
                        on_progress=lambda got, want: self._sync_fetch(got, want))
                except sources.Aborted:
                    break
                except Exception as exc:  # noqa: BLE001 - сбой одной площадки не валит синк
                    entry["error"] = str(exc)
                    self._log(f"Синк {title}: {exc}")
                    with self._lock:
                        self._sync["results"].append(entry)
                    continue

                plan = repo.plan_diff(conn, snapshot)

                def on_stage(name, state, current, total):
                    if stop.is_set():
                        raise sources.Aborted()
                    with self._lock:
                        self._sync["stage"] = {"id": name, "state": state,
                                               "current": current, "total": total}

                try:
                    stats = repo.commit_plan(conn, snapshot, plan,
                                             on_stage=on_stage)
                except sources.Aborted:
                    break
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = str(exc)
                    self._log(f"Синк {title}: запись не удалась - {exc}")
                    with self._lock:
                        self._sync["results"].append(entry)
                    continue

                # Новые строки: их id пригодятся для кнопки «в очередь».
                for item in plan["new_videos"]:
                    vid = repo.video_id_by_key(conn, item["key"])
                    if vid:
                        new_ids.append(vid)
                entry.update(new=stats["new_videos"],
                             known=stats["known_videos"],
                             removed=stats.get("removed") or 0)
                if source.get("sync_mode") == "full" and stats["playlist_id"]:
                    entry["queued"] = repo.enqueue_playlist(conn,
                                                            stats["playlist_id"])
                    queued_total += entry["queued"]

                with self._lock:
                    self._sync["results"].append(entry)
                    self._sync["new_total"] = len(new_ids)
                    self._heavy_at = 0.0
                if stats["new_videos"] or stats.get("removed"):
                    self._log(f"{title}: новых {stats['new_videos']}, "
                              f"убрано {stats.get('removed') or 0}")
        except Exception as exc:  # noqa: BLE001 - воркер не должен молча умереть
            self._log(f"Синхронизация упала: {exc}")
            with self._lock:
                self._sync["error"] = str(exc)
        finally:
            done = len(self._sync.get("results") or [])
            stopped = stop.is_set()
            with self._lock:
                self._sync.update(running=False, current=None, fetch=None,
                                  stage=None,
                                  new_ids=new_ids[:5000],
                                  new_total=len(new_ids),
                                  queued=queued_total)
                self._busy = False
                self._status = "Синхронизация завершена" if not stopped \
                    else "Синхронизация остановлена"
                self._heavy_at = 0.0
            self._log(("Остановлено" if stopped else "Готово") +
                      f": источников {done}/{len(sources_list)}")

    def _sync_fetch(self, got: int, total: int) -> None:
        with self._lock:
            self._sync["fetch"] = {"got": got, "total": total}

    def close(self) -> None:
        """Остановить фон и закрыть соединения (включая воркеров)."""
        self._stop_scan.set()
        self._add_stop.set()
        self._add_cancel.set()
        self._sync_stop.set()
        self.dl.stop()
        for thread in (self._scan_thread, self._add_thread, self._sync_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=10)
        self.dl.wait(timeout=15)
        self.db.close()


def run() -> int:
    """Открыть окно. Код возврата != 0 - окно не поднялось."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        print(f"pywebview недоступен: {exc}", file=sys.stderr)
        return 1

    from .ui import build_page

    api = Api()
    try:
        page = build_page()
        window = webview.create_window(
            "Omnistash",
            html=page,
            js_api=api,
            width=1240,
            height=840,
            min_size=(940, 620),
            background_color="#0c1622",
        )
        api.bind_window(window)
        webview.start(debug="--debug" in sys.argv)
    except Exception as exc:  # noqa: BLE001 - окно не поднялось, но причина видна
        print(f"Окно не открылось: {exc}", file=sys.stderr)
        traceback.print_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"Omnistash не смог открыть окно:\n\n{exc}",
                "Omnistash", 0x10)
        except Exception:  # noqa: BLE001
            pass
        api.close()
        return 1
    api.close()
    return 0
