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

import os
import sys
import threading
import time
import traceback

from . import __version__, indexer, migrate as migrate_mod, repo, settings
from . import settings_schema, sources
from . import storages as storages_mod
from .db import SYNC_MODES, Database
from .paths import log_path
from .queue import DownloadWorker
from .util import human_size

MAX_LOG = 2000          # строк в кольцевом буфере памяти
MAX_LOG_BYTES = 1_000_000   # ротация файла журнала: больше - обрезаем
KEEP_LOG_BYTES = 512_000    # сколько хвоста храним после ротации
HEAVY_TTL = 1.0  # секунд между пересчётом агрегатов


def _log_line(text: str) -> str:
    return f"[{time.strftime('%H:%M:%S')}] {text}"


class Api:
    """Методы, которые вызывает JavaScript (js_api)."""

    def __init__(self) -> None:
        # Ротация журнала - до первой записи: строки запуска должны попасть
        # в файл, а не в брошенный старый хвост.
        self.rotate_log_file()
        self.db = Database()
        # Перенос старых корней (library_roots/dest_dir) в таблицу хранилищ:
        # строго до первого save(), иначе эти ключи уйдут из файла вместе с
        # убранными из схемы полями и переносить станет нечего.
        self._boot = storages_mod.bootstrap(self.db, settings.read_raw())
        if self._boot.get("default") and not settings.read_raw().get(
                "default_storage_id"):
            settings.set_value("default_storage_id", self._boot["default"])
        self._boot_purged = settings.purge_transferred()
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
        # Перенос между хранилищами: свой воркер, отмена между файлами.
        self._migrate: dict = {"running": False, "done": 0, "total": 0,
                               "bytes_done": 0, "bytes_total": 0, "current": "",
                               "summary": None, "errors": []}
        self._migrate_stop = threading.Event()
        self._migrate_thread: threading.Thread | None = None
        # Качалка: очередь живёт в таблице videos, воркер - фоновый поток.
        self.dl = DownloadWorker(self.db, self._current_settings, log=self._log)
        settings.reload_if_changed(self._settings_cache)
        self._log("Индекс открыт: " + str(self.db.path))
        # Версия pywebview в журнале: API между мажорными версиями
        # меняется (диалог папки уже переезжал) - пусть видно, с чем работаем.
        try:
            from importlib.metadata import version as _package_version
            self._log(f"pywebview {_package_version('pywebview')}")
        except Exception:  # noqa: BLE001 - версия не обязательна для работы
            pass
        if self._boot.get("created"):
            self._log("Хранилища перенесены из настроек: "
                      f"{self._boot['created']}, без хранилища осталось "
                      f"{self._boot.get('orphans', 0)} файл(ов)")
        for path in self._boot.get("missing") or []:
            self._log(f"Старая папка не найдена и не перенесена: {path}")
        if self._boot_purged.get("purged"):
            self._log("Ключи настроек перенесены в базу: "
                      + ", ".join(self._boot_purged["purged"]))

        # Возобновление очереди: строки, поставленные прошлым запуском,
        # должны поехать сами - иначе «очередь переживает рестарт» врала бы.
        config = self._current_settings()
        pending = repo.stats(self.db.conn)["queued"]
        if config.get("resume_queue", True) and pending:
            self._log(f"Очередь возобновлена: ждут {pending}")
            self.dl.start()
        # Доступность хранилищ - в фоне: сетевой путь может висеть.
        # Поток обязан быть дожидаем в close(): иначе он может открыть
        # соединение уже после закрытия - файл базы останется занятым.
        self._storage_checking = False
        self._avail_thread = threading.Thread(
            target=self._availability_worker, daemon=True,
            name="omnistash-storages")
        self._avail_thread.start()

    # ------------------------------------------------------------------ #
    #  Служебное
    # ------------------------------------------------------------------ #

    def bind_window(self, window) -> None:
        """Окно нужно для системных диалогов (выбор папки)."""
        self._window = window

    def _log(self, text: str) -> None:
        """Строка в памяти (окно) и в файл (переживает закрытие окна)."""
        with self._lock:
            self._logs.append(_log_line(text))
            if len(self._logs) > MAX_LOG:
                del self._logs[: len(self._logs) - MAX_LOG]
            self._write_log_file(text)

    def _write_log_file(self, text: str) -> None:
        """Добавить строку в omnistash.log.

        Пишем с датой (в памяти дата не нужна), ошибки записи не поднимаем:
        журнал не должен ронять окно. Ротация - при старте, один раз.
        """
        try:
            with open(log_path(), "a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except OSError:
            pass

    @staticmethod
    def rotate_log_file() -> None:
        """Обрезать журнал до хвоста, если он разросся.

        Файл растёт при каждом запуске (мы пишем в него всегда), поэтому
        без ротации он превратится в неподъёмный текст уже через месяц.
        """
        path = log_path()
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= MAX_LOG_BYTES:
            return
        try:
            with open(path, "rb") as handle:
                handle.seek(-KEEP_LOG_BYTES, os.SEEK_END)
                tail = handle.read()
            path.write_bytes(
                b"--- rotated (log too big) ---\n" + tail)
        except OSError:
            pass

    def open_log(self) -> dict:
        """Открыть файл журнала в ассоциированной программе."""
        path = log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")
        except OSError as exc:
            return {"error": f"Не удалось создать журнал: {exc}"}
        self.open_path(str(path))
        return {"ok": True, "path": str(path)}

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
                    "storages": storages_mod.all_storages(conn,
                                                          include_detached=True),
                }
                self._heavy_at = now
            return self._heavy

    def _availability_worker(self) -> None:
        """Проверить доступность хранилищ, не мешая старту окна."""
        try:
            state = storages_mod.refresh_availability(self.db.conn)
        except Exception as exc:  # noqa: BLE001 - фон не должен валить старт
            self._log(f"Проверка хранилищ не удалась: {exc}")
            return
        with self._lock:
            self._heavy_at = 0.0
        if state.get("lost"):
            self._log(f"Хранилищ недоступно: {state['lost']} "
                      f"(проверено {state['checked']})")

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
            migrate_state = dict(self._migrate)
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
            "migrate": migrate_state,
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
        """Системный диалог выбора папки.

        Возвращает словарь, а не строку: «папка выбрана» ({path}),
        «пользователь отменил» ({cancelled}) и «диалог не открылся»
        ({error}) - три разных исхода. Раньше все три превращались в
        null, и баг выглядел как «нажал и ничего».

        pywebview 6: диалог живёт на ОКНЕ (Window.create_file_dialog),
        модульной функции в нём больше нет - как раз её отсутствие и
        ломало добавление папки.
        """
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            return {"error": f"pywebview недоступен: {exc}"}

        win = self._window
        if win is None:
            # Страховка (как в Synfronia): окно могло ещё не привязаться.
            win = webview.windows[0] if webview.windows else None
        if win is None:
            return {"error": "Окно ещё не готово - повторите через секунду"}

        # В 6.x константа FileDialog.FOLDER, FOLDER_DIALOG оставлен как
        # откат для 5.x (и там, и там значение 20).
        folder_type = getattr(webview, "FileDialog", None)
        kind = folder_type.FOLDER if folder_type is not None \
            else webview.FOLDER_DIALOG
        try:
            result = win.create_file_dialog(kind)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Диалог выбора папки не открылся: {exc}")
            return {"error": f"Не удалось открыть выбор папки: {exc}"}

        if not result:
            return {"cancelled": True}          # закрыли, ничего не выбрали
        picked = result[0] if isinstance(result, (list, tuple)) else result
        path = str(picked or "").strip()
        return {"path": path} if path else {"cancelled": True}

    def open_path(self, path) -> None:
        """Открыть папку в проводнике (кнопка в карточке, M3)."""
        if not path:
            return
        try:
            import os
            os.startfile(str(path))  # noqa: S606 - намеренный вызов проводника
        except OSError as exc:
            self._log(f"Не открылось {path}: {exc}")

    def open_url(self, url) -> None:
        """Открыть страницу площадки в браузере по умолчанию."""
        text = str(url or "").strip()
        if not text.startswith(("http://", "https://")):
            return
        try:
            import webbrowser
            webbrowser.open(text)
        except Exception as exc:  # noqa: BLE001 - браузер может быть не настроен
            self._log(f"Не открылось {text}: {exc}")

    # ------------------------------------------------------------------ #
    #  Переиндексация
    # ------------------------------------------------------------------ #

    def start_scan(self) -> dict:
        """Запустить скан всех хранилищ в фоне (кнопка «Переиндексировать»)."""
        roots = [row for row in storages_mod.all_storages(self.db.conn)
                 if row.get("enabled", 1)]
        if not roots:
            return {"error": "Сначала добавьте папку-хранилище в настройках"}
        config = self._current_settings()
        with self._lock:
            if self._scan and self._scan.get("running"):
                return {"error": "Переиндексация уже идёт"}
            self._stop_scan.clear()
            self._scan = {"running": True, "done": 0, "total": 0, "path": ""}
            self._busy = True
            self._status = "Переиндексация…"
        self._log(f"Переиндексация: хранилищ {len(roots)}")
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
        keep_sidecar = bool(self._current_settings().get("keep_sidecar", True))
        # Сначала доступность: недоступный носитель не должен получить
        # отметки «пропало» за все свои файлы.
        try:
            storages_mod.refresh_availability(self.db.conn,
                                              [r["id"] for r in roots if r.get("id")])
        except Exception as exc:  # noqa: BLE001
            self._log(f"Проверка хранилищ не удалась: {exc}")
        started = time.time()

        def progress(done: int, total: int, path: str) -> None:
            with self._lock:
                self._scan = {"running": True, "done": done, "total": total,
                              "path": path}
                self._status = f"Переиндексация {done}/{total}"

        try:
            report = indexer.scan(roots, self.db, progress=progress,
                                  stop=self._stop_scan,
                                  compute_hash=compute_hash,
                                  keep_sidecar=keep_sidecar)
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
                 if k not in ("roots", "errors", "stopped",
                              "dup_details", "move_details")}
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
        if report.get("sidecars"):
            parts.append(f"дописано сайдкаров {report['sidecars']}")
        if report.get("duplicates"):
            parts.append(f"копий найдено {report['duplicates']}")
        if report.get("possible_moves"):
            parts.append(f"похоже на переезд {report['possible_moves']}")
        if report["missing"]:
            parts.append(f"пропало {report['missing']}")
        if report["errors"]:
            parts.append(f"ошибок {len(report['errors'])}")
        if report["stopped"]:
            summary = "Остановлено: " + ", ".join(parts)
        else:
            summary = "Готово: " + ", ".join(parts)

        with self._lock:
            self._scan = {
                "running": False, "done": report["scanned"],
                "total": report["scanned"], "summary": summary,
                # Детали для раздела «Дубликаты»: копии и вероятные переезды,
                # найденные только что (список групп дублей тянется отдельно).
                "duplicates": report.get("dup_details") or [],
                "possible_moves": report.get("move_details") or [],
                "dup_count": report.get("duplicates") or 0,
                "move_count": report.get("possible_moves") or 0,
            }
            self._busy = False
            self._status = "Готово" if not report["stopped"] else "Остановлено"
            self._heavy_at = 0.0
        self._log(summary + f" ({report['duration_s']} c)")

    # ------------------------------------------------------------------ #
    #  Сверка: дубликаты и вероятные переезды
    # ------------------------------------------------------------------ #

    def duplicates(self) -> dict:
        """Группы видео с несколькими живыми копиями (полный список)."""
        groups = repo.find_duplicates(self.db.conn)
        return {"groups": groups, "count": len(groups),
                "files": sum(g["copies"] for g in groups)}

    def dedupe_resolve(self, request=None) -> dict:
        """«Оставить выбранную копию»: удалить остальные с диска.

        Работает и для «возможного переезда»: там оставляют новый файл.
        """
        request = request or {}
        try:
            video_id = int(request.get("video_id"))
            keep_file_id = int(request.get("keep_file_id") or 0)
        except (TypeError, ValueError, KeyError):
            return {"error": "Не выбран файл, который оставить"}
        if not keep_file_id and request.get("keep_path"):
            # Для «возможного переезда» у UI есть путь нового файла,
            # а его id - только в базе.
            row = self.db.conn.execute(
                "SELECT id FROM files WHERE path=? AND kind='video'",
                (str(request["keep_path"]),)).fetchone()
            if not row:
                return {"error": "Файл-кандидат не найден в индексе"}
            keep_file_id = int(row["id"])
            if row:
                keep_file_id = int(row["id"])
        if not keep_file_id:
            return {"error": "Не выбран файл, который оставить"}
        result = repo.resolve_duplicates(self.db.conn, video_id, keep_file_id)
        if result.get("error"):
            return result
        self._touch()
        self._log("Дубликаты: оставлена копия {kept} ({title}), удалено файлов "
                  "{n}".format(kept=result["kept"],
                               title=result.get("title") or "?",
                               n=len(result["removed"])))
        for error in result.get("errors") or []:
            self._log("Не удалилось: " + error)
        return result

    def rebind_file(self, request=None) -> dict:
        """Привязать неопознанный файл к видео из библиотеки вручную."""
        request = request or {}
        try:
            file_id = int(request.get("file_id"))
            video_id = int(request.get("video_id"))
        except (TypeError, ValueError, KeyError):
            return {"error": "Не выбран файл или видео"}
        result = repo.rebind_file(self.db.conn, file_id, video_id)
        if result.get("error"):
            return result
        self._touch()
        self._log(f"Файл привязан вручную к «{result['title']}»")
        return result

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
        """«Загрузить выбранные»: id строк (+куда) -> статус queued."""
        request = request or {}
        ids = request.get("ids") or []
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return {"error": "Некорректный список"}
        if not ids:
            return {"queued": 0}
        storage_id = str(request.get("storage_id") or "") or None
        if storage_id:
            storage = storages_mod.get(self.db.conn, storage_id)
            if not storage or storage["status"] != "active":
                return {"error": "Хранилище не найдено или отвязано"}
        queued = repo.enqueue(self.db.conn, ids, storage_id=storage_id)
        with self._lock:
            self._heavy_at = 0.0
        self._log(f"В очередь поставлено: {queued}"
                  + (f" -> {storage['label']}" if storage_id else ""))
        if queued:
            # Пользователь явно попросил качать - стартуем сразу.
            self.dl.start()
        return {"queued": queued, "storage_id": storage_id}

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
    #  Хранилища
    # ------------------------------------------------------------------ #

    def storages_list(self) -> dict:
        """Список хранилищ + выбранное по умолчанию."""
        return {
            "storages": storages_mod.all_storages(self.db.conn,
                                                  include_detached=True),
            "default_storage_id": self._current_settings().get(
                "default_storage_id") or "",
        }

    def storage_add(self, request=None) -> dict:
        """Добавить папку-хранилище (путь приходит из системного диалога)."""
        request = request or {}
        path = str(request.get("path") or "").strip()
        if not path:
            return {"error": "Не выбрана папка"}
        label = str(request.get("label") or "").strip() or None
        result = storages_mod.add(self.db.conn, path, label=label)
        if isinstance(result, dict) and (result.get("error") or result.get("hint")):
            # error - «папка не найдена»; hint - «уже есть»/«я эту папку
            # знаю»: плодить дубль нельзя, решение за окном.
            return result
        storage = result
        try:
            storages_mod.refresh_availability(self.db.conn, [storage["id"]])
        except Exception:  # noqa: BLE001 - не смог проверить не мешает добавлению
            pass
        self._make_default_if_unset(storage["id"])
        self._touch()
        self._log(f"Хранилище добавлено: {storage['label']} ({storage['path']})")
        return {"ok": True, "storage": storages_mod.get(self.db.conn, storage["id"]),
                "default_storage_id": self._current_settings().get(
                    "default_storage_id")}

    def storage_set_path(self, request=None) -> dict:
        """Переезд корня: новый путь вместо старого, файлы перепривязываются."""
        request = request or {}
        result = storages_mod.set_path(self.db.conn, str(request.get("id") or ""),
                                       str(request.get("path") or ""))
        if isinstance(result, dict) and result.get("error"):
            return result
        self._touch()
        self._log(f"Хранилище переехало: {result['old_path']} -> "
                  f"{result['new_path']} (файлов {result['files']}, "
                  f"найдено на месте {result['present']})")
        return result

    def storage_preview_detach(self, request=None) -> dict:
        """Сколько будет забыто - для диалога подтверждения."""
        return storages_mod.preview_detach(self.db.conn,
                                           str((request or {}).get("id") or ""))

    def storage_detach(self, request=None) -> dict:
        """Отвязать папку: keep_trace=1 оставить след для быстрого возврата."""
        request = request or {}
        keep = bool(request.get("keep_trace", True))
        result = storages_mod.detach(self.db.conn,
                                     str(request.get("id") or ""),
                                     keep_trace=keep)
        if isinstance(result, dict) and result.get("error"):
            return result
        self._touch()
        self._log("Хранилище отвязано: {} (забыто файлов {}, удалено локальных {}, "
                  "отмечено detached {}, след {})".format(
                      result["label"], result["files_removed"],
                      result["local_deleted"], result["detached"],
                      "остался" if result["kept_trace"] else "нет"))
        return result

    def storage_forget(self, request=None) -> dict:
        """Убрать след отвязанной папки совсем."""
        result = storages_mod.forget(self.db.conn,
                                     str((request or {}).get("id") or ""))
        if isinstance(result, dict) and result.get("error"):
            return result
        self._touch()
        self._log(f"След хранилища удалён: {result['label']}")
        return result

    def storage_restore(self, request=None) -> dict:
        """Вернуть отвязанную папку (файлы вернёт следующий скан)."""
        result = storages_mod.restore(self.db.conn,
                                      str((request or {}).get("id") or ""))
        if isinstance(result, dict) and result.get("error"):
            return result
        self._touch()
        self._log(f"Хранилище возвращено: {result['storage']['label']} - "
                  "запустите переиндексацию, чтобы вернуть файлы")
        return result

    def storage_enable(self, request=None) -> dict:
        """Временно выключить/включить сканирование хранилища."""
        request = request or {}
        result = storages_mod.set_enabled(self.db.conn,
                                          str(request.get("id") or ""),
                                          bool(request.get("enabled", True)))
        if isinstance(result, dict) and result.get("error"):
            return result
        self._touch()
        return result

    def storage_set_default(self, request=None) -> dict:
        """Глобальное хранилище, предвыбранное при загрузке."""
        storage_id = str((request or {}).get("id") or "")
        if storage_id:
            storage = storages_mod.get(self.db.conn, storage_id)
            if not storage or storage["status"] != "active":
                return {"error": "Хранилище не найдено или отвязано"}
        settings.set_value("default_storage_id", storage_id)
        with self._lock:
            self._settings_rev += 1
            self._settings_cache.clear()
        settings.reload_if_changed(self._settings_cache)
        self._log("Хранилище по умолчанию: "
                  + (storage["label"] if storage_id else "снято"))
        return {"ok": True, "default_storage_id": storage_id}

    def storage_check(self) -> dict:
        """Перепроверить доступность всех хранилищ (в фоне: сеть виснет)."""
        if getattr(self, "_storage_checking", False):
            return {"ok": True, "busy": True}
        self._storage_checking = True

        def work():
            try:
                state = storages_mod.refresh_availability(self.db.conn)
                self._log("Проверка хранилищ: доступно {available} из "
                          "{checked}".format(**state))
            except Exception as exc:  # noqa: BLE001
                self._log(f"Проверка хранилищ не удалась: {exc}")
            finally:
                self._storage_checking = False
                self._touch()

        thread = threading.Thread(target=work, name="omnistash-probe",
                                  daemon=True)
        self._check_thread = thread
        thread.start()
        return {"ok": True}

    def _make_default_if_unset(self, storage_id: str) -> None:
        """Первое хранилище само становится выбранным по умолчанию."""
        config = self._current_settings()
        if config.get("default_storage_id"):
            return
        settings.set_value("default_storage_id", storage_id)
        with self._lock:
            self._settings_rev += 1
            self._settings_cache.clear()
        settings.reload_if_changed(self._settings_cache)

    def _touch(self) -> None:
        """Сразу пересчитать агрегаты: состояние изменилось руками."""
        with self._lock:
            self._heavy_at = 0.0

    # ------------------------------------------------------------------ #
    #  Перенос между хранилищами (M8)
    # ------------------------------------------------------------------ #

    def _video_ids(self, request) -> tuple[list[int] | None, str | None]:
        """(id-шники, ошибка). Отдельно, а не «dict = ошибка»: успешный
        результат здесь тоже dict, и путать их нельзя."""
        try:
            ids = [int(v) for v in (request or {}).get("video_ids") or []]
        except (TypeError, ValueError):
            return None, "Не выбраны видео для переноса"
        if not ids:
            return None, "Не выбраны видео для переноса"
        return ids, None

    def _storage_or_error(self, storage_id) -> tuple[dict | None, str | None]:
        storage = storages_mod.get(self.db.conn, str(storage_id or ""))
        if not storage:
            return None, "Хранилище-получатель не найдено"
        if storage["status"] != "active":
            return None, "Хранилище-получатель отвязано"
        if not os.path.isdir(storage["path"]):
            return None, f"Носитель не подключён: {storage['label']}"
        return storage, None

    def migrate_preview(self, request=None) -> dict:
        """Превью: сколько пойдёт, что конфликтует, чего уже нет."""
        ids, error = self._video_ids(request)
        if error:
            return {"error": error}
        target, error = self._storage_or_error(
            (request or {}).get("target_storage_id"))
        if error:
            return {"error": error}
        plan = migrate_mod.plan_move(self.db.conn, ids, target)
        if plan.get("error"):
            return plan
        return {"ok": True, "count": plan["count"], "bytes": plan["bytes"],
                "conflicts": plan["conflicts"][:50],
                "conflict_total": len(plan["conflicts"]),
                "already": len(plan["already"]),
                "missing": plan["missing"][:50],
                "missing_total": len(plan["missing"]),
                "same_storage": plan["same_storage"],
                "target": plan["target"]}

    def migrate_start(self, request=None) -> dict:
        """Начать перенос: план пересчитывается на месте старта."""
        request = request or {}
        ids, error = self._video_ids(request)
        if error:
            return {"error": error}
        target, error = self._storage_or_error(request.get("target_storage_id"))
        if error:
            return {"error": error}
        with self._lock:
            if self._migrate.get("running"):
                return {"error": "Перенос уже идёт"}
        plan = migrate_mod.plan_move(self.db.conn, ids, target)
        if plan.get("error"):
            return plan
        if not plan["files"]:
            hint = "всё уже лежит в целевом хранилище"
            if plan["conflicts"]:
                hint = f"конфликтов имён: {len(plan['conflicts'])}"
            elif plan["missing"]:
                hint = f"файлов не найдено: {len(plan['missing'])}"
            return {"error": "Нечего переносить: " + hint}

        self._migrate_stop.clear()
        with self._lock:
            self._migrate = {"running": True, "done": 0,
                             "total": plan["count"], "bytes_done": 0,
                             "bytes_total": plan["bytes"], "current": "",
                             "summary": None, "errors": []}
            self._busy = True
            self._status = f"Перенос {plan['count']} файл(ов)"
        self._log(f"Перенос в «{target['label']}»: {plan['count']} файл(ов), "
                  f"{human_size(plan['bytes'])}, конфликтов {len(plan['conflicts'])}")
        thread = threading.Thread(target=self._migrate_worker,
                                  args=(plan, target), daemon=True,
                                  name="omnistash-migrate")
        self._migrate_thread = thread
        thread.start()
        return {"ok": True, "count": plan["count"], "bytes": plan["bytes"],
                "conflict_total": len(plan["conflicts"])}

    def migrate_stop(self) -> dict:
        """Прервать перенос: скопированное остаётся, источник цел."""
        with self._lock:
            running = self._migrate.get("running")
        if not running:
            return {"ok": False}
        self._migrate_stop.set()
        self._log("Перенос прерван пользователем")
        return {"ok": True}

    def _migrate_worker(self, plan: dict, target: dict) -> None:
        def progress(bytes_done, total_bytes, files_done, total_files, path):
            with self._lock:
                self._migrate.update(
                    bytes_done=bytes_done, bytes_total=total_bytes,
                    done=files_done, total=total_files, current=path)
                self._status = (f"Перенос {files_done}/{total_files}")

        try:
            result = migrate_mod.move_files(self.db.conn, plan, target,
                                            stop=self._migrate_stop,
                                            progress=progress)
            summary = (f"Перенесено {result['done']} из {result['total']} "
                       f"({human_size(result['bytes'])})")
            if result["errors"]:
                summary += f", замечаний: {len(result['errors'])}"
            if result["skipped_conflicts"]:
                summary += f", конфликтов пропущено: {result['skipped_conflicts']}"
            self._log(summary)
            for error in result["errors"][:5]:
                self._log("Перенос: " + error)
        except migrate_mod.MigrateCancelled:
            summary = "Перенос остановлен - уже перенесённое осталось в цели"
            self._log(summary)
            result = None
        except Exception as exc:  # noqa: BLE001 - фон не должен молча умереть
            summary = f"Ошибка переноса: {exc}"
            self._log(summary)
            result = None
        finally:
            with self._lock:
                self._migrate["running"] = False
                self._migrate["summary"] = summary
                self._migrate["current"] = ""
                self._busy = False
                self._status = "Готово"
                self._heavy_at = 0.0

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

    def sync_queue_new(self, request=None) -> dict:
        """«Поставить новые в очередь» после синка в частичном/ручном режиме."""
        request = request or {}
        with self._lock:
            ids = list(self._sync.get("new_ids") or [])
        if not ids:
            return {"queued": 0, "error": "Новых для загрузки нет"}
        return self.enqueue({"ids": ids,
                             "storage_id": request.get("storage_id")})

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
        self._migrate_stop.set()
        self.dl.stop()
        for thread in (self._scan_thread, self._add_thread, self._sync_thread,
                       self._migrate_thread,
                       getattr(self, "_avail_thread", None),
                       getattr(self, "_check_thread", None)):
            if thread is not None and thread.is_alive():
                thread.join(timeout=15)
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
